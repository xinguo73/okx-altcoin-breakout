#!/usr/bin/env python3
"""Compare BTC-correlation windows to find the best one for the
'filter out BTC-followers (high positive corr)' rule.

For each window W (in 15m bars), compute the coin-vs-BTC return correlation at
each signal and report the +12h/+24h win rate & avg return of:
  corr > 0.8   (BTC followers -> filter out)
  corr <= 0.8  (kept)
  corr < 0     (inverse/independent)
The best window is the one where corr>0.8 is clearly worst and the kept group
is clearly best (largest lift).
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
WINDOW_HOURS = 70
WINDOWS = [8, 12, 16, 24, 36, 48]   # 15m bars = 2h/3h/4h/6h/9h/12h

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
    # 每个信号存: (inst, base, ts, close, {W: corr})
    signals = []

    for inst, base in coins:
        bars = fetch_bars(inst, "15m", 300)
        n = len(bars)
        if n < 30:
            continue
        # 收益率序列 + 对齐 BTC 收益
        cret: list[float | None] = [None] * n
        bret: list[float | None] = [None] * n
        for j in range(1, n):
            c0 = float(bars[j - 1][4]); c1 = float(bars[j][4])
            if c0 == 0:
                continue
            t0 = int(bars[j - 1][0]) // 1000
            t1 = int(bars[j][0]) // 1000
            b0 = btc_close.get(t0); b1 = btc_close.get(t1)
            cret[j] = c1 / c0 - 1.0
            if b0 and b1:
                bret[j] = b1 / b0 - 1.0

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
                corrs = {}
                for W in WINDOWS:
                    xs2, ys2 = [], []
                    for j in range(i - W + 1, i + 1):
                        if cret[j] is not None and bret[j] is not None:
                            xs2.append(cret[j]); ys2.append(bret[j])
                    corrs[W] = pearson(xs2, ys2) if len(xs2) >= 8 else None
                signals.append((inst, base, ts, close, corrs))

    u1h = {}
    for inst in sorted({s[0] for s in signals}):
        u1h[inst] = {int(c[0]) // 1000: float(c[4])
                     for c in fetch_bars(inst, "1H", 100)}

    def ret_at(sig, oh):
        inst, ts, entry = sig[0], sig[2], sig[3]
        target = ((ts + 900 + oh * 3600) // 3600) * 3600
        c = u1h[inst].get(target)
        return (c - entry) / entry if c is not None else None

    def wl(grp, oh):
        vals = [ret_at(s, oh) for s in grp]
        vals = [v for v in vals if v is not None]
        if not vals:
            return "n/a"
        wins = sum(1 for v in vals if v > 0)
        return f"{wins / len(vals) * 100:.0f}%/{sum(vals) / len(vals) * 100:+.1f}%"

    print(f"窗口(W=15m柱)  |  corr>0.8(过滤)  |  corr≤0.8(保留)  |  corr<0(反向)")
    print("-" * 72)
    for W in WINDOWS:
        high = [s for s in signals if s[4].get(W) is not None and s[4][W] > 0.8]
        low = [s for s in signals if s[4].get(W) is not None and s[4][W] <= 0.8]
        inv = [s for s in signals if s[4].get(W) is not None and s[4][W] < 0]
        h24 = wl(high, 24); l24 = wl(low, 24); i24 = wl(inv, 24)
        print(f"  W={W:2d}({W*15//60}h)   | n={len(high):3d} {h24}  | "
              f"n={len(low):3d} {l24}  | n={len(inv):3d} {i24}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
