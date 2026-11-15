import os
from backend import config
from backend.agents.guardrails import REAL_ESTATE_DRAFT_INSTRUCTIONS, validate_reply
from backend.agents.terms import expand_financial_terms

def polish_followup(body, comment, decision):
    """Prepare text for human review; the result is never authorized to send here."""
    if not config.AI_ENABLED:
        return body, 'template'
    import json
    from openai import OpenAI
    result = OpenAI().responses.create(
        model=os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
        instructions=REAL_ESTATE_DRAFT_INSTRUCTIONS,
        input=json.dumps({'draft': body, 'approver_comment': comment, 'decision': decision}),
    )
    return expand_financial_terms(validate_reply(result.output_text, {'draft':body,'comment':comment})), 'openai'

def draft(p, ev, sender, subject):
    category = ev['classification']
    if category == 'INSUFFICIENT_DATA': content = 'Before completing our initial evaluation, could you please provide: ' + expand_financial_terms(', '.join(ev['missing']).replace('_',' ')) + '?'
    elif category == 'NEGOTIATE': content = f"The asking price appears higher than our preliminary buying range. Would the seller consider an offer closer to ${ev['metrics']['mao']:,.0f}?" if ev['metrics']['mao'] is not None and ev['metrics']['mao'] > 0 else 'Could you share whether the seller has flexibility on price?'
    elif category == 'REJECT': content = 'The property does not appear to fit our criteria at the proposed terms. Please keep us in mind for future opportunities.'
    else: content = 'The property appears potentially aligned with our criteria. Please share photographs, condition details, supporting documentation, availability and closing terms.'
    fallback = {'recipient':sender,'subject':'Re: '+subject.removeprefix('Re: '),'body':f"Hi {p.get('wholesaler_name') or 'there'},\n\nThank you for sharing this opportunity.\n\n{content}\n\nBest regards,\n{os.getenv('BUYER_NAME','Example Buyer')}\n{os.getenv('BUYER_COMPANY','')}"}
    if not config.AI_ENABLED:
        return fallback
    import json
    from openai import OpenAI
    client = OpenAI()
    tool = {'type':'function','name':'get_missing_information','description':'Return the deterministic missing fields for this property.', 'parameters':{'type':'object','properties':{},'required':[],'additionalProperties':False},'strict':True}
    context = json.dumps({'property':p,'evaluation':ev,'subject':subject,'buyer_name':os.getenv('BUYER_NAME','Example Buyer'),'buyer_company':os.getenv('BUYER_COMPANY','')})
    instructions = REAL_ESTATE_DRAFT_INSTRUCTIONS
    response = client.responses.create(model=os.getenv('OPENAI_MODEL','gpt-4.1-mini'),instructions=instructions,input=context,tools=[tool],tool_choice='required')
    calls = [x for x in response.output if x.type == 'function_call' and x.name == 'get_missing_information']
    if len(calls) != 1 or json.loads(calls[0].arguments) != {}:
        raise ValueError('Invalid drafting tool call')
    final = client.responses.create(model=os.getenv('OPENAI_MODEL','gpt-4.1-mini'),instructions=instructions,previous_response_id=response.id,input=[{'type':'function_call_output','call_id':calls[0].call_id,'output':json.dumps(ev['missing'])}])
    return {**fallback, 'body':expand_financial_terms(validate_reply(final.output_text, context))}


