"""TAIEX avatar controller. Reuses existing quote cache; no extra scheduler or database writes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import threading
import time
from decimal import Decimal
from pathlib import Path

LOG = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent
AVATAR_DIR = PROJECT_ROOT / "ace_chopper_market_avatars"
THRESHOLDS = {"strong_up": 2.0, "up": 0.5, "down": -0.5, "strong_down": -2.0}
AVATARS = dict(zip(("strong_up", "up", "flat", "down", "strong_down", "closed"),
                  ("01_strong_up.png", "02_up.png", "03_flat.png", "04_down.png",
                   "05_strong_down.png", "06_closed.png")))
CHECK_SECONDS = 60
MIN_EDIT_SECONDS = 900
CLOSE_CHECK_MINUTE = 13 * 60 + 35
RETRY_CHECK_SECONDS = 600
STATE_PATH = Path(os.getenv("DISCORD_AI_AVATAR_STATE_FILE") or
                  str((Path("/data") if Path("/data").is_dir() else PROJECT_ROOT / ".cache")
                      / "market_avatar_state.json"))


def classify_market(change_pct=None, *, closed=False):
    if closed:
        return "closed"
    if change_pct is None:
        raise ValueError("TAIEX change is missing")
    value = float(change_pct)
    if not math.isfinite(value):
        raise ValueError("TAIEX change is not finite")
    if value >= THRESHOLDS["strong_up"]:
        return "strong_up"
    if value >= THRESHOLDS["up"]:
        return "up"
    if value <= THRESHOLDS["strong_down"]:
        return "strong_down"
    if value <= THRESHOLDS["down"]:
        return "down"
    return "flat"


def market_state(now, quote, day_status="unknown"):
    """Daily closing state. Missing data never implies a holiday."""
    if now.weekday() >= 5 or day_status == "closed":
        return "closed"
    if now.hour * 60 + now.minute < CLOSE_CHECK_MINUTE:
        raise ValueError("not yet closing-check time")
    if not quote or quote.get("date") != now.strftime("%Y%m%d"):
        raise ValueError("TAIEX closing quote missing or not dated today; keep avatar")
    stamp = str(quote.get("time") or "")
    try:
        quote_minute = int(stamp[:2]) * 60 + int(stamp[3:5])
    except ValueError as exc:
        raise ValueError("TAIEX closing quote time invalid") from exc
    if quote_minute < 13 * 60 + 30:
        raise ValueError("TAIEX quote predates close; keep avatar")
    return classify_market(quote["change_pct"])


class AvatarController:
    def __init__(self, client, loop, *, state_path=STATE_PATH, avatar_dir=AVATAR_DIR):
        self.client, self.loop = client, loop
        self.state_path, self.avatar_dir = Path(state_path), Path(avatar_dir)
        self.lock = threading.Lock()
        self.pending = False
        self.next_check = 0.0
        try:
            self.saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(self.saved, dict):
                raise ValueError("invalid state file")
        except (OSError, ValueError):
            self.saved = {}

    def _save(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.saved), encoding="utf-8")
            temp.replace(self.state_path)
        except OSError:
            LOG.exception("Avatar state persistence failed; using current process state")

    def tick(self):
        with self.lock:
            if self.pending or time.monotonic() < self.next_check or self.loop.is_closed():
                return
            self.next_check = time.monotonic() + CHECK_SECONDS
            self.pending = True
        try:
            import warrant_ai_tools as tools
            import local_market_cache
            now = tools.taipei_now()
            day = now.strftime("%Y-%m-%d")
            if (self.saved.get("checked_day") == day
                    and self.saved.get("user_id") == str(getattr(self.client.user, "id", ""))
                    and self.saved.get("avatar_key") == getattr(getattr(self.client.user, "avatar", None), "key", None)):
                with self.lock:
                    self.pending = False
                return
            status = "closed" if now.weekday() >= 5 else "unknown"
            if now.weekday() < 5:
                status = local_market_cache.market_status([day])[day]["twse"]
                if status != "closed" and now.hour * 60 + now.minute < CLOSE_CHECK_MINUTE:
                    with self.lock:
                        self.pending = False
                    return
            quote = None
            if status != "closed":
                if tools.closed_quotes_only() and tools.finmind_background_allowed():   # 10-06：FinMind 額度不足改用證交所報價
                    frame = tools._load_index_bundle("TAIEX")["closed_df"]
                    if len(frame) < 2:
                        raise ValueError("TAIEX closing history missing; keep avatar")
                    raw = {"date": frame.index[-1], "time": "13:30",
                           "close": frame["Close"].iloc[-1],
                           "previous_close": frame["Close"].iloc[-2]}
                else:
                    raw = tools._cached("index_quote_TAIEX", max(20, tools.TTL_INTRADAY_SECONDS),
                                        lambda: tools.fetch_index_quote("TAIEX"))
                previous, close = float(raw["previous_close"]), float(raw["close"])
                if not math.isfinite(previous) or not math.isfinite(close) or previous <= 0 or close <= 0:
                    raise ValueError("TAIEX closing or previous closing index is invalid")
                quote = {"date": raw["date"].strftime("%Y%m%d"), "time": raw["time"],
                         "change_pct": float((Decimal(str(close)) / Decimal(str(previous)) - 1) * 100)}
            state = market_state(now, quote, status)
            asyncio.run_coroutine_threadsafe(self._update(state, day), self.loop)
        except Exception:
            self.next_check = time.monotonic() + RETRY_CHECK_SECONDS
            LOG.exception("Market avatar check failed; keeping current avatar")
            with self.lock:
                self.pending = False

    async def _update(self, state, day=None):
        try:
            user = self.client.user
            if user is None or self.client.is_closed():
                return
            data = await asyncio.to_thread((self.avatar_dir / AVATARS[state]).read_bytes)
            digest = hashlib.sha256(data).hexdigest()
            key = getattr(user.avatar, "key", None)
            if (self.saved.get("user_id") == str(user.id) and self.saved.get("state") == state
                    and self.saved.get("avatar_key") == key and self.saved.get("image_sha256") == digest):
                self.saved["checked_day"] = day
                await asyncio.to_thread(self._save)
                return
            now = time.time()
            if now < float(self.saved.get("next_edit", 0)):
                return
            # Persist attempt cooldown before API, including failed attempts and restarts.
            self.saved["next_edit"] = now + MIN_EDIT_SECONDS
            await asyncio.to_thread(self._save)
            updated = await user.edit(avatar=data)
            self.saved.update(user_id=str(user.id), state=state, checked_day=day,
                              avatar_key=getattr(updated.avatar, "key", None), image_sha256=digest)
            await asyncio.to_thread(self._save)
            print(f"Discord market avatar updated: {state} ({AVATARS[state]})", flush=True)
        except Exception as exc:
            retry = getattr(exc, "retry_after", None)
            if retry is not None:
                try:
                    self.saved["next_edit"] = max(float(self.saved.get("next_edit", 0)), time.time() + float(retry))
                    await asyncio.to_thread(self._save)
                except (TypeError, ValueError):
                    pass
            LOG.exception("Discord avatar update failed; bot continues")
        finally:
            with self.lock:
                self.pending = False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Offline avatar mapping preview; never calls Discord")
    parser.add_argument("--state", choices=AVATARS, required=True)
    args = parser.parse_args()
    path = AVATAR_DIR / AVATARS[args.state]
    print(f"{args.state}: {path.name} | exists={path.is_file()}")
    if not path.is_file():
        raise SystemExit(1)
