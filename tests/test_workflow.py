import os
import tempfile

_test_database_dir = tempfile.TemporaryDirectory(prefix='deal-agent-tests-')
os.environ['DATABASE_URL']='sqlite:///' + os.path.join(_test_database_dir.name, 'test.db').replace('\\', '/')
os.environ['HITL_EMAIL_ENABLED']='false'
os.environ['LIVE_EMAIL_INGESTION_ENABLED']='false'
os.environ['AI_ENABLED']='false'
os.environ['SECOND_DRAFT_APPROVAL_ENABLED']='false'
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
        for table in ('approval_followups','executions','approvals','deals','emails','audit'): c.execute('DELETE FROM '+table)

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
    assert client.get('/api/deals/'+did).json()['approvals'][0]['execution'] is None
    assert reply(a,'REJECTED').json()['status']=='REJECTED'
    assert client.get('/api/deals/'+did).json()['approvals'][0]['execution']['status']=='SMTP_ACCEPTED'
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
    from backend import config
    from backend.core import draft
    calls=[]
    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls)==1:
                return types.SimpleNamespace(id='response_1',output=[types.SimpleNamespace(type='function_call',name='get_missing_information',arguments='{}',call_id='call_1')])
            return types.SimpleNamespace(output_text='Thank you. Please share the missing information.')
    monkeypatch.setenv('OPENAI_API_KEY','fake-test-key')
    monkeypatch.setattr(config,'AI_ENABLED',True)
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




def test_ambiguous_comments_are_rejected():
    did,a=approval()
    result=reply(a,text=f"Approval ID: {a['id']}\nDecision: APPROVED\nComments: First\nComments: Second")
    assert result.status_code==400
    assert client.get('/api/deals/'+did).json()['approvals'][0]['status']=='PENDING'

def test_rejection_sends_polite_decline_after_one_decision(monkeypatch):
    from backend.services import approval_service
    import json
    sent=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    did,a=approval(); original=client.get('/api/deals/'+did).json()['draft']
    assert reply(a,'REJECTED').json()['execution']=='SMTP_ACCEPTED'
    state=client.get('/api/deals/'+did).json()
    assert len(sent)==2 and 'not be moving forward' in sent[-1][2]
    assert state['approvals'][0]['payload']==original
    assert json.loads(state['approvals'][0]['execution']['payload'])['body']==sent[-1][2]
    assert reply(a,'REJECTED').status_code==409 and len(sent)==2

def test_approved_comment_is_polished_and_sent_once(monkeypatch):
    from backend.services import approval_service
    import json
    sent=[]; calls=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    monkeypatch.setattr(approval_service,'polish_followup',lambda body,comment,decision:(calls.append((comment,decision)) or ('Polished: '+comment,'openai')))
    did,a=approval()
    result=reply(a,text=f"Approval ID: {a['id']}\nDecision: APPROVED\nComments: Please include photos")
    assert result.json()['execution']=='SMTP_ACCEPTED' and len(sent)==2
    assert sent[-1][2]=='Polished: Please include photos'
    state=client.get('/api/deals/'+did).json()
    assert json.loads(state['approvals'][0]['execution']['payload'])['body']==sent[-1][2]
    event=next(e for e in state['activity'] if e['event']=='approval_decision')
    assert event['details']['sender']=='approver@example.com' and event['details']['comment']=='Please include photos'
    assert calls==[('Please include photos','APPROVED')]
    assert reply(a).status_code==409 and len(sent)==2

def test_rejected_comment_and_ai_failure_send_fallback(monkeypatch):
    from backend.services import approval_service
    import json
    def fail(*args): raise RuntimeError('API unavailable')
    monkeypatch.setattr(approval_service,'polish_followup',fail)
    did,a=approval()
    result=reply(a,text=f"Approval ID: {a['id']}\nDecision: REJECTED\nComments: Budget is limited")
    assert result.json()['execution']=='SMTP_ACCEPTED'
    state=client.get('/api/deals/'+did).json()
    assert 'Budget is limited' in json.loads(state['approvals'][0]['execution']['payload'])['body']
    assert any(e['event']=='final_reply_prepared' and e['details']['source']=='template_after_ai_error' for e in state['activity'])

def test_ai_disabled_ignores_configured_key(monkeypatch):
    import openai
    from backend import config
    from backend.agents.property_agent import extract
    from backend.agents.email_agent import draft, polish_followup
    monkeypatch.setattr(config,'AI_ENABLED',False)
    monkeypatch.setenv('OPENAI_API_KEY','fake-key')
    def forbidden(*args,**kwargs): raise AssertionError('OpenAI must not be called when AI is disabled')
    monkeypatch.setattr(openai,'OpenAI',forbidden)
    properties,source=extract('Offer','Location: Dallas\nAsking Price: $115,000\nARV: $220,000\nRepairs: $35,000')
    assert properties[0]['asking_price']==115000 and 'AI disabled' in source
    assert 'Thank you' in draft({}, {'classification':'REJECT','missing':[],'metrics':{}},'seller@example.com','Offer')['body']
    assert polish_followup('Draft','note','APPROVED')==('Draft','template')

def test_langchain_ai_evaluation_uses_precomputed_metrics(monkeypatch):
    from backend.agents import evaluation_agent
    received={}
    class FakeModel:
        def __init__(self, **kwargs): received['config']=kwargs
        def with_structured_output(self, schema, method):
            received['schema']=schema; received['method']=method; return self
        def invoke(self, messages):
            received['messages']=messages
            return {'classification':'NEGOTIATE','explanation':'Price requires review.','strengths':['Location stated'],'risks':['Verify repairs'],'passed':['Minimum ARV'],'failed':[],'suggested_next_action':'Request more details.'}
    monkeypatch.setattr(evaluation_agent,'ChatOpenAI',FakeModel)
    rule={'classification':'POTENTIAL_MATCH','metrics':{'mao':119000},'criteria':{},'missing':[]}
    result=evaluation_agent.assess_property({'city':'Dallas','asking_price':115000,'arv':220000,'repairs':35000},rule)
    assert result['classification']=='NEGOTIATE'
    assert received['config']['use_responses_api'] is True
    assert received['method']=='json_schema'
    assert '119000' in received['messages'][1].content

def test_graph_uses_ai_classification_but_keeps_python_metrics(monkeypatch):
    from backend import config
    from backend.workflows import deal_workflow
    monkeypatch.setattr(config,'AI_ENABLED',True)
    def no_python_classification(*args): raise AssertionError('Python classification should not run in AI mode')
    monkeypatch.setattr(deal_workflow,'evaluate',no_python_classification)
    monkeypatch.setattr(deal_workflow,'extract',lambda subject,body:([{'asking_price':115000,'arv':220000,'repairs':35000}], 'mock AI'))
    monkeypatch.setattr(deal_workflow,'assess_property',lambda p,e:{'classification':'NEGOTIATE','explanation':'Review terms','strengths':[],'risks':['Unverified'],'passed':[],'failed':[],'suggested_next_action':'Ask for details'})
    monkeypatch.setattr(deal_workflow,'draft',lambda p,e,sender,subject:{'recipient':sender,'subject':subject,'body':'draft'})
    result=deal_workflow.run_workflow('Offer','body','seller@example.com',DEFAULT_CRITERIA)['results'][0]
    ev=result['evaluation']
    assert ev['classification']=='NEGOTIATE' and 'rule_classification' not in ev
    assert ev['metrics']['mao']==119000 and ev['metrics']['gross_spread']==70000
    assert ev['evaluation_source']=='openai'

def test_second_approval_reviews_exact_polished_reply(monkeypatch):
    import json
    from backend import config
    from backend.services import approval_service
    sent=[]; polish_calls=[]
    monkeypatch.setattr(config,'SECOND_DRAFT_APPROVAL_ENABLED',True)
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    monkeypatch.setattr(approval_service,'polish_followup',lambda body,comment,decision:(polish_calls.append(comment) or ('Polished: '+comment,'openai')))
    did,first=approval()
    result=reply(first,text=f"Approval ID: {first['id']}\nDecision: APPROVED\nComments: Please send photos")
    assert result.json()['execution']=='AWAITING_SECOND_APPROVAL'
    assert len(sent)==2  # two notifications, no wholesaler email
    second_id=result.json()['followup_approval_id']
    deal=client.get('/api/deals/'+did).json()
    second=next(a for a in deal['approvals'] if a['id']==second_id)
    assert second['parent_approval_id']==first['id'] and second['payload']['body']=='Polished: Please send photos'
    assert 'Polished: Please send photos' in second['notification']
    monkeypatch.setattr(config,'SECOND_DRAFT_APPROVAL_ENABLED',False) # persisted stage remains enforced
    second_reply={'id':second_id}
    assert reply(second_reply,text=f"Approval ID: {second_id}\nDecision: APPROVED\nComments: more changes").status_code==400
    assert len(sent)==2
    assert reply(second_reply).json()['execution']=='SMTP_ACCEPTED'
    assert len(sent)==3 and sent[-1][2]=='Polished: Please send photos'
    assert polish_calls==['Please send photos']
    state=client.get('/api/deals/'+did).json()
    sent_execution=next(a['execution'] for a in state['approvals'] if a['id']==second_id)
    assert json.loads(sent_execution['payload'])==second['payload']
    assert reply(second_reply).status_code==409 and len(sent)==3

def test_second_approval_rejection_cancels_sending(monkeypatch):
    from backend import config
    from backend.services import approval_service
    sent=[]
    monkeypatch.setattr(config,'SECOND_DRAFT_APPROVAL_ENABLED',True)
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    did,first=approval()
    result=reply(first,'REJECTED').json()
    assert result['execution']=='AWAITING_SECOND_APPROVAL' and len(sent)==2
    second={'id':result['followup_approval_id']}
    rejected=reply(second,'REJECTED')
    assert rejected.json()['status']=='REJECTED' and rejected.json()['execution'] is None
    assert len(sent)==2
    with db() as c: assert c.execute('SELECT 1 FROM executions WHERE approval_id=?',(second['id'],)).fetchone() is None

def test_second_approval_notification_failure_never_sends_wholesaler(monkeypatch):
    from backend import config
    from backend.services import approval_service
    calls=[]
    monkeypatch.setattr(config,'SECOND_DRAFT_APPROVAL_ENABLED',True)
    def send(*args,**kwargs):
        calls.append(args)
        if len(calls)==2: raise TimeoutError('notification outcome unknown')
        return 'SMTP_ACCEPTED'
    monkeypatch.setattr(approval_service.email_service,'send_message',send)
    did,first=approval()
    result=reply(first,text=f"Approval ID: {first['id']}\nDecision: APPROVED\nComments: More information")
    assert result.json()['execution']=='SECOND_APPROVAL_ERROR'
    assert len(calls)==2
    with db() as c:
        assert c.execute('SELECT status FROM executions WHERE approval_id=?',(first['id'],)).fetchone()[0]=='SECOND_APPROVAL_ERROR'

def test_second_approval_does_not_overwrite_draft_changed_during_polishing(monkeypatch):
    from backend import config
    from backend.services import approval_service
    sent=[]
    monkeypatch.setattr(config,'SECOND_DRAFT_APPROVAL_ENABLED',True)
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    did,a=approval()
    def polish(body,comment,decision):
        current=client.get('/api/deals/'+did).json()['draft']; current['body']='Human changed this draft'
        assert client.put('/api/deals/'+did+'/draft',json=current).status_code==200
        return 'AI replacement','openai'
    monkeypatch.setattr(approval_service,'polish_followup',polish)
    result=reply(a,text=f"Approval ID: {a['id']}\nDecision: APPROVED\nComments: Please include photos")
    assert result.json()['execution']=='SECOND_APPROVAL_ERROR'
    assert client.get('/api/deals/'+did).json()['draft']['body']=='Human changed this draft'
    assert len(sent)==1

@pytest.mark.parametrize('body',[
    'We expect a guaranteed profit of $999,999.',
    'We will buy the property immediately.',
    'Please visit https://untrusted.example/path.',
    'Contact invented@example.com for details.',
])
def test_ai_draft_guardrail_rejects_unsupported_content(body):
    from backend.agents.guardrails import validate_reply
    with pytest.raises(ValueError): validate_reply(body, {'property':'A Dallas property','arv':220000})

def test_ai_draft_guardrail_allows_supplied_values():
    from backend.agents.guardrails import validate_reply
    message='Would the seller consider a non-binding offer near $119,000?'
    assert validate_reply(message, {'metrics':{'mao':119000}})==message

def test_ai_hallucinated_number_falls_back_before_sending(monkeypatch):
    from backend.services import approval_service
    import json
    sent=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    monkeypatch.setattr(approval_service,'polish_followup',lambda *args:('Your guaranteed payout is $999,999.','openai'))
    did,a=approval()
    result=reply(a,text=f"Approval ID: {a['id']}\nDecision: APPROVED\nComments: Please share photos")
    assert result.json()['execution']=='SMTP_ACCEPTED'
    assert '$999,999' not in sent[-1][2] and 'Please share photos' in sent[-1][2]
    state=client.get('/api/deals/'+did).json()
    assert any(e['event']=='final_reply_prepared' and e['details']['source']=='template_after_guardrail' for e in state['activity'])
    assert json.loads(state['approvals'][0]['execution']['payload'])['body']==sent[-1][2]

def test_unsafe_comment_requires_review_without_sending(monkeypatch):
    from backend.services import approval_service
    sent=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    did,a=approval()
    result=reply(a,text=f"Approval ID: {a['id']}\nDecision: APPROVED\nComments: We will buy the property tomorrow")
    assert result.json()['execution']=='REQUIRES_REVIEW'
    assert len(sent)==1  # Only the approval notification was sent.
    assert client.get('/api/deals/'+did).json()['current_status']=='REQUIRES_REVIEW'

def test_ai_assessment_cannot_classify_missing_core_data_as_match(monkeypatch):
    from backend.agents import evaluation_agent
    class FakeModel:
        def __init__(self,**kwargs): pass
        def with_structured_output(self,*args,**kwargs): return self
        def invoke(self,messages):
            return {'classification':'STRONG_MATCH','explanation':'Looks strong','strengths':[],'risks':[],'passed':[],'failed':[],'suggested_next_action':'Review'}
    monkeypatch.setattr(evaluation_agent,'ChatOpenAI',FakeModel)
    with pytest.raises(ValueError): evaluation_agent.assess_property({'asking_price':100000,'arv':None,'repairs':None},{'metrics':{},'criteria':{},'missing':['arv','repairs']})

def test_model_permission_error_is_explained_without_exposing_details(monkeypatch):
    import httpx, openai
    from backend.api import routes
    response=httpx.Response(403,request=httpx.Request('POST','https://api.openai.com/v1/responses'))
    def fail(*args,**kwargs):
        raise openai.PermissionDeniedError('private provider details',response=response,body=None)
    monkeypatch.setattr(routes,'run_workflow',fail)
    result=client.post('/api/emails',json={'sender':'seller@example.com','subject':'Offer','body':'Property offer'})
    assert result.status_code==502
    assert 'model is unavailable' in result.json()['detail']
    assert 'private provider details' not in result.text
    with db() as c: assert c.execute('SELECT count(*) FROM emails').fetchone()[0]==0

def test_approval_notification_explains_deal_and_unknown_costs():
    from backend.services.approval_notification import build_notification
    property_details={'city':'Dallas','asking_price':115000,'arv':220000,'repairs':35000}
    evaluation={'classification':'POTENTIAL_MATCH','evaluation_source':'openai','initial_draft_source':'openai','explanation':'Asking price is below the calculated offer.','suggested_next_action':'Request photos.','metrics':{'mao':119000,'gross_spread':70000,'estimated_profit':None,'roi_percent':None},'risks':['Estimates are unverified'],'passed':['Repair limit'],'failed':[],'missing':['address'],'assumptions':['Unknown costs are not zero']}
    draft={'recipient':'seller@example.com','subject':'Re: Offer','body':'Please share photos.'}
    message=build_notification('RE-123','2026-01-01T00:20:00+00:00',property_details,evaluation,draft)
    assert 'OpenAI-assisted evaluation' in message and 'Asking price is below the calculated offer.' in message
    assert '$119,000.00' in message and '$70,000.00' in message
    assert 'Estimated net profit (calculated): Not available' in message
    assert 'Estimates are unverified' in message and 'Request photos.' in message
    assert 'address' in message and draft['body'] in message

def test_reanalyze_creates_new_deal_without_duplicate_email():
    did=create()
    old=client.get('/api/deals/'+did).json()
    result=client.post(f'/api/deals/{did}/reanalyze')
    assert result.status_code==200
    new_id=result.json()['deal_ids'][0]
    assert new_id!=did
    new=client.get('/api/deals/'+new_id).json()
    assert new['email_id']==old['email_id']
    with db() as c:
        assert c.execute('SELECT count(*) FROM emails').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM deals').fetchone()[0]==2

def test_reanalyze_refuses_while_related_approval_pending():
    did,a=approval()
    assert client.post(f'/api/deals/{did}/reanalyze').status_code==409
    assert client.get('/api/deals/'+did).json()['approvals'][0]['status']=='PENDING'

def test_cancel_pending_approval_allows_reanalysis_and_blocks_late_reply():
    did,a=approval()
    assert client.post(f'/api/deals/{did}/reanalyze').status_code==409
    assert client.post(f"/api/approvals/{a['id']}/cancel").json()['status']=='CANCELLED'
    assert reply(a).status_code==409
    assert client.post(f'/api/deals/{did}/reanalyze').status_code==200
    with db() as c: assert c.execute('SELECT count(*) FROM executions WHERE approval_id=?',(a['id'],)).fetchone()[0]==0
