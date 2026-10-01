"""保留股票與持倉資訊；幽默語氣由同一次語言模型解讀。"""
import re

_HOLDING = re.compile(r'持有|續抱|能抱|還抱|繼續抱|要不要抱|抱著|值得抱|能拿|還拿|繼續拿|拿著|能留|留著|要賣|該賣|要不要賣|該不該賣|能不能賣|賣了嗎|賣掉嗎|賣不賣|要砍|該砍|停損|停利|出場|加碼|減碼|套牢|賠錢|虧錢|虧損|回本|買錯|怎麼辦|怎樣辦|撐得住|能撐|砍掉|認賠')
_PRICE = r'-?\d+(?:,\d{3})*(?:\.\d+)?'
COST_RE = re.compile(r'(?:成本價?|均價|買在|買進價|進場價|套在|接在|入手價|買入價|買入價格)\s*(?:在|是|為|約|大約|大概|大概是|差不多|約莫)?\s*('
                     +_PRICE+r')(?![\d,./%％]|\s*張)\s*(?:元|塊)?'
                     r'|(?<![\d,./])('+_PRICE+r')(?![\d,./%％])\s*(?:元|塊)?\s*(?:買的|買進的|買入的|入手的|接的)')


def wants_analysis(question):
    return bool(_HOLDING.search(question) or COST_RE.search(question))


def holding_question(question):
    return bool(_HOLDING.search(question) or COST_RE.search(question))


def prepare(question, names):
    subjects = [(code, name) for code, name in names.items()
                if re.fullmatch(r'\d{4,6}[A-Z]?', code) and
                ((len(name) >= 2 and name in question) or
                 re.search(r'(?<![0-9A-Z])'+re.escape(code)+r'(?![0-9A-Z])', question))]
    # 多檔比較交給原有流程；不能為了接梗猜一檔股票。
    if len(subjects) != 1:
        return None
    # 語氣交給同一次AI解讀依原句產生，不再注入固定開場或泛用分析問句。
    return question, None


def ensure_inline_humor(question, card):
    """舊呼叫相容：保留模型原文，不再用關鍵字注入固定笑話。"""
    return card


def holding_reply(question, results):
    """以原始工具結果交代持倉處境，不猜日期、持有期間或使用者的計畫。"""
    import math
    if not _HOLDING.search(str(question)):
        return ''
    contexts=[r.data for r in results if r.ok and getattr(r,'name','')=='get_cost_position_context']
    if len(contexts)!=1:
        return ''
    data=contexts[0]
    try:
        cost,close=float(data['cost_price']),float(data['close'])
    except (KeyError,TypeError,ValueError):
        return ''
    if not all(math.isfinite(v) and v>0 for v in (cost,close)):
        return ''
    pnl=(close/cost-1)*100
    situation=('帳面虧損' if pnl<0 else '帳面獲利' if pnl>0 else '接近成本')
    lead=f'以你提供的成本 {cost:g} 元和本次行情 {close:g} 元計算，{situation}'
    if pnl:lead+=f'約 {abs(pnl):.2f}%。'
    else:lead+='。'
    if pnl<0:
        lead+='先核對持有理由是否失效、是否超出原先可承受的虧損；不能只等回本。'
    elif pnl>0:
        lead+='先核對持有理由是否仍成立，以及能承受多少獲利回吐；獲利不代表風險已消失。'
    else:
        lead+='接近成本不代表安全，仍要檢查持有理由和能承受的波動。'
    return lead


def integrate_holding_reply(question,card,results):
    lead=holding_reply(question,results)
    if not lead:return card
    answer=str(card.get('answer') or '')
    return dict(card,answer=answer+lead if card.get('response_style')=='playful' else lead+answer)
