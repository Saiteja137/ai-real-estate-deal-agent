import smtplib

import pytest

from backend.services import email_service


@pytest.fixture(autouse=True)
def smtp_settings(monkeypatch):
    for name, value in {'SMTP_HOST':'smtp.example.com','SMTP_USERNAME':'user','SMTP_PASSWORD':'password','SMTP_FROM':'buyer@example.com'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(email_service.time, 'sleep', lambda seconds: None)


class FakeSMTP:
    def __init__(self, behavior):
        self.behavior = behavior
    def ehlo(self): pass
    def starttls(self, **kwargs): pass
    def login(self, *args): pass
    def close(self): pass
    def send_message(self, message):
        return self.behavior(message)


def test_connection_retries_then_sends_once(monkeypatch):
    connections=[]; messages=[]
    def connect(*args, **kwargs):
        connections.append(1)
        if len(connections) < 3: raise ConnectionError('unavailable')
        return FakeSMTP(lambda message: messages.append(message) or {})
    monkeypatch.setattr(email_service.smtplib, 'SMTP', connect)
    assert email_service.send_message('seller@example.com', 'Subject', 'Body') == 'SMTP_ACCEPTED'
    assert len(connections)==3 and len(messages)==1


def test_temporary_rejection_reuses_message_and_then_sends(monkeypatch):
    messages=[]
    def send(message):
        messages.append(message['Message-ID'])
        if len(messages)==1: raise smtplib.SMTPDataError(451, b'try later')
        return {}
    monkeypatch.setattr(email_service.smtplib, 'SMTP', lambda *args,**kwargs:FakeSMTP(send))
    assert email_service.send_message('seller@example.com', 'Subject', 'Body') == 'SMTP_ACCEPTED'
    assert len(messages)==2 and messages[0]==messages[1]


def test_submission_disconnect_is_never_retried(monkeypatch):
    messages=[]
    def send(message):
        messages.append(message)
        raise smtplib.SMTPServerDisconnected('connection lost during submission')
    monkeypatch.setattr(email_service.smtplib, 'SMTP', lambda *args,**kwargs:FakeSMTP(send))
    with pytest.raises(smtplib.SMTPServerDisconnected):
        email_service.send_message('seller@example.com', 'Subject', 'Body')
    assert len(messages)==1


def test_partial_recipient_refusal_is_not_retried(monkeypatch):
    messages=[]
    monkeypatch.setattr(email_service.smtplib, 'SMTP', lambda *args,**kwargs:FakeSMTP(lambda msg:messages.append(msg) or {'other@example.com':(450,b'busy')}))
    with pytest.raises(RuntimeError, match='partially'):
        email_service.send_message('seller@example.com, other@example.com', 'Subject', 'Body')
    assert len(messages)==1


def test_temporary_failure_stops_after_three_retries(monkeypatch):
    calls=[]; delays=[]
    def connect(*args,**kwargs):
        calls.append(1)
        raise ConnectionError('unavailable')
    monkeypatch.setattr(email_service.smtplib, 'SMTP', connect)
    monkeypatch.setattr(email_service.time, 'sleep', delays.append)
    with pytest.raises(email_service.EmailNotSent):
        email_service.send_message('seller@example.com', 'Subject', 'Body')
    assert len(calls)==4 and delays==[1,2,4]


def test_permanent_recipient_refusal_does_not_retry(monkeypatch):
    calls=[]
    def send(message):
        calls.append(1)
        raise smtplib.SMTPRecipientsRefused({'seller@example.com':(550,b'no such user')})
    monkeypatch.setattr(email_service.smtplib, 'SMTP', lambda *args,**kwargs:FakeSMTP(send))
    with pytest.raises(email_service.EmailNotSent):
        email_service.send_message('seller@example.com', 'Subject', 'Body')
    assert len(calls)==1


def test_temporary_recipient_refusal_can_be_retried(monkeypatch):
    calls=[]
    def send(message):
        calls.append(1)
        if len(calls)==1:
            raise smtplib.SMTPRecipientsRefused({'seller@example.com':(450,b'busy')})
        return {}
    monkeypatch.setattr(email_service.smtplib, 'SMTP', lambda *args,**kwargs:FakeSMTP(send))
    assert email_service.send_message('seller@example.com', 'Subject', 'Body')=='SMTP_ACCEPTED'
    assert len(calls)==2
