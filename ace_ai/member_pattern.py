"""Member pattern presentation. No new detection, prices or trading rules."""
from __future__ import annotations
from copy import deepcopy
import math
import re
from urllib.parse import urlparse

VERSION = "member_pattern_v1"

def geometry(result):
    """Serialize the exact detection frame by date, not sliced-chart indices."""
    tri = result.get("triangle")
    frame = (result.get("debug") or {}).get("frame")
    if not tri or frame is None:
        return None
    rows = []
    anchors = tri.get("anchors") or {}
    for i, (day, bar) in enumerate(frame.iterrows()):
        up = tri["upper"][0] * i + tri["upper"][1]
        lo = tri["lower"][0] * i + tri["lower"][1]
        rows.append({"date": str(day)[:10], "ohlc": [float(bar[k]) for k in ("Open", "High", "Low", "Close")],
                     "upper": float(up), "lower": float(lo), "applicable": up > lo,
                     "show_upper": i >= min(anchors.get("upper") or [tri["start"]]),
                     "show_lower": i >= min(anchors.get("lower") or [tri["start"]]),
                     "reference": tri.get("reference_until") is not None and i < tri["reference_until"],
                     "upper_extension": i > max(anchors.get("upper") or [i]),
                     "lower_extension": i > max(anchors.get("lower") or [i])})
    last = rows[-1]
    return {"kind": tri["kind"], "candidate": bool(tri.get("candidate")),
            "candidate_reasons": tri.get("candidate_reasons") or [],
            "current_position": tri.get("current_position"), "near_tip": bool(tri.get("near_tip")),
            "state": tri.get("state", ""), "first_break": deepcopy(tri.get("first_break")),
            "formation_date": rows[max(0, int(tri.get("joint_start", tri["start"])))]["date"],
            "data_date": last["date"], "upper": last["upper"], "lower": last["lower"],
            "rows": rows}

def overlay(panel, view):
    """Fail closed when chart OHLC differs from the detector's price basis."""
    if not view or not panel.get("bars"):
        return None
    by_day = {r["date"]: r for r in view["rows"]}
    points = []
    for bar in panel["bars"]:
        row = by_day.get(bar["date"])
        if row is None:
            return dict(view, rows=[], visible=False, notice="三角線資料日期與圖表不同，暫不疊線")
        for actual, key in zip(row["ohlc"], ("Open", "High", "Low", "Close")):
            value = bar.get(key)
            if value is None or not math.isclose(float(value), actual, abs_tol=0.00001, rel_tol=0.000001):
                return dict(view, rows=[], visible=False, notice="三角線與圖表價格基準不同，暫不疊線")
        points.append(dict(row))
    last = points[-1]
    close = last["ohlc"][3]
    position = ("above" if close > last["upper"] else "below" if close < last["lower"] else "inside") if last["applicable"] else "not_applicable"
    return dict(view, rows=points, visible=True, detector_position=view.get("current_position"),
                current_position=position, data_date=last["date"], upper=last["upper"], lower=last["lower"])

def needs_for(parsed, plan, code):
    if plan.needs is not None:
        return set((plan.needs.get("per_stock") or {}).get(code, []))
    return set(parsed.intents)

def wants_spot(parsed, plan, code):
    intents = needs_for(parsed, plan, code)
    if plan.needs is not None:
        return bool(intents & {"spot", "spot_backtest"} or (intents & {"warrant"} and
                    getattr(parsed, "chip", "") in ("spot", "combined")))
    return bool(getattr(parsed, "spot_combo", False) or getattr(parsed, "spot_branch", "") or
                getattr(parsed, "chip", "") in ("spot", "combined") or
                intents & {"spot", "spot_backtest", "margin"})

def wants_score(parsed, plan, code):
    if plan.route in ("rule_index_compare", "rule_top_warrant"):
        return True
    if plan.needs is not None:
        if "score" in needs_for(parsed, plan, code):
            return True
        names = {code} | {nm for c, nm in parsed.stocks if c == code}
        return any(n.get("include_score") is True and
                   (not n.get("targets") or names.intersection(n.get("targets") or []))
                   for n in plan.needs.get("needs") or [])
    return "score" in parsed.intents

def comparing(plan):
    return plan.route == "rule_index_compare" or bool(plan.needs and (
        plan.needs.get("compare_with") or any(n.get("type") == "compare" for n in plan.needs.get("needs") or [])))

def structure_card(panel):
    view = panel["member_triangle"]
    name = view["kind"] + ("候選" if view["candidate"] else "")
    sections = []
    if not view["visible"]:
        sections.append({"type": "paragraph", "text": view["notice"]})
    else:
        if view["rows"][-1]["applicable"]:
            status = {"inside": "位於上下緣之間", "above": "位於上緣之上", "below": "位於下緣之下"}.get(view["current_position"], "位置待確認")
            label = "候選參考線" if view["candidate"] else "型態參考線"
            sections.append({"type": "paragraph", "text": f"{view['data_date']}｜{status}｜{label}：上緣 {view['upper']:,.2f}、下緣 {view['lower']:,.2f}"})
        else:
            sections.append({"type": "paragraph", "text": "上下緣已交會；延長線不再作為目前支撐壓力"})
        sections.append({"type": "note", "text": "橘：上緣｜藍：下緣；虛線為候選、參考或延伸段"})
    if view["candidate"]:
        sections.append({"type": "paragraph", "text": "尚待確認：" + "、".join(view["candidate_reasons"] or ["接觸證據待確認"]) + "；在線外不代表正式突破"})
    first = view.get("first_break") or {}
    if first and not view["candidate"]:
        word = "上緣" if first.get("type") == "break_up" else "下緣"
        confirm = f"；{first['confirmed_date']} 兩日確認" if first.get("confirm") == "確認" and first.get("confirmed_date") else "；突破仍待確認"
        sections.append({"type": "paragraph", "text": f"{first.get('date', '')} 首次收盤越過{word}" + confirm})
    if view["near_tip"]:
        sections.append({"type": "paragraph", "text": "接近尖端，注意剩餘空間與收盤越線後的延續性"})
    return {"branch_card": {"branch": f"{panel['stock_code']} {panel.get('stock_name', '')}｜{name}",
            "label": "型態重點", "tags": [], "sections": sections}, "member_structure": True}

def news_panel(data):
    sections, seen = [], set()
    articles = sorted(data.get("articles") or [], key=lambda a: str(a.get("date") or ""), reverse=True)
    selected = []
    for a in articles:
        title = str(a.get("title") or "").strip()
        key = a.get("event_key") or re.sub(r"\s", "", title)
        if not title or key in seen: continue
        seen.add(key); selected.append(a)
        if len(selected) == 3: break
    for a in selected:
        source = a.get("source") or urlparse(str(a.get("url") or "")).netloc or "來源未標示"
        sections.append({"type": "paragraph", "text": f"{a.get('date') or '日期未提供'}｜{source}：{a['title']}"})
    if not selected:
        sections.append({"type": "paragraph", "text": "指定期間未取得可核對日期的新聞；不代表這段期間没有新聞"})
    days = data.get("requested_days")
    label = f"近 {days} 天新聞重點" if days else "近期新聞重點"
    sections.append({"type": "note", "text": "最多列3則來源重點；僅涵蓋實際取得的新聞"})
    return {"branch_card": {"branch": f"{data.get('stock_code', '')} {data.get('stock_name', '')}｜{label}",
             "label": "新聞", "tags": [], "sections": sections}, "member_news": True,
            "news_articles": selected}

def chart_context(panels, compound=False):
    out = {"version": VERSION, "compound": bool(compound), "stocks": {}}
    for p in panels:
        if not p.get("bars"): continue
        stock = {"last_bar": deepcopy(p["bars"][-1]), "intraday": deepcopy(p.get("intraday") or {}),
                 "volume_profile": deepcopy(p.get("volume_profile") or {}), "show_score": bool(p.get("scorecard")),
                 "stock_name": p.get("stock_name", "")}
        view = p.get("member_triangle")
        if view:
            stock["triangle"] = {k: deepcopy(v) for k, v in view.items() if k != "rows"}
            stock["show_score"] = bool(p.get("scorecard"))
        out["stocks"][p["stock_code"]] = stock
    return out

def scoped_payload(payload, context):
    """Do not expose hidden score/KD/MACD facts as visible chart evidence."""
    payload = deepcopy(payload); payload["chart_context"] = deepcopy(context)
    for key, data in list((payload.get("tool_results") or {}).items()):
        if context.get("compound") and key.split(":")[0] == "get_recent_news" and isinstance(data, dict):
            data["articles"] = news_panel(data)["news_articles"]
        stock = context.get("stocks", {}).get(data.get("stock_code"), {}) if isinstance(data, dict) else {}
        view = stock.get("triangle")
        if view and not stock.get("show_score"):
            if key.split(":")[0] == "get_pattern_scorecard":
                del payload["tool_results"][key]; continue
            if key.split(":")[0] == "get_technical_analysis":
                payload["tool_results"][key] = {k: data[k] for k in ("stock_code", "stock_name", "data_date", "signal_status", "freshness", "triangle_structure") if k in data}
                structure = payload["tool_results"][key].get("triangle_structure")
                if isinstance(structure, dict):
                    structure["chart_position"] = view.get("current_position")
                    structure["chart_position_date"] = view.get("data_date")
    return payload


def without_hidden_scores(text, context):
    # Enforce an entirely score-free page without inventing replacement analysis.
    stocks = list(context.get("stocks", {}).values())
    if not stocks or any(s.get("show_score") for s in stocks) or not any(s.get("triangle") for s in stocks):
        return text
    parts = re.split(r"(?<=[。！？；])", str(text or ""))
    score = re.compile(r"評分|評等|型態分數|加分|扣分|得分|\d+(?:\.\d+)?\s*(?:/\s*100|分(?=[，。；、\s]|$))")
    return "".join(p for p in parts if not score.search(p)).strip()
