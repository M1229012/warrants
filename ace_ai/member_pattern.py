"""Member pattern presentation. No new detection, prices or trading rules."""
from __future__ import annotations
from copy import deepcopy
import math
import re
from urllib.parse import urlparse
from datetime import date

VERSION = "member_pattern_v9_adjusted_levels_tracking"

def date_key(value):
    """Calendar date key; retain display format and never change trading indices."""
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T].*)?", str(value).strip())
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None

def unavailable(view, notice, **diagnostic):
    return dict(view, rows=[], visible=False, notice=notice, current_position=None,
                upper=None, lower=None, near_tip=False, diagnostic=diagnostic)

def geometry(result, source_frame=None, events=None):
    """Serialize the exact detection frame by date, not sliced-chart indices."""
    tri = result.get("triangle")
    frame = (result.get("debug") or {}).get("frame")
    if not tri or frame is None:
        return None
    rows = []
    anchors = tri.get("anchors") or {}
    source_rows = {}
    verified_events = []
    basis_valid = True
    if source_frame is not None:
        for day, bar in source_frame.iterrows():
            key = date_key(day)
            if key is None or key in source_rows:
                basis_valid = False
            source_rows[key] = [float(bar[k]) for k in ("Open", "High", "Low", "Close")]
        # Mirror only pure cash events actually applied by detect.adjust().
        applied = {e['date'] for e in frame.attrs.get('share_adjustments') or []}
        if events and events.get('status') == 'ok':
            first, last = date_key(frame.index[0]), date_key(frame.index[-1])
            for event in sorted(events.get('items') or [], key=lambda e: e['date']):
                day = date_key(event.get('date'))
                if day and first < day <= last and event.get('date') not in applied and event.get('kind') in {'息', '除息'} and event.get('factor'):
                    factor = float(event['factor'])
                    if not math.isfinite(factor) or factor <= 0:
                        basis_valid = False
                    else:
                        verified_events.append((day, factor))
    for i, (day, bar) in enumerate(frame.iterrows()):
        up = tri["upper"][0] * i + tri["upper"][1]
        lo = tri["lower"][0] * i + tri["lower"][1]
        rows.append({"date": str(day)[:10], "ohlc": [float(bar[k]) for k in ("Open", "High", "Low", "Close")],
                     "upper": float(up), "lower": float(lo), "adjusted_upper": float(up), "adjusted_lower": float(lo), "applicable": up > lo,
                     "show_upper": i >= min(anchors.get("upper") or [tri["start"]]),
                     "show_lower": i >= min(anchors.get("lower") or [tri["start"]]),
                     "reference": tri.get("reference_until") is not None and i < tri["reference_until"],
                     "upper_extension": i > max(anchors.get("upper") or [i]),
                     "lower_extension": i > max(anchors.get("lower") or [i])})
        if source_frame is not None:
            key = date_key(day)
            factor = math.prod(f for event_day, f in verified_events if key < event_day)
            original = source_rows.get(key)
            if (not original or not math.isfinite(factor) or factor <= 0 or
                    not all(math.isfinite(v) and v > 0 for v in original) or
                    not all(math.isclose(actual, value * factor, abs_tol=0.00001, rel_tol=0.000001)
                            for actual, value in zip(rows[-1]["ohlc"], original))):
                basis_valid = False
            else:
                # Draw each date in the chart's original basis, without fitting new lines.
                rows[-1].update(chart_ohlc=original, basis_factor=factor,
                                upper=float(up / factor), lower=float(lo / factor))
    last = rows[-1]
    return {"kind": tri["kind"], "candidate": bool(tri.get("candidate")),
            "candidate_reasons": tri.get("candidate_reasons") or [],
            "current_position": tri.get("current_position"), "near_tip": bool(tri.get("near_tip")),
            "state": tri.get("state", ""), "first_break": deepcopy(tri.get("first_break")),
            "history": deepcopy(tri.get("history") or []),
            "formation_date": rows[max(0, int(tri.get("joint_start", tri["start"])))]["date"],
            "data_date": last["date"], "upper": last["upper"], "lower": last["lower"],
            "rows": rows, "basis_valid": basis_valid,
            "historical_conversion": bool(verified_events)}

def price_basis(result, source_frame, events):
    """Use the exact detector frame; validate cash factors rather than infer them."""
    frame = (result.get("debug") or {}).get("frame")
    if frame is None or source_frame is None or frame.empty:
        return None
    import pandas as pd
    source = {date_key(day): [float(bar[k]) for k in ("Open", "High", "Low", "Close")]
              for day, bar in source_frame.iterrows()}
    applied = {e["date"] for e in frame.attrs.get("share_adjustments") or []}
    cash = []
    if events and events.get("status") == "ok":
        for e in events.get("items") or []:
            day = date_key(e.get("date"))
            if (day and date_key(frame.index[0]) < day <= date_key(frame.index[-1])
                    and e.get("date") not in applied and e.get("kind") in {"息", "除息"}):
                try:
                    factor = float(e.get("factor"))
                except (TypeError, ValueError):
                    return None
                if not math.isfinite(factor) or factor <= 0:
                    return None
                cash.append((day, factor))
    close = pd.to_numeric(frame["Close"], errors="coerce")
    mas = {f"MA{n}": close.rolling(n).mean() for n in (5, 10, 20, 60)}
    rows = []
    for i, (day, bar) in enumerate(frame.iterrows()):
        key = date_key(day); original = source.get(key)
        ohlc = [float(bar[k]) for k in ("Open", "High", "Low", "Close")]
        factor = math.prod(f for event_day, f in cash if key < event_day)
        if not original or not all(math.isfinite(a) and a > 0 and math.isclose(a, b*factor, abs_tol=1e-5, rel_tol=1e-6)
                                   for a, b in zip(ohlc, original)):
            return None
        rows.append({"date": key, "source_ohlc": original,
                     **dict(zip(("Open", "High", "Low", "Close"), ohlc)),
                     "Volume": float(bar.get("Volume", 0)) if pd.notna(bar.get("Volume", 0)) else 0.,
                     **{k: float(v.iloc[i]) if pd.notna(v.iloc[i]) else None for k, v in mas.items()}})
    return {"data_date": rows[-1]["date"], "adjusted": bool(cash), "rows": rows,
            "events": [{"date": d, "factor": f} for d, f in cash]}


def apply_price_basis(panel, basis, profile_builder):
    """Atomic display-only conversion; recompute the same 40-bin profile after validation."""
    if not basis or not basis.get("adjusted"):
        return False
    bars = panel.get("bars") or []
    if not bars or date_key(bars[-1].get("date")) != basis.get("data_date"):
        raise ValueError("還原圖表截至日與型態資料不同")
    by_day = {r["date"]: r for r in basis["rows"]}
    converted = []
    previous = None
    for bar in bars:
        day = date_key(bar.get("date")); row = by_day.get(day)
        if not row or (previous is not None and day <= previous):
            raise ValueError("還原圖表日期缺漏或重複")
        previous = day
        for actual, key in zip(row["source_ohlc"], ("Open", "High", "Low", "Close")):
            if not math.isclose(float(bar[key]), actual, abs_tol=1e-5, rel_tol=1e-6):
                raise ValueError("還原圖表行情與型態行情不一致")
        converted.append(dict(bar, **{k: row[k] for k in ("Open", "High", "Low", "Close", "MA5", "MA10", "MA20", "MA60")}))
    import pandas as pd
    work = pd.DataFrame(converted)
    work.index = pd.to_datetime(work.pop("date"))
    stats = profile_builder(work, n_bins=40)
    if not stats:
        raise ValueError("還原價大量區無法計算，保留原圖價格基準")
    profile = {"bins": list(map(float, stats["bins"])), "profile": list(map(float, stats["profile"])),
               "max_idx": int(stats["max_idx"]), "second_idx": int(stats["second_idx"])}
    previous_close = basis["rows"][-2]["Close"] if len(basis["rows"]) > 1 else None
    panel.update(bars=converted, volume_profile=profile, pattern_adjusted_basis=True,
                 change_pct=(converted[-1]["Close"]/previous_close-1)*100 if previous_close else None,
                 price_basis_note="歷史股價已依核實除息參考價還原；均線、大量區、三角採同一基準")
    return True


def chart_zones(panel, ratio=.5):
    """Same grouped half-maximum nearby zones, from the profile actually drawn."""
    p = panel.get("volume_profile") or {}; bins, volumes = p.get("bins") or [], p.get("profile") or []
    if not volumes or len(bins) != len(volumes)+1:
        return {}
    a, b = int(p["max_idx"]), int(p["second_idx"])
    if not 0 <= a < len(volumes) or not 0 <= b < len(volumes):
        return {}
    zone = lambda i: {"price_low": bins[i], "price_high": bins[i+1]}
    groups, current = [], []
    for i, v in enumerate(volumes):
        if v > 0 and v >= volumes[a]*ratio:
            current.append(i)
        elif current:
            groups.append(current); current = []
    if current: groups.append(current)
    groups = [g for g in groups if a not in g and b not in g]
    close = float(panel["bars"][-1]["Close"])
    below, above = [g for g in groups if bins[g[0]] <= close], [g for g in groups if bins[g[0]] > close]
    nearby = ([max(below, key=lambda g: bins[g[0]])] if below else []) + ([min(above, key=lambda g: bins[g[0]])] if above else [])
    return {"maximum_volume_zone": zone(a), "second_volume_zone": zone(b),
            "near_volume_zones": [{"price_low": bins[g[0]], "price_high": bins[g[-1]+1]} for g in nearby]}


def levels_card(panel, technical, key_price_levels, ratio=.5):
    """Visible prices without calculating or exposing a hidden pattern score."""
    bar = panel["bars"][-1]
    tech = {"close": bar["Close"], "moving_averages": {k: {"value": bar.get(k)} for k in ("MA5", "MA10", "MA20", "MA60")}}
    levels = key_price_levels(tech, chart_zones(panel, ratio))
    view = panel.get("member_triangle") or {}
    if view.get("visible") and view.get("current_position") != "not_applicable":
        # Broken/candidate lines remain references; do not call them guaranteed support.
        for edge, label in (("upper", "三角上緣"), ("lower", "三角下緣")):
            price = view[edge]
            pool = levels["supports"] if price <= bar["Close"] else levels["resistances"]
            pool.append({"label": label, "price": price, "source": "triangle",
                         "distance_from_close_pct": (price/bar["Close"]-1)*100})
    basis = (technical.get("kline_patterns") or {}).get("member_basis") or {}
    deduction = basis.get("ma_deduction") if panel.get("pattern_adjusted_basis") else technical.get("ma_deduction")
    return {"levels_only": True, "hide_score": True, "show_tracked_branches": False,
            "close": bar["Close"], "stock_code": panel["stock_code"], "ma_deduction": deduction or {},
            "resistances_above_close": sorted(levels["resistances"], key=lambda x: x["price"]),
            "supports_below_close": sorted(levels["supports"], key=lambda x: -x["price"])}


def overlay(panel, view):
    """Fail closed when chart OHLC differs from the detector's price basis."""
    if not view or not panel.get("bars"):
        return None
    if view.get("basis_valid") is False:
        return unavailable(view, "三角線還原係數無法核對，暫不疊線", reason="unverified_basis")
    chart_day, line_day = date_key(panel["bars"][-1].get("date")), date_key(view.get("data_date"))
    if chart_day is None or chart_day != line_day:
        return unavailable(view, "三角線資料日期與圖表不同，暫不疊線", reason="latest_date_mismatch", chart_date=chart_day, line_date=line_day)
    by_day = {}
    for row in view["rows"]:
        key = date_key(row["date"])
        if key is None or key in by_day:
            return unavailable(view, "三角線資料日期無法核對，暫不疊線", date=row.get("date"), reason="invalid_or_duplicate_date")
        by_day[key] = row
    points = []
    previous = None
    for bar in panel["bars"]:
        key = date_key(bar["date"])
        row = by_day.get(key)
        if row is None or (previous is not None and key <= previous):
            return unavailable(view, "三角線資料日期與圖表不同，暫不疊線", date=bar.get("date"), reason="missing_or_unordered_date")
        previous = key
        for actual, key in zip(row["ohlc"] if panel.get("pattern_adjusted_basis") else row.get("chart_ohlc", row["ohlc"]), ("Open", "High", "Low", "Close")):
            value = bar.get(key)
            try:
                matches = math.isfinite(float(value)) and math.isclose(float(value), actual, abs_tol=0.00001, rel_tol=0.000001)
            except (TypeError, ValueError):
                matches = False
            if not matches:
                return unavailable(view, "三角線與圖表價格基準不同，暫不疊線", date=bar.get("date"), field=key, expected=actual, actual=value)
        points.append(dict(row, date=bar["date"]))
    if panel.get("pattern_adjusted_basis"):
        points = [dict(r, upper=r.get("adjusted_upper", r["upper"]*r.get("basis_factor", 1.)), lower=r.get("adjusted_lower", r["lower"]*r.get("basis_factor", 1.)),
                       chart_ohlc=r["ohlc"], basis_factor=1.) for r in points]
    last = points[-1]
    close = last.get("chart_ohlc", last["ohlc"])[3]
    position = ("above" if close > last["upper"] else "below" if close < last["lower"] else "inside") if last["applicable"] else "not_applicable"
    return dict(view, rows=points, visible=True, adjusted_display=bool(panel.get("pattern_adjusted_basis")), detector_position=view.get("current_position"),
                current_position=position, data_date=last["date"], upper=last["upper"], lower=last["lower"])

def chart_label(view):
    """A single existing chart-header line; no separate triangle card."""
    if not view:
        return ""
    if not view.get("visible"):
        return "三角線暫無法核對，未疊線"
    if view.get("current_position") == "not_applicable":
        return "三角歷史線｜上下緣已交會，目前位置不適用"
    name = view["kind"] + ("候選" if view.get("candidate") else "")
    tip = "｜接近尖端" if view.get("near_tip") else ""
    basis = ("｜全圖同一還原基準" if view.get("adjusted_display") else "｜歷史段按除息換算") if view.get("historical_conversion") else ""
    return f"{name}｜橘上緣 {view['upper']:,.2f}、藍下緣 {view['lower']:,.2f}（當日）｜虛線：候選／參考／延伸{tip}{basis}"

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
                 "stock_name": p.get("stock_name", ""), "show_bollinger": not p.get("hide_bollinger", False),
                 "adjusted_basis": bool(p.get("pattern_adjusted_basis")), "volume_zones": chart_zones(p),
                 "key_levels": deepcopy(p.get("key_levels") or {})}
        if not stock["show_bollinger"]:
            stock["last_bar"] = {k: v for k, v in stock["last_bar"].items() if not k.startswith("BB_")}
        bar = stock["last_bar"]
        stock["ma_positions"] = {}
        try:
            close = float(bar["Close"])
        except (KeyError, TypeError, ValueError):
            close = float("nan")
        for key in ("MA5", "MA10", "MA20", "MA60"):
            try:
                ma = float(bar[key])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(close) and math.isfinite(ma) and close > 0 and ma > 0:
                stock["ma_positions"][key] = "站上" if close > ma else "跌破" if close < ma else "持平"
        view = p.get("member_triangle")
        if view:
            stock["triangle"] = ({k: deepcopy(v) for k, v in view.items() if k not in ("rows", "detector_position", "diagnostic")}
                                 if view.get("visible") else {"visible": False, "notice": view.get("notice")})
            stock["show_score"] = bool(p.get("scorecard"))
        out["stocks"][p["stock_code"]] = stock
    return out

def _without_bollinger(value):
    """Remove hidden Bollinger fields from AI evidence, leaving MA20 and all volume zones intact."""
    if isinstance(value, dict):
        return {k: _without_bollinger(v) for k, v in value.items()
                if not str(k).startswith("BB_") and str(k).lower() not in {"bollinger", "bollinger_bands"}}
    if isinstance(value, list):
        return [_without_bollinger(v) for v in value if not isinstance(v, str) or "布林" not in v]
    return value

def scoped_payload(payload, context):
    """Do not expose hidden score/KD/MACD facts as visible chart evidence."""
    payload = deepcopy(payload); payload["chart_context"] = deepcopy(context)
    for key, data in list((payload.get("tool_results") or {}).items()):
        if context.get("compound") and key.split(":")[0] == "get_recent_news" and isinstance(data, dict):
            data["articles"] = news_panel(data)["news_articles"]
        stock = context.get("stocks", {}).get(data.get("stock_code"), {}) if isinstance(data, dict) else {}
        if stock and not stock.get("show_bollinger", True):
            data = _without_bollinger(data)
            payload["tool_results"][key] = data
        if stock.get("adjusted_basis") and key.split(":")[0] == "get_volume_profile":
            payload["tool_results"][key] = {"stock_code": data.get("stock_code"), "data_date": data.get("data_date"),
                                            "close": stock["last_bar"]["Close"], **deepcopy(stock["volume_zones"])}
            continue
        if stock.get("adjusted_basis") and key.split(":")[0] == "get_technical_analysis":
            data = dict(data, moving_averages={k: {"value": stock["last_bar"].get(k)} for k in ("MA5", "MA10", "MA20", "MA60")},
                        ma_deduction=deepcopy((stock.get("key_levels") or {}).get("ma_deduction") or {}))
            payload["tool_results"][key] = data
        view = stock.get("triangle")
        if view:
            tool = key.split(":")[0]
            if not stock.get("show_score") and tool == "get_pattern_scorecard":
                del payload["tool_results"][key]; continue
            if tool == "get_technical_analysis":
                if not stock.get("show_score"):
                    data = {k: data[k] for k in ("stock_code", "stock_name", "data_date", "signal_status", "freshness", "triangle_structure") if k in data}
                    payload["tool_results"][key] = data
                structure = data.get("triangle_structure")
                if not view.get("visible"):
                    if isinstance(structure, dict):
                        data["triangle_history"] = {"historical_only": True, "pattern_events": deepcopy(structure.get("pattern_events") or [])}
                    data.pop("triangle_structure", None)
                    data["kline_patterns"] = [s for s in data.get("kline_patterns") or [] if not _TRIANGLE_POSITION.search(str(s))]
                    data["triangle_chart_notice"] = view.get("notice")
                elif isinstance(structure, dict):
                    structure["detector_position"] = structure.pop("current_position", None)
                    structure["chart_position"] = view.get("current_position")
                    structure["chart_position_date"] = view.get("data_date")
    return payload

_TRIANGLE_POSITION = re.compile(r"三角|候選線|型態[內外]|[上下]緣|越線|交會|線內|線外")

def without_unverified_positions(text, context):
    """Also guard final/late AI output; prompts alone are not validation."""
    stocks = context.get("stocks", {})
    blocked = {code: s for code, s in stocks.items() if s.get("triangle") and not s["triangle"].get("visible")}
    if not blocked:
        return str(text or "")
    invalid_names = {code for code in blocked} | {s.get("stock_name") for s in blocked.values() if s.get("stock_name")}
    valid_names = {name for code, s in stocks.items() if code not in blocked
                   for name in (code, s.get("stock_name")) if name}
    kept = []
    for sentence in re.split(r"(?<=[。！？；\n])", str(text or "")):
        if not _TRIANGLE_POSITION.search(sentence):
            kept.append(sentence); continue
        if re.search(r"量區|布林", sentence) and not re.search(r"三角|候選線|型態[內外]|越線|交會|線[內外]", sentence):
            kept.append(sentence); continue
        if any(name in sentence for name in valid_names) and not any(name in sentence for name in invalid_names):
            kept.append(sentence)
    return "".join(kept).strip()


def without_hidden_scores(text, context):
    # Enforce an entirely score-free page without inventing replacement analysis.
    stocks = list(context.get("stocks", {}).values())
    if not stocks or any(s.get("show_score") for s in stocks) or not any(s.get("triangle") for s in stocks):
        return text
    parts = re.split(r"(?<=[。！？；])", str(text or ""))
    score = re.compile(r"評分|評等|型態分數|加分|扣分|得分|\d+(?:\.\d+)?\s*(?:/\s*100|分(?=[，。；、\s]|$))")
    return "".join(p for p in parts if not score.search(p)).strip()
