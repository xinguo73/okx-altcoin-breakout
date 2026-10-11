#!/usr/bin/env python3
"""Backtest a BTC-correlation filter: coins whose 15m returns track BTC too
closely (high |corr|) have no independent move (pure beta), so filter them out.

Correlation is computed over the 24 x 15m bars (6h) ending at the signal bar,
covering BOTH up and down co-movement (long/short directions), which is exactly
what the Pearson coefficient captures symmetrically.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
HEADERS = {"User-Agent": "okx-backtest/1.0"}
EXCLUDE = {"USDT", "USDC", "DAI", "TUSD", "BUSD", "FDUSD", "USDP", "USD1", "AEUR", "EURT"}
MIN_VOL_USDT = 10_000_000
ATR_MULT = 2.0
ATR_PERIOD = 14
MAX_ATR_PCT = 1.5
CORR_WINDOW = 24        # 15m bars = 6h
WINDOW_HOURS = 70

_SESSION = requests.Session()


def get(url, params):
    r = _SESSION.get(url, params=params, headers=HEADERS, timeout=30)
    return r.json()


def fetch_bars(inst, bar, limit):
    p = get(CANDLES_URL, {"instId": inst, "bar": bar, "limit": str(limit)})
    return [c for c in reversed(p.get("data") or []) if str(c[8]) == "1"]


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def corr_with_btc(bars, i, btc_close, W=CORR_WINDOW):
    """Pearson corr of returns over bars [i-W+1, i], aligned to BTC by ts."""
    xs, ys = [], []
    for j in range(i - W + 1, i + 1):
        if j <= 0:
            continue
        c0 = float(bars[j - 1][4]); c1 = float(bars[j][4])
        if c0 == 0:
            continue
        t0 = int(bars[j - 1][0]) // 1000
        t1 = int(bars[j][0]) // 1000
        b0 = btc_close.get(t0); b1 = btc_close.get(t1)
        if b0 is None or b1 is None or b0 == 0:
            continue
        xs.append(c1 / c0 - 1.0)
        ys.append(b1 / b0 - 1.0)
    if len(xs) < 8:
        return None
    return pearson(xs, ys)


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

    btc_bars = fetch_bars("BTC-USDT-SWAP", "15m", 300)
    btc_close = {int(c[0]) // 1000: float(c[4]) for c in btc_bars}

    cutoff = int(time.time()) - WINDOW_HOURS * 3600
    signals = []   # (inst, base, bar_idx, ts, close, corr)

    for inst, base in coins:
        bars = fetch_bars(inst, "15m", 300)
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
                corr = corr_with_btc(bars, i, btc_close)
                signals.append((inst, base, i, ts, close, corr))

    # 1H closes for returns
    u1h = {}
    for inst in sorted({s[0] for s in signals}):
        u1h[inst] = {int(c[0]) // 1000: float(c[4])
                     for c in fetch_bars(inst, "1H", 100)}

    def ret_at(sig, oh):
        inst, ts, entry = sig[0], sig[3], sig[4]
        target = ((ts + 900 + oh * 3600) // 3600) * 3600
        c = u1h[inst].get(target)
        return (c - entry) / entry if c is not None else None

    def winrate(grp, oh):
        vals = [ret_at(s, oh) for s in grp]
        vals = [v for v in vals if v is not None]
        if not vals:
            return "n/a"
        wins = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        return f"{wins / len(vals) * 100:.0f}% ({wins}/{len(vals)}) 均值{avg * 100:+.1f}%"

    with_corr = [s for s in signals if s[5] is not None]
    no_corr = [s for s in signals if s[5] is None]

    buckets = [
        ("跟随BTC  corr>0.8", lambda c: c > 0.8),
        ("中度     0.5<corr≤0.8", lambda c: 0.5 < c <= 0.8),
        ("弱相关   0≤corr≤0.5", lambda c: 0 <= c <= 0.5),
        ("反向     corr<0", lambda c: c < 0),
    ]

    print(f"回测窗口 {WINDOW_HOURS}h · 总信号 {len(signals)} "
          f"(可算相关性 {len(with_corr)} · 数据不足 {len(no_corr)})")
    for label, fn in buckets:
        grp = [s for s in with_corr if fn(s[5])]
        if not grp:
            print(f"  [{label}] n=0")
            continue
        print(f"  [{label}] n={len(grp)}  +12h {winrate(grp, 12)}  +24h {winrate(grp, 24)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
