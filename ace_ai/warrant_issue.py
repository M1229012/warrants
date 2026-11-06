"""11-06 使用者（測試版）：每檔權證目前的發行量（張），給「分點持有占發行量」用。

- 來源（每天更新的公開資料）：證交所 OpenAPI t187ap37_L「上市權證基本資料彙總表」、櫃買 OpenAPI mopsfin_t187ap37_O
  「上櫃權證基本資料彙總表」。欄位「發行單位數量(仟單位)」是原始發行量；發行商後來自行註銷的，只寫在「備註」
  （「申請自行註銷權證 2,550 仟單位，剩餘權證 2,450 仟單位」）→ 有「剩餘權證」就用最後一次寫的剩餘量。
- 11-06 實測上市約 4.9 萬檔、48MB，上櫃約 1.8 萬檔、18MB：整包 json 解析會吃掉一兩百 MB 記憶體，
  所以串流讀、逐筆用正規式取代號／發行量／剩餘量，只留 {代號: 張數}（約 6.6 萬筆）。一天抓一次；失敗回空，呼叫端照常出圖。
- 發行量含發行商自己手上沒賣出的部分，所以「分點持有÷發行量」是保守的下限（占投資人手上的比例只會更高）。
"""
from __future__ import annotations

import codecs
import re
import threading
from typing import Callable, Dict, Iterable

import warrant_ai_tools as tools

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap37_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap37_O"
CHUNK = 1 << 20

_CODE = re.compile(r'"權證代號"\s*:\s*"([^"]+)"')
_ISSUED = re.compile(r'"發行單位數量\(仟單位\)"\s*:\s*"([\d,.]+)"')
_LEFT = re.compile(r"剩餘權證\s*([\d,]+)\s*仟單位")
_SPLIT = re.compile(r"\}\s*,\s*\{")             # 筆與筆之間（中間可能有換行、空白）

_LOCK = threading.Lock()
_CACHE: Dict[str, Dict[str, float]] = {}      # {日期: {代號: 目前發行張數}}


def parse_record(text: str):
    """一筆權證基本資料（json 物件文字）→（代號, 目前發行張數）；認不出回 None。"""
    code = _CODE.search(text)
    issued = _ISSUED.search(text)
    if not code or not issued:
        return None
    try:
        lots = float(issued.group(1).replace(",", ""))
    except ValueError:
        return None
    left = _LEFT.findall(text)
    if left:                                  # 註銷過：備註最後一次寫的剩餘量
        try:
            lots = float(left[-1].replace(",", ""))
        except ValueError:
            pass
    return code.group(1).strip(), lots


def _stream(url: str, source: str, out: Dict[str, float]) -> int:
    """串流讀一份彙總表，逐筆解析寫進 out；回筆數。櫃買海外連線補中繼憑證（和 _official_json 相同）。"""
    kf = tools.core()
    session = kf.get_thread_session()
    getter = getattr(kf, "_requests_get_with_aia_fallback", None) if "tpex.org.tw" in url else None
    kwargs = {"headers": {"User-Agent": "Mozilla/5.0 AceAI/1.0"}, "timeout": (5, 60), "stream": True}
    resp = getter(session, url, **kwargs) if getter else session.get(url, **kwargs)
    resp.raise_for_status()
    decoder = codecs.getincrementaldecoder("utf-8")()
    buf, count = "", 0
    try:
        for chunk in resp.iter_content(CHUNK):
            buf += decoder.decode(chunk)
            parts = _SPLIT.split(buf)
            buf = parts.pop()                 # 最後一段可能還沒讀完
            for part in parts:
                hit = parse_record(part)
                if hit:
                    out[hit[0]] = hit[1]
                    count += 1
        buf += decoder.decode(b"", final=True)
        hit = parse_record(buf)
        if hit:
            out[hit[0]] = hit[1]
            count += 1
    finally:
        resp.close()
    tools.record_api_event(source, status=200, latency=0.0)
    return count


def issued(codes: Iterable[str], log: Callable[[str], None] = print) -> Dict[str, float]:
    """{權證代號: 目前發行張數}；只回有資料的代號。當天第一次呼叫時抓兩份彙總表（之後讀記憶體）。"""
    today = tools.taipei_now().strftime("%Y-%m-%d")
    with _LOCK:
        table = _CACHE.get(today)
        if table is None:
            table = {}
            ok = 0
            for url, source in ((TWSE_URL, "TWSE"), (TPEX_URL, "TPEx")):
                try:
                    n = _stream(url, source, table)
                    ok += 1
                    log(f"權證發行量｜{source} {n:,} 筆")
                except Exception as exc:
                    log(f"權證發行量｜{source} 讀取失敗｜{tools.err_text(exc)}")
            if ok == 2:                       # 兩份都成功才快取；失敗的話下次再抓
                _CACHE.clear()
                _CACHE[today] = table
    return {c: table[c] for c in codes if c in table and table[c] > 0}
