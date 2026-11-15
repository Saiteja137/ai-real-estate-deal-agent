import json, os, re, uuid

from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

from fastapi import HTTPException

from backend import config

from backend.database import db, now, stamp, payload_hash, audit, row, unpack

from backend.services import email_service



def create_approval(deal_id: str):

    if not config.HITL_EMAIL_ENABLED: raise HTTPException(409,'Email approvals are disabled')

    with db() as c:

        c.execute('BEGIN IMMEDIATE')

        d=unpack(row(c,'deals',deal_id),['property','evaluation','draft'])

        if c.execute("SELECT 1 FROM approvals WHERE deal_id=? AND status='PENDING'",(deal_id,)).fetchone(): raise HTTPException(409,'An approval is already pending')

        a='RE-'+uuid.uuid4().hex.upper(); created=now(); deadline=created+timedelta(minutes=config.APPROVAL_TIMEOUT_MINUTES); p=d['property']; ev=d['evaluation']; v=d['draft']

        notification=f"APPROVAL NOTIFICATION\nApproval ID: {a}\nDeadline: {stamp(deadline)}\nProperty: {p.get('address') or p.get('city') or 'Unknown'}\nWholesaler: {v['recipient']}\nAsking price: {p.get('asking_price')}\nARV: {p.get('arv')}\nRepairs: {p.get('repairs')}\nMAO: {ev['metrics']['mao']}\nGross spread: {ev['metrics']['gross_spread']}\nClassification: {ev['classification']}\nRisks: {'; '.join(ev['risks'])}\n\nPROPOSED RESPONSE\nTo: {v['recipient']}\nSubject: {v['subject']}\n\n{v['body']}\n\nReply with the following two lines:\nApproval ID: {a}\nDecision: APPROVED\n\nOr use Decision: REJECTED. No response means no email will be sent. Keep the approval ID confidential."

        c.execute('INSERT INTO approvals(id,deal_id,payload,payload_hash,status,created_at,deadline,notification) VALUES (?,?,?,?,?,?,?,?)',(a,deal_id,json.dumps(v),payload_hash(v),'PENDING',stamp(created),stamp(deadline),notification)); audit(c,'approval_requested',{'approval_id':a})

    try:

        email_service.send_message(os.environ['APPROVER_EMAILS'],f'[APPROVAL REQUIRED] Real Estate Deal – ID: {a}',notification,reply_to=os.environ['APPROVAL_REPLY_TO'])

    except Exception as exc:

        with db() as c:

            c.execute("UPDATE approvals SET status='NOTIFICATION_UNKNOWN' WHERE id=?",(a,)); audit(c,'notification_unknown',{'approval_id':a,'error_type':type(exc).__name__})

        raise HTTPException(502,'Approval notification failed or has an uncertain outcome; check the mailbox') from exc

    with db() as c: audit(c,'notification_accepted',{'approval_id':a})
    return {'id':a,'notification':notification,'deadline':stamp(deadline)}



def process_reply(approval_id, sender, text, received):
    if not config.HITL_EMAIL_ENABLED: raise HTTPException(409,'Email approvals are disabled')

    allowed={x.strip().lower() for x in os.getenv('APPROVER_EMAILS','approver@example.com').split(',')}

    if sender.strip().lower() not in allowed: raise HTTPException(403,'Unauthorized sender')

    # Discard conventional quoted replies, including the instructions embedded in the original notification.

    lines=[]

    for line in text.splitlines():

        if line.lstrip().startswith('>') or re.match(r'^On .+wrote:\s*$',line.strip(),re.I) or line.strip().startswith('-----Original Message-----'): break

        lines.append(line)

    clean='\n'.join(lines)

    ids=re.findall(r'(?im)^\s*Approval ID:\s*(RE-[A-F0-9]+)\s*$',clean)

    decisions=re.findall(r'(?im)^\s*Decision:\s*(APPROVED|APPROVE|REJECTED|REJECT)\s*$',clean)

    if len(ids)!=1 or ids[0]!=approval_id or len(decisions)!=1: raise HTTPException(400,'Invalid or ambiguous reply')

    if received.tzinfo is None: raise ValueError('Trusted receipt time must be timezone-aware')

    received=received.astimezone(timezone.utc)

    with db() as c:

        c.execute('BEGIN IMMEDIATE'); a=row(c,'approvals',approval_id)

        if a['status']!='PENDING': raise HTTPException(409,'Approval is no longer pending')

        if received < datetime.fromisoformat(a['created_at']) or received > datetime.fromisoformat(a['deadline']):

            if received > datetime.fromisoformat(a['deadline']):

                c.execute("UPDATE approvals SET status='EXPIRED' WHERE id=?",(approval_id,)); audit(c,'approval_expired',{'approval_id':approval_id}); return {'status':'EXPIRED'}

            raise HTTPException(400,'Receipt precedes approval request')

        status='APPROVED' if decisions[0].upper() in ('APPROVE','APPROVED') else 'REJECTED'

        payload=json.loads(a['payload'])

        if payload_hash(payload)!=a['payload_hash']: raise HTTPException(409,'Frozen payload integrity check failed')

        comment=re.search(r'(?im)^Comments:\s*(.*)$',clean)

        c.execute('UPDATE approvals SET status=?,decided_at=?,sender=?,comments=? WHERE id=?',(status,stamp(received),sender,comment.group(1) if comment else None,approval_id))

        execution=None

        if status=='APPROVED':

            execution='SENDING'

            c.execute('INSERT INTO executions VALUES (?,?,?,?,?)',(str(uuid.uuid4()),approval_id,execution,a['payload'],stamp(now())))

        audit(c,'approval_decision',{'approval_id':approval_id,'status':status,'execution':execution})

    if execution=='SENDING':

        try: result=email_service.send_message(payload['recipient'],payload['subject'],payload['body'])

        except Exception: result='NEEDS_RECONCILIATION'

        with db() as c:

            c.execute('UPDATE executions SET status=?,executed_at=? WHERE approval_id=?',(result,stamp(now()),approval_id)); audit(c,'email_execution',{'approval_id':approval_id,'status':result})

        execution=result

    return {'status':status,'execution':execution}



