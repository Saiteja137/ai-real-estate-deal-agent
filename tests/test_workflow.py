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

def test_openai_draft_returns_first_response_directly(monkeypatch):
    import types, openai
    from backend import config
    from backend.agents import email_agent
    calls = []
    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(output_text='Direct AI draft with $999,999.')
    monkeypatch.setattr(config, 'AI_ENABLED', True)
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: types.SimpleNamespace(responses=Responses()))
    result = email_agent.draft({}, {'classification':'INSUFFICIENT_DATA','missing':['arv'],'metrics':{}}, 'seller@example.com', 'Offer', original_email='Original offer details')
    assert 'Original offer details' in calls[0]['input']
    assert result['body'].startswith('Direct AI draft with $999,999.') and email_agent.AI_DISCLOSURE in result['body']
    assert len(calls) == 1

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

def test_definite_wholesaler_send_failure_is_recorded(monkeypatch):
    from backend.services import approval_service, email_service
    did,a=approval()
    def fail(*args, **kwargs): raise email_service.EmailNotSent('not accepted')
    monkeypatch.setattr(approval_service.email_service,'send_message',fail)
    assert reply(a).json()['execution']=='SEND_FAILED'
    assert client.get('/api/deals/'+did).json()['current_status']=='SEND_FAILED'

def test_definite_approval_notification_failure_is_recorded(monkeypatch):
    from backend.services import approval_service, email_service
    did=create()
    def fail(*args, **kwargs): raise email_service.EmailNotSent('not accepted')
    monkeypatch.setattr(approval_service.email_service,'send_message',fail)
    response=client.post(f'/api/deals/{did}/approvals')
    assert response.status_code==502
    state=client.get('/api/deals/'+did).json()
    assert state['current_status']=='NOTIFICATION_FAILED'
    assert any(event['event']=='notification_failed' for event in state['activity'])

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
    message=(f'From: approver@example.com\r\nSubject: reply\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nApproval ID: {a["id"]} Decision: APPROVED\r\n').encode()
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
    monkeypatch.setattr(approval_service,'polish_followup',lambda body,comment,decision,**kwargs:(calls.append((comment,decision)) or ('Polished: '+comment,'openai')))
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
            return {'classification':'NEGOTIATE','explanation':'Price requires review.','financial_assessment':'Review the calculated maximum offer.','criteria_assessment':'Compare the offer with the buying criteria.','due_diligence':['Verify repair estimates.'],'strengths':['Location stated'],'risks':['Verify repairs'],'passed':['Minimum ARV'],'failed':[],'recommended_action':'NEGOTIATE','suggested_next_action':'Request more details.'}
    monkeypatch.setattr(evaluation_agent,'ChatOpenAI',FakeModel)
    rule={'classification':'POTENTIAL_MATCH','metrics':{'mao':119000},'criteria':{},'missing':[]}
    result=evaluation_agent.assess_property({'city':'Dallas','asking_price':115000,'arv':220000,'repairs':35000},rule)
    assert result['classification']=='NEGOTIATE'
    assert received['config']['use_responses_api'] is True
    assert received['method']=='json_schema'
    assert '119000' in received['messages'][1].content
    assert received['config']['max_retries']==0

def test_reflection_agent_checks_each_stage_and_fails_closed(monkeypatch):
    import json
    from backend.agents import reflection_agent
    seen=[]
    class FakeModel:
        verdict='PASS'
        def __init__(self,**kwargs): pass
        def with_structured_output(self,schema,method):
            assert schema is reflection_agent.ReflectionResult and method=='json_schema'
            return self
        def invoke(self,messages):
            seen.append(json.loads(messages[1].content))
            return {'verdict':self.verdict,'issues':[] if self.verdict=='PASS' else ['Unsupported claim']}
    monkeypatch.setattr(reflection_agent,'ChatOpenAI',FakeModel)
    for stage in ('extraction','evaluation','draft','followup'):
        reflection_agent.review_ai_output(stage,{'source':'facts'},'candidate')
    assert [item['stage'] for item in seen]==['extraction','evaluation','draft','followup']
    FakeModel.verdict='FAIL'
    with pytest.raises(reflection_agent.ReflectionError):
        reflection_agent.review_ai_output('draft',{'source':'facts'},'candidate')

def test_reflection_unavailable_blocks_ai_analysis(monkeypatch):
    from backend.agents import reflection_agent
    from backend.api import routes
    class BrokenModel:
        def __init__(self,**kwargs): raise RuntimeError('provider unavailable')
    monkeypatch.setattr(reflection_agent,'ChatOpenAI',BrokenModel)
    def workflow(*args): reflection_agent.review_ai_output('extraction',{}, {})
    monkeypatch.setattr(routes,'run_workflow',workflow)
    response=client.post('/api/emails',json={'sender':'seller@example.com','subject':'Offer','body':'Property offer'})
    assert response.status_code==502 and 'quality review' in response.json()['detail']
    with db() as c:
        assert c.execute('SELECT count(*) FROM deals').fetchone()[0]==0

def test_graph_uses_ai_classification_but_keeps_python_metrics(monkeypatch):
    from backend import config
    from backend.workflows import deal_workflow
    monkeypatch.setattr(config,'AI_ENABLED',True)
    def no_python_classification(*args): raise AssertionError('Python classification should not run in AI mode')
    monkeypatch.setattr(deal_workflow,'evaluate',no_python_classification)
    monkeypatch.setattr(deal_workflow,'extract',lambda subject,body:([{'asking_price':115000,'arv':220000,'repairs':35000}], 'mock AI'))
    monkeypatch.setattr(deal_workflow,'assess_property',lambda p,e:{'classification':'NEGOTIATE','explanation':'Review terms','strengths':[],'risks':['Unverified'],'passed':[],'failed':[],'suggested_next_action':'Ask for details'})
    monkeypatch.setattr(deal_workflow,'draft',lambda p,e,sender,subject,**kwargs:{'recipient':sender,'subject':subject,'body':'draft'})
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
    monkeypatch.setattr(approval_service,'polish_followup',lambda body,comment,decision,**kwargs:(polish_calls.append(comment) or ('Polished: '+comment,'openai')))
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
    def polish(body,comment,decision,**kwargs):
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
    assert 'AI Deal Evaluation Agent' in message and 'Asking price is below the calculated offer.' in message
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

def test_approval_email_sections_and_bold_disclaimer():
    from backend.services.approval_notification import build_notification, notification_html, DISCLAIMER
    details={'city':'Dallas','asking_price':115000,'arv':220000,'repairs':35000}
    evaluation={'classification':'POTENTIAL_MATCH','evaluation_source':'openai','initial_draft_source':'openai','explanation':'Verify the repair estimate.','suggested_next_action':'Request photos.','metrics':{},'risks':[],'passed':[],'failed':[],'missing':[],'assumptions':[]}
    draft={'recipient':'seller@example.com','subject':'Re: Offer','body':'Please share <script>alert(1)</script> photos.'}
    text=build_notification('RE-123','2026-01-01T00:20:00+00:00',details,evaluation,draft,timeout_minutes=20)
    sections=['DISCLAIMER:', 'PROPERTY DETAILS','AI RECOMMENDATION','PROPOSED RESPONSE TO THE WHOLESALER','APPROVAL ID AND DEADLINE','APPROVAL REQUEST']
    positions=[text.index(s) for s in sections]
    assert positions==sorted(positions)
    assert 'Time to reply: 20 minutes' in text and 'Please share <script>alert(1)</script> photos.' in text
    rendered=notification_html(text)
    assert '<strong>'+DISCLAIMER+'</strong>' in rendered
    assert '<script>' not in rendered and '&lt;script&gt;' in rendered

def test_approval_sends_plain_and_bold_html_versions(monkeypatch):
    from backend.services import approval_service
    captured=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: captured.append((args,kwargs)) or 'SMTP_ACCEPTED')
    did,a=approval()
    assert len(captured)==1
    args,kwargs=captured[0]
    assert a['notification']==args[2]
    assert '<strong>DISCLAIMER:' in kwargs['html_body']
    assert kwargs['reply_to']=='inbox@example.com'
    stored=client.get('/api/deals/'+did).json()
    assert stored['approvals'][0]['notification']==a['notification']
    assert any(e['event']=='buyer_review_prepared' for e in stored['activity'])

def test_notification_hides_empty_criteria_and_uses_clear_comments():
    from backend.services.approval_notification import build_notification, notification_html
    details={'city':'Dallas','asking_price':115000,'arv':220000,'repairs':35000}
    evaluation={'classification':'POTENTIAL_MATCH','evaluation_source':'openai','metrics':{},'passed':[],'failed':[],'risks':[],'missing':[],'assumptions':[]}
    draft={'recipient':'seller@example.com','subject':'Offer','body':'Please share photos.'}
    text=build_notification('RE-123','2026-01-01T00:20:00+00:00',details,evaluation,draft)
    assert 'CRITERIA MET' not in text and 'CRITERIA NOT MET' not in text
    assert 'None recorded' not in text and 'AI Deal Evaluation Agent' in text
    assert 'Comments: followed by your comments, if any' in text
    html=notification_html(text)
    for heading in ('PROPERTY DETAILS','FINANCIAL DETAILS','AI RECOMMENDATION','PROPOSED RESPONSE TO THE WHOLESALER','APPROVAL ID AND DEADLINE','APPROVAL REQUEST'):
        assert '<strong>'+heading+'</strong>' in html
    assert 'background:#f2f7f3' in html and 'Please share photos.' in html

def test_buyer_message_preview_matches_saved_request_without_sending(monkeypatch):
    from backend.services import approval_service
    sent=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    did=create()
    preview=client.get(f'/api/deals/{did}/approval-preview')
    assert preview.status_code==200 and preview.json()['preview'] is True
    assert 'DISCLAIMER:' in preview.json()['notification']
    assert 'PROPOSED RESPONSE TO THE WHOLESALER' in preview.json()['notification']
    assert 'Assigned when requested' in preview.json()['notification']
    assert sent==[]
    with db() as c: assert c.execute('SELECT count(*) FROM approvals').fetchone()[0]==0
    actual=client.post(f'/api/deals/{did}/approvals').json()
    expected=actual['notification'].replace(actual['id'],'Assigned when requested').replace(actual['deadline'],'Assigned when requested')
    assert preview.json()['notification']==expected
    assert len(sent)==1

def test_buyer_message_preview_reflects_saved_draft():
    did=create(); draft=client.get('/api/deals/'+did).json()['draft']
    draft['body']='Please send the inspection report.'
    assert client.put(f'/api/deals/{did}/draft',json=draft).status_code==200
    assert 'Please send the inspection report.' in client.get(f'/api/deals/{did}/approval-preview').json()['notification']

def test_buyer_notification_includes_detailed_ai_analysis():
    from backend.services.approval_notification import build_notification,notification_html
    p={'city':'Dallas','asking_price':115000,'arv':220000,'repairs':35000}
    ev={'classification':'POTENTIAL_MATCH','evaluation_source':'openai','explanation':'Review this opportunity.','financial_assessment':'The asking price is below the calculated maximum offer. Net profit is unknown.','criteria_assessment':'The estimated repairs meet the configured repair limit.','due_diligence':['Verify repair estimates and comparable sales.'],'suggested_next_action':'Request documentation.','metrics':{},'missing':[]}
    body=build_notification('RE-123','2026-01-01T00:20:00+00:00',p,ev,{'recipient':'seller@example.com','subject':'Offer','body':'Please send details.'})
    assert 'FINANCIAL VIEW' in body and ev['financial_assessment'] in body
    assert 'FIT WITH BUYING CRITERIA' in body and ev['criteria_assessment'] in body
    assert 'DUE DILIGENCE QUESTIONS' in body and ev['due_diligence'][0] in body
    html=notification_html(body)
    assert '<strong>FINANCIAL VIEW</strong>' in html

def test_financial_abbreviations_expand_without_duplication():
    from backend.agents.terms import expand_financial_terms
    assert expand_financial_terms('ARV, MAO and ROI')=='ARV (After Repair Value), MAO (Maximum Allowable Offer) and ROI (Return on Investment)'
    assert expand_financial_terms('ARV (After Repair Value) and Return on Investment (ROI)')=='ARV (After Repair Value) and Return on Investment (ROI)'

def test_buyer_notification_expands_metrics_without_changing_frozen_reply():
    from backend.services.approval_notification import build_notification
    p={'city':'Dallas','asking_price':115000,'arv':220000,'repairs':35000}
    ev={'classification':'POTENTIAL_MATCH','evaluation_source':'openai','explanation':'Verify the ARV and ROI.','metrics':{'mao':119000,'roi_percent':None},'passed':['minimum ARV'],'missing':['arv'],'risks':['ROI is not confirmed'],'assumptions':[]}
    frozen={'recipient':'seller@example.com','subject':'Offer','body':'Please confirm the ARV.'}
    message=build_notification('RE-123','2026-01-01T00:20:00+00:00',p,ev,frozen)
    assert 'ARV (After Repair Value)' in message
    assert 'MAO (Maximum Allowable Offer)' in message
    assert 'ROI (Return on Investment)' in message
    assert 'To: seller@example.com\nSubject: Offer\n\nPlease confirm the ARV.' in message
    assert frozen['body']=='Please confirm the ARV.'

def test_same_line_approval_and_decision_is_accepted(monkeypatch):
    from backend.services import approval_service
    sent=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append(args) or 'SMTP_ACCEPTED')
    did,a=approval()
    text=f"Approval ID: {a['id']} Decision: APPROVED\n\nOn Sunday someone wrote:\n> Decision: REJECTED"
    result=reply(a,text=text)
    assert result.json()['execution']=='SMTP_ACCEPTED'
    assert len(sent)==2
    assert client.get('/api/deals/'+did).json()['approvals'][0]['status']=='APPROVED'

def test_same_line_ambiguous_decisions_remain_rejected():
    from backend.services.approval_service import parse_approval_reply
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        parse_approval_reply('Approval ID: RE-123 Decision: APPROVED Decision: REJECTED')

def test_same_line_fields_include_comment():
    from backend.services.approval_service import parse_approval_reply
    assert parse_approval_reply('Approval ID: RE-123 Decision: APPROVED Comments: Please send photos') == ('RE-123','APPROVED','Please send photos')

def test_email_action_links_prefill_decision_and_optional_comment():
    from html.parser import HTMLParser
    from urllib.parse import urlparse, parse_qs, unquote
    from backend.services.approval_notification import build_notification,notification_html
    from backend.services.approval_service import parse_approval_reply
    class Links(HTMLParser):
        def __init__(self): super().__init__(); self.hrefs=[]
        def handle_starttag(self,tag,attrs):
            if tag=='a': self.hrefs.append(dict(attrs).get('href',''))
    p={'city':'Dallas'}; ev={'classification':'INSUFFICIENT_DATA','metrics':{}}
    draft={'recipient':'seller@example.com','subject':'Offer','body':'Please share details.'}
    body=build_notification('RE-123','2026-01-01T00:20:00+00:00',p,ev,draft)
    html=notification_html(body,approval_id='RE-123',reply_to='approvals@example.com')
    parser=Links(); parser.feed(html)
    assert len(parser.hrefs)==2 and '>Approve</a>' in html and '>Reject</a>' in html
    for href,decision in zip(parser.hrefs,('APPROVED','REJECTED')):
        parsed=urlparse(href); values=parse_qs(parsed.query)
        assert parsed.scheme=='mailto' and unquote(parsed.path)=='approvals@example.com'
        assert parse_approval_reply(values['body'][0])==('RE-123',decision,'')
        assert 'Comments:' in values['body'][0]
    second=notification_html(body,approval_id='RE-123',reply_to='approvals@example.com',is_second_review=True)
    parser=Links();parser.feed(second)
    assert all('Comments:' not in parse_qs(urlparse(href).query)['body'][0] for href in parser.hrefs)

def test_email_action_links_reject_invalid_reply_address():
    from backend.services.approval_notification import notification_html
    with pytest.raises(ValueError):
        notification_html('text',approval_id='RE-123',reply_to='inbox@example.com\nBcc: attacker@example.com')

def test_button_prefilled_email_uses_normal_approval_validation(monkeypatch):
    from html.parser import HTMLParser
    from urllib.parse import urlparse, parse_qs
    from backend.services import approval_service
    sent=[]
    monkeypatch.setattr(approval_service.email_service,'send_message',lambda *args,**kwargs: sent.append((args,kwargs)) or 'SMTP_ACCEPTED')
    class Links(HTMLParser):
        def __init__(self): super().__init__(); self.hrefs=[]
        def handle_starttag(self,tag,attrs):
            if tag=='a': self.hrefs.append(dict(attrs)['href'])
    did,a=approval()
    parser=Links();parser.feed(sent[0][1]['html_body'])
    assert len(sent)==1  # Rendering or opening a link does not send a wholesaler message.
    body=parse_qs(urlparse(parser.hrefs[0]).query)['body'][0]+'Please share photos'
    result=reply(a,sender='attacker@example.com',text=body)
    assert result.status_code==403 and len(sent)==1
    result=reply(a,text=body)
    assert result.json()['status']=='APPROVED' and result.json()['execution']=='SMTP_ACCEPTED'
    assert len(sent)==2
    assert client.get('/api/deals/'+did).json()['approvals'][0]['comments']=='Please share photos'

def test_all_configured_buying_criteria_are_reported():
    from backend.tools.buying_criteria import criteria_checks
    p={'asking_price':115000,'arv':220000,'repairs':35000,'property_type':'single-family house'}
    checks=criteria_checks(p,DEFAULT_CRITERIA,metrics(p,DEFAULT_CRITERIA))
    assert len(checks)==7
    assert all(item['status']=='MET' for item in checks[:6])
    assert checks[-1]['name']=='Investment strategy' and checks[-1]['status']=='INFORMATIONAL'
    unknown=criteria_checks({'asking_price':115000},DEFAULT_CRITERIA,metrics({'asking_price':115000},DEFAULT_CRITERIA))
    assert unknown[0]['status']=='MET'
    assert all(item['status']=='UNKNOWN' for item in unknown[1:6])

def test_contradictory_values_do_not_count_as_passed():
    from backend.tools.buying_criteria import criteria_checks
    p={'asking_price':115000,'arv':220000,'repairs':35000,'property_type':'single-family','contradictions':['Conflicting asking prices']}
    checks=criteria_checks(p,DEFAULT_CRITERIA,metrics(p,DEFAULT_CRITERIA))
    assert all(item['status']=='UNKNOWN' for item in checks[:6])

def test_buyer_notification_shows_all_criteria_and_unknowns():
    from backend.services.approval_notification import build_notification
    from backend.tools.buying_criteria import criteria_checks
    p={'asking_price':115000}
    calculated=metrics(p,DEFAULT_CRITERIA)
    checks=criteria_checks(p,DEFAULT_CRITERIA,calculated)
    ev={'classification':'INSUFFICIENT_DATA','metrics':calculated,'criteria_results':checks,'missing':['arv','repairs']}
    text=build_notification('RE-123','2026-01-01T00:20:00Z',p,ev,{'recipient':'seller@example.com','subject':'Offer','body':'Please share details.'})
    assert 'BUYING CRITERIA' in text and 'Maximum purchase price: Met' in text
    assert 'Minimum after repair value: Unknown' in text
    assert 'Investment strategy: Configured' in text
    assert 'CRITERIA MET\n' not in text  # no duplicated lists when detailed criteria exist

def test_unknown_property_type_stays_unknown():
    from backend.tools.buying_criteria import criteria_checks
    p={'asking_price':115000,'arv':220000,'repairs':35000,'property_type':'Unknown'}
    assert criteria_checks(p,DEFAULT_CRITERIA,metrics(p,DEFAULT_CRITERIA))[5]['status']=='UNKNOWN'

def test_activity_records_approver_comment_and_wholesaler_send_stages(monkeypatch):
    from backend.services import approval_service
    sent=[]
    def transport(*args,**kwargs):
        if not kwargs.get('reply_to'):
            with db() as c:
                assert c.execute("SELECT status FROM executions LIMIT 1").fetchone()['status']=='SENDING'
                assert c.execute("SELECT count(*) FROM audit WHERE event='wholesaler_send_started'").fetchone()[0]==1
        sent.append(args)
        return 'SMTP_ACCEPTED'
    monkeypatch.setattr(approval_service.email_service,'send_message',transport)
    did,a=approval()
    result=reply(a,text=f"Approval ID: {a['id']}\nDecision: APPROVED\nComments: Please share photos")
    assert result.json()['execution']=='SMTP_ACCEPTED'
    events=client.get('/api/deals/'+did).json()['activity']
    relevant=[entry for entry in events if entry['event'] in ('approval_decision','final_reply_prepared','wholesaler_send_started','email_execution')]
    assert [entry['event'] for entry in relevant]==['approval_decision','final_reply_prepared','wholesaler_send_started','email_execution']
    assert relevant[0]['details']['sender']=='approver@example.com'
    assert relevant[0]['details']['comment']=='Please share photos'
    assert relevant[0]['details']['received_at']
    assert all(entry['details']['recipient']=='fictional@example.com' for entry in relevant[1:])
    assert relevant[-1]['details']['status']=='SMTP_ACCEPTED'

def test_rejected_approval_activity_records_reviewer_and_decline_recipient():
    did,a=approval()
    result=reply(a,text=f"Approval ID: {a['id']}\nDecision: REJECTED\nComments: Not within our budget")
    assert result.json()['status']=='REJECTED'
    events=client.get('/api/deals/'+did).json()['activity']
    decision=next(e for e in events if e['event']=='approval_decision')
    outcome=next(e for e in events if e['event']=='email_execution')
    assert decision['details']['status']=='REJECTED' and decision['details']['comment']=='Not within our budget'
    assert outcome['details']['recipient']=='fictional@example.com'
    assert outcome['details']['decision']=='REJECTED'

def test_old_execution_events_include_recorded_recipient():
    import json
    did,a=approval(); assert reply(a).status_code==200
    with db() as c:
        c.execute("UPDATE audit SET details=? WHERE event='email_execution'",(json.dumps({'approval_id':a['id'],'status':'SMTP_ACCEPTED'}),))
    event=next(e for e in client.get('/api/deals/'+did).json()['activity'] if e['event']=='email_execution')
    assert event['details']['recipient']=='fictional@example.com'


def test_deal_reply_intent_and_missing_information():
    from backend.agents.email_agent import resolution_intent, template_content
    ev = {'classification':'NEGOTIATE', 'metrics':{'mao':135000}, 'missing':['closing_costs']}
    body = template_content({'asking_price':140000}, ev)
    assert resolution_intent(ev) == 'NEGOTIATE'
    assert '$140,000.00' in body and '$135,000.00' in body
    assert 'closing costs' in body and 'Would the seller consider' in body
    ev['recommended_action'] = 'REQUEST_INFORMATION'
    assert resolution_intent(ev) == 'REQUEST_INFORMATION'
    assert 'closing costs' in template_content({}, ev)
    assert resolution_intent({'classification':'REJECT'}) == 'DECLINE'




def test_extraction_and_followup_use_direct_single_calls(monkeypatch):
    import types, openai
    from backend import config
    from backend.agents import property_agent, email_agent
    from backend.schemas.property import Properties, Property
    calls=[]
    responses=types.SimpleNamespace(
        parse=lambda **kwargs: calls.append('extract') or types.SimpleNamespace(output_parsed=Properties(properties=[Property(city='Dallas')])),
        create=lambda **kwargs: calls.append('followup') or types.SimpleNamespace(output_text='Direct reply'))
    monkeypatch.setattr(config,'AI_ENABLED',True)
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs: types.SimpleNamespace(responses=responses))
    assert property_agent.extract('Offer','Dallas')[0][0]['city']=='Dallas'
    assert email_agent.polish_followup('Draft','note','APPROVED')[0].startswith('Direct reply\n\nDisclosure:')
    assert calls==['extract','followup']


def test_workflow_ignores_empty_extracted_properties(monkeypatch):
    from backend.workflows import deal_workflow
    monkeypatch.setattr(deal_workflow, 'extract', lambda *args: ([{'address':None,'asking_price':None,'contradictions':[]}], 'test'))
    result = deal_workflow.run_workflow('Offer', 'No property details', 'seller@example.com', DEFAULT_CRITERIA)
    assert result['properties'] == [] and result['results'] == []
