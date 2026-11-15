import hashlib, json, os, re, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import openai
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
    return {'hitl_email_enabled': config.HITL_EMAIL_ENABLED, 'live_email_ingestion_enabled': config.LIVE_EMAIL_INGESTION_ENABLED, 'ai_enabled': config.AI_ENABLED, 'second_draft_approval_enabled':config.SECOND_DRAFT_APPROVAL_ENABLED}
@app.get('/api/criteria')
def get_criteria():
    with db() as c: return json.loads(c.execute("SELECT value FROM settings WHERE key='criteria'").fetchone()[0])
@app.put('/api/criteria')
def set_criteria(v: Criteria):
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key='criteria'",(v.model_dump_json(),))
        audit(c,'criteria_updated',v.model_dump())
    return v

def analyze_email(subject, body, sender, criteria):
    try:
        return run_workflow(subject,body,sender,criteria)
    except openai.AuthenticationError as exc:
        raise HTTPException(502,'OpenAI authentication failed. Check OPENAI_API_KEY in .env and restart the backend.') from exc
    except (openai.PermissionDeniedError, openai.NotFoundError) as exc:
        raise HTTPException(502,'The configured OpenAI model is unavailable to this project. Check OPENAI_MODEL and the project model permissions, then restart the backend.') from exc
    except openai.RateLimitError as exc:
        raise HTTPException(502,'OpenAI rate limit or quota was reached. Check API usage and billing, then try again.') from exc
    except (openai.APIConnectionError, openai.APITimeoutError) as exc:
        raise HTTPException(502,'Could not reach OpenAI. Check the internet connection and try again.') from exc
    except Exception as exc:
        raise HTTPException(502,'Property analysis or drafting failed. Check the server configuration and try again.') from exc

@app.post('/api/emails')
def process_email(e: EmailInput):
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',e.sender): raise HTTPException(422,'Invalid sender')
    fp=payload_hash(e.model_dump())
    with db() as c:
        if c.execute('SELECT 1 FROM emails WHERE fingerprint=?',(fp,)).fetchone(): raise HTTPException(409,'This email has already been processed. Select the existing deal in the dashboard.')
    criteria=get_criteria()
    result = analyze_email(e.subject,e.body,e.sender,criteria)
    properties, source = result['properties'], result['source']
    if not properties: raise HTTPException(422,'No property found')
    eid=str(uuid.uuid4()); ids=[]
    with db() as c:
        try: c.execute('INSERT INTO emails VALUES (?,?,?,?,?,?)',(eid,e.sender,e.subject,e.body,stamp(now()),fp))
        except sqlite3.IntegrityError: raise HTTPException(409,'This email has already been processed. Select the existing deal in the dashboard.')
        for item in result['results']:
            p=item['property']; ev=item['evaluation']; d=item['draft']; did=str(uuid.uuid4())
            c.execute('INSERT INTO deals VALUES (?,?,?,?,?,?,?)',(did,eid,json.dumps(p),json.dumps(ev),json.dumps(d),source,stamp(now())))
            ids.append(did)
        audit(c,'email_processed',{'email_id':eid,'deal_ids':ids,'extraction':source})
        for did in ids: audit(c,'deal_evaluated',{'deal_id':did})
    return {'deal_ids':ids}

@app.post('/api/deals/{deal_id}/reanalyze')
def reanalyze_deal(deal_id: str):
    with db() as c:
        previous=row(c,'deals',deal_id)
        incoming=row(c,'emails',previous['email_id'])
        if c.execute("SELECT 1 FROM approvals a JOIN deals d ON d.id=a.deal_id WHERE d.email_id=? AND a.status='PENDING'",(previous['email_id'],)).fetchone():
            raise HTTPException(409,'An approval is pending for this email. Resolve it before creating a new analysis.')
    result=analyze_email(incoming['subject'],incoming['body'],incoming['sender'],get_criteria())
    if not result['results']: raise HTTPException(422,'No property found')
    ids=[]
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM approvals a JOIN deals d ON d.id=a.deal_id WHERE d.email_id=? AND a.status='PENDING'",(previous['email_id'],)).fetchone():
            raise HTTPException(409,'An approval is pending for this email. Resolve it before creating a new analysis.')
        for item in result['results']:
            did=str(uuid.uuid4())
            c.execute('INSERT INTO deals VALUES (?,?,?,?,?,?,?)',(did,previous['email_id'],json.dumps(item['property']),json.dumps(item['evaluation']),json.dumps(item['draft']),result['source'],stamp(now())))
            audit(c,'deal_reanalyzed',{'deal_id':did,'previous_deal_id':deal_id})
            ids.append(did)
    return {'deal_ids':ids}

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
    if a['status']=='APPROVED' and not execution: return 'FOLLOWUP_DRAFT_READY'
    return execution['status'] if execution else a['status']
@app.get('/api/deals/{deal_id}')
def deal(deal_id: str):
    with db() as c:
        d=unpack(row(c,'deals',deal_id),['property','evaluation','draft']); d['email']=row(c,'emails',d['email_id'])
        d['approvals']=[dict(r) for r in c.execute('SELECT * FROM approvals WHERE deal_id=? ORDER BY created_at DESC',(deal_id,))]
        for a in d['approvals']:
            a['payload']=json.loads(a['payload']); a['execution']=dict(x) if (x:=c.execute('SELECT * FROM executions WHERE approval_id=?',(a['id'],)).fetchone()) else None
            parent=c.execute('SELECT parent_approval_id FROM approval_followups WHERE approval_id=?',(a['id'],)).fetchone()
            a['parent_approval_id']=parent['parent_approval_id'] if parent else None
        d['current_status']=deal_status(c,deal_id)
        approval_ids={a['id'] for a in d['approvals']}
        events=[{'at':d['email']['received_at'],'event':'email_received','details':{}},
                {'at':d['created_at'],'event':'deal_evaluated_and_draft_created','details':{'extraction':d['extraction']}}]
        for event in c.execute('SELECT at,event,details FROM audit ORDER BY id'):
            details=json.loads(event['details'])
            if details.get('deal_id')==deal_id or details.get('approval_id') in approval_ids:
                if event['event']=='approval_decision':
                    approval=next((a for a in d['approvals'] if a['id']==details.get('approval_id')),None)
                    if approval:
                        details.setdefault('sender',approval.get('sender'))
                        details.setdefault('comment',approval.get('comments'))
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

@app.post('/api/approvals/{approval_id}/cancel')
def cancel_approval(approval_id: str):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        approval=row(c,'approvals',approval_id)
        if approval['status']!='PENDING':
            raise HTTPException(409,'This approval request is no longer pending')
        c.execute("UPDATE approvals SET status='CANCELLED' WHERE id=?",(approval_id,))
        audit(c,'approval_cancelled',{'approval_id':approval_id,'deal_id':approval['deal_id']})
    return {'status':'CANCELLED'}

