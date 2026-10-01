"""常見股票玩梗用規則接住，不另外呼叫語言模型。"""
import re

_SLANG = re.compile(r'豁達|亞起來|要噴|噴起來|起飛|帶我飛|帶飛|衝什麼|沒力|有點抖|抖爆|洗盤|真的弱|救救我|住套房|套房|船票|韭菜|火箭|睡公園|吃土|財富自由|人生翻身|退休|芭比Q|丸子')
_HOLDING = re.compile(r'持有|續抱|能抱|還抱|繼續抱|要不要抱|抱著|值得抱|能拿|還拿|繼續拿|拿著|能留|留著|要賣|該賣|要不要賣|該不該賣|能不能賣|賣了嗎|賣掉嗎|賣不賣|要砍|該砍|停損|停利|出場|加碼|減碼|套牢|賠錢|虧錢|虧損|回本|買錯|怎麼辦|怎樣辦|撐得住|能撐|砍掉|認賠')
_PRICE = r'-?\d+(?:,\d{3})*(?:\.\d+)?'
COST_RE = re.compile(r'(?:成本價?|均價|買在|買進價|進場價|套在|接在|入手價|買入價|買入價格)\s*(?:在|是|為|約|大約|大概|大概是|差不多|約莫)?\s*('
                     +_PRICE+r')(?![\d,./%％]|\s*張)\s*(?:元|塊)?'
                     r'|(?<![\d,./])('+_PRICE+r')(?![\d,./%％])\s*(?:元|塊)?\s*(?:買的|買進的|買入的|入手的|接的)')


def wants_analysis(question):
    return bool(_SLANG.search(question) or _HOLDING.search(question) or COST_RE.search(question))


def holding_question(question):
    return bool(_HOLDING.search(question) or COST_RE.search(question))


def prepare(question, names):
    if not _SLANG.search(question):
        return None
    subjects = [(code, name) for code, name in names.items()
                if re.fullmatch(r'\d{4,6}[A-Z]?', code) and
                ((len(name) >= 2 and name in question) or
                 re.search(r'(?<![0-9A-Z])'+re.escape(code)+r'(?![0-9A-Z])', question))]
    # 多檔比較交給原有流程；不能為了接梗猜一檔股票。
    if len(subjects) != 1:
        return None
    # 語氣交給同一次AI解讀依原句產生，不再注入固定開場或泛用分析問句。
    return question, None
