import json, os, re, uuid

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend import config

from backend.database import db, now, stamp, payload_hash, audit, row, unpack

from backend.services import email_service
from backend.services.approval_notification import build_notification, notification_html
from backend.agents.email_agent import polish_followup, template_content



def create_approval(deal_id: str, *, followup_of=None, prepared_payload=None):

    if not config.HITL_EMAIL_ENABLED: raise HTTPException(409,'Email approvals are disabled')

    with db() as c:

        c.execute('BEGIN IMMEDIATE')

        d=unpack(row(c,'deals',deal_id),['property','evaluation','draft'])

        if c.execute("SELECT 1 FROM approvals WHERE deal_id=? AND status='PENDING'",(deal_id,)).fetchone(): raise HTTPException(409,'An approval is already pending')

        if followup_of is not None:
            parent=row(c,'approvals',followup_of)
            if parent['deal_id'] != deal_id or prepared_payload is None:
                raise HTTPException(409,'Invalid follow-up approval')
            if payload_hash(d['draft']) != parent['payload_hash']:
                raise HTTPException(409,'The draft changed while the follow-up was being prepared')
            changed=c.execute("UPDATE executions SET status='AWAITING_SECOND_APPROVAL',payload=? WHERE approval_id=? AND status='PREPARING_REVIEW'",(json.dumps(prepared_payload),followup_of))
            if changed.rowcount != 1: raise HTTPException(409,'Follow-up is no longer available')
            c.execute('UPDATE deals SET draft=? WHERE id=?',(json.dumps(prepared_payload),deal_id))
            d['draft']=prepared_payload
        a='RE-'+uuid.uuid4().hex.upper(); created=now(); deadline=created+timedelta(minutes=config.APPROVAL_TIMEOUT_MINUTES); p=d['property']; ev=d['evaluation']; v=d['draft']

        notification=build_notification(a,stamp(deadline),p,ev,v,
            is_second_review=followup_of is not None,
            second_review_enabled=config.SECOND_DRAFT_APPROVAL_ENABLED,timeout_minutes=config.APPROVAL_TIMEOUT_MINUTES)
        c.execute('INSERT INTO approvals(id,deal_id,payload,payload_hash,status,created_at,deadline,notification) VALUES (?,?,?,?,?,?,?,?)',(a,deal_id,json.dumps(v),payload_hash(v),'PENDING',stamp(created),stamp(deadline),notification)); audit(c,'approval_requested',{'approval_id':a})
        audit(c,'buyer_review_prepared',{'approval_id':a,'deal_id':deal_id,'evaluation_source':ev.get('evaluation_source'),'initial_draft_source':ev.get('initial_draft_source')})
        if followup_of is not None:
            c.execute('INSERT INTO approval_followups(approval_id,parent_approval_id) VALUES (?,?)',(a,followup_of))
            audit(c,'second_approval_requested',{'approval_id':a,'parent_approval_id':followup_of,'deal_id':deal_id})

    try:

        email_service.send_message(os.environ['APPROVER_EMAILS'],f'[APPROVAL REQUIRED] Real Estate Deal – ID: {a}',notification,reply_to=os.environ['APPROVAL_REPLY_TO'],html_body=notification_html(notification,approval_id=a,reply_to=os.environ['APPROVAL_REPLY_TO'],is_second_review=followup_of is not None))

    except email_service.EmailNotSent as exc:
        with db() as c:
            c.execute("UPDATE approvals SET status='NOTIFICATION_FAILED' WHERE id=?",(a,))
            audit(c,'notification_failed',{'approval_id':a})
        raise HTTPException(502,'Approval notification was not sent after SMTP retries. Check the email settings and try again.') from exc
    except Exception as exc:

        with db() as c:

            c.execute("UPDATE approvals SET status='NOTIFICATION_UNKNOWN' WHERE id=?",(a,)); audit(c,'notification_unknown',{'approval_id':a,'error_type':type(exc).__name__})

        raise HTTPException(502,'Approval notification failed or has an uncertain outcome; check the mailbox') from exc

    with db() as c: audit(c,'notification_accepted',{'approval_id':a})
    return {'id':a,'notification':notification,'deadline':stamp(deadline)}



def parse_approval_reply(text):
    """Parse only the new reply, allowing labeled fields on one or more lines."""
    lines=[]
    for line in text.splitlines():
        if line.lstrip().startswith('>') or re.match(r'^On .+wrote:\s*$',line.strip(),re.I) or line.strip().startswith('-----Original Message-----'):
            break
        # Mail clients sometimes collapse a pasted two-line reply into one line.
        line=re.sub(r'[ \t]+(?=(?:Approval[ \t]+ID|Decision|Comments?)[ \t]*:)', '\n', line, flags=re.I)
        lines.append(line)
    clean='\n'.join(lines)
    ids=re.findall(r'(?im)^[ \t]*Approval[ \t]+ID[ \t]*:[ \t]*(RE-[A-F0-9]+)[ \t]*$',clean)
    decisions=re.findall(r'(?im)^[ \t]*Decision[ \t]*:[ \t]*(APPROVED|APPROVE|REJECTED|REJECT)[ \t]*$',clean)
    comments=re.findall(r'(?im)^[ \t]*Comments?[ \t]*:[ \t]*([^\r\n]*)',clean)
    if len(ids)!=1 or len(decisions)!=1 or len(comments)>1:
        raise HTTPException(400,'Invalid or ambiguous reply')
    comment=comments[0].strip() if comments else ''
    if len(comment)>2000: raise HTTPException(400,'Comment is too long')
    return ids[0].upper(), decisions[0].upper(), comment

def process_reply(approval_id, sender, text, received):
    if not config.HITL_EMAIL_ENABLED: raise HTTPException(409,'Email approvals are disabled')

    allowed={x.strip().lower() for x in os.getenv('APPROVER_EMAILS','approver@example.com').split(',')}

    if sender.strip().lower() not in allowed: raise HTTPException(403,'Unauthorized sender')

    parsed_id, decision, comment=parse_approval_reply(text)
    if parsed_id != approval_id.upper(): raise HTTPException(400,'Invalid or ambiguous reply')

    if received.tzinfo is None: raise ValueError('Trusted receipt time must be timezone-aware')

    received=received.astimezone(timezone.utc)

    with db() as c:

        c.execute('BEGIN IMMEDIATE'); a=row(c,'approvals',approval_id)

        if a['status']!='PENDING': raise HTTPException(409,'Approval is no longer pending')

        if received < datetime.fromisoformat(a['created_at']) or received > datetime.fromisoformat(a['deadline']):

            if received > datetime.fromisoformat(a['deadline']):

                c.execute("UPDATE approvals SET status='EXPIRED' WHERE id=?",(approval_id,)); audit(c,'approval_expired',{'approval_id':approval_id}); return {'status':'EXPIRED'}

            raise HTTPException(400,'Receipt precedes approval request')

        status='APPROVED' if decision in ('APPROVE','APPROVED') else 'REJECTED'

        payload=json.loads(a['payload'])

        if payload_hash(payload)!=a['payload_hash']: raise HTTPException(409,'Frozen payload integrity check failed')

        is_followup = c.execute('SELECT 1 FROM approval_followups WHERE approval_id=?',(approval_id,)).fetchone() is not None
        if is_followup and comment: raise HTTPException(400,'Do not add a comment to a second decision; reject if changes are required')

        c.execute('UPDATE approvals SET status=?,decided_at=?,sender=?,comments=? WHERE id=?',(status,stamp(received),sender,comment or None,approval_id))

        if is_followup and status=='REJECTED':
            audit(c,'approval_decision',{'approval_id':approval_id,'status':status,'execution':None,'sender':sender,'comment':None,'received_at':stamp(received)})
            audit(c,'wholesaler_reply_cancelled',{'approval_id':approval_id,'deal_id':a['deal_id'],'recipient':payload['recipient'],'reason':'Second approval rejected'})
            return {'status':status,'execution':None}

        # Reserve the unique execution before any external calls. This prevents
        # duplicate decisions from sending twice, including after a restart.
        needs_second_review = not is_followup and config.SECOND_DRAFT_APPROVAL_ENABLED and (status=='REJECTED' or bool(comment))
        execution='PREPARING_REVIEW' if needs_second_review else 'PREPARING'
        c.execute('INSERT INTO executions VALUES (?,?,?,?,?)',(str(uuid.uuid4()),approval_id,execution,a['payload'],stamp(now())))
        audit(c,'approval_decision',{'approval_id':approval_id,'status':status,'execution':execution,'sender':sender,'comment':comment or None,'received_at':stamp(received)})

    final_payload = dict(payload)
    with db() as c:
        deal_context = unpack(row(c, 'deals', a['deal_id']), ['property', 'evaluation', 'draft'])
    if status == 'REJECTED' and not is_followup:
        property_details = deal_context['property']
        name = property_details.get('wholesaler_name') or 'there'
        signature = '\n'.join(part for part in (os.getenv('BUYER_NAME', 'Example Buyer'), os.getenv('BUYER_COMPANY', '')) if part)
        final_payload['body'] = (f'Hi {name},\n\nThank you for sharing this opportunity.\n\n'
            'After reviewing the available information, we will not be moving forward '
            'with this property at this time. ' + template_content(property_details, {**deal_context['evaluation'], 'recommended_action':'DECLINE'}) + '\n\n'
            f'Best regards,\n{signature}')
    if comment:
        final_payload['body'] += f'\n\nAdditional note: {comment}'
    template_body = final_payload['body']
    source='original' if status=='APPROVED' and not comment else 'template'
    if not is_followup and (comment or status=='REJECTED'):
        try:
            final_payload['body'], source = polish_followup(final_payload['body'], comment, status, context={'property':deal_context['property'], 'evaluation':deal_context['evaluation'], 'original_draft':payload['body']})
        except Exception:
            # Keep the drafted reply and the note when AI is unavailable.
            source='template_after_ai_error'
    if needs_second_review:
        try:
            followup = create_approval(a['deal_id'],followup_of=approval_id,prepared_payload=final_payload)
        except Exception:
            with db() as c:
                c.execute("UPDATE executions SET status='SECOND_APPROVAL_ERROR' WHERE approval_id=? AND status IN ('PREPARING_REVIEW','AWAITING_SECOND_APPROVAL')",(approval_id,))
                audit(c,'second_approval_error',{'approval_id':approval_id,'deal_id':a['deal_id']})
            return {'status':status,'execution':'SECOND_APPROVAL_ERROR'}
        return {'status':status,'execution':'AWAITING_SECOND_APPROVAL','followup_approval_id':followup['id']}
    # Persist the exact message before sending. The approval's frozen original
    # remains untouched; execution.payload records what was actually attempted.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        updated=c.execute("UPDATE executions SET payload=?,status='SENDING' WHERE approval_id=? AND status='PREPARING'",(json.dumps(final_payload),approval_id))
        if updated.rowcount != 1: raise RuntimeError('Execution reservation unavailable')
        audit(c,'final_reply_prepared',{'deal_id':a['deal_id'],'approval_id':approval_id,'source':source,'decision':status,'recipient':final_payload['recipient']})
        audit(c,'wholesaler_send_started',{'deal_id':a['deal_id'],'approval_id':approval_id,'recipient':final_payload['recipient']})
    try:
        result=email_service.send_message(final_payload['recipient'],final_payload['subject'],final_payload['body'])
    except email_service.EmailNotSent:
        result='SEND_FAILED'
    except Exception:
        result='NEEDS_RECONCILIATION'
    with db() as c:
        c.execute('UPDATE executions SET status=?,executed_at=? WHERE approval_id=?',(result,stamp(now()),approval_id))
        audit(c,'email_execution',{'approval_id':approval_id,'status':result,'recipient':final_payload['recipient'],'decision':status})
    execution=result

    return {'status':status,'execution':execution}



