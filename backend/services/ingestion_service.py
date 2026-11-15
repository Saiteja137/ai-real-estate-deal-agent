"""Optional IMAP ingestion of likely property offers; it never approves or sends mail."""
import email, imaplib, os, re
from email import policy
from email.utils import parseaddr
from fastapi import HTTPException
from backend.database import db, now, stamp, audit

OFFER = re.compile(r'\b(property|real estate|off.market|wholesale|investment opportunity)\b', re.I)
PRICE = re.compile(r'\b(asking|price|arv|repairs?)\b|\$\s*\d', re.I)

def poll_incoming():
    # Uses a separate folder when configured so approval replies are not ingested as offers.
    folder=os.getenv('INGESTION_IMAP_FOLDER','INBOX')
    with imaplib.IMAP4_SSL(os.environ['IMAP_HOST'],int(os.getenv('IMAP_PORT','993')),timeout=20) as mailbox:
        mailbox.login(os.environ['IMAP_USERNAME'],os.environ['IMAP_PASSWORD'])
        status,_=mailbox.select(folder,readonly=True)
        if status!='OK': raise RuntimeError('Cannot select ingestion mailbox')
        validity=mailbox.response('UIDVALIDITY')[1]
        if not validity or not validity[0]: raise RuntimeError('Missing UIDVALIDITY')
        epoch=validity[0].decode() if isinstance(validity[0],bytes) else str(validity[0])
        status,items=mailbox.uid('search',None,'ALL')
        if status!='OK': raise RuntimeError('Ingestion mailbox search failed')
        for uid in items[0].split():
            key=f'ingestion:{folder}:{epoch}:{uid.decode()}'
            with db() as c:
                if c.execute('SELECT 1 FROM mailbox_messages WHERE id=?',(key,)).fetchone(): continue
            status,parts=mailbox.uid('fetch',uid,'(BODY.PEEK[])')
            if status!='OK': raise RuntimeError('Ingestion mailbox fetch failed')
            entry=next((x for x in parts if isinstance(x,tuple)),None)
            if not entry: raise RuntimeError('Incomplete ingestion message')
            msg=email.message_from_bytes(entry[1],policy=policy.default)
            sender=parseaddr(msg.get('From',''))[1]; subject=str(msg.get('Subject',''))
            part=msg.get_body(preferencelist=('plain','html')) if msg.is_multipart() else msg
            body=part.get_content() if part and part.get_content_type() in ('text/plain','text/html') else ''
            text=body if isinstance(body,str) else ''
            if part and part.get_content_type()=='text/html':
                from backend.api.routes import PlainHTML
                parser=PlainHTML(); parser.feed(text); text=''.join(parser.parts)
            result='IGNORED'
            if sender and OFFER.search(subject+'\n'+text) and PRICE.search(subject+'\n'+text) and not re.search(r'(?im)^\s*(Decision|Approval ID):',text):
                # Import lazily to avoid coupling the worker lifecycle to the router import.
                from backend.api.routes import process_email, EmailInput
                try:
                    process_email(EmailInput(sender=sender,subject=subject,body=text)); result='PROCESSED'
                except HTTPException as exc:
                    if exc.status_code==409: result='DUPLICATE'
                    elif exc.status_code==422: result='INVALID'
                    else: raise
            with db() as c:
                c.execute('INSERT OR IGNORE INTO mailbox_messages VALUES (?,?,?)',(key,stamp(now()),result))
                audit(c,'email_ingestion',{'mailbox_uid':key,'result':result})
