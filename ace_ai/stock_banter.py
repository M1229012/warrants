"""保留股票與持倉資訊；幽默語氣由同一次語言模型解讀。"""
import re

_HOLDING = re.compile(r'持有|續抱|能抱|還抱|繼續抱|要不要抱|抱著|值得抱|能拿|還拿|繼續拿|拿著|能留|留著|要賣|該賣|要不要賣|該不該賣|能不能賣|賣了嗎|賣掉嗎|賣不賣|要砍|該砍|停損|停利|出場|加碼|減碼|套牢|賠錢|虧錢|虧損|回本|買錯|怎麼辦|怎樣辦|撐得住|能撐|砍掉|認賠')
_PRICE = r'-?\d+(?:,\d{3})*(?:\.\d+)?'
COST_RE = re.compile(r'(?:成本價?|均價|買在|買進價|進場價|套在|接在|入手價|買入價|買入價格)\s*(?:在|是|為|約|大約|大概|大概是|差不多|約莫)?\s*('
                     +_PRICE+r')(?![\d,./%％]|\s*張)\s*(?:元|塊)?'
                     r'|(?<![\d,./])('+_PRICE+r')(?![\d,./%％])\s*(?:元|塊)?\s*(?:買的|買進的|買入的|入手的|接的)')


# 一般分析與交易回顧共用；教理解原則，不按用詞匹配或選擇固定答案。
RESPONSE_LANGUAGE_RULES = """理解與語氣（優先於制式開場與篇幅要求）：先依完整原句、台灣口語／台語借音、錯字與股票語境理解真正的問題；不要依固定關鍵字、單字聯想或題庫作答。
需求不限問句：陳述持股、成本或困境也可能在尋求協助，不以問號或疑問詞作門檻。像「我買在某價，現在套住了」應先回應持倉處境與風險，說明現有支撐、壓力和轉弱條件；不只是報型態。單純型態或行情陳述仍回答行情，不捏造持倉；成本、日期、部位或計畫缺少時不推測，沒有問號也不用要求重問。
教學例：「牙起來」在這個股票問句是借用突然發作／發飆，問股價是否突然動起來，不是牙齒或咬勁；這是語意示例，不是指定回覆，也不能把其他玩笑全套成同一解釋。
判斷response_style：serious認真分析；playful輕鬆玩笑；concerned擔憂。先理解整句原意，再自然接話；能自然延續原意才在humor_opening現寫最多一句短話，不得只抓單字硬湊無關聯想。即使playful，接不自然也可留空並用輕鬆口吻直接分析，不強迫搞笑；serious、concerned留空，不嘲笑虧損、不淡化困境。不使用固定笑話模板。
humor_opening不報行情數字、不承諾漲跌、不給買賣指令；不要用「我不知道／無法預測」當接話。問明天不代表要拒答：不禁止語言上的幽默，但分析只談有資料依據的目前條件。正文直接回答原意，不重複接話；程式合併成同一段解讀。
在本次輸出內自檢：答到真正問題？接話貼合整句且不牽強？數字有依據？未提供的理由／計畫是否被誤寫成沒有做？若有問題先修正，不另呼叫模型。"""


NATURAL_ANALYSIS_RULES = """所有AI解讀的寫作原則：依這次原句與證據現寫，不靠預寫答案或換股名、價格套句。先找出這題最重要的疑問或矛盾，挑真正影響答案的證據；不要每題固定均線→支撐→布林，也不要為變化而改變事實或刻意換同義詞。
在本次資料內綜合股票位階與近期變化、支撐壓力距離、成本相對現價、使用者表達的情緒和提問目的，決定先解釋什麼。底部、突破、高檔與回檔的風險不同，但位階與型態只能引用已核實資料；不能僅因某個詞就選固定答案。問型態聚焦結構；問防守解釋相關價位失守的意義；問操作改善回看當時證據；玩笑自然接原意；焦慮分析造成擔憂的實際變化。只有使用者明示或資料能支持的背景才使用。
持倉焦慮要區分帳面虧損、獲利回吐與短線整理，處境依成本與行情確認，不替人推測動機。用當下的具體變化解釋擔心是否有依據；不要固定以「以你提供的成本…計算」「先核對持有理由」「能承受多少獲利回吐」「感到慌張是正常的」開場。不重複朗讀圖上成本與報酬；必要數字才引用，未提供計畫不能說當時沒做。
解讀可短可長，理由、觀察條件、心得與總結依需求選擇，不強制兩種情境或三條心得。沒有新增資訊的欄位留空；若需要觀察條件，只寫真正相關的1至2項與意義，不硬湊多空各一項。總結若只重複開頭就留空。保留指定資料格式、事實核對與專項資料規則，不增加模型呼叫；無關的提醒省略，不杜撰行情、不給個人買賣決定。"""


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


def holding_reply(question, results, *, semantic_holding=False):
    """以原始工具結果交代持倉處境，不猜日期、持有期間或使用者的計畫。"""
    import math
    if not (semantic_holding or holding_question(str(question))):
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
    """相容舊呼叫；正常AI輸出不再注入持倉模板，失敗備援才用holding_reply。"""
    return card
