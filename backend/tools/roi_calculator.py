from decimal import Decimal

def metrics(p, criteria):
    def d(name):
        v = p.get(name)
        return Decimal(str(v)) if v is not None else None
    price, arv, repairs = d('asking_price'), d('arv'), d('repairs')
    mao = arv * Decimal(str(criteria['max_ratio'])) - repairs if arv is not None and repairs is not None else None
    spread = arv - price - repairs if None not in (price, arv, repairs) else None
    costs = [d('closing_costs'), d('assignment_fee')]
    all_in = price + repairs + sum(costs) if price is not None and repairs is not None and all(x is not None for x in costs) else None
    selling = d('selling_costs')
    profit = arv - all_in - selling if arv is not None and all_in is not None and selling is not None else None
    roi = profit / all_in * 100 if profit is not None and all_in else None
    return {k: float(v.quantize(Decimal('.01'))) if v is not None else None for k,v in {'mao':mao,'gross_spread':spread,'all_in_cost':all_in,'estimated_profit':profit,'roi_percent':roi}.items()}


