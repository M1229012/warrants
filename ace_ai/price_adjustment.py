"""公司行動價格基準；只使用核實參考價，不從市場跌幅猜比例。"""
import math
import pandas as pd

SHARE_KINDS = {'權','權息','除權','除權息','分割','反分割','面額變更'}


def event_from_row(row, kind):
    before = row.get('before_close') if kind == '面額變更' else row.get('before_price')
    after = row.get('after_ref_close') if kind == '面額變更' else (row.get('reference_price') or row.get('after_price'))
    try:
        before, after = float(before), float(after)
        factor = after / before if before > 0 and after > 0 else None
        if factor is not None and not math.isfinite(factor):
            factor = None
    except (TypeError, ValueError, ZeroDivisionError):
        factor = None
    # 純股數事件用參考價比率換算等值股數；權息混合不能把現金也當成股數。
    volume_factor = 1 / factor if factor and kind in SHARE_KINDS - {'權息','除權息'} else None
    return {'date':str(row.get('date'))[:10], 'kind':kind, 'factor':factor,
            'volume_factor':volume_factor, 'before_price':before, 'reference_price':after}


def adjust_shares(frame, events, as_of=None):
    out = frame.copy()
    if out.empty:
        return out
    if out.attrs.get('share_adjustments') is not None:
        validate_adjusted(out)
        return out
    applied=[]
    if events and events.get('status') == 'ok':
        seen={}
        for ev in sorted(events.get('items') or [], key=lambda e:e['date']):
            if ev.get('kind') not in SHARE_KINDS:
                continue
            day=pd.Timestamp(ev['date'])
            if day > pd.Timestamp(as_of if as_of is not None else out.index[-1]) or day <= out.index[0]:
                continue
            factor=ev.get('factor')
            if not factor or not math.isfinite(float(factor)) or factor <= 0:
                raise ValueError(f"{ev['date']} {ev['kind']}缺少有效參考價，暫停跨事件技術分析")
            if ev['date'] in seen:
                if not math.isclose(seen[ev['date']],factor,rel_tol=1e-6):
                    raise ValueError(f"{ev['date']}公司行動還原因子矛盾，暫停分析")
                continue
            seen[ev['date']]=factor
            mask=out.index < day
            for column in ['Open','High','Low','Close']:
                out[column]=out[column].astype(float)
                out.loc[mask,column] *= factor
            vf=ev.get('volume_factor')
            if vf and 'Volume' in out:
                out['Volume']=out['Volume'].astype(float)
                out.loc[mask,'Volume'] *= vf
            # 權息混合事件沒有純股數比率：成交量維持原始張數（與券商 App 一致），不清空
            applied.append(dict(ev))
    # 大幅斷層僅作資料警示，絕不從這個比例反推還原因子。
    validate_adjusted(out)
    out.attrs['share_adjustments']=applied
    out.attrs['share_adjustment_as_of']=str(pd.Timestamp(as_of if as_of is not None else out.index[-1]).date())
    return out


def validate_adjusted(frame):
    if frame.empty:
        return
    close = pd.to_numeric(frame['Close'], errors='coerce')
    if not close.map(lambda v: math.isfinite(v) and v > 0).all():
        raise ValueError('日K含無效價格，暫停覆盤與技術分析')
    ratios = close.div(close.shift())
    if ((ratios < .65) | (ratios > 1.8)).any():
        raise ValueError('日K仍有未核實的大幅價格斷層，請更新公司行動參考價後再分析')


def factor_on_date(frame, day):
    factor=1.0
    for ev in frame.attrs.get('share_adjustments') or []:
        if pd.Timestamp(day) < pd.Timestamp(ev['date']):
            factor *= ev['factor']
    return factor


def basis_note(frame):
    items=frame.attrs.get('share_adjustments') or []
    if not items:
        return ''
    names='、'.join(f"{e['date']} {e['kind']}" for e in items)
    extra='；權息事件前成交量為原始張數' if any(not e.get('volume_factor') for e in items) else '；成交量同步換算等值股數（估）'
    return f'已依核實參考價還原至最新股數基準（{names}），非當年實際成交價'+extra
