#!/usr/bin/env python3
"""Backfill the signal ledger with historical signals (last ~70h) for dashboard viewing.

Replays the ATR-expansion signal over the last 70h for all liquid USDT swaps and
writes the resulting signals (with entry price, ATR ratio, and relative-strength RS)
into the ledger, so the dashboard can show a full historical picture.

Note: the live scanner computes RS on 5m candles; this backfill uses 15m candles
(≈2h window) as a faster approximation of the same "BTC 阴线时币不阴线" measure.
Pure stdlib + requests.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import STATE_DIR, week_key, save_ledger  # noqa: E402

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
HEADERS = {"User-Agent": "okx-backfill/1.0"}
EXCLUDE_BASE = {"USDT", "USDC", "DAI", "TUSD", "BUSD", "FDUSD",
                "USDP", "USD1", "AEUR", "EURT"}
MIN_VOL_USDT = 10_000_000
ATR_MULT = 2.0
ATR_PERIOD = 14
MAX_ATR_PCT = 1.5
RS_WINDOW = 8           # 15m bars ≈ 2h（近似 5m 的 1h 窗口）
RS_MIN_BTC_DOWN = 3
CORR_WINDOW = 16        # 相关性窗口（15m 柱数 = 4h，与扫描器一致）
CORR_MAX = 0.8          # 相关性上限：> 此值 = 跟随 BTC，过滤
COOLDOWN_HOURS = 12     # 同一币种冷却期（小时）：期内不重复报信号
WINDOW_HOURS = 70
CST = timezone(timedelta(hours=8))

_SESSION = requests.Session()


def fetch(inst: str, bar: str, limit: int) -> list[list]:
    r = _SESSION.get(CANDLES_URL,
                     params={"instId": inst, "bar": bar, "limit": str(limit)},
                     headers=HEADERS, timeout=20)
    p = r.json()
    if p.get("code") != "0":
        return []
    return p.get("data") or []   # 最新在前


def true_range(c, i):
    hi = float(c[i][2]); lo = float(c[i][3]); pc = float(c[i + 1][4])
    return max(hi - lo, abs(hi - pc), abs(lo - pc))


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


def corr_at(bars, btc, i, W=CORR_WINDOW):
    """bars[i] 相对 BTC 的 15m 收益率相关性（窗口 W，结束于 i）。"""
    xs, ys = [], []
    for j in range(i - W + 1, i + 1):
        if j <= 0 or j >= len(btc):
            continue
        try:
            c0 = float(bars[j - 1][4]); c1 = float(bars[j][4])
            b0 = float(btc[j - 1][4]); b1 = float(btc[j][4])
        except (IndexError, ValueError, TypeError):
            continue
        if c0 == 0 or b0 == 0:
            continue
        xs.append(c1 / c0 - 1.0)
        ys.append(b1 / b0 - 1.0)
    if len(xs) < 8:
        return None
    return pearson(xs, ys)


def main() -> int:
    tk = _SESSION.get(TICKERS_URL, params={"instType": "SWAP"},
                      headers=HEADERS, timeout=30).json()["data"]
    coins = []
    for t in tk:
        inst = t.get("instId") or ""
        if not inst.endswith("-USDT-SWAP"):
            continue
        base = inst.split("-")[0].upper()
        if base in EXCLUDE_BASE:
            continue
        try:
            last = float(t["last"])
        except (TypeError, ValueError):
            continue
        if last <= 0:
            continue
        vol = float(t.get("volCcy24h") or 0) * last
        if vol > MIN_VOL_USDT:
            coins.append((inst, base, vol))

    btc = [c for c in reversed(fetch("BTC-USDT-SWAP", "15m", 300)) if str(c[8]) == "1"]

    signals = []
    latest_bar: dict[str, int] = {}
    cutoff = int(time.time()) - WINDOW_HOURS * 3600

    for inst, base, vol in coins:
        bars = [c for c in reversed(fetch(inst, "15m", 300)) if str(c[8]) == "1"]
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
                # 相关性 vs BTC：过滤跟随 BTC 的品种
                corr = corr_at(bars, btc, i)
                if corr is not None and corr > CORR_MAX:
                    continue
                btc_down = coin_held = 0
                for j in range(max(0, i - RS_WINDOW + 1), i + 1):
                    if j >= len(btc):
                        break
                    if float(btc[j][4]) < float(btc[j][1]):
                        btc_down += 1
                        if float(bars[j][4]) >= float(bars[j][1]):
                            coin_held += 1
                rs = coin_held / btc_down if btc_down >= RS_MIN_BTC_DOWN else None
                signals.append({
                    "symbol": base,
                    "inst_id": inst,
                    "signal_ts": ts + 900,   # bar 收盘时间
                    "entry_price": close,
                    "vol_usdt": round(vol, 2),
                    "chg_24h": 0.0,
                    "atr_ratio": round(ratio, 3),
                    "atr_pct": round(ap, 3),
                    "rs": round(rs, 3) if rs is not None else None,
                    "corr": round(corr, 3) if corr is not None else None,
                })
                latest_bar[base] = ts

    # 去重 (symbol, signal_ts)
    seen = set()
    uniq = []
    for s in signals:
        k = (s["symbol"], s["signal_ts"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    uniq.sort(key=lambda s: s["signal_ts"])
    # 冷却去重：同一币种 COOLDOWN_HOURS 小时内只保留第一个信号
    cooled = []
    last_by_sym: dict[str, int] = {}
    for s in uniq:
        sym = s["symbol"]
        if sym in last_by_sym and s["signal_ts"] - last_by_sym[sym] < COOLDOWN_HOURS * 3600:
            continue
        last_by_sym[sym] = s["signal_ts"]
        cooled.append(s)
    uniq = cooled
    for s in uniq:
        s["signal_date"] = datetime.fromtimestamp(s["signal_ts"], tz=CST).strftime("%Y-%m-%d")
        s["week"] = week_key(s["signal_ts"])

    # 补算收益（last_price + ret_4h/8h/12h），让看板首屏渲染快
    ticker_map = {}
    try:
        tk2 = _SESSION.get(TICKERS_URL, params={"instType": "SWAP"},
                           headers=HEADERS, timeout=30).json()["data"]
        for t in tk2:
            try:
                ticker_map[t["instId"].split("-")[0].upper()] = float(t["last"])
            except (KeyError, TypeError, ValueError):
                pass
    except Exception:
        pass

    hourly_cache: dict[str, dict[int, float]] = {}
    for s in uniq:
        inst = s["inst_id"]
        if inst not in hourly_cache:
            hourly_cache[inst] = {}
            try:
                p = _SESSION.get(CANDLES_URL,
                                 params={"instId": inst, "bar": "1H", "limit": "100"},
                                 headers=HEADERS, timeout=20).json()
                for c in p.get("data") or []:
                    hourly_cache[inst][int(c[0]) // 1000] = float(c[4])
            except Exception:
                pass
        candles = hourly_cache[inst]
        t0 = s["signal_ts"]
        for label, oh in (("4h", 4), ("8h", 8), ("12h", 12)):
            target = ((t0 + oh * 3600) // 3600) * 3600
            close = candles.get(target)
            if close is None:
                continue
            s[f"price_{label}"] = close
            s[f"ret_{label}"] = (close - s["entry_price"]) / s["entry_price"]
        last = ticker_map.get(s["symbol"])
        if last and last > 0:
            s["last_price"] = last
            s["ret_now"] = (last - s["entry_price"]) / s["entry_price"]

    save_ledger({"signals": uniq})
    # 写 state 文件，让 live 扫描器不重复报最近这根柱
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for sym, ts in latest_bar.items():
        (STATE_DIR / f"okx_breakout_{sym}.json").write_text(
            json.dumps({"last_alert_bar_ts": ts}))

    strong = sum(1 for s in uniq if s.get("rs") is not None and s["rs"] >= 0.7)
    print(f"backfilled {len(uniq)} signals (strong RS>=0.7: {strong}) "
          f"over last {WINDOW_HOURS}h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
