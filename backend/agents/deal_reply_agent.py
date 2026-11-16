"""Acquisitions-focused wholesaler replies based on the completed deal evaluation."""
import os
from backend import config
from backend.agents.guardrails import REAL_ESTATE_DRAFT_INSTRUCTIONS
from backend.agents.terms import expand_financial_terms

DEAL_REPLY_SYSTEM_PROMPT = """Act as the acquisitions decision-maker for a real estate investment company, communicating with the wholesaler on behalf of the buyer. Your task is to turn the completed evaluation into a practical, professional email, not to perform a new evaluation or independently approve a purchase.

Use evaluation.resolution_intent and evaluation.recommended_action as the intended response, and evaluation.classification, explanation, criteria results, financial metrics, risks, missing fields and due-diligence questions as the reasons and supporting context. The original wholesaler email and extracted property data provide the specific facts. Use the Python-calculated metrics as supplied; do not invent or recalculate figures. If information is uncertain, acknowledge it and ask for clarification. Do not treat the existence of a recommendation or an approved email as authorization to buy.

Write a substantive response with enough detail for the wholesaler to understand the buyer's position. Where supplied and relevant, identify the property and state the asking price, estimated after repair value and estimated repairs. Explain which specific aspects appear aligned with the buyer's criteria and which concerns, unknown costs or missing documents prevent a decision; do not simply say the deal is favorable. Explain the effect of missing costs on the completeness of the analysis without inventing profit or return. Ask specific questions drawn from the missing fields and due-diligence questions, using a readable list when useful. Mention a calculated reference price only when it is relevant to a negotiation, without exposing other confidential thresholds. Do not repeat information or ask for details already supplied except to request verification. For a decline, state the actual supplied concerns politely and do not request additional information as a condition of proceeding. Provide a practical next step and a courteous close.

For an initial draft, use the evaluation's intended action. For a follow-up, the explicit human approval or rejection decision takes precedence: a rejection is a polite decline, while an approval authorizes preparation of the response, not a purchase. Incorporate any safe reviewer comment without changing the decision. Express a negotiation as a non-binding question. Keep the message commercially useful and focused on what the wholesaler should do next.

""" + REAL_ESTATE_DRAFT_INSTRUCTIONS

AI_DISCLOSURE = ('IMPORTANT DISCLOSURE: This email was drafted with AI assistance and reflects a preliminary review of information that has not been independently verified. '
                 'Any figures, estimates and proposed terms are subject to confirmation and due diligence. This message is not an offer, acceptance, commitment to purchase, or guarantee of any outcome.')

def disclose_ai(body):
    return body.rstrip() + '\n\n' + AI_DISCLOSURE if AI_DISCLOSURE not in body else body

def polish_followup(body, comment, decision, context=None):
    """Prepare text for human review; the result is never authorized to send here."""
    if not config.AI_ENABLED:
        return body, 'template'
    import json
    from openai import OpenAI
    result = OpenAI(max_retries=0).responses.create(
        model=os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
        instructions=DEAL_REPLY_SYSTEM_PROMPT,
        input=json.dumps({'draft': body, 'approver_comment': comment, 'decision': decision, 'deal_context': context or {}}),
    )
    return disclose_ai(result.output_text), 'openai'

def resolution_intent(ev):
    """Record the intended response independently of approval or delivery status."""
    action = ev.get('recommended_action')
    if action in ('NEGOTIATE', 'REQUEST_INFORMATION', 'DECLINE', 'PROCEED_TO_DUE_DILIGENCE'):
        return action
    return {'INSUFFICIENT_DATA':'REQUEST_INFORMATION', 'NEGOTIATE':'NEGOTIATE',
            'REJECT':'DECLINE'}.get(ev.get('classification'), 'PROCEED_TO_DUE_DILIGENCE')


def template_content(p, ev):
    intent = resolution_intent(ev)
    missing = [str(field).replace('_', ' ') for field in ev.get('missing', [])]
    metrics = ev.get('metrics') or {}
    price = p.get('asking_price')
    mao = metrics.get('mao')
    figures = [('asking price', price), ('estimated after repair value', p.get('arv')), ('estimated repairs', p.get('repairs'))]
    known = [f'{name} of ${value:,.2f}' for name, value in figures if isinstance(value, (int, float)) and not isinstance(value, bool)]
    summary = ('Our preliminary review considered the ' + ', '.join(known) + '. The value and repair figures are unverified estimates.\n\n') if known else ''
    met = [item['name'] for item in ev.get('criteria_results', []) if item.get('status') == 'MET']
    if met:
        summary += 'The following aspects appear consistent with our criteria: ' + ', '.join(met) + '.\n\n'
    if intent == 'DECLINE':
        failed = [item['name'] for item in ev.get('criteria_results', []) if item.get('status') == 'NOT_MET']
        reason = (' In particular, our review identified concerns with: ' + ', '.join(failed) + '.') if failed else ''
        return summary + 'After reviewing the opportunity, the proposed terms do not fit our current acquisition criteria.' + reason + ' We appreciate you thinking of us and would be glad to review future opportunities.'

    if intent == 'REQUEST_INFORMATION':
        position = 'We would like to complete our initial review before deciding on next steps.'
    elif intent == 'NEGOTIATE':
        if price is not None and mao is not None and price > mao > 0:
            position = (f'Based on our preliminary review of the ${price:,.2f} asking price, '
                        f'would the seller consider a price closer to ${mao:,.2f}? This is a non-binding question, subject to due diligence.')
        else:
            position = ('Based on our preliminary review, the proposed terms need further discussion. '
                        'Would the seller consider flexibility on price?')
    else:
        position = 'The opportunity appears potentially aligned with our acquisition criteria, subject to further review.'

    questions = [expand_financial_terms(item) for item in missing]
    questions += [expand_financial_terms(str(item)) for item in (ev.get('due_diligence') or [])]
    if not questions:
        questions = ['Property photographs and condition details', 'Supporting documentation and proposed closing terms']
    requests = '\n'.join('- ' + item.rstrip('? .') for item in questions)
    if any(field in ev.get('missing', []) for field in ('closing_costs', 'assignment_fee', 'selling_costs')):
        position += ' We cannot complete our profit and return analysis until the relevant costs are available.'
    return summary + position + '\n\nTo help us assess the opportunity, could you please share or confirm:\n' + requests + '\n\nWe appreciate your help and look forward to your response.'


def _template_draft(p, ev, sender, subject):
    content = template_content(p, ev)
    location = p.get('address') or ', '.join(str(p[k]) for k in ('city','state') if p.get(k))
    opening = f'Thank you for sharing the opportunity in {location}.' if location else 'Thank you for sharing this opportunity.'
    return {'recipient':sender,'subject':'Re: '+subject.removeprefix('Re: '),'body':f"Hi {p.get('wholesaler_name') or 'there'},\n\n{opening}\n\n{content}\n\nBest regards,\n{os.getenv('BUYER_NAME','Example Buyer')}\n{os.getenv('BUYER_COMPANY','')}"}


def draft(p, ev, sender, subject, original_email=None):
    fallback = _template_draft(p, ev, sender, subject)
    if not config.AI_ENABLED:
        return fallback
    import json
    from openai import OpenAI
    context = json.dumps({'property':p,'evaluation':ev,'subject':subject,'original_wholesaler_email':original_email,'buyer_name':os.getenv('BUYER_NAME','Example Buyer'),'buyer_company':os.getenv('BUYER_COMPANY','')})
    response = OpenAI(max_retries=0).responses.create(
        model=os.getenv('OPENAI_MODEL','gpt-4.1-mini'),
        instructions=DEAL_REPLY_SYSTEM_PROMPT,
        input=context,
    )
    return {**fallback, 'body':disclose_ai(response.output_text)}
