"""會員使用統計；不保存問題原文，不呼叫 AI，沿用永久市場資料庫。"""
import csv
import io
import re
from datetime import datetime, timedelta, timezone
import local_market_cache as db
import review_language


def now():
    return datetime.now(timezone(timedelta(hours=8)))


class StatsCommand:
    def __init__(self,kind,period='近30天',detail=False):
        self.kind,self.period,self.detail=kind,period,detail
    def groups(self):return self.kind,self.period


def command(question):
    text=re.sub(r'\s+','',str(question))
    if re.search(r'可以買|能買嗎|該買嗎|推薦買|怎麼買|能賣嗎',text):return None
    if re.search(r'[A-Za-z]{3,}',re.sub('CSV','',text,flags=re.I)):return None
    exact=re.fullmatch(r'(問答次數完整名單|問答次數|使用統計|股票詢問統計|熱門股票|問題類型統計)(近\d+天|本月|今日|累計)?',text)
    if exact:return StatsCommand(exact[1],exact[2] or '近30天',exact[1]=='問答次數完整名單')
    # 群組使用情形的查詢，不攔截「2330外資買超統計」這類行情問題。
    kind=None
    if re.search(r'問題類型|提問類型|問的類型|問什麼類型|型態.*(?:籌碼|權證).*(?:多少|幾次|次數|比例)',text):kind='問題類型統計'
    elif re.search(r'(?:大家|會員|群組).*(?:問|詢問).*(?:股票|哪幾檔)|(?:股票|股號|個股).*(?:問|詢問).*(?:次|多少|統計)|(?:股票|股號|個股).*(?:被問|詢問|問得|問最多|最多人問|熱門|詢問次數)|(?:問|詢問|關注).*(?:哪些股票|什麼股票|哪幾檔|哪檔最多)|熱門股票',text):kind='股票詢問統計'
    elif re.search(r'(?:誰|哪些人).*(?:問最多|用最多|最常問|最常用)|(?:問答|提問|使用|用量).*(?:排名|排行|次數|完整名單)',text):kind='問答次數'
    elif re.search(r'(?:群組|會員|大家|機器人|助手|喬巴).*(?:使用情況|使用狀況|使用統計|用量|使用人數)|使用統計',text):kind='使用統計'
    # 10-04 白話補強：「大家都問什麼問題」「哪個會員問最多次」「最常被問的股票」「熱門個股排行」等
    if not kind:
        stock_word=re.search(r'股票|個股|哪檔|哪幾檔|哪些股|哪支|哪隻',text)
        who_word=re.search(r'誰|哪個會員|哪位|哪些會員|重度使用|使用者排|會員排',text)
        rank_word=re.search(r'最多|最常|最愛|熱門|排行|排名|前\d+|前十|多少人|幾個人|次數',text)
        ask_word=re.search(r'問|詢問|查|用|使用',text)
        if re.search(r'(?:大家|會員|群組).*(?:問|查).*(?:什麼|哪些|哪類).*(?:問題|類型)',text):kind='問題類型統計'
        elif stock_word and (rank_word or re.search(r'大家|會員|群組',text)) and (ask_word or '熱門' in text):kind='股票詢問統計'
        elif who_word and (rank_word or ask_word):kind='問答次數'
        elif re.search(r'(?:多少人|幾個人|幾人).*(?:用|使用|在用)|使用人數|有人在用',text):kind='使用統計'
        elif re.search(r'排行榜|排名榜',text):kind='問答次數'
    if not kind:return None
    period='近30天'
    days=re.search(r'(?:近|最近|過去|這)(\d+)(?:天|日)',text)
    if days:period=f'近{int(days[1])}天'
    elif re.search(r'累計|全部時間|所有時間|歷來|從開始|總共',text):period='累計'
    elif re.search(r'今天|今日|當天',text):period='今日'
    elif re.search(r'本月|這個月',text):period='本月'
    elif re.search(r'這週|本週|最近一週|近一週',text):period='近7天'
    detail=bool(re.search(r'完整|全部名單|全部排行|匯出|CSV',text,re.I))
    if detail and kind=='問答次數':kind='問答次數完整名單'
    return StatsCommand(kind,period,detail)


def _init(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS member_usage (
        request_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
        day TEXT NOT NULL, ts TEXT NOT NULL, outcome TEXT NOT NULL,
        category TEXT NOT NULL, route TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_member_usage_guild_day ON member_usage(guild_id,day)")
    conn.execute("CREATE TABLE IF NOT EXISTS member_usage_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO member_usage_meta VALUES ('started',?)", (now().isoformat(timespec='seconds'),))
    conn.execute("CREATE TABLE IF NOT EXISTS member_usage_admins (guild_id TEXT,user_id TEXT,PRIMARY KEY(guild_id,user_id))")
    # AI 解讀額度用完（仍可問一般題）：每人每天一筆，用來判斷額度是不是使用量的瓶頸
    conn.execute("CREATE TABLE IF NOT EXISTS member_ai_quota_hits (user_id TEXT,day TEXT,PRIMARY KEY(user_id,day))")


    conn.execute("CREATE INDEX IF NOT EXISTS idx_member_usage_guild_outcome_day ON member_usage(guild_id,outcome,day,user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_member_usage_user_first ON member_usage(guild_id,user_id,outcome,day)")
    conn.execute("CREATE TABLE IF NOT EXISTS stock_questions (request_id TEXT,stock_code TEXT,stock_name TEXT NOT NULL,PRIMARY KEY(request_id,stock_code))")
    conn.execute("CREATE TABLE IF NOT EXISTS question_types (request_id TEXT,question_type TEXT,PRIMARY KEY(request_id,question_type))")
    conn.execute("CREATE TABLE IF NOT EXISTS stock_question_types (request_id TEXT,stock_code TEXT,question_type TEXT,PRIMARY KEY(request_id,stock_code,question_type))")
    conn.execute("INSERT OR IGNORE INTO member_usage_meta VALUES ('stock_started',?)",(now().isoformat(timespec='seconds'),))


def query_types(question,route=''):
    kinds=[]
    for name,pattern in [('持倉',r'成本|買在|套在|還能抱|持有|續抱|回本|賠錢|虧損|怎麼辦|停損|停利'),
                          ('權證籌碼',r'權證|事件勝率'),('法人籌碼',r'外資|投信|自營商|法人'),
                          ('現股籌碼',r'現股|籌碼|分點|主力'),('新聞',r'新聞|消息|利多|利空|題材'),
                          ('型態',r'型態|走勢|技術|均線|月線|季線|布林|支撐|壓力|K線|K棒'),
                          ('比較',r'比較|哪個強|誰比較'),('大盤期貨',r'大盤|台指|期貨|櫃買')]:
        if re.search(pattern,str(question),re.I):kinds.append(name)
    if '權證籌碼' in kinds and '現股籌碼' in kinds and not re.search(r'現股|股票分點',question):kinds.remove('現股籌碼')
    if route=='trade_review' or review_language.intent(str(question))=='review':kinds.append('覆盤')
    return kinds or ['型態' if route.startswith('rule_pattern') or route=='answer_cache' else '其他研究']


def identities(stocks):
    # 呼叫端使用既有股票名冊解析的股號／股名，以股號作唯一鍵。
    unique={}
    for code,name in stocks or []:
        code=str(code).strip().upper()
        if re.fullmatch(r'\d{4,6}[A-Z]?|TAIEX|TPEX',code):unique[code]=str(name or code)
    return sorted(unique.items())


def category(question):
    for name, pattern in [("兩檔比較", r"比較|跟.+(?:誰|比)|與.+比較"),
                          ("新聞", r"新聞|利多|利空"), ("權證分點", r"權證|事件勝率"),
                          ("法人動向", r"外資|投信|自營商|法人"),
                          ("現股籌碼", r"現股|籌碼|分點|主力"),
                          ("支撐壓力", r"支撐|壓力"), ("大盤期貨", r"大盤|台指|期貨|櫃買")]:
        if re.search(pattern, question):
            return name
    return "個股／其他研究"


def record(request_id, guild_id, user_id, outcome, question='', route='', admin=False, simulation=False, stocks=()):
    if not guild_id or simulation:
        return
    try:
        stamp = now()
        with db._LOCK, db._db() as conn, conn:
            _init(conn)
            if admin:
                conn.execute("INSERT OR IGNORE INTO member_usage_admins VALUES (?,?)", (str(guild_id), str(user_id)))
            elif outcome in ('success', 'denied', 'failed'):
                inserted=conn.execute("INSERT OR IGNORE INTO member_usage VALUES (?,?,?,?,?,?,?,?)",
                             (str(request_id), str(guild_id), str(user_id), stamp.strftime('%Y-%m-%d'),
                              stamp.isoformat(timespec='seconds'), outcome, category(question), route)).rowcount
                if inserted and outcome=='success':
                    conn.executemany('INSERT OR IGNORE INTO stock_questions VALUES (?,?,?)',[(str(request_id),c,n) for c,n in identities(stocks)])
                    conn.executemany('INSERT OR IGNORE INTO question_types VALUES (?,?)',[(str(request_id),k) for k in query_types(question,route)])
                    conn.executemany('INSERT OR IGNORE INTO stock_question_types VALUES (?,?,?)',[(str(request_id),code,kind) for code,kinds in stock_types(question,stocks,route).items() for kind in kinds])
    except Exception as exc:
        db._warn('會員統計寫入', exc)


def record_ai_quota_hit(user_id):
    """會員這一題已沒有 AI 解讀額度：同一人同一天只記一次。失敗只寫 Log，不影響回答。"""
    try:
        with db._LOCK, db._db() as conn, conn:
            _init(conn)
            conn.execute('INSERT OR IGNORE INTO member_ai_quota_hits VALUES (?,?)', (str(user_id), now().strftime('%Y-%m-%d')))
    except Exception as exc:
        db._warn('AI額度用完紀錄', exc)


def successful(result):
    route = str(result.route)
    return (not result.denied_feature and
            not re.search(r'error|fail|help|clarif|unknown|unsupported|queue|quota|denied', route) and
            (route.startswith('rule_') or route in ('planner', 'answer_cache', 'trade_review')))


NAMES = {}   # Discord 使用者 ID → 顯示名稱（查詢時由 Bot 填入，10-04）


def report(question, guild_id, excluded=(), names=None):
    if names:
        NAMES.update({str(k): v for k, v in names.items()})
    match = command(question)
    if not match:return '請詢問使用統計、熱門股票或問題類型統計。',None
    kind, period = match.groups()
    period = period or '近30天'
    today = now().date()
    if period == '累計':
        start = '0000-00-00'
    elif period == '本月':
        start = today.replace(day=1).isoformat()
    elif period == '今日':
        start = today.isoformat()
    else:
        days = int(period[1:-1])
        if not 1 <= days <= 3650:
            return '期間請使用近1天～近3650天、本月、今日或累計。', None
        start = (today - timedelta(days=days-1)).isoformat()
    if kind in ('股票詢問統計','熱門股票','問題類型統計'):
        return stock_report(match,guild_id,excluded,start,today)
    with db._LOCK, db._db() as conn, conn:
        _init(conn)
        conn.executemany('INSERT OR IGNORE INTO member_usage_admins VALUES (?,?)',
                         [(str(guild_id), str(uid)) for uid in excluded])
        started = conn.execute("SELECT value FROM member_usage_meta WHERE key='started'").fetchone()[0]
        valid="u.guild_id GLOB ? AND NOT EXISTS (SELECT 1 FROM member_usage_admins a WHERE a.user_id=u.user_id)"
        rows=conn.execute(f"SELECT u.user_id,u.outcome,u.category,COUNT(*),COUNT(DISTINCT u.day),MAX(u.day) FROM member_usage u WHERE {valid} AND u.day BETWEEN ? AND ? GROUP BY u.user_id,u.outcome,u.category",(str(guild_id),start,today.isoformat())).fetchall()
        first=dict(conn.execute(f"SELECT u.user_id,MIN(u.day) FROM member_usage u WHERE {valid} AND u.outcome='success' AND u.day<=? GROUP BY u.user_id",(str(guild_id),today.isoformat())).fetchall())
        hits=conn.execute("SELECT COUNT(*),COUNT(DISTINCT h.user_id) FROM member_ai_quota_hits h WHERE h.day BETWEEN ? AND ? "
                          "AND NOT EXISTS (SELECT 1 FROM member_usage_admins a WHERE a.user_id=h.user_id)",(start,today.isoformat())).fetchone()
        active=dict(conn.execute(f"SELECT u.user_id,COUNT(DISTINCT u.day) FROM member_usage u WHERE {valid} AND u.outcome='success' AND u.day BETWEEN ? AND ? GROUP BY u.user_id",(str(guild_id),start,today.isoformat())).fetchall())
    users,features={},{}
    denied=failed=0
    for uid,outcome,feature,count,days,last in rows:
        if outcome=='denied':denied+=count
        elif outcome=='failed':failed+=count
        else:
            row=users.setdefault(uid,{'count':0,'days':range(active[uid]),'last':last})
            row['count']+=count
            row['last']=max(row['last'],last)
            features[feature]=features.get(feature,0)+count
    ranking = sorted(users.items(), key=lambda item: (-item[1]['count'], item[0]))
    total = sum(row['count'] for row in users.values())
    heading = f'艾斯助手｜喬巴 · {kind}（{period}）\n統計起算：{started[:10]}｜台灣時間｜已排除管理員與測試\n'
    if kind == '使用統計':
        top = sum(row['count'] for _, row in ranking[:10])
        text = (heading + f'\n成功問答：{total} 題\n使用人數：{len(users)} 人\n'
                f'新使用者：{sum(first[uid] >= start for uid in users)} 人（紀錄起算後首次成功）\n'
                f'回訪人數：{sum(len(row["days"]) >= 2 for row in users.values())} 人（期間內跨日使用）\n'
                f'前10名用量占比：{top / total * 100 if total else 0:.1f}%\n'
                f'權限拒絕：{denied} 次｜失敗：{failed} 次\n'
                f'AI 解讀額度用完：{hits[0]} 人次（{hits[1]} 人）\n\n功能使用（按問句分類）：\n' +
                ('\n'.join(f'{name}：{count} 題' for name, count in sorted(features.items(), key=lambda x: -x[1])) or '尚無資料'))
        return text, None
    text = heading + f'\n成功問答 {total} 題｜使用 {len(users)} 人\n\n'
    text += '\n'.join(f'{i}. {NAMES.get(uid) or "ID " + uid}：{row["count"]} 題'
                      for i, (uid, row) in enumerate(ranking[:10], 1)) or '尚無資料'
    if kind != '問答次數完整名單':
        return text, None
    buf = io.StringIO(newline='')
    writer = csv.writer(buf)
    writer.writerow(['排名', '名稱', 'Discord ID', '成功問答次數', '使用天數', '首次成功日期', '最近成功日期'])
    for i, (uid, row) in enumerate(ranking, 1):
        # ID 以文字保留，避免 Excel 把 18 位 ID 四捨五入。
        writer.writerow([i, NAMES.get(uid, ''), "'" + uid, row['count'], len(row['days']), first[uid], row['last']])
    return text + '\n\n完整名單請見 CSV 附件（ID 欄為文字）。', buf.getvalue().encode('utf-8-sig')


def stock_report(match,guild_id,excluded,start,today):
    end=today.isoformat()
    with db._LOCK,db._db() as conn,conn:
        _init(conn)
        conn.executemany('INSERT OR IGNORE INTO member_usage_admins VALUES (?,?)',[(str(guild_id),str(uid)) for uid in excluded])
        since=conn.execute("SELECT value FROM member_usage_meta WHERE key='stock_started'").fetchone()[0][:10]
        valid="u.guild_id GLOB ? AND u.outcome='success' AND u.day BETWEEN ? AND ? AND NOT EXISTS (SELECT 1 FROM member_usage_admins a WHERE a.user_id=u.user_id)"
        args=(str(guild_id),start,end)
        kinds=conn.execute(f"SELECT q.question_type,COUNT(*),COUNT(DISTINCT u.user_id) FROM question_types q JOIN member_usage u ON u.request_id=q.request_id WHERE {valid} GROUP BY q.question_type ORDER BY COUNT(*) DESC,q.question_type",args).fetchall()
        rows=conn.execute(f"SELECT s.stock_code,MAX(s.stock_name),COUNT(*),COUNT(DISTINCT u.user_id) FROM stock_questions s JOIN member_usage u ON u.request_id=s.request_id WHERE {valid} GROUP BY s.stock_code ORDER BY COUNT(*) DESC,s.stock_code",args).fetchall()
        previous={}
        if match.period!='累計':
            previous_end=datetime.strptime(start,'%Y-%m-%d').date()-timedelta(days=1)
            span=(today-datetime.strptime(start,'%Y-%m-%d').date()).days+1
            previous_start=(previous_end-timedelta(days=span-1)).isoformat()
            previous=dict(conn.execute(f"SELECT s.stock_code,COUNT(*) FROM stock_questions s JOIN member_usage u ON u.request_id=s.request_id WHERE {valid} GROUP BY s.stock_code",(str(guild_id),previous_start,previous_end.isoformat())).fetchall())
        breakdown=conn.execute(f"SELECT s.stock_code,q.question_type,COUNT(*) FROM stock_questions s JOIN member_usage u ON u.request_id=s.request_id JOIN stock_question_types q ON q.request_id=u.request_id AND q.stock_code=s.stock_code WHERE {valid} GROUP BY s.stock_code,q.question_type",args).fetchall()
        totals=conn.execute(f"SELECT COUNT(*),COUNT(DISTINCT u.user_id) FROM member_usage u WHERE {valid} AND EXISTS(SELECT 1 FROM question_types q WHERE q.request_id=u.request_id)",args).fetchone()
        unknown=conn.execute(f"SELECT COUNT(*) FROM member_usage u WHERE {valid} AND NOT EXISTS(SELECT 1 FROM question_types q WHERE q.request_id=u.request_id)",args).fetchone()[0]
    heading=f'艾斯助手｜喬巴 · {match.kind}（{match.period}）\n新增股票／類型統計起算：{since}｜已排除管理員與測試\n'
    if match.kind=='問題類型統計':
        text=heading+f'\n已分類問答 {totals[0]} 題｜使用 {totals[1]} 人\n'
        text+='\n'.join(f'{k}：{n} 題／{people} 人' for k,n,people in kinds) or '尚無資料'
        text+='\n同題可能包含多種類型，類型合計可能高於題數。'
        if unknown:text+=f'\n更新前 {unknown} 題保留於會員統計；未猜測補分類。'
        return text,None
    per={}
    for code,kind,count in breakdown:per.setdefault(code,[]).append((kind,count))
    text=heading+f'\n有辨識股票的詢問 {sum(n for _,_,n,_ in rows)} 檔次（同題每檔只算一次）\n\n'
    text+='\n'.join(f'{i}. {code} {name}：{count} 次／{people} 人'+(f'（較前期 {count-previous.get(code,0):+d} 次）' if match.period!='累計' else '')+'\n   '+ '、'.join(f'{k} {n}' for k,n in sorted(per.get(code,[]),key=lambda x:-x[1])) for i,(code,name,count,people) in enumerate(rows[:10],1)) or '尚無資料'
    text+='\n\n此為群組關注度；不代表買進訊號。'
    if match.period!='累計':text+='\n前期為前一個同長期間；若早於新統計起算日，資料可能尚未完整。'
    if not match.detail:return text,None
    buf=io.StringIO(newline='');writer=csv.writer(buf)
    writer.writerow(['股號','股名','詢問次數','詢問人數','問題類型','類型次數'])
    for code,name,count,people in rows:
        for kind,n in per.get(code,[]):writer.writerow(["'"+code,name,count,people,kind,n])
    return text+'\n完整名單請見CSV附件。',buf.getvalue().encode('utf-8-sig')


# 匯出／匯入僅使用以下固定資料表與欄位；備份檔不能指定任意SQL。
STAT_TABLES={
    'member_usage':('request_id','guild_id','user_id','day','ts','outcome','category','route'),
    'member_usage_meta':('key','value'),
    'member_usage_admins':('guild_id','user_id'),
    'stock_questions':('request_id','stock_code','stock_name'),
    'question_types':('request_id','question_type'),
    'stock_question_types':('request_id','stock_code','question_type'),
    'usage_log':('ts','day','route','gemini_calls','input_tokens','output_tokens','token_source','api_json','elapsed','cache_hit'),
}
STAT_PREFIXES=('gemini_usage:','railway_usage:')


def export_statistics(conn):
    _init(conn)
    tables={name:[list(row) for row in conn.execute(f"SELECT {','.join(columns)} FROM {name}")]
            for name,columns in STAT_TABLES.items()}
    states=[list(row) for row in conn.execute("SELECT key,value,updated_at FROM kv WHERE key LIKE 'gemini_usage:%' OR key LIKE 'railway_usage:%'")]
    return {'tables':tables,'states':states}


def validate_statistics(data):
    import json,math
    if not isinstance(data,dict) or set(data)!={'tables','states'}:raise ValueError('統計備份結構錯誤')
    if not isinstance(data['tables'],dict) or set(data['tables'])-set(STAT_TABLES):raise ValueError('統計資料表不在白名單')
    for name,rows in data['tables'].items():
        if not isinstance(rows,list):raise ValueError('統計資料列格式錯誤')
        for row in rows:
            if not isinstance(row,list) or len(row)!=len(STAT_TABLES[name]):raise ValueError('統計欄位不符')
            if any(v is not None and (type(v) not in (str,int,float) or (type(v)==float and not math.isfinite(v))) for v in row):raise ValueError('統計欄位型別不符')
            if name!='usage_log' and any(not isinstance(v,str) for v in row):raise ValueError('統計識別欄位須為文字')
            if name=='member_usage' and row[5] not in ('success','denied','failed'):raise ValueError('問答結果不符')
    if not isinstance(data['states'],list):raise ValueError('統計狀態格式錯誤')
    for row in data['states']:
        if not isinstance(row,list) or len(row)!=3 or not all(isinstance(v,str) for v in row):raise ValueError('統計狀態欄位不符')
        if not row[0].startswith(STAT_PREFIXES):raise ValueError('統計狀態不在白名單')
        if not isinstance(json.loads(row[1]),dict):raise ValueError('統計狀態內容須為物件')


def import_statistics(conn,data):
    _init(conn)
    count=0
    for name,rows in data['tables'].items():
        columns=STAT_TABLES[name]
        for row in rows:
            if name=='usage_log':
                if conn.execute('SELECT 1 FROM usage_log WHERE ts=? AND route=? AND input_tokens=? AND output_tokens=? AND api_json=? LIMIT 1',(row[0],row[2],row[4],row[5],row[7])).fetchone():continue
            if name=='member_usage_meta':
                conn.execute("INSERT INTO member_usage_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=MIN(value,excluded.value)",row)
            else:
                count+=conn.execute(f"INSERT OR IGNORE INTO {name} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",row).rowcount
    # 同一日模型/伺服器用量可能已存在；以每日計數的最大值補缺，不能重複相加。
    import json
    def merge(old,new):
        if isinstance(old,dict) and isinstance(new,dict):
            result=dict(old)
            for key,value in new.items():result[key]=merge(result[key],value) if key in result else value
            return result
        if type(old) in (int,float) and type(new) in (int,float):return max(old,new)
        return old
    for key,value,at in data['states']:
        old=conn.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
        value=json.dumps(merge(json.loads(old[0]),json.loads(value)),ensure_ascii=False) if old else value
        conn.execute('INSERT INTO kv VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=MAX(updated_at,excluded.updated_at)',(key,value,at))
    return count


def stock_types(question,stocks,route=''):
    pairs=identities(stocks)
    overall=query_types(question,route)
    if len(pairs)<2:return {code:overall for code,_ in pairs}
    subjects={term:code for code,name in pairs for term in (code,name) if term}
    pattern=re.compile('|'.join(re.escape(t) for t in sorted(subjects,key=len,reverse=True)))
    hits=list(pattern.finditer(question))
    segments={}
    for i,hit in enumerate(hits):
        text=question[hit.end():hits[i+1].start() if i+1<len(hits) else len(question)]
        # 各檔有獨立題目才拆；「2330和2409型態比較」共用題目，不偏向最後一檔。
        if re.search(r'型態|籌碼|權證|外資|法人|新聞|月線|支撐|壓力|成本|買在|走勢',text):
            segments.setdefault(subjects[hit.group()],set()).update(query_types(text,route))
    if len(segments)==len(pairs):
        return {code:sorted(kinds|({'比較'} if '比較' in overall else set())) for code,kinds in segments.items()}
    return {code:overall for code,_ in pairs}


def diagnose():
    """管理員診斷：各伺服器寫入筆數、管理員排除數、最後紀錄時間、資料表欄位（查「為什麼統計是 0」）。"""
    try:
        with db._LOCK, db._db() as conn:
            _init(conn)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(member_usage)")]
            started = conn.execute("SELECT value FROM member_usage_meta WHERE key='started'").fetchone()[0]
            rows = conn.execute("SELECT guild_id, outcome, COUNT(*), COUNT(DISTINCT user_id), MAX(ts) FROM member_usage GROUP BY guild_id, outcome").fetchall()
            admins = conn.execute("SELECT guild_id, COUNT(*) FROM member_usage_admins GROUP BY guild_id").fetchall()
    except Exception as exc:
        return f"會員統計診斷失敗：{type(exc).__name__}: {exc}"
    lines = [f"資料庫：{db.DB_PATH}", f"統計起算：{started}", f"member_usage 欄位：{', '.join(cols)}"]
    lines += [f"伺服器 {g}｜{o}｜{n} 筆｜{u} 人｜最後 {t}" for g, o, n, u, t in rows] or ["member_usage 沒有任何紀錄"]
    lines += [f"伺服器 {g}｜被排除的管理員 {n} 人" for g, n in admins]
    return chr(10).join(lines)
