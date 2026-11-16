"""Render the buyer's approval request from the saved deal and frozen reply."""
from html import escape
import re
from urllib.parse import quote, urlencode
from backend.agents.terms import expand_financial_terms

DISCLAIMER = ('DISCLAIMER: This automated review may use AI (Artificial Intelligence), is preliminary, and is based on unverified '
              'wholesaler information. It is not an appraisal or legal, tax, or financial advice '
              'and does not guarantee investment results. Independently verify the details '
              'and complete your due diligence before deciding.')

def _amount(value, suffix=''):
    if value is None: return 'Not available'
    return f'{value:,.2f}{suffix}' if suffix else f'${value:,.2f}'

def _items(values, empty='None recorded'):
    return '\n'.join(f'- {expand_financial_terms(value)}' for value in values) if values else f'- {empty}'

def _section(title, values):
    return f'\n\n{title}\n{_items(values)}' if values else ''

def build_notification(approval_id, deadline, property_details, evaluation, reply, *, is_second_review=False, second_review_enabled=False, timeout_minutes=20):
    p=property_details; ev=evaluation; metrics=ev.get('metrics') or {}
    location=p.get('address') or ', '.join(str(v) for v in (p.get('city'),p.get('state'),p.get('zip_code')) if v) or 'Not provided'
    buyer_explanation=expand_financial_terms(ev.get('explanation') or 'No written explanation was recorded for this evaluation. Review the criteria and financial inputs below.')
    next_action=expand_financial_terms(ev.get('suggested_next_action') or 'Review the figures, risks and missing information before deciding.')
    actions={'PROCEED_TO_DUE_DILIGENCE':'Proceed to due diligence','NEGOTIATE':'Negotiate','DECLINE':'Decline','REQUEST_INFORMATION':'Request information'}
    action_label=actions.get(ev.get('recommended_action'))
    if action_label:
        next_action=f'{action_label}: {next_action}'
    source={'openai':'AI Deal Evaluation Agent (Artificial Intelligence)','python_rules':'Rule-based Deal Evaluation'}.get(ev.get('evaluation_source'),'Not recorded for this deal')
    draft_source={'openai':'AI Reply Drafting Agent (Artificial Intelligence)','template':'Standard template','template_after_ai_failure':'Standard template (AI draft could not be validated)'}.get(ev.get('initial_draft_source'),'Not recorded for this deal')
    criteria_results=ev.get('criteria_results') or []
    status_labels={'MET':'Met','NOT_MET':'Not met','UNKNOWN':'Unknown','INFORMATIONAL':'Configured'}
    criteria_lines=[f"{item['name']}: {status_labels.get(item['status'],'Unknown')} — {item['requirement']}" for item in criteria_results]
    criteria_section=_section('BUYING CRITERIA',criteria_lines)
    if not criteria_results:
        criteria_section=_section('CRITERIA MET',ev.get('passed'))+_section('CRITERIA NOT MET',ev.get('failed'))
    evidence = (criteria_section
                + _section('STRENGTHS', ev.get('strengths')) + _section('RISKS AND ITEMS TO VERIFY', ev.get('risks'))
                + _section('MISSING INFORMATION', [str(x).replace('_',' ') for x in ev.get('missing',[])])
                + _section('ASSUMPTIONS', ev.get('assumptions')))
    buyer_detail = ''
    if ev.get('financial_assessment'):
        buyer_detail += '\n\nFINANCIAL VIEW\n' + expand_financial_terms(ev['financial_assessment'])
    if ev.get('criteria_assessment'):
        buyer_detail += '\n\nFIT WITH BUYING CRITERIA\n' + expand_financial_terms(ev['criteria_assessment'])
    buyer_detail += _section('DUE DILIGENCE QUESTIONS', ev.get('due_diligence'))
    text=f'''{DISCLAIMER}

PROPERTY DETAILS
Location: {location}
Property type: {p.get('property_type') or 'Not provided'}
Bedrooms: {p.get('bedrooms') if p.get('bedrooms') is not None else 'Not provided'}
Bathrooms: {p.get('bathrooms') if p.get('bathrooms') is not None else 'Not provided'}
Square footage: {p.get('square_feet') if p.get('square_feet') is not None else 'Not provided'}
Occupancy: {p.get('occupancy') or 'Not provided'}
Closing timeline: {p.get('closing_timeline') or 'Not provided'}
Wholesaler: {p.get('wholesaler_name') or 'Name not provided'}
Proposed recipient: {reply['recipient']}

FINANCIAL DETAILS
Asking price (wholesaler): {_amount(p.get('asking_price'))}
ARV (After Repair Value), wholesaler estimate: {_amount(p.get('arv'))}
Repairs (wholesaler estimate): {_amount(p.get('repairs'))}
MAO (Maximum Allowable Offer), calculated: {_amount(metrics.get('mao'))}
Gross spread before other costs (calculated): {_amount(metrics.get('gross_spread'))}
All-in cost (calculated): {_amount(metrics.get('all_in_cost'))}
Estimated net profit (calculated): {_amount(metrics.get('estimated_profit'))}
Estimated ROI (Return on Investment), calculated: {_amount(metrics.get('roi_percent'), '%')}

AI RECOMMENDATION
Classification: {ev.get('classification') or 'Not available'}
Resolution intent: {actions.get(ev.get('resolution_intent'), 'Not recorded for this deal')}
Evaluation source: {source}
Reasoning: {buyer_explanation}
Suggested next action: {next_action}
{buyer_detail}
{evidence}

PROPOSED RESPONSE TO THE WHOLESALER
Initial draft source: {draft_source}
To: {reply['recipient']}
Subject: {reply['subject']}

{reply['body']}

APPROVAL ID AND DEADLINE
Approval ID: {approval_id}
Time to reply: {timeout_minutes} minutes
Reply by (Coordinated Universal Time): {deadline}

APPROVAL REQUEST
Please review the analysis, recipient and complete proposed response. Reply with both lines:
Approval ID: {approval_id}
Decision: APPROVED
'''
    if is_second_review:
        text += '\nOr use Decision: REJECTED to cancel sending. Do not include a comment in this second decision.\n\nSECOND REVIEW: APPROVED sends the exact message shown. REJECTED cancels sending.\n'
    else:
        text += '\nOr use Decision: REJECTED to propose a polite decline. Add Comments: followed by your comments, if any. The reply may be rewritten using AI to include your note.\n'
        if second_review_enabled: text += '\nIf your decision requires a revised reply, it will be sent back for a second approval.\n'
        else: text += '\nAn approved reply or polite decline may be sent to the wholesaler after your decision; no second approval is requested.\n'
    return text+'\nNo response means no email will be sent. Keep the approval ID confidential.'

def notification_html(text, *, approval_id=None, reply_to=None, is_second_review=False):
    """Render escaped email content with bold headings and a clear reply card."""
    escaped=escape(text)
    escaped_disclaimer=escape(DISCLAIMER)
    if escaped.startswith(escaped_disclaimer):
        escaped='<strong>'+escaped_disclaimer+'</strong>'+escaped[len(escaped_disclaimer):]
    headings=('PROPERTY DETAILS','FINANCIAL DETAILS','AI RECOMMENDATION','FINANCIAL VIEW','BUYING CRITERIA',
              'FIT WITH BUYING CRITERIA','DUE DILIGENCE QUESTIONS','CRITERIA MET',
              'CRITERIA NOT MET','STRENGTHS','RISKS AND ITEMS TO VERIFY','MISSING INFORMATION',
              'ASSUMPTIONS','PROPOSED RESPONSE TO THE WHOLESALER','APPROVAL ID AND DEADLINE','APPROVAL REQUEST')
    escaped=re.sub(r'^(?:'+ '|'.join(re.escape(h) for h in headings) +r')$',lambda m:'<strong>'+m.group(0)+'</strong>',escaped,flags=re.MULTILINE)
    text_style='white-space:pre-wrap;font-family:Arial,sans-serif;line-height:1.6;color:#243b33'
    start='<strong>PROPOSED RESPONSE TO THE WHOLESALER</strong>'
    end='<strong>APPROVAL ID AND DEADLINE</strong>'
    if start in escaped and end in escaped:
        before, remainder=escaped.split(start,1)
        card, after=remainder.split(end,1)
        content=('<div style="'+text_style+'">'+before+'</div>'
                 '<div style="'+text_style+';background:#f2f7f3;border:1px solid #d5e5d9;border-radius:8px;padding:16px;margin:16px 0">'
                 +start+card+'</div><div style="'+text_style+'">'+end+after+'</div>')
    else:
        content='<div style="'+text_style+'">'+escaped+'</div>'
    actions=''
    if approval_id is not None or reply_to is not None:
        if not approval_id or not re.fullmatch(r'RE-[A-F0-9]+',approval_id):
            raise ValueError('Invalid approval ID for email actions')
        if not reply_to or not re.fullmatch(r'[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+',reply_to):
            raise ValueError('Invalid approval reply address for email actions')
        def compose_link(decision):
            body=f'Approval ID: {approval_id}\r\nDecision: {decision}'
            if not is_second_review: body+='\r\nComments: '
            query=urlencode({'subject':f'Approval response - ID: {approval_id}','body':body},quote_via=quote)
            return escape(f'mailto:{quote(reply_to,safe="@.+-_")}?{query}',quote=True)
        actions=('''<div style="margin-top:24px;padding:20px;background:#f4f8fc;border:1px solid #dce8f4;border-radius:10px;font-family:Arial,sans-serif">
<strong style="color:#244564;font-size:16px">Your decision</strong>
<p style="color:#536d82;font-size:13px;line-height:1.5">Choose an option to open a prefilled email reply. Add comments if needed, then press Send in your email app.</p>
<table role="presentation" cellspacing="0" cellpadding="0"><tr>
<td style="padding:0 10px 8px 0"><a href="'''+compose_link('APPROVED')+'''" style="display:inline-block;padding:12px 22px;background:#198754;color:#fff;text-decoration:none;border-radius:6px;font-weight:bold">Approve</a></td>
<td style="padding:0 0 8px 0"><a href="'''+compose_link('REJECTED')+'''" style="display:inline-block;padding:12px 22px;background:#c43d4b;color:#fff;text-decoration:none;border-radius:6px;font-weight:bold">Reject</a></td>
</tr></table><p style="color:#64788a;font-size:12px;margin:4px 0 0">A decision is recorded only after your email reply is sent and validated.</p></div>''')
    return '<html><body style="margin:0;padding:24px;background:#f6f8f6"><div style="max-width:680px;margin:auto;background:#fff;padding:24px;border-radius:10px">'+content+actions+'</div></body></html>'
