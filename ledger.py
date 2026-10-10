#!/usr/bin/env python3
"""Signal ledger: persist OKX altcoin breakout signals + return tracking.

Shared by the scanner (writes new signals) and the dashboard server
(reads + refreshes returns). Pure stdlib.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".hermes" / "data" / "crypto_signals"
LEDGER_PATH = STATE_DIR / "okx_breakout_ledger.json"
CST = timezone(timedelta(hours=8))


def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            data = json.loads(LEDGER_PATH.read_text())
            if isinstance(data, dict) and isinstance(data.get("signals"), list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"signals": []}


def save_ledger(ledger: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2))
    tmp.replace(LEDGER_PATH)


def append_signal(sig: dict) -> bool:
    """Append a new signal event. Dedup by (symbol, signal_ts). Returns True if added."""
    ledger = load_ledger()
    sym = sig["symbol"]
    for s in ledger["signals"]:
        if s.get("symbol") == sym and s.get("signal_ts") == sig["signal_ts"]:
            return False
    sig["signal_date"] = datetime.fromtimestamp(sig["signal_ts"], tz=CST).strftime("%Y-%m-%d")
    sig["week"] = week_key(sig["signal_ts"])
    sig.setdefault("price_4h", None)
    sig.setdefault("ret_4h", None)
    sig.setdefault("price_8h", None)
    sig.setdefault("ret_8h", None)
    sig.setdefault("price_12h", None)
    sig.setdefault("ret_12h", None)
    sig.setdefault("last_price", None)
    sig.setdefault("ret_now", None)
    ledger["signals"].append(sig)
    ledger["signals"].sort(key=lambda s: s["signal_ts"])
    save_ledger(ledger)
    return True


def week_key(ts: int) -> str:
    """ISO week key like '2026-W41' (Monday start)."""
    iso = datetime.fromtimestamp(ts, tz=CST).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_label(ts: int) -> str:
    """Human label for a week, e.g. '10-06 ~ 10-12'."""
    dt = datetime.fromtimestamp(ts, tz=CST)
    monday = dt - timedelta(days=dt.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%m-%d')} ~ {sunday.strftime('%m-%d')}"


def fmt_cst(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=CST).strftime("%m-%d %H:%M")
