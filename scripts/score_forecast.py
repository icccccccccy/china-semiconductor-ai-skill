#!/usr/bin/env python3
"""Score a frozen result against subsequently reported consolidated actuals.
python3 scripts/score_forecast.py frozen-result.json updated-model.json --output-file output/score.json
Never replaces the frozen forecast or fills missing actuals with estimates.
"""
import argparse
import hashlib
import json
from pathlib import Path
from earnings_valuation import normalize, require, finite, ValidationError


def score(frozen, actual):
    require(frozen['ticker']==actual['ticker'],'ticker mismatch')
    require(actual['currency']=='CNY' and actual['amount_unit']=='yuan' and actual['scope']=='consolidated','actual unit/scope mismatch')
    require(actual['analysis_date']>frozen['as_of'],'actual snapshot must be newer')
    target=frozen['forecast_year'];rows=[]
    for p in actual['periods']:
        require(p['source'] in actual['source_ledger'],'actual source missing')
        require(p['status']=='reported','actual cannot be estimate')
        require(actual['source_ledger'][p['source']]['published']<=actual['analysis_date'],'future actual source')
        require(finite(p['revenue']) and p['revenue']>0,'invalid actual revenue')
    for p in normalize(actual['periods']):
        if p['year']!=target or p['revenue_quarter'] is None:continue
        for c in frozen['cases']:
            pred=c['next_year_quarters'][p['quarter']-1]
            error=p['revenue_quarter']/pred-1
            rows.append({'scenario':c['name'],'year':target,'quarter':p['quarter'],
                         'original_revenue_forecast':pred,'actual_revenue':p['revenue_quarter'],
                         'error_ratio':error,'review_required':abs(error)>actual['validation_policy']['revenue_error_review'],
                         'source':p['source']})
    return {'ticker':actual['ticker'],'forecast_as_of':frozen['as_of'],'actual_as_of':actual['analysis_date'],
            'forecast_year':target,'revenue_scores':rows,
            'status':'awaiting_reported_actuals' if not rows else 'review_required' if any(r['review_required'] for r in rows) else 'within_review_threshold',
            'note':'Trigger means re-underwrite, not automatic buy/sell. GM and R&D assumptions are annual sensitivities; inspect comparable full-year actuals separately.'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('frozen_result',type=Path);p.add_argument('actual_model',type=Path);p.add_argument('--output-file',type=Path,required=True);a=p.parse_args()
    try:
        r=score(json.loads(a.frozen_result.read_text()),json.loads(a.actual_model.read_text()))
        r['frozen_sha256']=hashlib.sha256(a.frozen_result.read_bytes()).hexdigest();r['actual_sha256']=hashlib.sha256(a.actual_model.read_bytes()).hexdigest()
        a.output_file.parent.mkdir(parents=True,exist_ok=True);a.output_file.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(r['status'])
    except (ValidationError,KeyError,ValueError,OSError) as e:p.exit(2,'VALIDATION FAILED: '+str(e)+'\n')


if __name__=='__main__':main()
