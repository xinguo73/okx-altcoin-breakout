#!/usr/bin/env python3
"""Backtest the 5m-volume filter: at signal time, the 5m candle's USDT
trading value (volCcyQuote) must exceed VOL_5M_MIN (3,000,000 USDT).

Replays the 15m ATR signal over the last 70h, then for each signal fetches the
3 constituent 5m candles and checks their max USDT volume. Reports signal counts
and +12h/+24h win rates split by pass/fail of the filter.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
HIST_URL = "https://www.okx.com/api/v5/market/history-candles"
HEADERS = {"User-Agent": "okx-backtest/1.0"}
EXCLUDE = {"USDT", "USDC", "DAI", "TUSD", "BUSD", "FDUSD", "USDP", "USD1", "AEUR", "EURT"}
MIN_VOL_USDT = 10_000_000
ATR_MULT = 2.0
ATR_PERIOD = 14
MAX_ATR_PCT = 1.5
VOL_5M_MIN = 3_000_000   # 5m 成交金额下限 (USDT)
WINDOW_HOURS = 70

_SESSION = requests.Session()


def get(url, params):
    r = _SESSION.get(url, params=params, headers=HEADERS, timeout=30)
    return r.json()


def fetch_5m_usdt(inst: str, since: int) -> dict[int, float]:
    """5m open-time (s) -> USDT volume (volCcyQuote), from `since` to now.

    OKX history-candles: `after=X` returns records OLDER than X (descending).
    So page backward: start newest, then `after=oldest` each step.
    """
    out: dict[int, float] = {}
    after = None
    for _ in range(15):
        params = {"instId": inst, "bar": "5m", "limit": "100"}
        if after:
            params["after"] = str(after * 1000)
        p = get(HIST_URL, params)
        data = p.get("data") or []
        if not data:
            break
        for c in data:
            ts = int(c[0]) // 1000
            if ts < since:
                continue
            try:
                out[ts] = float(c[7])
            except (IndexError, ValueError, TypeError):
                out[ts] = 0.0
        oldest = int(data[-1][0]) // 1000
        after = oldest
        if len(data) < 100 or oldest <= since:
            break
    return out


def fetch_1h_closes(inst: str) -> dict[int, float]:
    p = get(CANDLES_URL, {"instId": inst, "bar": "1H", "limit": "100"})
    out: dict[int, float] = {}
    for c in p.get("data") or []:
        try:
            out[int(c[0]) // 1000] = float(c[4])
        except (IndexError, ValueError, TypeError):
            continue
    return out


def main() -> int:
    tk = get(TICKERS_URL, {"instType": "SWAP"})["data"]
    coins = []
    for t in tk:
        inst = t.get("instId") or ""
        if not inst.endswith("-USDT-SWAP"):
            continue
        base = inst.split("-")[0].upper()
        if base in EXCLUDE:
            continue
        try:
            last = float(t["last"])
        except (TypeError, ValueError):
            continue
        if last <= 0:
            continue
        vol = float(t.get("volCcy24h") or 0) * last
        if vol > MIN_VOL_USDT:
            coins.append((inst, base))

    cutoff = int(time.time()) - WINDOW_HOURS * 3600
    signals = []   # (inst, base, bar_ts(open s), entry, atr_ratio, atr_pct)

    for inst, base in coins:
        p = get(CANDLES_URL, {"instId": inst, "bar": "15m", "limit": "300"})
        bars = [c for c in reversed(p.get("data") or []) if str(c[8]) == "1"]
        n = len(bars)
        if n < 30:
            continue
        trs = []
        for i in range(n):
            hi = float(bars[i][2]); lo = float(bars[i][3])
            pc = float(bars[i - 1][4]) if i > 0 else float(bars[i][1])
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
        for i in range(14, n):
            ts = int(bars[i][0]) // 1000
            if ts < cutoff:
                continue
            w = trs[i - 13:i + 1]
            atr = sum(w) / len(w)
            close = float(bars[i][4]); opn = float(bars[i][1])
            if atr <= 0:
                continue
            ratio = trs[i] / atr
            ap = atr / close * 100
            if ratio > ATR_MULT and ap < MAX_ATR_PCT and close > opn:
                signals.append((inst, base, ts, close, ratio, ap))

    # 5m 成交额 + 1H 收盘（按币缓存）
    u5 = {}; u1h = {}
    syms = sorted({s[0] for s in signals})
    for inst in syms:
        u5[inst] = fetch_5m_usdt(inst, cutoff)
        u1h[inst] = fetch_1h_closes(inst)

    def ret_at(sig, oh):
        inst, _, ts, entry = sig[0], sig[1], sig[2], sig[3]
        closes = u1h[inst]
        target = ((ts + 900 + oh * 3600) // 3600) * 3600
        c = closes.get(target)
        return (c - entry) / entry if c is not None else None

    pass_sig, fail_sig = [], []
    for s in signals:
        inst, base, ts = s[0], s[1], s[2]
        vols = u5[inst]
        peak = max(vols.get(ts, 0), vols.get(ts + 300, 0), vols.get(ts + 600, 0))
        if peak > VOL_5M_MIN:
            pass_sig.append(s)
        else:
            fail_sig.append(s)

    def winrate(grp, oh):
        vals = [ret_at(s, oh) for s in grp]
        vals = [v for v in vals if v is not None]
        if not vals:
            return "n/a"
        wins = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        return f"{wins / len(vals) * 100:.0f}% ({wins}/{len(vals)}) 均值{avg * 100:+.1f}%"

    print(f"回测窗口 {WINDOW_HOURS}h · 候选 {len(coins)} 币 · 总信号 {len(signals)}")
    m = VOL_5M_MIN / 1e4   # 万 USDT
    print(f"5m成交额>{m:.0f}万USDT: {len(pass_sig)} 个"
          f" · ≤{m:.0f}万: {len(fail_sig)} 个")
    for grp_name, grp in (("通过(>300万)", pass_sig), ("未过(≤300万)", fail_sig)):
        if not grp:
            continue
        print(f"  [{grp_name}] n={len(grp)}  "
              f"+12h {winrate(grp, 12)}  +24h {winrate(grp, 24)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
