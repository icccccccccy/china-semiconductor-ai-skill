#!/usr/bin/env python3
"""Offline, standard-library revenue/PS engine. All amounts CNY yuan.
Usage: python3 scripts/earnings_valuation.py assets/valuation/688361/model.json --output-dir output/688361
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


class ValidationError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ValidationError(message)


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def positive(x, label):
    require(finite(x) and x > 0, label + ' must be positive and finite')
    return x


def quantile(values, p):
    s = sorted(values)
    require(bool(s), 'empty quantile')
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def ttm(snapshot):
    r = positive(snapshot['revenue_ytd'], 'revenue_ytd')
    if snapshot['months'] == 12:
        return r
    require(snapshot['months'] in (3, 6, 9), 'unsupported YTD duration')
    return positive(snapshot['prior_fy_revenue'] + r - snapshot['prior_year_same_ytd'], 'TTM')


def normalize(periods):
    """Cumulative flows differenced; stock variables remain end-of-period stocks."""
    rows = []
    by = {(p['year'], p['months']): p for p in periods}
    require(len(by) == len(periods), 'duplicate financial period')
    for (y, m), p in sorted(by.items()):
        prev = by.get((y, m-3)) if m > 3 else None
        d = {'year': y, 'quarter': m//3, 'months': m, 'source': p['source']}
        for key in ('revenue', 'cost', 'rd', 'cfo', 'sales_cash_receipts'):
            v = p.get(key)
            d[key+'_ytd'] = v
            # A standalone annual without its Q3 is not a Q4 observation.
            d[key+'_quarter'] = (v if m == 3 else v-prev[key]
                                    if prev and v is not None and prev.get(key) is not None else None)
        for suffix in ('ytd', 'quarter'):
            rev, cost, rd = (d.get(k+'_'+suffix) for k in ('revenue','cost','rd'))
            d['gross_margin_'+suffix] = 1-cost/rev if rev and cost is not None else None
            d['rd_ratio_'+suffix] = rd/rev if rev and rd is not None else None
        for key in ('contract_liabilities', 'inventory_net', 'accounts_receivable_net'):
            d[key] = p.get(key)
        rows.append(d)
    return rows


def forecast(s, m):
    y = m['forecast_year'] - 1
    by = {(p['year'],p['months']):p for p in m['periods']}
    actual = [by[y,3]['revenue'], by[y,6]['revenue']-by[y,3]['revenue'],
              by[y,9]['revenue']-by[y,6]['revenue']]
    prior_q4 = m['quarter_revenues'][str(y-1)][3]
    q4 = positive(prior_q4*(1+s['q4_current_year_yoy']), 'forecast Q4')
    current = actual+[q4]
    if s['mode'] == 'quarter_growth':
        gs = s['next_year_quarter_yoy']
        require(len(gs)==4 and all(finite(g) and g > -1 for g in gs), 'four valid quarter growth assumptions required')
        next_q = [r*(1+g) for r,g in zip(current,gs)]
    elif s['mode'] == 'acceptance_cohorts':
        # Each disjoint cohort stores ex-tax sales value, NOT inventory cost or advances.
        next_q = [0.0]*4
        seen = set()
        for c in s['cohorts']:
            require(c['id'] not in seen, 'overlapping cohort id')
            seen.add(c['id'])
            require(c['basis']=='sales_ex_tax', 'cohort requires ex-tax sales value')
            require(c['source'] in m['source_ledger'], 'cohort source missing')
            value = positive(c['sales_value'], 'cohort sales value')
            weights = c['quarter_acceptance_weights']
            require(len(weights)==4 and all(finite(w) and 0<=w<=1 for w in weights), 'invalid cohort conversion')
            require(sum(weights)<=1+1e-10, 'cohort conversion exceeds 100%')
            for i,w in enumerate(weights):
                next_q[i] += value*w
        require(len(s['service_revenue_quarters'])==4, 'four disjoint service quarters required')
        for i,r in enumerate(s['service_revenue_quarters']):
            require(finite(r) and r>=0, 'negative service revenue')
            next_q[i] += r
    else:
        raise ValidationError('unknown forecasting mode')
    revenue = positive(sum(next_q), 'next year revenue')
    gm = s['next_year_gross_margin']
    require(finite(gm) and 0<=gm<1, 'invalid gross margin')
    # Separately labeled expense sensitivity; no invented net-profit forecast.
    prior_rd_q4 = by[y-1,12]['rd'] - by[y-1,9]['rd']
    rd_current = by[y,9]['rd'] + prior_rd_q4*(1+s['current_year_q4_rd_yoy'])
    rd_next = positive(rd_current*(1+s['next_year_rd_growth']), 'R&D sensitivity')
    market_cap = m['market']['close_unadjusted']*m['market']['total_shares']
    fy_current = sum(current)
    return {'name':s['name'], 'mode':s['mode'], 'evidence_class':'analyst_assumption',
            'current_year_quarters':current,'current_year_revenue':fy_current,
            'next_year_quarters':next_q,'next_year_revenue':revenue,
            'next_year_revenue_growth':revenue/fy_current-1,'forward_ps':market_cap/revenue,
            'next_year_gross_margin_assumption':gm,'next_year_gross_profit_sensitivity':revenue*gm,
            'next_year_rd_sensitivity':rd_next,'next_year_rd_ratio_sensitivity':rd_next/revenue,
            'rationale':s['rationale'],'falsifiers':s['falsifiers']}


def bridge(r0, ps0, r1, ps1):
    for v in (r0,ps0,r1,ps1): positive(v, 'bridge input')
    # Revenue-first exact bridge. Interaction assigned to the multiple term.
    v0, v1 = r0*ps0, r1*ps1
    growth, rerating = (r1-r0)*ps0, r1*(ps1-ps0)
    require(math.isclose(v1-v0,growth+rerating,abs_tol=.01), 'bridge mismatch')
    return {'start_value':v0,'end_value':v1,'growth_at_constant_ps':growth,
            'multiple_change_including_interaction':rerating,'return':v1/v0-1}


def historical_band(m, prices):
    h=m['history']; series=[]; excluded=[]; forward=[]
    snapshots=sorted(m['historical_revenue_snapshots'],key=lambda s:s['published'])
    # Window is explicitly partial. Date-only publication usable strictly next calendar day.
    share_events=sorted(m['shares_history'],key=lambda s:s['effective'])
    for p in prices:
        date=p['date']
        if not h['start']<=date<=h['end']: continue
        ss=[s for s in snapshots if s['published']<date]
        ev=[s for s in share_events if s['effective']<=date]
        if not ss or not ev:
            excluded.append({'date':date,'reason':'missing PIT financial/share evidence'});continue
        e=ev[-1]
        if e['public_from']>date:
            excluded.append({'date':date,'reason':'share registration/disclosure ambiguity'});continue
        revenue=ttm(ss[-1]); cap=float(p['close_unadjusted'])*e['total_shares']
        series.append({'date':date,'close_unadjusted':float(p['close_unadjusted']),
                       'total_shares':e['total_shares'],'market_cap':cap,'revenue_ttm':revenue,
                       'ps_ttm':cap/revenue,'revenue_source':ss[-1]['source'],
                       'revenue_published':ss[-1]['published'],'share_source':e['source']})
        # Only frozen historical next-calendar-year forecasts in same calendar quarter.
        # Do not substitute realized future revenue or reconstruct old forecasts.
        fs=[s for s in m.get('historical_forward_snapshots',[])
            if s['created_on']<date and s['target_year']==int(date[:4])+1
            and (int(date[5:7])-1)//3 == (int(m['analysis_date'][5:7])-1)//3]
        if fs:
            f=max(fs,key=lambda s:s['created_on'])
            forward.append({'date':date,'ps_forward':cap/positive(f['revenue'],'historical forecast'),
                            'forecast_id':f['id']})
    vals=[x['ps_ttm'] for x in series]
    band={f'p{int(q*100):02d}':quantile(vals,q) for q in (.1,.25,.5,.75,.9)} if vals else None
    fv=[x['ps_forward'] for x in forward]
    return {'definition':'equity market cap / consolidated revenue TTM; no EV or float cap',
            'scope_note':h['scope_note'],'requested_start':h['start'],'requested_end':h['end'],
            'actual_start':series[0]['date'] if series else None,'actual_end':series[-1]['date'] if series else None,
            'n':len(series),'quantiles':band,'excluded':excluded,'series':series,
            'historical_forward_n':len(fv),'historical_forward_quantiles':
                {f'p{int(q*100):02d}':quantile(fv,q) for q in (.1,.25,.5,.75,.9)} if fv else None,
            'forward_comparability':'missing archived forecasts' if not fv else 'next calendar FY; same calendar quarter; inspect company stage and vintage coverage'}


def validate(m):
    require(m['schema_version']==1,'unsupported schema')
    require(m['currency']=='CNY' and m['amount_unit']=='yuan' and m['scope']=='consolidated','units/currency/scope mismatch')
    require(m['forecast_year']==int(m['analysis_date'][:4])+1, 'target must be next calendar year')
    ledger=m['source_ledger']
    def source(s): require(s in ledger and bool(ledger[s].get('url')), 'unknown source '+str(s))
    for p in m['periods']:
        source(p['source']);require(p['months'] in (3,6,9,12),'invalid duration')
        require(p['status']=='reported','historical facts must be reported')
        require(ledger[p['source']]['published']<=m['analysis_date'],'future filing')
        positive(p['revenue'],'reported revenue')
        for key,v in p.items():
            if isinstance(v,(int,float)): require(finite(v), 'non-finite '+key)
    normalize(m['periods'])
    for s in m['historical_revenue_snapshots']:
        source(s['source']); require(s['published']==ledger[s['source']]['published'],'publication mismatch')
        require(s['published']<=m['analysis_date'],'future historical snapshot');ttm(s)
    for e in m['shares_history']:
        source(e['source']);positive(e['total_shares'],'total shares')
    for year,qs in m['quarter_revenues'].items():
        require(len(qs)==4 and all(finite(q) and q>=0 for q in qs),'quarter table invalid')
        fy=next((p for p in m['periods'] if p['year']==int(year) and p['months']==12),None)
        require(fy and abs(sum(qs)-fy['revenue'])<.05,'quarters do not tie to FY')
    mk=m['market'];source(mk['price_source']);source(mk['shares_source'])
    positive(mk['close_unadjusted'],'close');positive(mk['total_shares'],'shares')
    require(mk['date']==m['analysis_date'],'stale market date: explicitly refresh model date')
    ev=[e for e in m['shares_history'] if e['effective']<=mk['date']]
    require(ev and ev[-1]['total_shares']==mk['total_shares'],'market share mismatch')
    calc=mk['close_unadjusted']*mk['total_shares']
    require(abs(calc-mk['reported_market_cap_yuan'])/calc<=mk['market_cap_relative_tolerance'],'market cap does not reconcile')
    require(m['history']['price_basis']=='unadjusted','adjusted prices prohibited for market cap')
    require(m['history']['end']<=m['analysis_date'],'future band cutoff')
    names=[s['name'] for s in m['scenarios']]
    require(len(set(names))==3 and set(names)=={'bear','base','bull'}, 'three unique bear/base/bull scenarios required')
    for s in m['scenarios']:
        require(s['evidence_class']=='analyst_assumption','forecast must label assumptions')
        require(bool(s.get('rationale')) and bool(s.get('falsifiers')),'scenario rationale/falsifiers required')
    for f in m.get('historical_forward_snapshots',[]):
        source(f['source']);positive(f['revenue'],'archived forecast')
        require(f['created_on']>=ledger[f['source']]['published'],'forecast predates supporting evidence')
    return True


def run(m, prices):
    validate(m)
    ds=[p['date'] for p in prices];require(ds==sorted(set(ds)),'duplicate/unsorted price dates')
    for p in prices: positive(float(p['close_unadjusted']),'historical close')
    current=next((p for p in prices if p['date']==m['market']['date']),None)
    require(current and math.isclose(float(current['close_unadjusted']),m['market']['close_unadjusted'],abs_tol=.005),'latest price does not match snapshot')
    case_inputs={s['name']:s for s in m['scenarios']}
    cases=[forecast(case_inputs[n],m) for n in ('bear','base','bull')]
    require(cases[0]['next_year_revenue']<=cases[1]['next_year_revenue']<=cases[2]['next_year_revenue'],'scenario order inverted')
    cap=m['market']['close_unadjusted']*m['market']['total_shares']
    latest=max(m['historical_revenue_snapshots'],key=lambda x:x['published'])
    curttm=ttm(latest);hist=historical_band(m,prices)
    # Historical percentile is TTM vs TTM only; never forward vs historical TTM.
    vals=[r['ps_ttm'] for r in hist['series']]
    curps=cap/curttm
    percentile=sum(v<=curps for v in vals)/len(vals) if vals else None
    base=next(c for c in cases if c['name']=='base')
    sensitivity=[]
    for c in cases:
        for ps in m['valuation_ps_grid']:
            positive(ps,'sensitivity PS')
            b=bridge(base['current_year_revenue'],cap/base['current_year_revenue'],c['next_year_revenue'],ps)
            sensitivity.append({'scenario':c['name'],'ps_assumption':ps,'revenue':c['next_year_revenue'],
                                'equity_value':ps*c['next_year_revenue'],'price':ps*c['next_year_revenue']/m['market']['total_shares'],**b})
    return {'status':'screen-grade','as_of':m['analysis_date'],'ticker':m['ticker'],'market_cap':cap,
            'total_shares':m['market']['total_shares'],'close_unadjusted':m['market']['close_unadjusted'],
            'current_ttm_revenue':curttm,'current_ps_ttm':curps,'ttm_percentile_within_partial_window':percentile,
            'cases':cases,'normalized_financials':normalize(m['periods']),'history':hist,
            'sensitivity':sensitivity,'missing_data':m['missing_data'],
            'warnings':['Scenario growth/margins/R&D are analyst assumptions, not company guidance.',
                        'Historical band is a partial descriptive sample; forward comparison unavailable without frozen vintages.',
                        'PS grid is a sensitivity grid, not calibrated fair multiples or target prices.',
                        'Inventory cost and contract advances are not additive sales backlog; no net-profit or cash forecast implied.']}


def csv_write(path, rows):
    if not rows: return
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('model',type=Path);ap.add_argument('--output-dir',type=Path,required=True)
    ap.add_argument('--previous-result',type=Path)
    args=ap.parse_args()
    try:
        m=json.loads(args.model.read_text());pricepath=args.model.parent/m['history']['price_file']
        with pricepath.open() as f: prices=list(csv.DictReader(f))
        result=run(m,prices)
        if args.previous_result:
            previous=json.loads(args.previous_result.read_text())
            require(previous['ticker']==result['ticker'],'previous ticker mismatch')
            require(previous['as_of']<result['as_of'],'previous version must predate current')
            result['revision_attribution']=[]
            for c in result['cases']:
                old=next(x for x in previous['cases'] if x['name']==c['name'])
                # Same target FY required: rolling year changes cannot masquerade as forecast upgrades.
                require(m['forecast_year']==previous['forecast_year'],'target year rolled; compare common horizon separately')
                result['revision_attribution'].append({'scenario':c['name'],
                    'price_factor_revenue':c['next_year_revenue']/old['next_year_revenue'],
                    'price_factor_multiple':c['forward_ps']/old['forward_ps'],
                    'price_factor_shares':previous['total_shares']/result['total_shares'],
                    **bridge(old['next_year_revenue'],old['forward_ps'],c['next_year_revenue'],c['forward_ps'])})
        result['forecast_year']=m['forecast_year']
        result['input_sha256']=hashlib.sha256(args.model.read_bytes()).hexdigest()
        result['prices_sha256']=hashlib.sha256(pricepath.read_bytes()).hexdigest()
        result['engine_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        out=args.output_dir;out.mkdir(parents=True,exist_ok=True)
        for name,rows in [('quarterly_actuals',result['normalized_financials']),('historical_ps',result['history']['series']),('sensitivity',result['sensitivity'])]: csv_write(out/(name+'.csv'),rows)
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        print(json.dumps({'status':result['status'],'market_cap':result['market_cap'],'current_ps_ttm':result['current_ps_ttm'],'cases':[{k:c[k] for k in ('name','current_year_revenue','next_year_revenue','forward_ps')} for c in result['cases']],'band':{k:result['history'][k] for k in ('n','quantiles','historical_forward_n')},'excluded':len(result['history']['excluded'])},ensure_ascii=False))
    except (ValidationError,KeyError,ValueError,OSError) as e:
        ap.exit(2,'VALIDATION FAILED: '+str(e)+'\n')


if __name__=='__main__': main()
