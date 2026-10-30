"""Failure-mode tests for period alignment, historical information and valuation."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from score_forecast import score

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('valuation',ROOT/'scripts/earnings_valuation.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)


class ValuationTests(unittest.TestCase):
    def setUp(self): self.m=json.loads((ROOT/'assets/valuation/688361/model.json').read_text())

    def test_q3_income_and_margin_differencing(self):
        q=next(x for x in v.normalize(self.m['periods']) if x['year']==2026 and x['quarter']==3)
        self.assertAlmostEqual(q['revenue_quarter'],673041663.13,places=2)
        self.assertAlmostEqual(q['gross_margin_quarter'],1-(833737100.10-507011409.20)/673041663.13)
        self.assertEqual(q['contract_liabilities'],1430483142.03)

    def test_annual_is_not_quarter_without_predecessor(self):
        r=v.normalize([self.m['periods'][0]])[0]
        self.assertIsNone(r['revenue_quarter'])

    def test_ttm_not_nine_month_annualization(self):
        self.assertAlmostEqual(v.ttm(self.m['historical_revenue_snapshots'][-1]),2465633103.33,places=2)

    def test_publication_day_excluded_and_no_future_data(self):
        m=copy.deepcopy(self.m);m['history'].update(start='2026-10-29',end='2026-10-30')
        ps=[{'date':'2026-10-29','close_unadjusted':'228.27'},{'date':'2026-10-30','close_unadjusted':'213.28'}]
        h=v.historical_band(m,ps)
        self.assertTrue(all(x['revenue_source']=='h12026' for x in h['series']))
        self.assertIsNone(h['historical_forward_quantiles'])

    def test_share_registration_gap_not_filled(self):
        m=copy.deepcopy(self.m);m['history'].update(start='2026-07-06',end='2026-07-10')
        prices=[{'date':d,'close_unadjusted':'100'} for d in ['2026-07-06','2026-07-07','2026-07-08','2026-07-09','2026-07-10']]
        h=v.historical_band(m,prices)
        self.assertEqual(len(h['excluded']),3)
        self.assertEqual([r['total_shares'] for r in h['series']],[350163237,352051671])

    def test_units_adjustment_and_market_cap_fail_closed(self):
        for field,value in [('amount_unit','1e8'),('scope','parent')]:
            m=copy.deepcopy(self.m);m[field]=value
            with self.assertRaises(v.ValidationError):v.validate(m)
        m=copy.deepcopy(self.m);m['history']['price_basis']='qfq'
        with self.assertRaises(v.ValidationError):v.validate(m)
        m=copy.deepcopy(self.m);m['market']['total_shares']=320000000
        with self.assertRaises(v.ValidationError):v.validate(m)

    def test_future_and_nan_fail_closed(self):
        m=copy.deepcopy(self.m);m['source_ledger']['q32026']['published']='2026-11-01'
        with self.assertRaises(v.ValidationError):v.validate(m)
        m=copy.deepcopy(self.m);m['periods'][-1]['revenue']=float('nan')
        with self.assertRaises(v.ValidationError):v.validate(m)

    def test_scenario_base_and_forward_ps(self):
        c=v.forecast(self.m['scenarios'][1],self.m)
        self.assertAlmostEqual(c['next_year_revenue'],3723063139.2532277,places=2)
        self.assertAlmostEqual(c['forward_ps'],20.167689233962516)

    def test_bridge_no_double_counting(self):
        b=v.bridge(100,20,135,16)
        self.assertEqual(b['growth_at_constant_ps'],700)
        self.assertEqual(b['multiple_change_including_interaction'],-540)
        self.assertAlmostEqual(b['return'],.08)

    def test_cohorts_reject_duplicate_and_conversion_above_one(self):
        s=copy.deepcopy(self.m['scenarios'][0]);s['mode']='acceptance_cohorts'
        c={'id':'x','basis':'sales_ex_tax','sales_value':100,'source':'q32026','quarter_acceptance_weights':[.3,.3,.3,.3]}
        s['cohorts']=[c];s['service_revenue_quarters']=[0]*4
        with self.assertRaises(v.ValidationError):v.forecast(s,self.m)
        c['quarter_acceptance_weights']=[.25]*4;s['cohorts']=[c,copy.deepcopy(c)]
        with self.assertRaises(v.ValidationError):v.forecast(s,self.m)
        s['cohorts']=[c];self.assertEqual(v.forecast(s,self.m)['next_year_revenue'],100)
        c['basis']='inventory_cost'
        with self.assertRaises(v.ValidationError):v.forecast(s,self.m)

    def test_forecast_scoring_never_fills_missing_actuals(self):
        frozen={'ticker':'688361.SH','as_of':'2026-10-30','forecast_year':2027,
                'cases':[v.forecast(self.m['scenarios'][1],self.m)]}
        self.m['analysis_date']='2027-04-30'
        self.assertEqual(score(frozen,self.m)['status'],'awaiting_reported_actuals')
        p=copy.deepcopy(self.m['periods'][-1]);p.update(year=2027,months=3,revenue=4e8,source='new')
        self.m['periods'].append(p);self.m['source_ledger']['new']={'published':'2027-04-29','url':'fixture-only'}
        out=score(frozen,self.m)
        self.assertEqual(out['status'],'review_required')
        self.assertEqual(len(out['revenue_scores']),1)
        self.assertLess(out['revenue_scores'][0]['error_ratio'],-.10)

    def test_noncalendar_fy_roll_rejected(self):
        self.m['forecast_year']=2028
        with self.assertRaises(v.ValidationError):v.validate(self.m)


if __name__=='__main__':unittest.main()
