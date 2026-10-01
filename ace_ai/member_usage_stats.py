"""會員使用統計；不保存問題原文，不呼叫 AI，沿用永久市場資料庫。"""
import csv
import io
import re
from datetime import datetime, timedelta, timezone
import local_market_cache as db


def now():
    return datetime.now(timezone(timedelta(hours=8)))


def command(question):
    return re.fullmatch(r"(問答次數完整名單|問答次數|使用統計)(近\d+天|本月|今日|累計)?",
                        re.sub(r"\s+", "", question))


def _init(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS member_usage (
        request_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
        day TEXT NOT NULL, ts TEXT NOT NULL, outcome TEXT NOT NULL,
        category TEXT NOT NULL, route TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_member_usage_guild_day ON member_usage(guild_id,day)")
    conn.execute("CREATE TABLE IF NOT EXISTS member_usage_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO member_usage_meta VALUES ('started',?)", (now().isoformat(timespec='seconds'),))
    conn.execute("CREATE TABLE IF NOT EXISTS member_usage_admins (guild_id TEXT,user_id TEXT,PRIMARY KEY(guild_id,user_id))")


def category(question):
    for name, pattern in [("兩檔比較", r"比較|跟.+(?:誰|比)|與.+比較"),
                          ("新聞", r"新聞|利多|利空"), ("權證分點", r"權證|事件勝率"),
                          ("法人動向", r"外資|投信|自營商|法人"),
                          ("現股籌碼", r"現股|籌碼|分點|主力"),
                          ("支撐壓力", r"支撐|壓力"), ("大盤期貨", r"大盤|台指|期貨|櫃買")]:
        if re.search(pattern, question):
            return name
    return "個股／其他研究"


def record(request_id, guild_id, user_id, outcome, question='', route='', admin=False, simulation=False):
    if not guild_id or simulation:
        return
    try:
        stamp = now()
        with db._LOCK, db._db() as conn, conn:
            _init(conn)
            if admin:
                conn.execute("INSERT OR IGNORE INTO member_usage_admins VALUES (?,?)", (str(guild_id), str(user_id)))
            elif outcome in ('success', 'denied', 'failed'):
                conn.execute("INSERT OR IGNORE INTO member_usage VALUES (?,?,?,?,?,?,?,?)",
                             (str(request_id), str(guild_id), str(user_id), stamp.strftime('%Y-%m-%d'),
                              stamp.isoformat(timespec='seconds'), outcome, category(question), route))
    except Exception as exc:
        db._warn('會員統計寫入', exc)


def successful(result):
    route = str(result.route)
    return (not result.denied_feature and
            not re.search(r'error|fail|help|clarif|unknown|unsupported|queue|quota|denied', route) and
            (route.startswith('rule_') or route in ('planner', 'answer_cache')))


def report(question, guild_id, excluded=()):
    match = command(question)
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
    with db._LOCK, db._db() as conn, conn:
        _init(conn)
        conn.executemany('INSERT OR IGNORE INTO member_usage_admins VALUES (?,?)',
                         [(str(guild_id), str(uid)) for uid in excluded])
        started = conn.execute("SELECT value FROM member_usage_meta WHERE key='started'").fetchone()[0]
        # 管理員在提問時或查詢時辨識；過去會員期間的用量也排除。
        rows = conn.execute("""SELECT u.user_id,u.day,u.outcome,u.category FROM member_usage u
            WHERE u.guild_id=? AND u.day<=? AND NOT EXISTS
            (SELECT 1 FROM member_usage_admins a WHERE a.guild_id=u.guild_id AND a.user_id=u.user_id)
            ORDER BY u.day,u.user_id""", (str(guild_id), today.isoformat())).fetchall()
    first = {}
    users = {}
    features = {}
    denied = failed = 0
    for uid, day, outcome, feature in rows:
        if outcome == 'success':
            first.setdefault(uid, day)
        if day < start:
            continue
        if outcome == 'denied':
            denied += 1
        elif outcome == 'failed':
            failed += 1
        else:
            row = users.setdefault(uid, {'count': 0, 'days': set()})
            row['count'] += 1
            row['days'].add(day)
            features[feature] = features.get(feature, 0) + 1
    ranking = sorted(users.items(), key=lambda item: (-item[1]['count'], item[0]))
    total = sum(row['count'] for row in users.values())
    heading = f'艾斯助手｜喬巴 · {kind}（{period}）\n統計起算：{started[:10]}｜台灣時間｜已排除管理員與測試\n'
    if kind == '使用統計':
        top = sum(row['count'] for _, row in ranking[:10])
        text = (heading + f'\n成功問答：{total} 題\n使用人數：{len(users)} 人\n'
                f'新使用者：{sum(first[uid] >= start for uid in users)} 人（紀錄起算後首次成功）\n'
                f'回訪人數：{sum(len(row["days"]) >= 2 for row in users.values())} 人（期間內跨日使用）\n'
                f'前10名用量占比：{top / total * 100 if total else 0:.1f}%\n'
                f'權限拒絕：{denied} 次｜失敗：{failed} 次\n\n功能使用（按問句分類）：\n' +
                ('\n'.join(f'{name}：{count} 題' for name, count in sorted(features.items(), key=lambda x: -x[1])) or '尚無資料'))
        return text, None
    text = heading + f'\n成功問答 {total} 題｜使用 {len(users)} 人\n\n'
    text += '\n'.join(f'{i}. ID {uid}：{row["count"]} 題'
                      for i, (uid, row) in enumerate(ranking[:10], 1)) or '尚無資料'
    if kind != '問答次數完整名單':
        return text, None
    buf = io.StringIO(newline='')
    writer = csv.writer(buf)
    writer.writerow(['排名', 'Discord ID', '成功問答次數', '使用天數', '首次成功日期', '最近成功日期'])
    for i, (uid, row) in enumerate(ranking, 1):
        # ID 以文字保留，避免 Excel 把 18 位 ID 四捨五入。
        writer.writerow([i, "'" + uid, row['count'], len(row['days']), first[uid], max(row['days'])])
    return text + '\n\n完整名單請見 CSV 附件（ID 欄為文字）。', buf.getvalue().encode('utf-8-sig')
