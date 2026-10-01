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


# 模型忽略接梗時的短句補救；只接語氣，不添加行情事實或交易指令。
_REPLIES = [
    (r'亞起來', '亞起來靠量價，靠喊聲只會啞起來。'),
    (r'豁達', '股名可以豁達，持股心情還得看走勢。'),
    (r'套房|住套', '套房先別裝潢，先看看結構有沒有變。'),
    (r'船票', '船票有了，航向還得看量價。'),
    (r'起飛|帶我飛|帶飛|火箭', '起飛先看量價，股名可不是登機證。'),
    (r'要噴|噴起來', '噴不噴先看量價，喊聲不是燃料。'),
    (r'吃土|睡公園', '先把公園露營計畫收起來，回頭看結構。'),
    (r'財富自由|人生翻身|退休', '退休計畫先別交給一檔股票，先看走勢。'),
    (r'韭菜', '先不急著替自己貼韭菜標籤，看看依據。'),
    (r'芭比Q', '先別宣布芭比Q，讓量價把話說完。'),
    (r'丸子', '先別急著喊丸子，讓量價把話說完。'),
]


def humor_reply(question):
    for pattern,reply in _REPLIES:
        if re.search(pattern,str(question)):
            return reply
    return ''


def ensure_inline_humor(question, card):
    reply=humor_reply(question)
    if not reply:
        return card
    answer=str(card.get('answer') or '')
    if not answer:
        return card
    first=re.split(r'[。！？!?]',answer,1)[0]
    # 保留模型已生成的比喻／接梗，不能把一般技術敘述中的股票名字當成幽默。
    playful=re.search(r'喊聲|啞起來|豁達.*(?:心情|股名|人生)|裝潢|房東|登機|燃料|不是.*(?:票|證)|露營|退休計畫|芭比Q|韭菜標籤',first)
    if playful:
        return card
    return dict(card,answer=reply+answer)
