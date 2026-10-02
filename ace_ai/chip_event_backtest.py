"""Test-only observed branch accumulation and margin-event outcome study.

No score, no actual broker/account P&L. All prices must be verified, adjusted,
closed OHLC supplied by the existing price loader. Missing is never zero.
"""
import math
import os
import time
from collections import defaultdict

HORIZONS = (5, 10, 20)
VERSION = 'chip-events-v1'


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
    """Daily source is truncated; absence means not listed, not actual net=0."""
    dates = sorted(set(dates))
    complete = set(complete_dates)
    daily = defaultdict(dict)
    for row in rows:
        net = finite(row.get('net'))
        if net is not None and row.get('date') in complete:
            daily[row['date']][row['branch_name']] = net

    def signal(branch, i):
        window = dates[i - 4:i + 1]
        if i < 4 or len(window) < 5 or not set(window).issubset(complete) or dates[i] not in prices or set(window).intersection(action_dates):
            return False
        observed = [daily[d].get(branch) for d in window]
        return (sum(v is not None and v > 0 for v in observed) >= 3
                and observed[-1] is not None and observed[-1] > 0
                and sum(v for v in observed if v is not None) > 0)

    recent = dates[-20:]
    recent_actions = [d for d in action_dates if recent and recent[0] <= d <= recent[-1]]
    if recent_actions:
        recent = [d for d in recent if d >= max(recent_actions)]
    totals = defaultdict(float)
    for d in recent:
        for b, net in daily[d].items():
            totals[b] += net
    names = [branch_name] if branch_name else sorted(
        (b for b in totals if signal(b, len(dates) - 1)), key=lambda b: (-totals[b], b))[:3]
    studied = []
    background = [d for i, d in enumerate(dates) if i >= 4 and set(dates[i - 4:i + 1]).issubset(complete)
                  and not set(dates[i - 4:i + 1]).intersection(action_dates)]
    price_index = {d: i for i, d in enumerate(prices)}
    for b in names:
        events, last = [], -1000
        for i, day in enumerate(dates):
            if signal(b, i) and price_index[day] - last >= 20:
                events.append(day)
                last = price_index[day]
        if not events and not branch_name:
            continue
        window = dates[-5:]
        buys = [(prices[d]['close'], daily[d][b]) for d in recent
                if d in prices and daily[d].get(b, 0) > 0]
        cost = sum(p * n for p, n in buys) / sum(n for _, n in buys) if buys else None
        studied.append({'branch': b, 'active_now': signal(b, len(dates) - 1),
                        'buy_days_5': sum(daily[d].get(b, 0) > 0 for d in window),
                        'observed_net_5': round(sum(daily[d].get(b, 0) for d in window), 1),
                        'observed_net_20': round(totals[b], 1),
                        'estimated_buy_cost': round(cost, 2) if cost is not None else None,
                        'signal_dates': events, 'metrics': summarize(prices, events, background)})
    return {'available': bool(dates), 'period_start': dates[0] if dates else '',
            'period_end': dates[-1] if dates else '', 'complete_days': len(complete.intersection(dates)),
            'requested_days': len(dates), 'branches': studied,
            'definition': '近5日至少3日上榜買超、末日買超且觀察到的累積買超為正；同分點事件間隔至少20個股票交易日。跨股數調整的5日窗口不產生訊號；近20日估計均價與張數合計排除最近股數調整前資料。未上榜不是實際淨買超為零；這是持續買進候選，不確認分點身分、意圖或庫存。'}


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
    signals, details, background, last = [], [], [], -1000
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
        if r['delta'] > 0 and r['delta'] >= threshold and i - last >= 20:
            signals.append(day)
            details.append({'date': day, 'balance_delta': r['delta'],
                            'balance_increase_pct': round(r['delta'] / r['base'] * 100, 2)})
            last = i
    return {'events': details, 'signal_dates': signals,
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


def branch_card(payload):
    data = payload['spot']
    sections = [{'type': 'badge', 'text': f"回測範圍｜{data['period_start']}～{data['period_end']}｜完整分點日 {data['complete_days']}/{data['requested_days']}"}]
    for b in data['branches']:
        sections.extend([{'type': 'heading', 'text': b['branch'] + ('｜持續買進候選' if b['active_now'] else '｜歷史事件')},
                         {'type': 'stats', 'items': [
                             {'label': '近5日上榜買超', 'value': f"{b['buy_days_5']} 天"},
                             {'label': '近5日觀察淨買超', 'value': f"{b['observed_net_5']:+,.0f} 張", 'tone': 'signed'},
                             {'label': '近20日買進均價（估）', 'value': f"{b['estimated_buy_cost']:,.2f}" if b['estimated_buy_cost'] is not None else '—'}]},
                         metric_table(b['metrics'])])
        pending = b['metrics']['20']['pending']
        sections.append({'type': 'note', 'text': f'20日尚未走完 {pending} 筆，不納入20日統計；少於10筆屬小樣本。'})
    if not data['branches']:
        sections.append({'type': 'note', 'text': '目前沒有符合持續買進條件的分點，或最近5日資料未完整；不以其他分點充數。'})
    sections.append({'type': 'note', 'text': data['definition']})
    sections.append({'type': 'note', 'text': '訊號日收盤後才成立，次日開盤起算，5／10／20日收盤報酬達3%／5%才列入；不是期間內曾碰到的漲幅。最大回撤為各樣本持有期間最差的先前收盤高水位至日低價跌幅。目前分點由當下條件選出，再回看歷史，存在選樣偏差，不能視為已驗證策略。同股背景＝相同資料期間、不要求分點訊號的同股平均報酬，不是大盤指數或配對控制組；未扣費稅，事件期可能相互重疊，不等於分點實際交易獲利。'})
    return {'branch': f"{payload['stock_code']} {payload['stock_name']}", 'label': '現股分點回測',
            'tags': ['回測觀察'], 'sections': sections}


def margin_card(payload):
    data = payload.get('margin')
    if not data or not data.get('events'):
        return None
    sections = [{'type': 'badge', 'text': f"融資資料｜{data['period_start']}～{data['period_end']}"},
                metric_table(data['metrics']),
                {'type': 'rows', 'items': [{'lead': e['date'], 'parts': [f"餘額淨增 {e['balance_delta']:+,.0f} 張", f"增幅 {e['balance_increase_pct']:.2f}%"]}
                                         for e in data['events'][-3:]]},
                {'type': 'note', 'text': data['definition']},
                {'type': 'note', 'text': f"20日未完成 {data['metrics']['20']['pending']} 筆不計入；少於10筆屬小樣本。報酬、達標比例與回撤口徑同現股分點回測，未扣費稅。"}]
    return {'branch': f"{payload['stock_code']} {payload['stock_name']}", 'label': '大額融資淨增事件回測',
            'tags': ['管理員'], 'sections': sections}


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
    dates = [d for d in report.get('window_dates', []) if d <= cutoff]
    complete = [d for d in report.get('complete_dates', []) if d in dates]
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
