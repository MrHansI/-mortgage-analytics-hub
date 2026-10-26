"""Pure calculations for official volume series (all amounts in millions of rubles)."""
from datetime import date
from statistics import mean


def summarize_volume(points: list[tuple[date,float]]) -> dict:
    points = sorted(points)
    if not points:
        return {'count':0}
    last_date, last = points[-1]
    by_date = dict(points)
    prev = date(last_date.year - (last_date.month == 1), (last_date.month - 2) % 12 + 1, 1)
    last_year = date(last_date.year-1,last_date.month,1)
    prior = by_date.get(prev); yoy = by_date.get(last_year)
    year = last_date.year
    annual = {str(y):round(sum(v for d,v in points if d.year == y),2) for y in sorted({d.year for d,_ in points})}
    return {'count':len(points),'period':last_date.isoformat(), 'value':last,
            'mom_pct':round(100*(last/prior-1),2) if prior else None,
            'yoy_pct':round(100*(last/yoy-1),2) if yoy else None,
            'avg_3m':round(mean(v for _,v in points[-3:]),2),
            'avg_12m':round(mean(v for _,v in points[-12:]),2),
            'sum_ytd':round(sum(v for d,v in points if d.year==year),2),
            'annual_totals':annual,'unit':'млн руб.'}
