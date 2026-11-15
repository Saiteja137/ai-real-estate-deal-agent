import hashlib, json, os, re, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
from pydantic import BaseModel, Field
from backend.core import DEFAULT_CRITERIA, run_workflow

from backend.database import db, init, now, stamp, payload_hash
init()
app = APIRouter()

from backend.database import audit, row, unpack
from backend.services import approval_service
from backend import config
class EmailInput(BaseModel):
    sender: str = Field(min_length=3,max_length=320)
    subject: str = Field(min_length=1,max_length=500)
    body: str = Field(min_length=1,max_length=50000)
class Draft(BaseModel):
    recipient: str = Field(min_length=3,max_length=320)
    subject: str = Field(min_length=1,max_length=500)
    body: str = Field(min_length=1,max_length=30000)
class Decision(BaseModel):
    sender: str
    text: str
class Criteria(BaseModel):
    max_price: float = Field(ge=0)
    min_arv: float = Field(ge=0)
    max_repairs: float = Field(ge=0)
    max_ratio: float = Field(gt=0,le=1)
    min_spread: float = Field(ge=0)
    property_type: str
    strategy: str

@app.get('/')
def index():
    if not Path('frontend/dist/index.html').exists(): raise HTTPException(503, 'Build the React frontend or run the Vite development server')
    return FileResponse('frontend/dist/index.html')
@app.get('/api/samples')
def samples(): return json.loads(Path('data/sample_emails.json').read_text(encoding='utf-8'))
@app.get('/api/config')
def public_config():
    return {'hitl_email_enabled': config.HITL_EMAIL_ENABLED, 'live_email_ingestion_enabled': config.LIVE_EMAIL_INGESTION_ENABLED}
@app.get('/api/criteria')
def get_criteria():
    with db() as c: return json.loads(c.execute("SELECT value FROM settings WHERE key='criteria'").fetchone()[0])
@app.put('/api/criteria')
def set_criteria(v: Criteria):
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key='criteria'",(v.model_dump_json(),))
        audit(c,'criteria_updated',v.model_dump())
    return v
@app.post('/api/emails')
def process_email(e: EmailInput):
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',e.sender): raise HTTPException(422,'Invalid sender')
    fp=payload_hash(e.model_dump())
    with db() as c:
        if c.execute('SELECT 1 FROM emails WHERE fingerprint=?',(fp,)).fetchone(): raise HTTPException(409,'Email already processed')
    criteria=get_criteria()
    try:
        result = run_workflow(e.subject,e.body,e.sender,criteria)
        properties, source = result['properties'], result['source']
    except Exception as exc: raise HTTPException(502,'Extraction failed; check OpenAI configuration and logs') from exc
    if not properties: raise HTTPException(422,'No property found')
    eid=str(uuid.uuid4()); ids=[]
    with db() as c:
        try: c.execute('INSERT INTO emails VALUES (?,?,?,?,?,?)',(eid,e.sender,e.subject,e.body,stamp(now()),fp))
        except sqlite3.IntegrityError: raise HTTPException(409,'Email already processed')
        for item in result['results']:
            p=item['property']; ev=item['evaluation']; d=item['draft']; did=str(uuid.uuid4())
            c.execute('INSERT INTO deals VALUES (?,?,?,?,?,?,?)',(did,eid,json.dumps(p),json.dumps(ev),json.dumps(d),source,stamp(now())))
            ids.append(did)
        audit(c,'email_processed',{'email_id':eid,'deal_ids':ids,'extraction':source})
        for did in ids: audit(c,'deal_evaluated',{'deal_id':did})
    return {'deal_ids':ids}

class EmlInput(BaseModel):
    content: str = Field(min_length=1, max_length=500000)

class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.hidden+=1
        if tag in ('p','br','div','li','tr'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style') and self.hidden: self.hidden-=1
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)

@app.post('/api/import-eml')
def import_eml(e: EmlInput):
    message=BytesParser(policy=policy.default).parsebytes(e.content.encode('utf-8'))
    sender=parseaddr(message.get('From',''))[1]
    subject=str(message.get('Subject',''))
    part=message.get_body(preferencelist=('plain','html')) if message.is_multipart() else message
    if part is None: raise HTTPException(422,'Email has no readable body')
    try: body=part.get_content()
    except (LookupError, UnicodeError): raise HTTPException(422,'Email body encoding is invalid')
    if not isinstance(body,str): raise HTTPException(422,'Email body must be text')
    if part.get_content_type()=='text/html':
        parser=PlainHTML(); parser.feed(body); body=''.join(parser.parts)
    return {'sender':sender,'subject':subject,'body':body.strip()}

@app.get('/api/overview')
def overview():
    with db() as c:
        classifications={r['classification']:r['count'] for r in c.execute("SELECT json_extract(evaluation,'$.classification') AS classification, count(*) AS count FROM deals GROUP BY classification")}
        statuses={r['status']:r['count'] for r in c.execute('SELECT status,count(*) AS count FROM approvals GROUP BY status')}
        return {'emails':c.execute('SELECT count(*) FROM emails').fetchone()[0], 'deals':c.execute('SELECT count(*) FROM deals').fetchone()[0], 'classifications':classifications, 'approvals':statuses, 'sent':c.execute("SELECT count(*) FROM executions WHERE status='SMTP_ACCEPTED'").fetchone()[0]}
@app.get('/api/deals')
def deals():
    with db() as c:
        items = [unpack(dict(r),['property','evaluation','draft']) for r in c.execute('SELECT * FROM deals ORDER BY created_at DESC')]
        for item in items:
            item['current_status'] = deal_status(c, item['id'])
        return items

def deal_status(c, deal_id):
    a=c.execute('SELECT id,status FROM approvals WHERE deal_id=? ORDER BY created_at DESC LIMIT 1',(deal_id,)).fetchone()
    if not a or a['status']=='INVALIDATED': return 'READY_FOR_REVIEW' if not config.HITL_EMAIL_ENABLED else 'READY_FOR_APPROVAL'
    execution=c.execute('SELECT status FROM executions WHERE approval_id=?',(a['id'],)).fetchone()
    return execution['status'] if execution else a['status']
@app.get('/api/deals/{deal_id}')
def deal(deal_id: str):
    with db() as c:
        d=unpack(row(c,'deals',deal_id),['property','evaluation','draft']); d['email']=row(c,'emails',d['email_id'])
        d['approvals']=[dict(r) for r in c.execute('SELECT * FROM approvals WHERE deal_id=? ORDER BY created_at DESC',(deal_id,))]
        for a in d['approvals']:
            a['payload']=json.loads(a['payload']); a['execution']=dict(x) if (x:=c.execute('SELECT * FROM executions WHERE approval_id=?',(a['id'],)).fetchone()) else None
        d['current_status']=deal_status(c,deal_id)
        approval_ids={a['id'] for a in d['approvals']}
        events=[{'at':d['email']['received_at'],'event':'email_received','details':{}},
                {'at':d['created_at'],'event':'deal_evaluated_and_draft_created','details':{'extraction':d['extraction']}}]
        for event in c.execute('SELECT at,event,details FROM audit ORDER BY id'):
            details=json.loads(event['details'])
            if details.get('deal_id')==deal_id or details.get('approval_id') in approval_ids:
                events.append({'at':event['at'],'event':event['event'],'details':details})
        d['activity']=sorted(events,key=lambda e:e['at'])
        return d
@app.put('/api/deals/{deal_id}/draft')
def save_draft(deal_id: str, d: Draft):
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',d.recipient): raise HTTPException(422,'Invalid recipient')
    with db() as c:
        row(c,'deals',deal_id)
        invalidated=[r['id'] for r in c.execute("SELECT id FROM approvals WHERE deal_id=? AND status='PENDING'",(deal_id,))]
        c.execute("UPDATE approvals SET status='INVALIDATED' WHERE deal_id=? AND status='PENDING'",(deal_id,))
        for approval_id in invalidated: audit(c,'approval_invalidated',{'approval_id':approval_id})
        c.execute('UPDATE deals SET draft=? WHERE id=?',(d.model_dump_json(),deal_id)); audit(c,'draft_saved',{'deal_id':deal_id})
    return d
@app.post('/api/deals/{deal_id}/approvals')
def create_approval(deal_id: str):
    return approval_service.create_approval(deal_id)

