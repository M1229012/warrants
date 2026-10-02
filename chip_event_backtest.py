"""Test-only observed branch accumulation and margin-event outcome study.

No score, no actual broker/account P&L. All prices must be verified, adjusted,
closed OHLC supplied by the existing price loader. Missing is never zero.
"""
import math
import os
import time
from collections import defaultdict

HORIZONS = (5, 10, 20)
VERSION = 'chip-events-compact-v2'


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def price_rows(frame):
    rows = {}
    for index, row in frame.iterrows():
        values = {k.lower(): finite(row.get(k)) for k in ('Open', 'High', 'Low', 'Close')}
        if all(v is not None and v > 0 for v in values.values()):
            rows[str(index)[:10]] = values
    return dict(sorted(rows.items()))


def outcome(prices, signal, horizon):
    """Known-at-close signal, next open entry, H-th session close exit."""
    dates = list(prices)
    if signal not in prices:
        return None
    i = dates.index(signal)
    if i + horizon >= len(dates):
        return None
    path = [prices[d] for d in dates[i + 1:i + horizon + 1]]
    entry = path[0]['open']
    peak, worst = entry, 0.0
    # The peak uses only entry/prior closes; no assumption about intraday H/L order.
    for bar in path:
        worst = min(worst, (bar['low'] / peak - 1) * 100)
        peak = max(peak, bar['close'])
    return {'return_pct': (path[-1]['close'] / entry - 1) * 100,
            'drawdown_pct': worst, 'signal_date': signal,
            'entry_date': dates[i + 1], 'exit_date': dates[i + horizon]}


def summarize(prices, signals, background_dates):
    output = {}
    for h in HORIZONS:
        mature = [v for d in signals if (v := outcome(prices, d, h)) is not None]
        background = [v['return_pct'] for d in background_dates if (v := outcome(prices, d, h)) is not None]
        n = len(mature)
        avg = sum(v['return_pct'] for v in mature) / n if n else None
        baseline = sum(background) / len(background) if background else None
        output[str(h)] = {
            'samples': n, 'pending': len(signals) - n,
            'avg_return_pct': round(avg, 2) if avg is not None else None,
            'reach_3_pct': round(sum(v['return_pct'] >= 3 - 1e-9 for v in mature) / n * 100, 1) if n else None,
            'reach_5_pct': round(sum(v['return_pct'] >= 5 - 1e-9 for v in mature) / n * 100, 1) if n else None,
            'worst_drawdown_pct': round(min(v['drawdown_pct'] for v in mature), 2) if n else None,
            'avg_drawdown_pct': round(sum(v['drawdown_pct'] for v in mature) / n, 2) if n else None,
            'stock_background_avg_pct': round(baseline, 2) if baseline is not None else None,
            'background_samples': len(background),
            'excess_return_pct': round(avg - baseline, 2) if avg is not None and baseline is not None else None,
            'small_sample': n < 10,
            'recent_events': mature[-3:],
        }
    return output


def branch_study(dates, complete_dates, rows, prices, branch_name='', action_dates=()):
    """Study all observed branches first; select accumulation/history independently."""
    dates = sorted(set(dates))
    complete = set(complete_dates)
    daily = defaultdict(dict)
    for row in rows:
        net = finite(row.get('net'))
        if net is not None and row.get('date') in complete:
            daily[row['date']][row['branch_name']] = net
    observation = dates[-70:]
    actions = [d for d in action_dates if observation and observation[0] <= d <= observation[-1]]
    comparable = [d for d in observation if not actions or d >= max(actions)]
    recent = comparable[-20:]
    window = dates[-5:]

    def signal(branch, i):
        w = dates[i-4:i+1]
        if i < 4 or len(w) != 5 or not set(w).issubset(complete) or dates[i] not in prices or set(w).intersection(action_dates):
            return False
        values = [daily[d].get(branch) for d in w]
        return (sum(v is not None and v > 0 for v in values) >= 3
                and sum(v for v in values if v is not None) > 0)

    background = [d for i,d in enumerate(dates) if i>=4 and set(dates[i-4:i+1]).issubset(complete)
                  and not set(dates[i-4:i+1]).intersection(action_dates)]
    price_index = {d:i for i,d in enumerate(prices)}
    names = [branch_name] if branch_name else sorted({b for values in daily.values() for b in values})
    studied = []
    for b in names:
        events, last = [], -1000
        for i,day in enumerate(dates):
            if signal(b,i) and price_index[day]-last >= 20:
                events.append(day);last=price_index[day]
        records = [{'date':d,'net':daily[d][b],'reference_close':prices.get(d,{}).get('close'),
                    'action':'buy' if daily[d][b]>0 else 'sell' if daily[d][b]<0 else 'flat'}
                   for d in dates if b in daily[d]]
        buy_refs = [(prices[d]['close'],daily[d][b]) for d in comparable if d in prices and daily[d].get(b,0)>0]
        cost = sum(p*n for p,n in buy_refs)/sum(n for _,n in buy_refs) if buy_refs else None
        latest = daily[dates[-1]].get(b) if dates else None
        active = signal(b,len(dates)-1) if dates else False
        net5 = sum(daily[d].get(b,0) for d in window)
        if latest is not None and latest<0:
            status='累積・調節' if active else '近期調節'
        elif net5<0:
            status='近期淨賣超'
        elif active:
            status='持續買進' if latest is not None and latest>0 else '累積・末日未上榜'
        else:
            status='近期未符合累積條件'
        metrics = summarize(prices,events,background)
        studied.append({'branch':b,'active_now':active,'status':status,'latest_net':latest,
            'buy_days_5':sum(daily[d].get(b,0)>0 for d in window),
            'observed_net_5':round(net5,1),
            'observed_net_20':round(sum(daily[d].get(b,0) for d in recent),1),
            'observed_net_70':round(sum(daily[d].get(b,0) for d in comparable),1),
            'buy_days_70':sum(daily[d].get(b,0)>0 for d in observation),
            'estimated_buy_cost':round(cost,2) if cost is not None else None,
            'estimated_return_pct':round((prices[max(prices)]['close']/cost-1)*100,2) if cost else None,
            'cost_basis':'近70日上榜買超日、核實還原收盤價按正淨買超加權；股數調整前排除，不是持股庫存成本',
            'signal_dates':events,'metrics':metrics,'records':records,'selection_reasons':[]})
    if branch_name:
        selected = studied
        for b in selected:b['selection_reasons']=['指定分點']
    else:
        current = sorted((b for b in studied if b['active_now']),key=lambda b:(-b['observed_net_70'],b['branch']))[:3]
        minimum=max(3,int(os.getenv('TEST_BRANCH_HISTORY_MIN_SAMPLES','3')))
        historical=sorted((b for b in studied if b['metrics']['20']['samples']>=minimum
            and b['metrics']['20']['avg_return_pct'] is not None and b['metrics']['20']['avg_return_pct']>0),
            key=lambda b:(-b['metrics']['20']['reach_3_pct'],-b['metrics']['20']['avg_return_pct'],
                          -b['metrics']['20']['samples'],-b['metrics']['20']['worst_drawdown_pct'],b['branch']))[:2]
        selected=list(current)
        for b in current:b['selection_reasons'].append('近期累積')
        for b in historical:
            b['selection_reasons'].append('歷史表現')
            if b not in selected:selected.append(b)
    return {'available':bool(dates),'period_start':observation[0] if observation else '',
        'period_end':observation[-1] if observation else '',
        'history_start':dates[0] if dates else '', 'history_end':dates[-1] if dates else '',
        'complete_days':len(complete.intersection(observation)),'requested_days':len(observation),
        'history_complete_days':len(complete.intersection(dates)), 'branches':selected,
        'compared_branches':len(studied),'share_adjusted_window':bool(actions),
        'definition':'近5日至少3日上榜買超、觀察累積淨買超為正，末日可調節；事件間隔20個交易日。歷史表現組獨立篩選、不要求近期買超；至少3筆且平均報酬為正，再依20日達3%比例、報酬與樣本比較，少於10筆屬小樣本。未上榜不是零，股數調整前張數與估價排除。現在篩選再回看歷史有選樣偏差，並非已驗證策略。'}


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
    return {'type': 'table', 'columns': ['期間', '樣本', '平均報酬', '達3%', '達5%', '最大回撤', '同股背景'],
            'widths': [.08, .07, .18, .13, .13, .19, .22], 'signed': ('平均報酬', '最大回撤', '同股背景'),
            'rows': [[f'{h}日', str(m['samples']), pct(m['avg_return_pct'], True), pct(m['reach_3_pct']),
                      pct(m['reach_5_pct']), pct(m['worst_drawdown_pct'], True), pct(m['stock_background_avg_pct'], True)]
                     for h in HORIZONS for m in [metrics[str(h)]]]}


def _margin_sections(payload, detailed=False):
    data=payload.get('margin')
    if not data or not data.get('events'):return []
    m=data['metrics']['20'];event=data['events'][-1]
    return [{'type':'heading','text':'大額融資淨增｜管理員'},
        {'type':'table','columns':['最近事件','樣本','達3%','達5%','平均報酬','最大回撤'],
         'widths':[.27,.09,.16,.16,.16,.16],'accent':('達3%','達5%'),'signed':('平均報酬','最大回撤'),
         'rows':[[event['date'][5:]+f" 淨增 {event['balance_increase_pct']:+.1f}%",str(m['samples']),
                 pct(m['reach_3_pct']),pct(m['reach_5_pct']),pct(m['avg_return_pct'],True),pct(m['worst_drawdown_pct'],True)]]}]


def branch_card(payload, detailed=False, page=1):
    data=payload['spot'];branches=data['branches']
    sections=[{'type':'badge','text':f"觀察{data['requested_days']}日 {data['period_start']}～{data['period_end']}｜完整{data['complete_days']}/{data['requested_days']}"},
              {'type':'note','text':f"回測實際資料 {data['history_start']}～{data['history_end']}｜事件後20日｜買賣超：張"}]
    rows=[]
    for b in branches:
        m=b['metrics']['20']
        cost=f"{b['estimated_buy_cost']:,.2f}" if b['estimated_buy_cost'] is not None else '—'
        label=b['branch']+'\n'+('歷史・'+b['status'] if '歷史表現' in b['selection_reasons'] else b['status'])
        rows.append([label,f"{b['observed_net_70']:+,.0f}\n{b['observed_net_5']:+,.0f}",
                     cost+'\n'+pct(b['estimated_return_pct'],True),str(m['samples']),
                     pct(m['reach_3_pct']),pct(m['reach_5_pct']),pct(m['avg_return_pct'],True),pct(m['worst_drawdown_pct'],True)])
    if rows:
        sections.append({'type':'table','columns':['分點／狀態','70日／5日淨超','估均價／報酬','樣本','達3%','達5%','平均報酬','最大回撤'],
            'widths':[.22,.17,.14,.05,.10,.10,.11,.11], 'signed':('平均報酬','最大回撤'),
            'accent':('達3%','達5%'),'rows':rows})
    else:
        sections.append({'type':'note','text':'目前沒有符合近期累積或歷史表現條件的分點，不硬湊名單。'})
    if data.get('share_adjusted_window'):
        sections.append({'type':'note','text':'期間有股數調整：淨超與估均價僅計調整後可比較資料。'})
    sections.extend(_margin_sections(payload,detailed))
    if payload.get('display_marks'):
        sections.append({'type':'note','text':'圖號在K線外側：紅色買超／融資淨增、綠色賣超／融資淨減；可問買賣點位明細對照。'})
    sections.append({'type':'note','text':'比例與平均報酬以次日開盤起算；少於10筆為小樣本。估均價非庫存成本，預估報酬非實際損益。'})
    if detailed:
        for b in branches:
            sections.append({'type':'heading','text':b['branch']+'｜各期回測'})
            sections.append(metric_table(b['metrics']))
            records=list(reversed(b['records']));start=(max(1,int(page))-1)*20
            selected=records[start:start+20]
            sections.append({'type':'heading','text':f"上榜買賣超明細｜第{page}頁，共{len(records)}筆"})
            mark_numbers={m['date']:str(m['no']) for m in payload.get('display_marks',[]) if m.get('branch')==b['branch']}
            sections.append({'type':'table','columns':['日期／圖號','方向','淨買賣超','當日還原收盤'],
                'widths':[.27,.18,.28,.27],'signed':('淨買賣超',),
                'rows':[[r['date']+(' #'+mark_numbers[r['date']] if r['date'] in mark_numbers else ''),'買超' if r['net']>0 else '賣超' if r['net']<0 else '持平',
                         f"{r['net']:+,.0f}",f"{r['reference_close']:,.2f}" if r['reference_close'] else '無價格'] for r in selected]})
            if not selected:sections.append({'type':'note','text':'此頁沒有更多已保存明細。'})
            sections.append({'type':'note','text':f"每頁20筆，可問第2頁等；最早已保存上榜日 {records[-1]['date'] if records else '無'}。未上榜不可當零；更早資料未取得。"})
        if payload.get('margin'):
            sections.append({'type':'heading','text':'融資事件｜各期回測'})
            sections.append(metric_table(payload['margin']['metrics']))
            marks=[m for m in payload.get('display_marks',[]) if m['kind']=='margin']
            if marks:
                sections.append({'type':'table','columns':['圖號','日期','融資餘額變動','淨增減張數'],
                    'widths':[.12,.28,.3,.3],'rows':[[str(m['no']),m['date'],m['label'],f"{m['value']:+,.0f}"] for m in marks]})
    return {'branch':f"{payload['stock_code']} {payload['stock_name']}",'label':'現股分點回測','tags':['70日觀察'],'sections':sections}


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
    import spot_chip
    import local_market_cache as store
    import warrant_ai_tools as tools
    with tools.quote_policy(admin_live=False):
        bundle = tools._load_price_bundle(code)
    frame = tools.closed_frame(bundle)
    prices = price_rows(frame)
    prices = {d: p for d, p in prices.items() if not as_of or d <= as_of}
    if not prices:
        raise ValueError('沒有核實收盤OHLC，不能回測')
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
    if allow_margin:
        try:
            margin = margin_study(load_margin_records(code, list(prices)), prices, action_dates)
            if margin['events']:
                result['margin'] = margin
        except Exception as exc:
            print(f'⚠️ 測試融資事件回測略過｜{code}｜{type(exc).__name__}: {exc}', flush=True)
    return result
