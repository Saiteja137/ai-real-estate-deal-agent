import re
from backend.tools.roi_calculator import metrics

def evaluate(p, c):
    m = metrics(p,c)
    missing = [x for x in ('address','property_type','asking_price','arv','repairs','closing_costs','assignment_fee','selling_costs') if p.get(x) is None]
    tests = {'purchase price': p.get('asking_price') is not None and p['asking_price'] <= c['max_price'], 'minimum ARV': p.get('arv') is not None and p['arv'] >= c['min_arv'], 'repair limit': p.get('repairs') is not None and p['repairs'] <= c['max_repairs'], 'offer to ARV': p.get('asking_price') is not None and p.get('arv') not in (None,0) and p['asking_price']/p['arv'] <= c['max_ratio'], 'gross spread': m['gross_spread'] is not None and m['gross_spread'] >= c['min_spread']}
    known_type = p.get('property_type')
    if known_type:
        normalize = lambda s: re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()
        expected = normalize(c['property_type'])
        actual = normalize(known_type)
        tests['property type'] = actual == expected or (expected == 'single family residential' and actual in ('single family', 'single family home', 'single family house'))
    failed = [k for k,v in tests.items() if not v]
    if any(p.get(k) is None for k in ('asking_price','arv','repairs')) or p.get('arv') == 0 or p.get('contradictions'): classification = 'INSUFFICIENT_DATA'
    elif (known_type and not tests['property type']) or p['repairs'] > c['max_repairs'] or p['arv'] < c['min_arv']: classification = 'REJECT'
    elif failed: classification = 'NEGOTIATE'
    elif not known_type or not p.get('address'): classification = 'POTENTIAL_MATCH'
    else: classification = 'STRONG_MATCH'
    if classification == 'INSUFFICIENT_DATA':
        failed = []
    return {'classification':classification,'metrics':m,'passed':[k for k,v in tests.items() if v], 'failed':failed,'missing':missing,'risks':['Preliminary only; verify wholesaler estimates, title, condition and comparable sales.'] + (p.get('contradictions') or []), 'assumptions':['Unknown costs are not treated as zero; net profit and ROI require documented closing, assignment and selling costs.'], 'criteria':c}


