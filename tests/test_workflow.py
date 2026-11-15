import os
import tempfile

_test_database_dir = tempfile.TemporaryDirectory(prefix='deal-agent-tests-')
os.environ['DATABASE_URL']='sqlite:///' + os.path.join(_test_database_dir.name, 'test.db').replace('\\', '/')
os.environ['HITL_EMAIL_ENABLED']='false'
os.environ['LIVE_EMAIL_INGESTION_ENABLED']='false'
os.environ.pop('OPENAI_API_KEY',None)
from fastapi.testclient import TestClient
from backend.main import app, db
from backend.core import metrics, evaluate, DEFAULT_CRITERIA
import pytest

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    from backend import config
    from backend.services import approval_service
    monkeypatch.setattr(config,'HITL_EMAIL_ENABLED',True)
    monkeypatch.setenv('APPROVER_EMAILS','approver@example.com')
    monkeypatch.setenv('APPROVAL_REPLY_TO','inbox@example.com')
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs:'SMTP_ACCEPTED')
    with db() as c:
        for table in ('executions','approvals','deals','emails','audit'): c.execute('DELETE FROM '+table)

client=TestClient(app)
def create():
    r=client.post('/api/emails',json={'sender':'fictional@example.com','subject':'Offer','body':'Location: Dallas\nAsking Price: $115,000\nARV: $220,000\nRepairs: $35,000'})
    assert r.status_code==200, r.text
    return r.json()['deal_ids'][0]
def approval():
    did=create(); r=client.post(f'/api/deals/{did}/approvals'); assert r.status_code==200
    return did,r.json()
def reply(a,decision='APPROVED',sender='approver@example.com',text=None):
    from backend.services.approval_service import process_reply
    from datetime import datetime, timezone
    from fastapi import HTTPException
    class Result:
        def __init__(self, status_code, value): self.status_code=status_code; self.value=value
        def json(self): return self.value
    try:
        value=process_reply(a['id'],sender,text or f"Approval ID: {a['id']}\nDecision: {decision}",datetime.now(timezone.utc))
        return Result(200,value)
    except HTTPException as exc: return Result(exc.status_code,{'detail':exc.detail})
def test_metrics():
    p={'asking_price':115000,'arv':220000,'repairs':35000,'closing_costs':2000,'assignment_fee':1000,'selling_costs':5000}
    m=metrics(p,DEFAULT_CRITERIA)
    assert m=={'mao':119000.0,'gross_spread':70000.0,'all_in_cost':153000.0,'estimated_profit':62000.0,'roi_percent':40.52}
    p['selling_costs']=None
    assert metrics(p,DEFAULT_CRITERIA)['estimated_profit'] is None
def test_classification():
    assert evaluate({'asking_price':125000,'arv':230000,'repairs':85000},DEFAULT_CRITERIA)['classification']=='REJECT'
    assert evaluate({'asking_price':140000},DEFAULT_CRITERIA)['classification']=='INSUFFICIENT_DATA'
    assert evaluate({'asking_price':210000,'arv':240000,'repairs':45000},DEFAULT_CRITERIA)['classification']=='NEGOTIATE'
def test_approval_and_idempotence():
    did,a=approval(); assert 'PROPOSED RESPONSE' in a['notification']
    assert reply(a).json()['execution']=='SMTP_ACCEPTED'
    assert reply(a).status_code==409
    d=client.get('/api/deals/'+did).json(); assert d['approvals'][0]['execution']['status']=='SMTP_ACCEPTED'
def test_rejection_and_unauthorized():
    did,a=approval(); assert reply(a,sender='attacker@example.com').status_code==403
    assert reply(a,'REJECTED').json()['status']=='REJECTED'
    assert client.get('/api/deals/'+did).json()['approvals'][0]['execution'] is None
def test_expiration_and_invalidation():
    did,a=approval()
    with db() as c: c.execute("UPDATE approvals SET status='EXPIRED' WHERE id=?",(a['id'],))
    assert reply(a).status_code==409
    b=client.post(f'/api/deals/{did}/approvals').json()
    d=client.get('/api/deals/'+did).json()['draft']; d['body']='Changed'
    client.put(f'/api/deals/{did}/draft',json=d)
    assert reply(b).status_code==409

def test_deadline_and_frozen_draft():
    from datetime import datetime, timedelta
    did,a=approval()
    d=client.get('/api/deals/'+did).json(); frozen=d['approvals'][0]
    assert datetime.fromisoformat(frozen['deadline'])-datetime.fromisoformat(frozen['created_at']) == timedelta(minutes=20)
    assert frozen['payload']==d['draft']
    assert frozen['payload']['body'] in frozen['notification']
    assert reply(a).status_code==200
    d=client.get('/api/deals/'+did).json(); import json
    assert json.loads(d['approvals'][0]['execution']['payload'])==frozen['payload']

def test_tampered_frozen_payload_cannot_execute():
    did,a=approval()
    with db() as c: c.execute("UPDATE approvals SET payload=? WHERE id=?", ('{}',a['id']))
    assert reply(a).status_code==409
    assert client.get('/api/deals/'+did).json()['approvals'][0]['execution'] is None

def test_ambiguous_reply_cannot_execute():
    did,a=approval()
    r=reply(a,text=f"Approval ID: {a['id']}\nDecision: APPROVED\nDecision: REJECTED")
    assert r.status_code==400
    assert client.get('/api/deals/'+did).json()['approvals'][0]['execution'] is None

def test_late_reply_cannot_execute():
    from datetime import datetime, timedelta, timezone
    did,a=approval()
    with db() as c: c.execute('UPDATE approvals SET deadline=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),a['id']))
    assert reply(a).json()['status']=='EXPIRED'
    assert client.get('/api/deals/'+did).json()['approvals'][0]['execution'] is None

def test_html_eml_import_strips_scripts():
    raw='From: Seller <seller@example.com>\nSubject: Offer\nContent-Type: text/html; charset=utf-8\n\n<p>Hello</p><script>malicious()</script><p>Offer</p>'
    r=client.post('/api/import-eml',json={'content':raw})
    assert r.status_code==200
    assert r.json()['sender']=='seller@example.com'
    assert 'Hello' in r.json()['body'] and 'malicious' not in r.json()['body']

def test_missing_costs_and_zero_divisor():
    p={'asking_price':0,'arv':100000,'repairs':0,'closing_costs':0,'assignment_fee':0,'selling_costs':0}
    assert metrics(p,DEFAULT_CRITERIA)['roi_percent'] is None
    p['closing_costs']=None
    assert metrics(p,DEFAULT_CRITERIA)['all_in_cost'] is None

def test_duplicate_input_and_overview():
    create()
    r=client.post('/api/emails',json={'sender':'fictional@example.com','subject':'Offer','body':'Location: Dallas\nAsking Price: $115,000\nARV: $220,000\nRepairs: $35,000'})
    assert r.status_code==409
    assert client.get('/api/overview').json()['deals']==1

def test_openai_drafting_function_call_is_validated(monkeypatch):
    import types, openai
    from backend.core import draft
    calls=[]
    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls)==1:
                return types.SimpleNamespace(id='response_1',output=[types.SimpleNamespace(type='function_call',name='get_missing_information',arguments='{}',call_id='call_1')])
            return types.SimpleNamespace(output_text='Thank you. Please share the missing information.')
    monkeypatch.setenv('OPENAI_API_KEY','fake-test-key')
    monkeypatch.setattr(openai,'OpenAI',lambda:types.SimpleNamespace(responses=Responses()))
    result=draft({}, {'classification':'INSUFFICIENT_DATA','missing':['arv'],'metrics':{}},'seller@example.com','Offer')
    assert result['recipient']=='seller@example.com'
    assert result['body']=='Thank you. Please share the missing information.'
    assert calls[0]['tools'][0]['name']=='get_missing_information'
    assert calls[1]['input'][0]['output']=='["arv"]'

def test_live_approved_reply_smtp_once(monkeypatch):
    from backend import config
    from backend.services import approval_service
    from datetime import datetime, timezone
    did,a=approval(); sent=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    text=f"Approval ID: {a['id']}\nDecision: APPROVED"
    out=approval_service.process_reply(a['id'],'approver@example.com',text,datetime.now(timezone.utc))
    assert out['execution']=='SMTP_ACCEPTED' and len(sent)==1
    with pytest.raises(Exception): approval_service.process_reply(a['id'],'approver@example.com',text,datetime.now(timezone.utc))
    assert len(sent)==1

def test_smtp_uncertainty_is_not_retried(monkeypatch):
    from backend import config
    from backend.services import approval_service
    from datetime import datetime, timezone
    did,a=approval()
    def fail(*args,**kwargs): raise TimeoutError()
    monkeypatch.setattr(approval_service.email_service,'send_message',fail)
    result=approval_service.process_reply(a['id'],'approver@example.com',f"Approval ID: {a['id']}\nDecision: APPROVED",datetime.now(timezone.utc))
    assert result['execution']=='NEEDS_RECONCILIATION'
    assert client.get('/api/deals/'+did).json()['approvals'][0]['execution']['status']=='NEEDS_RECONCILIATION'

def test_timely_received_processed_after_deadline():
    from backend.services.approval_service import process_reply
    from datetime import datetime, timedelta, timezone
    did,a=approval(); d=client.get('/api/deals/'+did).json()['approvals'][0]
    received=datetime.fromisoformat(d['created_at'])+timedelta(seconds=1)
    with db() as c: c.execute('UPDATE approvals SET deadline=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(microseconds=1)).isoformat(),a['id']))
    # Put the original deadline in the past while retaining a receipt in the window.
    with db() as c: c.execute('UPDATE approvals SET created_at=?, deadline=? WHERE id=?',((received-timedelta(minutes=2)).isoformat(),(received+timedelta(seconds=1)).isoformat(),a['id']))
    result=process_reply(a['id'],'approver@example.com',f"Approval ID: {a['id']}\nDecision: APPROVED",received)
    assert result['status']=='APPROVED'

def test_imap_worker_processes_trusted_timestamp_and_deduplicates(monkeypatch):
    from backend.services import approval_poller
    from email.utils import format_datetime
    from datetime import datetime, timezone, timedelta
    did,a=approval(); received=datetime.now(timezone.utc)
    message=(f'From: approver@example.com\r\nSubject: reply\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nApproval ID: {a["id"]}\r\nDecision: APPROVED\r\n').encode()
    class FakeIMAP:
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def login(self,*a): return 'OK',[]
        def select(self,*a,**k): return 'OK',[]
        def response(self,*a): return ('UIDVALIDITY',[b'123'])
        def uid(self,command,*args):
            if command=='search': return 'OK',[b'42']
            return 'OK',[(b'1 (UID 42 INTERNALDATE "'+format_datetime(received).encode()+b'"',message)]
    monkeypatch.setenv('IMAP_HOST','mail.example.com'); monkeypatch.setenv('IMAP_USERNAME','example'); monkeypatch.setenv('IMAP_PASSWORD','test')
    monkeypatch.setattr(approval_poller.imaplib,'IMAP4_SSL',FakeIMAP)
    with db() as c: c.execute('DELETE FROM mailbox_messages')
    # IMAP INTERNALDATE has one-second resolution; ensure request predates it.
    with db() as c: c.execute('UPDATE approvals SET created_at=? WHERE id=?',((received-timedelta(seconds=2)).isoformat(),a['id']))
    approval_poller.poll_once(); approval_poller.poll_once()
    with db() as c:
        assert c.execute('SELECT count(*) FROM mailbox_messages').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM executions WHERE approval_id=?',(a['id'],)).fetchone()[0]==1

def test_tool_registry_denies_model_execution():
    from backend.tools.registry import invoke
    with pytest.raises(PermissionError): invoke('execute_approved_reply',{'id':'RE-123'})
    with pytest.raises(PermissionError): invoke('create_email_approval',{'id':'123'})

def test_configured_approval_timeout(monkeypatch):
    from backend import config
    from datetime import datetime, timedelta
    monkeypatch.setattr(config,'APPROVAL_TIMEOUT_MINUTES',35)
    did,a=approval(); stored=client.get('/api/deals/'+did).json()['approvals'][0]
    assert datetime.fromisoformat(stored['deadline'])-datetime.fromisoformat(stored['created_at']) == timedelta(minutes=35)

def test_buyer_company_in_fallback_draft(monkeypatch):
    from backend.agents.email_agent import draft
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.setenv('BUYER_NAME','Test Buyer'); monkeypatch.setenv('BUYER_COMPANY','Test Company')
    result=draft({}, {'classification':'REJECT','missing':[],'metrics':{}},'seller@example.com','Offer')
    assert 'Test Buyer' in result['body'] and 'Test Company' in result['body']

def test_imap_ingestion_filters_and_deduplicates(monkeypatch):
    from backend.services import ingestion_service
    data=b'From: seller@example.com\r\nSubject: Off-market property\r\nContent-Type: text/plain\r\n\r\nLocation: Dallas\nAsking Price: $115,000\nARV: $220,000\nRepairs: $35,000'
    class FakeIMAP:
        def __init__(self,*args,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def login(self,*args): return 'OK',[]
        def select(self,*args,**kwargs): return 'OK',[]
        def response(self,*args): return ('UIDVALIDITY',[b'989'])
        def uid(self,command,*args): return ('OK',[b'8']) if command=='search' else ('OK',[(b'header',data)])
    monkeypatch.setenv('IMAP_HOST','example.com'); monkeypatch.setenv('IMAP_USERNAME','test'); monkeypatch.setenv('IMAP_PASSWORD','test')
    monkeypatch.setattr(ingestion_service.imaplib,'IMAP4_SSL',FakeIMAP)
    with db() as c: c.execute("DELETE FROM mailbox_messages WHERE id LIKE 'ingestion:%'")
    ingestion_service.poll_incoming(); ingestion_service.poll_incoming()
    with db() as c:
        assert c.execute("SELECT count(*) FROM mailbox_messages WHERE id LIKE 'ingestion:%' AND result='PROCESSED'").fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM emails').fetchone()[0]==1

def test_approval_disabled_does_not_create_record(monkeypatch):
    from backend import config
    monkeypatch.setattr(config,'HITL_EMAIL_ENABLED',False)
    did=create()
    assert client.post(f'/api/deals/{did}/approvals').status_code==409
    with db() as c: assert c.execute('SELECT count(*) FROM approvals').fetchone()[0]==0

def test_hitl_off_stays_in_dashboard_and_blocks_decisions(monkeypatch):
    from backend import config
    from backend.services.approval_service import process_reply
    from fastapi import HTTPException
    from datetime import datetime, timezone
    did,a=approval()
    monkeypatch.setattr(config,'HITL_EMAIL_ENABLED',False)
    with pytest.raises(HTTPException):
        process_reply(a['id'],'approver@example.com',f"Approval ID: {a['id']}\nDecision: APPROVED",datetime.now(timezone.utc))
    assert client.get('/api/deals/'+did).json()['approvals'][0]['status']=='PENDING'
    with db() as c: assert c.execute('SELECT count(*) FROM executions WHERE approval_id=?',(a['id'],)).fetchone()[0]==0

def test_activity_tracks_approval_and_delivery():
    did,a=approval()
    before=client.get('/api/deals/'+did).json()
    assert before['current_status']=='PENDING'
    assert {'email_received','deal_evaluated_and_draft_created','approval_requested','notification_accepted'} <= {x['event'] for x in before['activity']}
    assert reply(a).status_code==200
    after=client.get('/api/deals/'+did).json()
    assert after['current_status']=='SMTP_ACCEPTED'
    assert {'approval_decision','email_execution'} <= {x['event'] for x in after['activity']}

def test_hitl_off_new_deal_ready_for_review(monkeypatch):
    from backend import config
    monkeypatch.setattr(config,'HITL_EMAIL_ENABLED',False)
    did=create(); deal=client.get('/api/deals/'+did).json()
    assert deal['current_status']=='READY_FOR_REVIEW'
    assert client.get('/api/config').json()['hitl_email_enabled'] is False
    assert client.post(f'/api/deals/{did}/approvals').status_code==409
