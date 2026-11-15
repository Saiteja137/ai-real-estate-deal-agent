import email, imaplib, logging, os, re, threading
from datetime import datetime, timezone
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from fastapi import HTTPException
from backend import config
from backend.database import db, now, stamp, audit
from backend.services.approval_service import process_reply

log=logging.getLogger(__name__)

def poll_once():
    cutoff=now()
    with imaplib.IMAP4_SSL(os.environ['IMAP_HOST'],int(os.getenv('IMAP_PORT','993')),timeout=20) as mailbox:
        mailbox.login(os.environ['IMAP_USERNAME'],os.environ['IMAP_PASSWORD'])
        status,_=mailbox.select(os.getenv('IMAP_FOLDER','INBOX'),readonly=True)
        if status!='OK': raise RuntimeError('Cannot select approval mailbox')
        validity=mailbox.response('UIDVALIDITY')[1]
        if not validity or not validity[0]: raise RuntimeError('Mailbox did not supply UIDVALIDITY')
        epoch=validity[0].decode() if isinstance(validity[0],bytes) else str(validity[0])
        status,items=mailbox.uid('search',None,'ALL')
        if status!='OK': raise RuntimeError('Mailbox search failed')
        for uid in items[0].split():
            key=epoch+':'+uid.decode()
            with db() as c:
                if c.execute('SELECT 1 FROM mailbox_messages WHERE id=?',(key,)).fetchone(): continue
            status,parts=mailbox.uid('fetch',uid,'(BODY.PEEK[] INTERNALDATE)')
            if status!='OK': raise RuntimeError('Mailbox fetch failed')
            entry=next((x for x in parts if isinstance(x,tuple)),None)
            if not entry: raise RuntimeError('Mailbox returned incomplete message')
            match=re.search(rb'INTERNALDATE "([^"]+)"',entry[0])
            if not match: raise RuntimeError('Mailbox did not supply trusted INTERNALDATE')
            received=parsedate_to_datetime(match.group(1).decode()).astimezone(timezone.utc)
            msg=email.message_from_bytes(entry[1],policy=policy.default)
            sender=parseaddr(msg.get('From',''))[1]
            part=msg.get_body(preferencelist=('plain',)) if msg.is_multipart() else msg
            text=part.get_content() if part and part.get_content_type()=='text/plain' else ''
            match=re.search(r'(?im)^\s*Approval ID:\s*(RE-[A-F0-9]+)\s*$',text if isinstance(text,str) else '')
            result='IGNORED'
            if match:
                try: result=process_reply(match.group(1),sender,text,received)['status']
                except HTTPException as exc:
                    result='REJECTED_INPUT_'+str(exc.status_code)
                    with db() as c:
                        audit(c,'approval_reply_ignored',{'approval_id':match.group(1),'reason':result})
            with db() as c:
                c.execute('INSERT OR IGNORE INTO mailbox_messages VALUES (?,?,?)',(key,stamp(received),result))
        # Expire only after a successful complete mailbox scan, for deadlines already past at scan start.
        with db() as c:
            ids=[r[0] for r in c.execute("SELECT id FROM approvals WHERE status='PENDING' AND deadline < ?",(stamp(cutoff),))]
            c.executemany("UPDATE approvals SET status='EXPIRED' WHERE id=? AND status='PENDING'",[(i,) for i in ids])
            for i in ids: audit(c,'approval_expired',{'approval_id':i})

class ApprovalPoller:
    def __init__(self): self.stop_event=threading.Event(); self.thread=None
    def start(self):
        if not config.HITL_EMAIL_ENABLED and not config.LIVE_EMAIL_INGESTION_ENABLED: return
        def run():
            while not self.stop_event.is_set():
                try:
                    if config.HITL_EMAIL_ENABLED: poll_once()
                    if config.LIVE_EMAIL_INGESTION_ENABLED:
                        from backend.services.ingestion_service import poll_incoming
                        poll_incoming()
                except Exception: log.exception('Approval polling failed; approvals will not be expired until a complete scan succeeds')
                self.stop_event.wait(max(1,int(os.getenv('APPROVAL_POLL_INTERVAL_SECONDS','10'))))
        self.thread=threading.Thread(target=run,name='approval-poller',daemon=True); self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=25)
