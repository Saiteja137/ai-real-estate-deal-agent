import os
from backend import config
from backend.agents.guardrails import REAL_ESTATE_DRAFT_INSTRUCTIONS
from backend.agents.terms import expand_financial_terms

AI_DISCLOSURE = 'Disclosure: This message was drafted with AI assistance. Our review is preliminary, based on unverified information, and is not a binding offer.'

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
        instructions=REAL_ESTATE_DRAFT_INSTRUCTIONS,
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
    missing_text = expand_financial_terms(', '.join(missing))
    metrics = ev.get('metrics') or {}
    figures = [("asking price",p.get('asking_price')), ("estimated after repair value",p.get('arv')), ("estimated repairs",p.get('repairs')), ("calculated gross spread before other costs",metrics.get('gross_spread'))]
    known = [f'{name}: ${value:,.2f}' for name,value in figures if isinstance(value,(int,float))]
    summary = ('Our preliminary review used the following information: ' + '; '.join(known) + '. ' if known else '')
    met = [item['name'] for item in ev.get('criteria_results',[]) if item.get('status') == 'MET']
    if met: summary += 'The following buying criteria appear to be met: ' + ', '.join(met) + '. '
    questions = ev.get('due_diligence') or []
    if questions: missing_text += ('; ' if missing_text else '') + '; '.join(questions)
    if intent == 'DECLINE':
        return summary + 'The property does not appear to fit our criteria at the proposed terms. Please keep us in mind for future opportunities.'
    if intent == 'REQUEST_INFORMATION':
        if missing_text:
            return summary + f'Before completing our preliminary evaluation, could you please provide: {missing_text}?' 
        return summary + 'Could you please share supporting property details and documentation so we can complete our preliminary evaluation?'
    if intent == 'NEGOTIATE':
        mao = (ev.get('metrics') or {}).get('mao')
        price = p.get('asking_price')
        context = f'The asking price of ${price:,.2f} exceeds our preliminary calculated maximum allowable offer of ${mao:,.2f}. ' if price is not None and mao is not None and price > mao > 0 else 'Based on our preliminary evaluation, '
        question = f'Would the seller consider a price closer to ${mao:,.2f}?' if mao is not None and mao > 0 else 'would the seller consider flexibility on price?'
        extra = f' Could you also provide: {missing_text}?' if missing_text else ''
        return summary + context + question + extra
    extra = f' Please also provide: {missing_text}.' if missing_text else ''
    return summary + 'The property appears potentially aligned with our criteria. Please share photographs, condition details, supporting documentation, availability and closing terms.' + extra


def _template_draft(p, ev, sender, subject):
    content = template_content(p, ev)
    return {'recipient':sender,'subject':'Re: '+subject.removeprefix('Re: '),'body':f"Hi {p.get('wholesaler_name') or 'there'},\n\nThank you for sharing this opportunity.\n\n{content}\n\nBest regards,\n{os.getenv('BUYER_NAME','Example Buyer')}\n{os.getenv('BUYER_COMPANY','')}"}


def draft(p, ev, sender, subject, original_email=None):
    fallback = _template_draft(p, ev, sender, subject)
    if not config.AI_ENABLED:
        return fallback
    import json
    from openai import OpenAI
    context = json.dumps({'property':p,'evaluation':ev,'subject':subject,'original_wholesaler_email':original_email,'buyer_name':os.getenv('BUYER_NAME','Example Buyer'),'buyer_company':os.getenv('BUYER_COMPANY','')})
    response = OpenAI(max_retries=0).responses.create(
        model=os.getenv('OPENAI_MODEL','gpt-4.1-mini'),
        instructions=REAL_ESTATE_DRAFT_INSTRUCTIONS,
        input=context,
    )
    return {**fallback, 'body':disclose_ai(response.output_text)}
