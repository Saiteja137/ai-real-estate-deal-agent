"""Evaluate individual configured criteria without classifying or inventing values."""
import re
from decimal import Decimal

def _number(value):
    return Decimal(str(value)) if value is not None else None

def _type(value):
    normalized=re.sub(r'[^a-z0-9]+',' ',str(value).lower()).strip()
    if normalized in ('single family','single family home','single family house','single family residential','single family residence','single family detached','single family detached home','sfr'):
        return 'single family residential'
    return normalized

def criteria_checks(property_details, criteria, financial_metrics):
    p=property_details; c=criteria
    conflicted=bool(p.get('contradictions'))
    def check(name, requirement, inputs, predicate):
        known=not conflicted and all(value is not None for value in inputs)
        return {'name':name,'requirement':requirement,'status':('MET' if predicate(*inputs) else 'NOT_MET') if known else 'UNKNOWN'}
    price=_number(p.get('asking_price')); arv=_number(p.get('arv')); repairs=_number(p.get('repairs'))
    spread=_number(financial_metrics.get('gross_spread'))
    ratio_limit=_number(c['max_ratio'])
    ratio_inputs=[price,arv] if arv is not None and arv>0 else [price,None]
    property_type=p.get('property_type') or None
    if property_type and _type(property_type) in ('unknown','not provided','not specified','n a'):
        property_type=None
    return [
        check('Maximum purchase price',f"At most ${c['max_price']:,.0f}",[price],lambda value:value<=_number(c['max_price'])),
        check('Minimum after repair value',f"At least ${c['min_arv']:,.0f}",[arv],lambda value:value>=_number(c['min_arv'])),
        check('Maximum repair cost',f"At most ${c['max_repairs']:,.0f}",[repairs],lambda value:value<=_number(c['max_repairs'])),
        check('Asking price to after repair value',f"At most {ratio_limit*100:g}%",ratio_inputs,lambda price,arv:price/arv<=ratio_limit),
        check('Minimum gross spread',f"At least ${c['min_spread']:,.0f}",[spread],lambda value:value>=_number(c['min_spread'])),
        check('Property type',str(c['property_type']),[property_type],lambda value:_type(value)==_type(c['property_type'])),
        {'name':'Investment strategy','requirement':str(c['strategy']),'status':'INFORMATIONAL'},
    ]
