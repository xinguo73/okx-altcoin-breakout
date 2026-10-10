#!/usr/bin/env python3
"""OKX 山寨币启动扫描（5日新高 + 24h成交额>5000万USDT 的 USDT 永续合约）。

信号定义（"启动"= 今日首次突破）：
  - 现价 last > 前5日最高价（不含今日，data[1:6] 的 high 最大值）
  - 且昨日 high 未超过"昨日的前5日最高价"（data[2:7]），即昨日尚未启动、
    今日才首次突破 → 抓的是启动那一刻，不是连续上涨中的每日新高。

过滤：
  - 仅 USDT 本位永续（instId 以 -USDT-SWAP 结尾）
  - 剔除 BTC/ETH 及稳定币
  - 24h 成交额 volCcy24h × 现价 >= 50_000_000 USDT（volCcy24h 是币数，非 USDT）

去重：每标的每"启动事件"只报一次（last_alert_date 状态文件 + freshness 门槛）。
纯标准库 requests+json；stdout 输出信号行，无信号=空输出=不投递(no_agent cron watchdog)。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# 让脚本无论从哪个 cwd 运行都能 import 同目录的 ledger 模块
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import append_signal  # noqa: E402

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
STATE_DIR = Path.home() / ".hermes" / "data" / "crypto_signals"

# ── 参数 ──────────────────────────────────────────────────────────────────
MIN_VOL_USDT = 50_000_000   # 24h 成交额下限（USDT）
BREAKOUT_DAYS = 5           # 创 N 日新高
CANDLE_LIMIT = 7            # 日线根数（今日 + 昨日 + 前5日 + 昨日的1日冗余）
EXCLUDE_BASE = {"BTC", "ETH", "USDT", "USDC", "DAI", "TUSD", "BUSD", "FDUSD",
                "USDP", "USD1", "AEUR", "EURT"}
HEADERS = {"User-Agent": "okx-altcoin-breakout-monitor/1.0"}

CST = timezone(timedelta(hours=8))

_SESSION = requests.Session()


def fetch_tickers() -> list[dict]:
    r = _SESSION.get(TICKERS_URL, params={"instType": "SWAP"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    p = r.json()
    if p.get("code") != "0":
        raise RuntimeError(f"tickers code={p.get('code')} msg={p.get('msg')}")
    return p.get("data") or []


def fetch_daily_candles(inst_id: str) -> list[list]:
    r = _SESSION.get(CANDLES_URL,
                     params={"instId": inst_id, "bar": "1D",
                             "limit": str(CANDLE_LIMIT)},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    p = r.json()
    if p.get("code") != "0":
        raise RuntimeError(f"candles code={p.get('code')} msg={p.get('msg')}")
    return p.get("data") or []   # 最新在前


def max_high(candles: list[list], lo: int, hi: int) -> float | None:
    """candles[lo:hi] 的最高价（hi 不含）。"""
    vals = []
    for c in candles[lo:hi]:
        try:
            vals.append(float(c[2]))
        except (IndexError, ValueError, TypeError):
            continue
    return max(vals) if vals else None


def load_state(sym: str) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    sf = STATE_DIR / f"okx_breakout_{sym}.json"
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(sym: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"okx_breakout_{sym}.json").write_text(json.dumps(state))


def fmt_num(x: float | None, nd: int = 4) -> str:
    if x is None:
        return "-"
    if x >= 1000:
        return f"{x:,.0f}"
    return f"{x:.{nd}f}".rstrip("0").rstrip(".")


def fmt_vol(v: float) -> str:
    if v >= 1e9:
        return f"{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v:,.0f}"


def format_signal(sig: dict) -> str:
    sym = sig["base"]
    lines = [f"【山寨启动】{sym} 创{BREAKOUT_DAYS}日新高"]
    lines.append(f"现价 {fmt_num(sig['last'])} USDT（24h {sig['chg_24h'] * 100:+.2f}%）")
    lines.append(f"24h成交额 {fmt_vol(sig['vol_usdt'])} USDT")
    lines.append(f"突破前{BREAKOUT_DAYS}日高点 +{sig['breakout_pct'] * 100:.2f}%"
                 f"（前高 {fmt_num(sig['prior_high'])})")
    lines.append(f"时间 {sig['cst_time']}（北京）")
    return "\n".join(lines)


def main() -> int:
    if "--selftest" in sys.argv:
        print("【山寨启动扫描·投递测试】收到此消息即表示飞书群投递正常。"
              f"策略：USDT永续，现价创{BREAKOUT_DAYS}日新高且昨日未启动，"
              f"24h成交额≥{MIN_VOL_USDT / 1e6:.0f}M USDT，剔除BTC/ETH/稳定币。")
        return 0

    try:
        tickers = fetch_tickers()
    except Exception as e:
        print(json.dumps({"status": "fetch_tickers_failed", "error": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 1

    now = datetime.now(CST)
    today_str = now.strftime("%Y-%m-%d")
    cst_time = now.strftime("%m-%d %H:%M")

    candidates = []
    for t in tickers:
        inst = t.get("instId") or ""
        if not inst.endswith("-USDT-SWAP"):
            continue
        base = inst.split("-")[0].upper()
        if base in EXCLUDE_BASE:
            continue
        try:
            vol_base = float(t.get("volCcy24h") or 0)
            last = float(t.get("last") or 0)
            open24h = float(t.get("open24h") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0:
            continue
        # volCcy24h 是 24h 成交量（币数），×现价才是 USDT 成交额
        vol_usdt = vol_base * last
        if vol_usdt < MIN_VOL_USDT:
            continue
        candidates.append({"inst_id": inst, "base": base, "vol_usdt": vol_usdt,
                           "last": last, "open24h": open24h})

    emitted = 0
    for c in candidates:
        try:
            candles = fetch_daily_candles(c["inst_id"])
        except Exception as e:
            print(json.dumps({"inst_id": c["inst_id"], "status": "candle_fetch_failed",
                              "error": str(e)}, ensure_ascii=False), file=sys.stderr)
            continue
        if len(candles) < CANDLE_LIMIT:
            continue

        prior_5d_high = max_high(candles, 1, 1 + BREAKOUT_DAYS)   # 昨日至前5日
        yday_prior_5d_high = max_high(candles, 2, 2 + BREAKOUT_DAYS)  # 昨日的前5日
        if prior_5d_high is None or yday_prior_5d_high is None:
            continue
        try:
            yday_high = float(candles[1][2])
        except (IndexError, ValueError, TypeError):
            continue

        # 今日突破 + 昨日尚未突破（freshness 门槛 = "启动"而非连涨中的每日新高）
        today_breakout = c["last"] > prior_5d_high
        yday_not_breakout = yday_high <= yday_prior_5d_high
        if not (today_breakout and yday_not_breakout):
            continue

        # 去重：同一"启动事件"只报一次（同日状态兜底）
        state = load_state(c["base"])
        if state.get("last_alert_date") == today_str:
            continue

        chg_24h = (c["last"] - c["open24h"]) / c["open24h"] if c["open24h"] > 0 else 0.0
        breakout_pct = (c["last"] - prior_5d_high) / prior_5d_high
        sig = {
            "base": c["base"],
            "inst_id": c["inst_id"],
            "last": c["last"],
            "vol_usdt": c["vol_usdt"],
            "chg_24h": chg_24h,
            "prior_high": prior_5d_high,
            "breakout_pct": breakout_pct,
            "cst_time": cst_time,
        }
        print(format_signal(sig))
        emitted += 1
        save_state(c["base"], {"last_alert_date": today_str,
                               "last_alert_ts": int(now.timestamp()),
                               "inst_id": c["inst_id"],
                               "prior_high": prior_5d_high})
        # 记入信号台账（供网页看板追踪 4h/8h/12h/当下收益）
        append_signal({
            "symbol": c["base"],
            "inst_id": c["inst_id"],
            "signal_ts": int(now.timestamp()),
            "entry_price": c["last"],
            "vol_usdt": c["vol_usdt"],
            "chg_24h": chg_24h,
            "prior_high": prior_5d_high,
            "breakout_pct": breakout_pct,
        })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
