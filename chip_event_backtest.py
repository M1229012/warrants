"""Test-only observed branch accumulation and margin-event outcome study.

No score, no actual broker/account P&L. All prices must be verified, adjusted,
closed OHLC supplied by the existing price loader. Missing is never zero.
"""
import math
import os
import time
from statistics import median
from collections import defaultdict

HORIZONS = (5, 10, 20)
VERSION = 'chip-events-year-v6'


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def price_rows(frame):
    rows={}
    for index,row in frame.iterrows():
        values={k.lower():finite(row.get(k)) for k in ('Open','High','Low','Close')}
        if all(v is not None and v>0 for v in values.values()):
            volume=finite(row.get('Volume'))
            values['volume_lots']=volume/1000 if volume is not None and volume>0 else None
            values['verified']=True
            rows[str(index)[:10]]=values
    return dict(sorted(rows.items()))


def exclusion_reason(prices,signal,horizon):
    dates=list(prices)
    if signal not in prices:return 'missing_signal_price'
    i=dates.index(signal)
    if i+horizon>=len(dates):return 'pending'
    path=[prices[d] for d in dates[i:i+horizon+1]]
    if any(p.get('verified') is False for p in path):return 'unverified_price'
    entry=path[1]
    limit=finite(entry.get('verified_limit_up'))
    if limit and math.isclose(entry['open'],limit,rel_tol=1e-7,abs_tol=1e-5):return 'open_at_verified_limit'
    return ''


def outcome(prices, signal, horizon):
    if exclusion_reason(prices,signal,horizon):return None
    dates=list(prices);i=dates.index(signal)
    path=[prices[d] for d in dates[i+1:i+horizon+1]]
    entry=path[0]['open'];peak=entry;worst=0.
    for bar in path:
        worst=min(worst,(bar['low']/peak-1)*100)
        peak=max(peak,bar['close'])
    return {'return_pct':(path[-1]['close']/entry-1)*100,'drawdown_pct':worst,
            'max_gain_pct':max(0.,max(b['high'] for b in path)/entry*100-100),
            'entry_depth_pct':min(0.,min(b['low'] for b in path)/entry*100-100),
            'signal_date':signal,'entry_date':dates[i+1],'exit_date':dates[i+horizon]}


def summarize(prices,signals,background_dates):
    output={}
    for h in HORIZONS:
        mature=[v for d in signals if (v:=outcome(prices,d,h)) is not None]
        background=[v for d in background_dates if (v:=outcome(prices,d,h)) is not None]
        excluded=[{'date':d,'reason':exclusion_reason(prices,d,h)} for d in signals if exclusion_reason(prices,d,h) not in ('','pending')]
        n=len(mature);returns=[v['return_pct'] for v in mature]
        baseline=sum(v['return_pct'] for v in background)/len(background) if background else None
        avg=sum(returns)/n if n else None
        output[str(h)]={'samples':n,'pending':sum(exclusion_reason(prices,d,h)=='pending' for d in signals),
            'excluded':excluded,'excluded_count':len(excluded),'small_sample':n<5,
            'avg_return_pct':round(avg,2) if avg is not None else None,
            'median_return_pct':round(median(returns),2) if n else None,
            'reach_3_pct':round(sum(v>=3-1e-9 for v in returns)/n*100,1) if n else None,
            'reach_5_pct':round(sum(v>=5-1e-9 for v in returns)/n*100,1) if n else None,
            'median_entry_depth_pct':round(median(v['entry_depth_pct'] for v in mature),2) if n else None,
            'median_max_gain_pct':round(median(v['max_gain_pct'] for v in mature),2) if n else None,
            'worst_drawdown_pct':round(min(v['drawdown_pct'] for v in mature),2) if n else None,
            'avg_drawdown_pct':round(sum(v['drawdown_pct'] for v in mature)/n,2) if n else None,
            'stock_background_avg_pct':round(baseline,2) if baseline is not None else None,
            'background_samples':len(background),
            'background_reach_3_pct':round(sum(v['return_pct']>=3-1e-9 for v in background)/len(background)*100,1) if len(background)>=40 else None,
            'background_reach_5_pct':round(sum(v['return_pct']>=5-1e-9 for v in background)/len(background)*100,1) if len(background)>=40 else None,
            'excess_return_pct':round(avg-baseline,2) if avg is not None and baseline is not None else None,
            'recent_events':mature[-4:],'events':mature}
    return output


def volume_mean(prices,dates,i):
    window=dates[max(0,i-19):i+1]
    values=[finite(prices[d].get('volume_lots')) for d in window]
    return sum(values)/20 if len(values)==20 and all(v is not None and v>0 for v in values) else None


def overnight_study(branch,daily,dates,complete,prices,action_dates=()):
    ratio=max(.001,float(os.getenv('TEST_OVERNIGHT_BUY_VOLUME_RATIO','.03')))
    minimum=max(1.,float(os.getenv('TEST_OVERNIGHT_MIN_LOTS','100')))
    reverse=max(.01,float(os.getenv('TEST_OVERNIGHT_REVERSE_RATIO','.5')))
    qualifying=checked=unknown=0;pairs=[];actions=set(action_dates)
    for i,day in enumerate(dates[:-1]):
        buy=daily[day].get(branch);mean=volume_mean(prices,dates,i)
        if day not in complete or prices[day].get('verified') is False or day in actions or buy is None or mean is None or buy<max(minimum,mean*ratio):continue
        qualifying+=1;next_day=dates[i+1]
        sell=daily[next_day].get(branch)
        if next_day not in complete or next_day in actions or sell is None or prices[next_day].get('verified') is False:
            unknown+=1;continue
        checked+=1
        if sell<0 and abs(sell)>=max(20.,buy*reverse):
            pairs.append({'buy_date':day,'sell_date':next_day,'buy_lots':buy,'sell_lots':abs(sell),
                'reverse_pct':round(abs(sell)/buy*100,1),
                'buy_volume_pct':round(buy/mean*100,2)})
    rate=len(pairs)/checked*100 if checked else None
    label='疑似隔日沖' if checked>=5 and len(pairs)>=3 and rate>=50 else '隔日反向紀錄' if pairs else ''
    return {'qualifying_buy_days':qualifying,'checked_pairs':checked,'unobserved_next_days':unknown,
        'reverse_events':len(pairs),'reverse_rate_pct':round(rate,1) if rate is not None else None,
        'median_reverse_pct':round(median(p['reverse_pct'] for p in pairs),1) if pairs else None,
        'label':label,'pairs':pairs,'minimum_lots':minimum,'volume_ratio':ratio,'reverse_ratio':reverse}


def branch_study(dates,complete_dates,rows,prices,branch_name='',action_dates=()):
    dates=sorted(set(dates).intersection(prices));complete=set(complete_dates);daily=defaultdict(dict)
    for row in rows:
        value=finite(row.get('net'))
        if value is not None and row.get('date') in complete:daily[row['date']][row['branch_name']]=value
    observation=dates[-70:];window=dates[-5:]
    actions=set(action_dates);recent_action=max((d for d in actions if observation and observation[0]<=d<=observation[-1]),default='')
    comparable=[d for d in observation if not recent_action or d>=recent_action]
    def active(branch):
        if len(window)<5 or not set(window).issubset(complete) or set(window)&actions:return False
        vals=[daily[d].get(branch) for d in window]
        return sum(v is not None and v>0 for v in vals)>=3 and sum(v for v in vals if v is not None)>0
    background=[d for d in dates if d in complete]
    names=[branch_name] if branch_name else sorted({b for values in daily.values() for b in values})
    threshold_ratio=max(.0001,float(os.getenv('TEST_BRANCH_WAVE_VOLUME_RATIO','.01')))
    minimum=max(1.,float(os.getenv('TEST_BRANCH_WAVE_MIN_LOTS','20')))
    studied=[]
    for b in names:
        events=[];waves=[];start=last_buy=None;cum=0.;triggered=False;first_sell=None
        for i,day in enumerate(dates):
            # A missing source day breaks knowledge continuity; it is NOT a zero-trade day.
            if day not in complete or day in actions:
                start=last_buy=None;cum=0.;triggered=False;first_sell=None;continue
            net=daily[day].get(b)
            if last_buy is not None and i-last_buy>5:
                start=last_buy=None;cum=0.;triggered=False;first_sell=None
            if net is None:continue
            if net>0:
                if start is None:start=day;cum=0.;triggered=False;first_sell=None
                last_buy=i
            if start is None:continue
            cum+=net
            if net<0 and first_sell is None:
                first_sell=day
                if triggered and waves and waves[-1]['start_date']==start:waves[-1]['first_observed_sell']=day
            mean=volume_mean(prices,dates,i)
            if not triggered and mean is not None and cum>=max(minimum,mean*threshold_ratio) and prices[day].get('verified') is not False:
                events.append(day);waves.append({'start_date':start,'signal_date':day,'observed_net_at_signal':round(cum,1),
                    'threshold_lots':round(max(minimum,mean*threshold_ratio),1),'first_observed_sell':first_sell})
                triggered=True
        records=[{'date':d,'net':daily[d][b],'reference_close':prices[d]['close'],
            'action':'buy' if daily[d][b]>0 else 'sell' if daily[d][b]<0 else 'flat'} for d in dates if b in daily[d]]
        buy_refs=[(prices[d]['close'],daily[d][b]) for d in comparable if daily[d].get(b,0)>0]
        cost=sum(p*n for p,n in buy_refs)/sum(n for _,n in buy_refs) if buy_refs else None
        latest=daily[dates[-1]].get(b) if dates else None
        net5=sum(daily[d].get(b,0) for d in window);net70=sum(daily[d].get(b,0) for d in comparable)
        is_active=active(b)
        status='累積・近期調節' if is_active and latest is not None and latest<0 else '持續買進' if is_active else '近期調節' if net5<0 else '近期買超' if net5>0 else '近期未上榜'
        overnight=overnight_study(b,daily,dates,complete,prices,actions)
        overnight['recent_match'] = overnight['label']=='疑似隔日沖' and any(q['sell_date'] in window for q in overnight['pairs'])
        studied.append({'branch':b,'active_now':is_active,'status':status,'latest_net':latest,
            'buy_days_5':sum(daily[d].get(b,0)>0 for d in window),'buy_days_70':sum(daily[d].get(b,0)>0 for d in observation),
            'observed_net_5':round(net5,1),'observed_net_20':round(sum(daily[d].get(b,0) for d in dates[-20:]),1),
            'observed_net_70':round(net70,1),'estimated_buy_cost':round(cost,2) if cost else None,
            'estimated_return_pct':round((prices[max(prices)]['close']/cost-1)*100,2) if cost else None,
            'signal_dates':events,'wave_events':waves,'metrics':summarize(prices,events,background),
            'records':records,'overnight':overnight,'selection_reasons':[]})
    if branch_name:
        selected=studied
        for b in selected:b['selection_reasons']=['指定分點']
    else:
        current=sorted((b for b in studied if b['active_now']),key=lambda b:(-b['observed_net_5'],b['branch']))[:3]
        rest=sorted((b for b in studied if b not in current and
            (b['observed_net_70']>0 and b['observed_net_5']<0 or
             b['metrics']['20']['samples']>=5 and any(r['date'] in window for r in b['records']))),
            key=lambda b:(-abs(b['observed_net_5']),b['branch']))[:3]
        selected=current+rest
        if not selected:selected=sorted((b for b in studied if any(r['date'] in window for r in b['records'])),key=lambda b:(-abs(b['observed_net_5']),b['branch']))[:3]
        for b in selected:b['selection_reasons']=['近期動向']
    return {'available':bool(dates),'period_start':observation[0] if observation else '',
        'period_end':observation[-1] if observation else '', 'history_start':dates[0] if dates else '',
        'history_end':dates[-1] if dates else '', 'complete_days':len(complete.intersection(observation)),
        'requested_days':len(observation),'history_complete_days':len(complete.intersection(dates)),
        'branches':selected,'all_branches':studied,'compared_branches':len(studied),
        'background':summarize(prices,[],background)['20'],'share_adjusted_window':bool(recent_action),
        'definition':f'觀察到的買超波段：相隔不超過5交易日歸同一波，累積淨買超達20日均量{threshold_ratio*100:g}%且至少{minimum:g}張才成立；次日開盤起算。一波只成立一次。未上榜不是零，資料缺日中止波段；訊號觀察期仍可能重疊。門檻為測試預設，非已驗證策略。'}


def margin_study(records, prices, action_dates=()):
    rows = {}
    for row in records:
        day = str(row.get('date', ''))[:10]
        today = finite(row.get('MarginPurchaseTodayBalance'))
        yesterday = finite(row.get('MarginPurchaseYesterdayBalance'))
        if day in prices and today is not None and yesterday is not None and today >= 0 and yesterday > 0:
            rows[day] = {'delta': today - yesterday, 'base': yesterday,
                         'action': day in action_dates or any(w in str(row.get('Note', '')) for w in ('分割', '合併', '減資'))}
    dates = list(prices)
    signals, details, background, reductions, last = [], [], [], [], -1000
    ratio = max(0.001, float(os.getenv('TEST_MARGIN_EVENT_BALANCE_RATIO', '0.05')))
    multiple = max(1.0, float(os.getenv('TEST_MARGIN_EVENT_DELTA_MULTIPLE', '2')))
    for i, day in enumerate(dates):
        past = dates[i - 20:i]
        if i < 20 or day not in rows or any(d not in rows or rows[d]['action'] for d in past + [day]):
            continue
        background.append(day)
        r = rows[day]
        positive_mean = sum(max(0, rows[d]['delta']) for d in past) / 20
        threshold = max(r['base'] * ratio, positive_mean * multiple)
        if r['delta'] < 0 and abs(r['delta']) >= threshold:
            reductions.append({'date':day,'balance_delta':r['delta'],'balance_increase_pct':round(r['delta']/r['base']*100,2)})
        if r['delta'] > 0 and r['delta'] >= threshold and i - last >= 20:
            signals.append(day)
            details.append({'date': day, 'balance_delta': r['delta'],
                            'balance_increase_pct': round(r['delta'] / r['base'] * 100, 2)})
            last = i
    return {'events': details, 'reductions':reductions, 'signal_dates': signals,
            'metrics': summarize(prices, signals, background),
            'period_start': min(rows) if rows else '', 'period_end': max(rows) if rows else '',
            'definition': f'單日融資餘額淨增加至少為前日餘額{ratio * 100:g}%，且至少為前20日正增量日均值{multiple:g}倍；前20日須完整，排除標記股數調整的區間，事件間隔至少20個股票交易日。餘額淨增加不等於實際融資買入額，不能判定大戶或散戶。'}


def load_margin_records(code, dates):
    import local_market_cache as store
    import warrant_ai_tools as tools
    if not dates:
        return []
    key = 'test_event_margin_v1:' + code
    old = store.get_state(key, {}) or {}
    if old.get('start') == dates[0] and old.get('end') == dates[-1] and time.time() - old.get('checked', 0) < 21600:
        return old.get('rows', [])
    raw = tools.core()._finmind_get_data('TaiwanStockMarginPurchaseShortSale', data_id=code,
                                       start_date=dates[0], end_date=dates[-1], allow_empty=True)
    records = raw.to_dict('records') if hasattr(raw, 'to_dict') else list(raw or [])
    try:
        store.set_state(key, {'start': dates[0], 'end': dates[-1], 'checked': time.time(), 'rows': records})
    except Exception as exc:
        print(f'⚠️ 測試融資回測快取寫入失敗｜{code}｜{type(exc).__name__}', flush=True)
    return records


def pct(value, signed=False):
    return '—' if value is None else (f'{value:+.2f}%' if signed else f'{value:.1f}%')


def metric_table(metrics):
    return {'type':'table','columns':['期間','成熟波段','報酬中位數','達3%','達5%','最高漲幅中位數','最深跌幅中位數'],
        'widths':[.08,.1,.16,.12,.12,.21,.21],'signed':['報酬中位數','最高漲幅中位數','最深跌幅中位數'],
        'rows':[[f'{h}日',str(m['samples']),pct(m['median_return_pct'],True),pct(m['reach_3_pct']),pct(m['reach_5_pct']),
            pct(m['median_max_gain_pct'],True),pct(m['median_entry_depth_pct'],True)] for h in HORIZONS for m in [metrics[str(h)]]]}


def _margin_sections(payload, detailed=False):
    data=payload.get('margin')
    if not data or not data.get('events'):return []
    m=data['metrics']['20'];event=data['events'][-1]
    return [{'type':'heading','text':'大額融資淨增｜管理員'},
        {'type':'table','columns':['最近事件','樣本','達3%','達5%','報酬中位數','最深跌幅中位數'],
         'widths':[.27,.09,.16,.16,.16,.16],'accent':('達3%','達5%'),'signed':('報酬中位數','最深跌幅中位數'),
         'rows':[[event['date'][5:]+f" 淨增 {event['balance_increase_pct']:+.1f}%",str(m['samples']),
                 pct(m['reach_3_pct']) if m['samples']>=5 else '不足',pct(m['reach_5_pct']) if m['samples']>=5 else '不足',pct(m['median_return_pct'],True),pct(m['median_entry_depth_pct'],True)]]}]


def branch_card(payload,detailed=False,page=1):
    data=payload['spot'];branches=data['branches'];sections=[]
    flows=[];performance=[];small=[];badges=[]
    ready=data.get('readiness') or {'resolved_days':data.get('complete_days',0),'requested_days':data.get('requested_days',70)}
    done=int(ready['resolved_days']);wanted=int(ready['requested_days'])
    sections.append({'type':'paragraph','text':f'近70日資料：{done}/{wanted}日已確認'+('｜補齊中' if done<wanted else '｜本期已齊')})
    for b in branches:
        m=b['metrics']['20'];cost=f"{b['estimated_buy_cost']:,.2f}" if b['estimated_buy_cost'] is not None else '—'
        status=b['status']
        badges.append('疑似隔日沖' if b.get('overnight',{}).get('recent_match') else '')
        flows.append([b['branch'],status,f"{b['observed_net_5']:+,.0f}",f"{b['observed_net_70']:+,.0f}",cost,pct(b['estimated_return_pct'],True)])
        if m['samples']>=5:
            performance.append([b['branch'],str(m['samples'])+'筆',pct(m['median_return_pct'],True),pct(m['reach_3_pct']),pct(m['reach_5_pct']),pct(m['median_entry_depth_pct'],True)])
        elif m['samples']:
            small.append([b['branch'],str(m['samples'])+'筆', '｜'.join(e['signal_date'][5:]+' '+pct(e['return_pct'],True) for e in m['recent_events'])])
    if flows:
        sections+=[{'type':'heading','text':'近期動向｜淨買賣超：張'},
            {'type':'table','columns':['券商分點','目前動向','5日淨超','70日淨超','估計均價','現價相對估均價'],
             'widths':[.28,.12,.10,.12,.14,.24],'signed':['5日淨超','70日淨超','現價相對估均價'],'rows':flows,'row_badges':badges}]
    else:sections.append({'type':'paragraph','text':'目前沒有可顯示的分點動向'})
    if performance or small:
        sections.append({'type':'paragraph','text':'歷史區間｜'+data.get('history_start','')+'～'+data.get('history_end','')+f"｜已確認{data.get('history_complete_days',0)}日"})
    if performance:
        sections+=[{'type':'heading','text':'歷史表現｜次日開盤至第20個交易日收盤'},
            {'type':'table','columns':['券商分點','成熟波段數','收盤報酬中位數','20日達3%','20日達5%','最深跌幅中位數'],
             'widths':[.22,.13,.17,.13,.13,.22],'signed':['收盤報酬中位數','最深跌幅中位數'],'accent':['20日達3%','20日達5%'],'rows':performance}]
    if small:
        sections.append({'type':'paragraph','text':f'{len(small)}家分點僅有1～4筆成熟波段，逐筆結果請查分點明細'})
    if not performance and not small:sections.append({'type':'paragraph','text':'歷史表現：尚無完成20日觀察的波段'})
    base=data.get('background',{})
    sections.append({'type':'paragraph','text':
        '同期20日收盤參考｜達3% '+pct(base.get('background_reach_3_pct'))+'・達5% '+pct(base.get('background_reach_5_pct'))
        if base.get('background_samples',0)>=40 else '同期股價參考：資料不足'})
    sections.extend(_margin_sections(payload,detailed))
    if detailed:
        for b in branches:
            sections+=[{'type':'heading','text':b['branch']+'｜各期結果'},metric_table(b['metrics'])]
            m=b['metrics']['20']
            if 0<m['samples']<5:
                sections.append({'type':'table','columns':['事件成立','20日收盤報酬'],'widths':[.45,.55],
                    'signed':['20日收盤報酬'],'rows':[[e['signal_date'],pct(e['return_pct'],True)] for e in m['recent_events']]})
            if m.get('excluded_count'):
                labels={'open_at_verified_limit':'開盤觸及漲停，成交可行性未確認','unverified_price':'價格還原未核實','missing_signal_price':'缺少事件日價格'}
                sections.append({'type':'table','columns':['排除事件日期','原因'],'widths':[.25,.75],
                    'rows':[[e['date'],labels.get(e['reason'],e['reason'])] for e in m['excluded'][-10:]]})
            sections.append({'type':'table','columns':['持有期','最大回撤'],'widths':[.4,.6],
                'rows':[[str(h)+'日',pct(b['metrics'][str(h)]['worst_drawdown_pct'],True)] for h in HORIZONS]})
            events=b.get('wave_events',[])
            if events:sections.append({'type':'table','columns':['波段開始','事件成立','首次上榜賣超'],
                'widths':[.33,.33,.34],'rows':[[e['start_date'],e['signal_date'],e.get('first_observed_sell') or '未觀察到'] for e in events[-10:]]})
            records=list(reversed(b['records']));start=(max(1,int(page))-1)*20
            nums={m['date']:str(m['no']) for m in payload.get('display_marks',[]) if m.get('branch')==b['branch']}
            sections+=[{'type':'heading','text':f'上榜買賣超｜第{page}頁'},
                {'type':'table','columns':['日期／圖號','方向','淨買賣超','還原收盤'],'widths':[.3,.16,.27,.27],
                 'signed':['淨買賣超'],'rows':[[r['date']+(' #'+nums[r['date']] if r['date'] in nums else ''),
                    '買超' if r['net']>0 else '賣超',f"{r['net']:+,.0f}",f"{r['reference_close']:,.2f}"] for r in records[start:start+20]]}]
            if b.get('overnight',{}).get('pairs'):
                sections.append({'type':'table','columns':['買超日期','買超張數','隔日賣超日期','賣超張數','反向量比例'],
                    'widths':[.23,.16,.23,.16,.22],'rows':[[q['buy_date'],f"{q['buy_lots']:,.0f}",q['sell_date'],f"{q['sell_lots']:,.0f}",pct(q['reverse_pct'])] for q in b['overnight']['pairs'][-10:]]})
    return {'branch':f"{payload['stock_code']} {payload['stock_name']}",'label':data.get('period_end',''),
            'tags':['70日觀察'],'clean_display':True,'sections':sections}


def margin_card(payload):
    # 管理員融資摘要併入現股卡，避免同一頁另起一張長卡。
    return None


def event_marks(payload, bars, branch_name='', include_margin=False):
    visible={str(b['date']).replace('/','-') for b in bars};marks=[]
    if branch_name:
        for b in payload['spot']['branches']:
            if b['branch']!=branch_name:continue
            for r in b['records']:
                if r['date'] in visible and r['action']!='flat':
                    marks.append({'date':r['date'],'side':r['action'],'label':'買超' if r['net']>0 else '賣超',
                                  'value':r['net'],'branch':b['branch'],'kind':'spot'})
    if include_margin and payload.get('margin'):
        for event in payload['margin']['events']:
            if event['date'] in visible:
                marks.append({'date':event['date'],'side':'buy','label':'融資淨增',
                              'value':event['balance_delta'],'branch':'融資餘額','kind':'margin'})
        for r in payload['margin'].get('reductions',[]):
            if r['date'] in visible:
                marks.append({'date':r['date'],'side':'sell','label':'融資淨減',
                              'value':r['balance_delta'],'branch':'融資餘額','kind':'margin'})
    marks.sort(key=lambda m:(m['date'],m['kind'],m['side']))
    for i,m in enumerate(marks,1):m['no']=i
    return marks


def prepare(code, name='', as_of='', report=None, allow_margin=False, branch_name=''):
    import spot_history
    with spot_history.active(code,query=True):
        return _prepare_inner(code,name,as_of,report,allow_margin,branch_name)


def _prepare_inner(code, name='', as_of='', report=None, allow_margin=False, branch_name=''):
    import spot_chip
    import local_market_cache as store
    import warrant_ai_tools as tools
    with tools.quote_policy(admin_live=False):
        bundle = tools._load_price_bundle(code, spot_history_mode=True)
    frame = tools.closed_frame(bundle)
    prices = price_rows(frame)
    actions=bundle.get('corporate_actions') or {}
    # Lack of corporate-action coverage is not proof that no action occurred.
    if not actions.get('complete'):
        for p in prices.values():p['verified']=False
    verified_limits=store.get_state('spot_verified_limit_up:'+code,{}) or {}
    from price_adjustment import factor_on_date
    for day,item in verified_limits.items():
        if day in prices and isinstance(item,dict) and item.get('verified') and finite(item.get('price')):
            prices[day]['verified_limit_up']=float(item['price'])*factor_on_date(frame,day)
    unknown_limits=sum('verified_limit_up' not in p for p in prices.values())
    print(f'📐 回測價格核實｜{code}｜公司行動完整={bool(actions.get("complete"))}｜未取得核實漲停價={unknown_limits}日（不猜固定10%）',flush=True)
    prices = {d: p for d, p in prices.items() if not as_of or d <= as_of}
    if not prices:
        raise ValueError('沒有核實收盤OHLC，不能回測')
    import spot_history
    spot_history.touch(code)
    report = report if report is not None else spot_chip.build_report(code, 'full')
    cutoff = min(max(prices), report.get('latest_complete_date') or max(prices))
    saved = store.spot_stock_dates(code)
    history_start = min(saved + list(report.get('window_dates', []))) if saved or report.get('window_dates') else min(prices)
    dates = [d for d in prices if history_start <= d <= cutoff]
    complete = sorted(set(saved + list(report.get('complete_dates', []))).intersection(dates))
    rows = store.load_spot_rows(code, complete)
    from price_adjustment import SHARE_KINDS
    action_dates = {str(e.get('date', ''))[:10] for e in (bundle.get('corporate_actions') or {}).get('items', [])
                    if e.get('kind') in SHARE_KINDS or e.get('kind') in ('合併', '減資')}
    result = {'stock_code': code, 'stock_name': name, 'data_date': max(prices),
              'version': VERSION, 'spot': branch_study(dates, complete, rows, prices, branch_name, action_dates),
              'price_period_start': min(prices), 'price_period_end': max(prices),
              'price_basis': '已核實股數還原收盤OHLC；不含盤中棒；現金股利不計入報酬'}
    result['spot']['readiness']={'resolved_days':report.get('resolved_days',len(report.get('complete_dates',[]))),
        'requested_days':report.get('requested_days',70)}
    if allow_margin:
        try:
            margin = margin_study(load_margin_records(code, list(prices)), prices, action_dates)
            if margin['events']:
                result['margin'] = margin
        except Exception as exc:
            print(f'⚠️ 測試融資事件回測略過｜{code}｜{type(exc).__name__}: {exc}', flush=True)
    print(f'📊 波段規則｜{code}｜'+result['spot']['definition'],flush=True)
    for b in result['spot']['branches']:
        print(f"📊 波段與隔日反向｜{code}｜{b['branch']}｜"+str({'metrics':b['metrics']['20'],'overnight':b['overnight']}),flush=True)
    print('📊 同期參考｜'+str(result['spot']['background']),flush=True)
    return result
