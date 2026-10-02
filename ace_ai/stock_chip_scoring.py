"""Versioned major-branch observations. No investor identities or margin score."""
import math
import time
from collections import defaultdict
import spot_chip as spot

VERSION = 'spot-v1-70-30'
WEIGHTS = {'technical':70, 'spot':30}
CAPS = (30,25,25,20)
LABELS = ('淨買賣超程度','方向持續性','近期方向變化','原買超分點動向')


def _scale(value, cap, full):
    return round(cap * max(0, min(1, .5 + value / (2 * full))), 2)


def calculate(dates, statuses, rows, bars):
    dates=sorted(set(dates))[-70:]
    out={'version':VERSION,'score':None,'complete':False,'data_date':dates[-1] if dates else '',
         'components':[], 'source_limitation':spot.SOURCE_LIMITATION,
         'method':'30/25/25/20；比較最近20日與之前50日；缺席不是零；不辨識身分或勝率'}
    if len(dates)!=70 or any((statuses.get(d) or {}).get('status')!='complete' for d in dates):
        out['reason']='70個交易日尚未完整'
        return out
    if any(not bars.get(d) or not math.isfinite(bars[d][1]) or bars[d][1]<=0 for d in dates):
        out['reason']='同期成交量不完整'
        return out
    daily=defaultdict(dict)
    for row in rows:
        if row['date'] in dates:
            value=float(row['net'])
            if not math.isfinite(value):
                out['reason']='分點數值異常'
                return out
            daily[row['date']][row['branch_name']]=value
    if any(not daily[d] for d in dates):
        out['reason']='分點明細不完整'
        return out
    old,recent=dates[:-20],dates[-20:]
    def concentration(window):
        net=defaultdict(float)
        for day in window:
            for branch,value in daily[day].items(): net[branch]+=value
        buys=sum(sorted((v for v in net.values() if v>0),reverse=True)[:15])
        sells=sum(sorted((-v for v in net.values() if v<0),reverse=True)[:15])
        return (buys-sells)/sum(bars[d][1] for d in window)*100
    c20,c50=concentration(recent),concentration(old)
    # Signed daily balance: each day gets one vote, independent of trading size.
    def daily_signal(day):
        vals=daily[day].values()
        balance=(sum(sorted((v for v in vals if v>0),reverse=True)[:15])-
                 sum(sorted((-v for v in vals if v<0),reverse=True)[:15])) / bars[day][1]
        return 1 if balance>=.005 else -1 if balance<=-.005 else 0
    signals=[daily_signal(d) for d in recent]
    persistence=sum(signals)/20
    prior=defaultdict(float)
    for day in old:
        for branch,value in daily[day].items(): prior[branch]+=value
    leaders=sorted(((b,n) for b,n in prior.items() if n>0),key=lambda x:(-x[1],x[0]))[:5]
    observed=[]
    for branch,weight in leaders:
        days=[d for d in recent if branch in daily[d]]
        if len(days)>=3:
            ratio=sum(daily[d][branch] for d in days)/sum(bars[d][1] for d in days)*100
            observed.append((branch,weight,ratio,len(days)))
    leader_value=None
    if leaders and len(observed)>=min(3,len(leaders)):
        leader_value=sum(w*r for b,w,r,n in observed)/sum(w for b,w,r,n in observed)
    values=[_scale(c20,30,5), _scale(persistence,25,1), _scale(c20-c50,25,5),
            _scale(leader_value,20,2) if leader_value is not None else None]
    notes=[f'近20日主要分點淨集中度 {c20:+.2f}%',
           f'近20日買超方向 {signals.count(1)}日、賣超方向 {signals.count(-1)}日；每日門檻為成交量0.5%',
           f'近20日 {c20:+.2f}%／之前50日 {c50:+.2f}%；差 {c20-c50:+.2f}個百分點',
           f'前期前5家中可觀察 {len(observed)}/{len(leaders)}家；每家至少出現3日，未出現不當成零']
    out['components']=[{'label':label,'value':value,'max':cap,'reason':note}
                       for label,value,cap,note in zip(LABELS,values,CAPS,notes)]
    out.update(concentration_20=c20,concentration_previous_50=c50,
               observed_leaders=[{'branch':b,'ratio':r,'days':n} for b,w,r,n in observed])
    out['complete']=all(v is not None for v in values)
    if out['complete']: out['score']=round(sum(values),2)
    else: out['reason']='原主要買超分點的後續可觀察樣本不足'
    return out


def combine(technical, scoring, as_of):
    valid=isinstance(technical,(int,float)) and math.isfinite(technical) and 0<=technical<=100
    complete=valid and scoring.get('complete') and scoring.get('data_date')==as_of
    return {'weights':WEIGHTS,'technical_score':technical if valid else None,
            'spot_score':scoring.get('score'), 'total':round(technical*.7+scoring['score']*.3,2) if complete else None,
            'complete':bool(complete),'data_date':as_of,'version':VERSION,
            'method':'技術70%＋主要分點籌碼30%；融資券、分點勝率不計分；不是上漲機率'}


def prepare(code, as_of, budget=8):
    calendar=spot.candidate_dates()
    dates=[d for d in calendar[0] if d<=as_of][-70:]
    if dates and budget > 0:
        spot.ensure_days(code, dates, budget=budget, latest_date=dates[-1],lock_wait=.25,retry_pending=True)
    statuses=spot.local_market_cache.spot_day_status(code,dates)
    rows=spot.local_market_cache.load_spot_rows(code,dates)
    result=calculate(dates,statuses,rows,spot._bars(code))
    if result.get('data_date')!=as_of:
        result.update(score=None,complete=False,reason='分點與技術日期不同')
    if not result['complete']:
        print(f'主要分點評分未完整｜{code}｜{as_of}｜{result.get("reason", "")}',flush=True)
        if dates and any((statuses.get(d) or {}).get('status') not in spot.CONFIRMED_STATUSES for d in dates):
            spot.continue_in_background(code,dates,dates[-1])
    return result,dates


def sections(scoring, composite, margin=None):
    def num(v): return f'{v:.1f}' if v is not None else '—'
    result=[{'type':'heading','text':'主要分點籌碼與綜合評分'},
            {'type':'table','columns':['技術／100','籌碼／100','綜合／100'], 'widths':[.33,.33,.34],
             'rows':[[num(composite['technical_score']),num(composite['spot_score']),num(composite['total'])]]}]
    if scoring.get('components'):
        result.append({'type':'table','columns':['籌碼項目','得分','依據'],'widths':[.25,.12,.63],
                       'rows':[[c['label'],f"{num(c['value'])}／{c['max']}",c['reason']] for c in scoring['components']]})
    result.append({'type':'note','text':'技術70%＋籌碼30%；70日每日前段分點近似統計，不代表持股、身分或上漲機率。'+
                   ('資料未完整，綜合不計分。' if not composite['complete'] else '')})
    if margin is not None:
        result.append({'type':'heading','text':'融資券觀察｜管理員・不計分'})
        if margin.get('available'):
            result.append({'type':'rows','items':[{'lead':str(margin.get('data_date') or ''),
                'parts':[f"融資餘額 {margin.get('margin_balance',0):,.0f} 張",f"融券餘額 {margin.get('short_balance',0):,.0f} 張"]}]})
            result.append({'type':'table','columns':['期間','融資增減／張','融券增減／張'],'widths':[.24,.38,.38],
                'rows':[[f'近{n}日',f"{p['margin_change']:+,.0f}" if p.get('complete') else '—',
                         f"{p['short_change']:+,.0f}" if p.get('complete') else '—']
                        for n in (20,70) for p in [margin.get('periods',{}).get(str(n),{})]]})
        result.append({'type':'note','text':'餘額增減包含償還；融券不含借券，不推定大戶、散戶或軋空。'})
    return result


def enrich_card(card, allow_margin=False):
    code=str(card['stock_code'])
    as_of=str(card.get('data_date') or '')[:10].replace('/','-')
    scoring,dates=prepare(code,as_of)
    composite=combine(card.get('pattern_score'),scoring,as_of)
    card=dict(card, spot_scoring=scoring,composite_score=composite,card_title='技術評分')
    margin={'available':False} if allow_margin else None
    if allow_margin and dates:
        import custom_chip_compare
        margin=custom_chip_compare.load_margin(code,dates)
        card['margin_observation']=margin
    card['chip_sections']=sections(scoring,composite,margin)
    card['card_note']='技術原分數；綜合另按技術70%＋籌碼30%計算'
    return card
