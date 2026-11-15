"""Render a buyer's approval request from the saved deal and frozen reply."""

def _amount(value, suffix=''):
    if value is None: return 'Not available'
    return f'{value:,.2f}{suffix}' if suffix else f'${value:,.2f}'

def _items(values, empty='None recorded'):
    return '\n'.join(f'- {value}' for value in values) if values else f'- {empty}'

def build_notification(approval_id, deadline, property_details, evaluation, reply, *, is_second_review=False, second_review_enabled=False):
    p=property_details; ev=evaluation; metrics=ev.get('metrics') or {}
    location=p.get('address') or ', '.join(str(v) for v in (p.get('city'),p.get('state'),p.get('zip_code')) if v) or 'Not provided'
    buyer_explanation=ev.get('explanation') or 'No written explanation was recorded for this evaluation. Review the criteria and financial inputs below.'
    next_action=ev.get('suggested_next_action') or 'Review the figures, risks and missing information before deciding.'
    source={'openai':'OpenAI-assisted evaluation','python_rules':'Python rule-based evaluation'}.get(ev.get('evaluation_source'),'Not recorded for this deal')
    text=f'''APPROVAL REQUIRED — PRELIMINARY PROPERTY REVIEW

Approval ID: {approval_id}
Deadline: {deadline}

PROPERTY AND WHOLESALER
Location: {location}
Property type: {p.get('property_type') or 'Not provided'}
Wholesaler: {p.get('wholesaler_name') or 'Name not provided'}
Proposed recipient: {reply['recipient']}

DEAL RECOMMENDATION
Classification: {ev.get('classification') or 'Not available'}
Evaluation source: {source}
Initial draft source: { {'openai':'OpenAI','template':'Template'}.get(ev.get('initial_draft_source'),'Not recorded for this deal') }
Reasoning: {buyer_explanation}
Suggested next action: {next_action}

FINANCIAL SUMMARY
Asking price (wholesaler): {_amount(p.get('asking_price'))}
ARV (wholesaler estimate): {_amount(p.get('arv'))}
Repairs (wholesaler estimate): {_amount(p.get('repairs'))}
Maximum allowable offer (calculated): {_amount(metrics.get('mao'))}
Gross spread before other costs (calculated): {_amount(metrics.get('gross_spread'))}
All-in cost (calculated): {_amount(metrics.get('all_in_cost'))}
Estimated net profit (calculated): {_amount(metrics.get('estimated_profit'))}
Estimated ROI (calculated): {_amount(metrics.get('roi_percent'), '%')}

CRITERIA PASSED
{_items(ev.get('passed'))}

CRITERIA FAILED
{_items(ev.get('failed'))}

STRENGTHS
{_items(ev.get('strengths'))}

RISKS AND ITEMS TO VERIFY
{_items(ev.get('risks'))}

MISSING INFORMATION
{_items([str(x).replace('_',' ') for x in ev.get('missing',[])], 'None recorded')}

ASSUMPTIONS
{_items(ev.get('assumptions'))}

The evaluation is preliminary and based on unverified wholesaler information. Unknown costs are not represented as zero. Please review the proposed reply and recipient before deciding.

PROPOSED RESPONSE
To: {reply['recipient']}
Subject: {reply['subject']}

{reply['body']}

APPROVAL INSTRUCTIONS
Reply with both lines:
Approval ID: {approval_id}
Decision: APPROVED
'''
    if is_second_review:
        text += '\nOr use Decision: REJECTED to cancel sending. Do not include a comment in this second decision.\n\nSECOND REVIEW: APPROVED sends the exact message shown. REJECTED cancels sending.\n'
    else:
        text += '\nOr use Decision: REJECTED to propose a polite decline. Optionally add one line: Comments: your note. The reply may be rewritten using AI to include your note.\n'
        if second_review_enabled: text += '\nIf your decision requires a revised reply, it will be sent back for a second approval.\n'
    return text+'\nNo response means no email will be sent. Keep the approval ID confidential.'
