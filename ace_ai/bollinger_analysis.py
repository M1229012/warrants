"""Observable Bollinger states; thresholds below are project rules, not predictions.

Bands are supplied by the report's 20-period mean +/- 2 sample standard deviations.
No prices are inferred and missing history is kept unknown rather than false.
"""
from __future__ import annotations

import math
import pandas as pd


def value(number, digits=4):
    try:
        number = float(number)
        return round(number, digits) if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


RULES = {
    'squeeze': '本日帶寬不高於前60個有效交易日帶寬的20百分位；不足60日不判斷',
    'width_trend': '帶寬相對5個交易日前增加至少10%為擴張、減少至少10%為收窄',
    'sideways': '近10日收盤皆在當日軌道內、中軌變化絕對值不超過1%、收盤高低差不超過平均收盤6%、且帶寬未擴張',
    'band_walk': '連續3日%b至少80%且中軌5日上升為沿上軌；%b至多20%且中軌下降為沿下軌',
    'breakout': '收盤從軌道內跨到軌道外；影線穿越但收盤未站出僅為觸及；前日軌外、本日回到軌內記為重返通道',
    'squeeze_breakout': '近5個交易日曾符合壓縮條件，且本日首次收盤跨越上軌或下軌',
}


def analyze_bollinger(df: pd.DataFrame) -> dict:
    result = {
        'upper': None, 'mid': None, 'lower': None, 'width': None,
        'width_pct_of_mid': None, 'percent_b': None, 'position': '資料不足',
        'breakout_up': None, 'breakout_down': None, 'return_inside': None,
        'squeeze': None, 'squeeze_threshold_pct': None, 'squeeze_reference_count': 0,
        'width_change_5d_pct': None, 'width_trend': '資料不足', 'sideways': None,
        'mid_change_10d_pct': None, 'close_range_10d_pct': None,
        'band_walk': '資料不足', 'squeeze_breakout': None,
        'volume_ratio_prior20': None, 'signals': [], 'rules': RULES,
        'note': '觸軌、突破與壓縮為條件觀察，不代表必然反轉或突破方向。',
    }
    columns = ['Close', 'BB_UPPER', 'BB_MID', 'BB_LOWER']
    if df is None or df.empty or any(c not in df for c in columns):
        result['signals'] = ['布林資料不足']
        return result
    data = df.sort_index().copy()
    for c in columns + ['High', 'Low', 'Volume']:
        if c in data:
            data[c] = pd.to_numeric(data[c], errors='coerce').replace([float('inf'), -float('inf')], float('nan'))
    valid = data[columns].notna().all(axis=1) & (data.BB_MID > 0) & (data.BB_UPPER >= data.BB_MID) & (data.BB_MID >= data.BB_LOWER)
    if not valid.iloc[-1]:
        result['signals'] = ['布林資料不足']
        return result
    latest = data.iloc[-1]
    close, upper, mid, lower = [float(latest[c]) for c in columns]
    width = (data.BB_UPPER-data.BB_LOWER).where(valid)
    bandwidth = (width / data.BB_MID * 100).where(valid)
    percent_b = ((data.Close-data.BB_LOWER)/width.where(width > 0)*100).where(valid)
    result.update(upper=value(upper), mid=value(mid), lower=value(lower), width=value(upper-lower),
                  width_pct_of_mid=value(bandwidth.iloc[-1]), percent_b=value(percent_b.iloc[-1]))
    result['position'] = ('收盤位於上軌外' if close > upper else '收盤位於下軌外' if close < lower else
                          '位於中軌與上軌之間' if close >= mid else '位於下軌與中軌之間')
    signals = result['signals']
    if len(data) >= 2 and valid.iloc[-2]:
        prev = data.iloc[-2]
        up = close > upper and prev.Close <= prev.BB_UPPER
        down = close < lower and prev.Close >= prev.BB_LOWER
        returned = lower <= close <= upper and (prev.Close > prev.BB_UPPER or prev.Close < prev.BB_LOWER)
        result.update(breakout_up=bool(up), breakout_down=bool(down), return_inside=bool(returned))
        if up:
            signals.append('收盤向上突破上軌')
        elif down:
            signals.append('收盤向下跌破下軌')
        elif returned:
            signals.append('前日軌外，本日重返通道')
        elif close > upper:
            signals.append('收盤持續位於上軌外')
        elif close < lower:
            signals.append('收盤持續位於下軌外')
    if lower <= close <= upper:
        if value(latest.get('High')) is not None and latest.High > upper:
            signals.append('盤中穿越上軌，收盤未站上')
        if value(latest.get('Low')) is not None and latest.Low < lower:
            signals.append('盤中穿越下軌，收盤已收回')
    previous_widths = bandwidth.iloc[:-1].dropna().tail(60)
    result['squeeze_reference_count'] = len(previous_widths)
    if len(previous_widths) == 60:
        threshold = previous_widths.quantile(.20)
        result['squeeze_threshold_pct'] = value(threshold)
        result['squeeze'] = bool(bandwidth.iloc[-1] <= threshold)
        if result['squeeze']:
            signals.append('布林壓縮：帶寬位於近期低檔')
    else:
        signals.append('歷史不足60個有效帶寬，壓縮未判定')
    if len(data) >= 6 and pd.notna(bandwidth.iloc[-6]):
        past = bandwidth.iloc[-6]
        if past > 0:
            change = (bandwidth.iloc[-1]/past-1)*100
            result['width_change_5d_pct'] = value(change)
            result['width_trend'] = '擴張' if change >= 10 else '收窄' if change <= -10 else '持平'
        elif bandwidth.iloc[-1] > 0:
            result['width_trend'] = '擴張'
        else:
            result['width_trend'] = '持平'
        signals.append('布林帶寬' + result['width_trend'])
    if len(data) >= 10 and valid.tail(10).all() and result['width_trend'] != '資料不足':
        window = data.tail(10)
        mid_change = (mid/window.BB_MID.iloc[0]-1)*100
        range_pct = (window.Close.max()-window.Close.min())/window.Close.mean()*100 if window.Close.mean() > 0 else float('inf')
        inside = ((window.Close <= window.BB_UPPER) & (window.Close >= window.BB_LOWER)).all()
        result.update(mid_change_10d_pct=value(mid_change), close_range_10d_pct=value(range_pct),
                      sideways=bool(inside and abs(mid_change) <= 1 and range_pct <= 6 and result['width_trend'] != '擴張'))
        if result['sideways']:
            signals.append('符合橫盤整理條件')
    if len(data) >= 6 and valid.tail(6).all() and percent_b.tail(3).notna().all():
        mid_rising = mid > data.BB_MID.iloc[-6]
        mid_falling = mid < data.BB_MID.iloc[-6]
        result['band_walk'] = ('沿上軌' if (percent_b.tail(3) >= 80).all() and mid_rising else
                               '沿下軌' if (percent_b.tail(3) <= 20).all() and mid_falling else '未符合沿軌條件')
        if result['band_walk'] in ('沿上軌', '沿下軌'):
            signals.append(result['band_walk'])
    if result['breakout_up'] or result['breakout_down']:
        recent_squeeze = []
        for pos in range(max(0, len(data)-6), len(data)-1):
            history = bandwidth.iloc[:pos].dropna().tail(60)
            if len(history) == 60 and pd.notna(bandwidth.iloc[pos]):
                recent_squeeze.append(bool(bandwidth.iloc[pos] <= history.quantile(.20)))
        if any(recent_squeeze):
            result['squeeze_breakout'] = '壓縮後向上突破' if result['breakout_up'] else '壓縮後向下跌破'
            signals.append(result['squeeze_breakout'])
        elif len(recent_squeeze) == 5:
            result['squeeze_breakout'] = '近5日無壓縮條件'
    if 'Volume' in data and len(data) >= 21:
        past_volume = data.Volume.iloc[-21:-1]
        if past_volume.notna().all() and (past_volume >= 0).all() and past_volume.mean() > 0 and value(latest.Volume) is not None and latest.Volume >= 0:
            result['volume_ratio_prior20'] = value(latest.Volume/past_volume.mean())
    if not signals:
        signals.append(result['position'])
    return result
