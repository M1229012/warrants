"""白話交易日期／價格解析，不使用 AI，不猜測缺少的日期。"""
import re
from datetime import date, timedelta

DATE = re.compile(r'(?<!\d)(?:(20\d{2})[/\-年])?(\d{1,2})[/\-月](\d{1,2})[日號]?(?!\d)|今天|今日|昨天|昨日|前天')
VERB = re.compile(r'賣在|賣出|賣掉|出場|出清|買在|買進|買入|進場|賣|買')
NUMBER = r'(\d+(?:,\d{3})*(?:\.\d+)?)'
_REVIEW = re.compile(r'覆盤|复盘|復盤|交易回顧|回顧.{0,6}(?:交易|操作)|檢討.{0,6}(?:交易|操作|買|賣)|(?:這筆|這次|我的).{0,10}(?:操作|交易|進場).{0,8}(?:如何|怎麼|好不好|好嗎|改進|進步|優缺點)|這筆.{0,8}(?:優缺點|如何|好不好|改進|進步)|操作.{0,6}(?:哪裡錯|哪邊錯|可以更好)|買得對嗎|買錯了嗎')
_TRADE_QUESTION = re.compile(r'改善|改進|進步|更好|優缺點|哪(?:裡|邊).{0,6}(?:錯|問題)|買得.{0,4}(?:如何|怎樣|好不好)|(?:理由|這樣買|這樣操作).{0,8}(?:合理|對嗎|如何|怎麼看)|合理嗎|有沒有道理')
_CURRENT = re.compile(r'(?:現在|目前|今天|今日|後續|接下來).{0,12}(?:型態|走勢|技術|支撐|壓力|月線|均線|季線|新聞|消息|籌碼|法人|外資|股價|價格|報價|量能|成交量|還能抱|持有|該賣|要賣|要不要賣|轉強|轉弱|還站|還好嗎|還行嗎)|(?:現在|目前)?(?:型態|走勢|技術面).{0,6}(?:如何|怎麼|好不好)|還值得持有|還能抱|還能拿|要不要賣|該不該賣|賣不賣|(?:能|可以|會|何時|什麼時候).{0,3}回本|怎麼辦')
_TENTATIVE = re.compile(r'要不要|該不該|能不能|可不可以|可以|應該|想要|想|打算|準備|考慮|計畫|如果|假如|是否|還沒')


def dated_purchase(text):
    """日期必須連著交易動詞，普通行情日期不算個人交易。"""
    scope = re.split(r'理由|因為|原因', text, maxsplit=1)[0]
    for hit in DATE.finditer(scope):
        before, after = scope[max(0,hit.start()-16):hit.start()], scope[hit.end():hit.end()+14]
        if (re.search(r'(?:買在|買進|買入|進場)\s*$',before) or
                re.match(r'\s*(?:我)?\s*(?:買的|買進|買入|買了|買)',after)):
            if _TENTATIVE.search(before):
                continue
            if not re.search(r'外資|投信|自營|法人|分點|主力',before) or '我' in before:
                return True
    return False


def intent(text):
    """先判斷使用者要回顧交易，還是看目前行情；不因出現日期就覆盤。"""
    if re.search(r'(?:不要|不用|不需要|別)(?:做|幫我)?(?:覆盤|复盘|復盤|回顧)|覆盤(?:晚點|之後|以後|下次|改天|稍後)',text):
        return 'current'
    if _REVIEW.search(text):
        return 'review'
    # 個人已發生的交易＋檢討問題優先；背景中的『目前』不是當前型態提問。
    personal_trade = dated_purchase(text) or bool(re.search(r'(?:我|本人).{0,16}(?:買在|買進|買入|買了|買的|進場)|(?:買在|成本|均價)\s*\d', text))
    if personal_trade and _TRADE_QUESTION.search(text):
        return 'review'
    if _CURRENT.search(text):
        return 'current'
    # 10-04：「友達我9/1買的，現在這樣要注意什麼」問的是接下來，不是檢討交易（會員會被擋在覆盤）
    if re.search(r'注意什麼|要注意|怎麼看|接下來|之後|還能|還可以|要不要|該不該|該怎麼|怎麼辦|會不會|風險|支撐|壓力|抱|續抱|現在', text):
        return 'current'
    if dated_purchase(text):
        return 'review'
    # 已完成的買賣／操作檢討，即使沒有日期或「覆盤」二字，也屬於交易回顧。
    if re.search(r'買在|買進|買入|成本',text) and re.search(r'賣在|已(?:經)?賣|賣掉了|賣出後|賣了',text) and not _TENTATIVE.search(text):
        return 'review'
    return 'current'


def clean_reason(text):
    """分開進場依據與後面的提問，保留使用者的原話。"""
    text = re.split(r'(?:你覺得|您覺得|這筆操作|這筆交易|這(?:筆|次)(?:哪裡|哪邊|怎樣|怎麼)|這個理由|這樣買|這樣操作|這樣的理由|理由合理嗎|合理嗎|合理不合理|對不對|有沒有道理|好不好|怎樣可以|如何可以|可以怎麼|有什麼可以|後續|接下來|請問|請幫我|幫我看|該怎麼|哪邊可以|哪(?:裡|邊).{0,6}(?:改進|改善|進步|更好)|能怎麼改|買得怎樣)', text, maxsplit=1)[0]
    # 問句是請求，不是進場動機；按句界分離，而不是把每一種問法加入題庫。
    clauses=re.split(r'[，,。；;\n]',str(text or ''))
    kept=[]
    for clause in clauses:
        if re.search(r'[？?]|什麼|怎麼|如何|能否|是否|嗎',clause):break
        kept.append(clause)
    text='，'.join(kept)
    text = re.sub(r'^(?:我\s*|看到\s*|因為\s*|由於\s*)+', '', text)
    text = re.sub(r'^(?:買在|買進|買入|進場|買的|買了|買(?=$|[\s，,。]))\s*', '', text)
    return text.strip(' ，,。；;：:')


def extract(text, code, today):
    result = dict(buy_date=None, sell_date=None, price=None, sell_price=None, parse_error='')
    hits = list(DATE.finditer(text))
    for i, hit in enumerate(hits):
        if hit.group(2):
            year, month, day = hit.group(1), int(hit.group(2)), int(hit.group(3))
            try:
                value = date(int(year) if year else today.year, month, day)
                if not year and value > today:
                    value = date(today.year-1, month, day)
            except ValueError:
                result['parse_error'] = f'「{hit.group()}」不是有效日期，請確認一下。'
                continue
        else:
            value = today-timedelta(days={'昨天':1,'昨日':1,'前天':2}.get(hit.group(),0))
        before = text[hits[i-1].end() if i else 0:hit.start()]
        after = text[hit.end():hits[i+1].start() if i+1<len(hits) else len(text)]
        preceding = re.search(r'(賣在|賣出|賣掉|出場|出清|買在|買進|買入|進場|賣|買)\s*$', before)
        if i and preceding and before.strip() == preceding.group(1):
            preceding = None  # 「9/30買進 9/1賣出」的買進屬於上一個日期。
        following = VERB.search(after)
        verb = preceding.group(1) if preceding else following.group() if following else ''
        if re.search(r'賣|出場|出清',verb) and _TENTATIVE.search(before[-10:]+after[:following.start() if following else 0]):
            continue  # 「今天要不要賣」只是提問，不是已賣出。
        if not verb and hit.group(2) is None:
            continue  # 「今天型態如何」不會變成第二筆交易日期。
        sell = bool(re.search(r'賣|出場|出清',verb)) if verb else result['buy_date'] is not None
        result['sell_date' if sell else 'buy_date'] = value
    # 日期先移除，9/1 不會被誤判成買價 9 元。
    prices = DATE.sub(' ',text)
    for key, words in [('price',r'買在|買進價|買進|買入|成本|均價|進場|買'),
                       ('sell_price',r'賣在|賣出價|賣出|賣掉|出場|出清|賣')]:
        hit = re.search(r'(?:'+words+r')\s*'+NUMBER+r'(?![\d.,]|\s*張)', prices)
        if not hit:
            hit = re.search(r'(?<![\d.,])'+NUMBER+r'\s*(?:元)?\s*(?:'+words+r')',prices)
            if hit and hit.group(1).replace(',','') == code:
                hit = None
        if hit:
            result[key] = float(hit.group(1).replace(',',''))
            if result[key] <= 0:
                result['parse_error'] = '買賣價格必須大於 0，請確認一下。'
    result['closed'] = bool(result['sell_date'] or result['sell_price'] or
                            (re.search(r'已(?:經)?賣|賣掉|出清|(?:今天|今日|昨天|昨日|前天)\s*賣',text) and not _TENTATIVE.search(text)))
    if result['sell_date'] and result['buy_date'] and result['sell_date'] < result['buy_date']:
        result['parse_error'] = '賣出日早於買進日，請確認買賣日期。'
    if any(result[key] and result[key] > today for key in ('buy_date','sell_date')):
        result['parse_error'] = '交易日期在未來，請確認年份或日期。'
    return result
