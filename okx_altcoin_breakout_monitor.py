#!/usr/bin/env python3
"""OKX 山寨币启动扫描（15m 波动放大 + 24h成交额>1000万USDT 的 USDT 永续合约）。

信号定义（"启动" = 波动突然放大）：
  - 最新一根已完成 15m K 线的真实波幅 TR > ATR_MULT × ATR14
    （ATR14 = 前 14 根已完成 15m 柱的平均真实波幅）
  - 且 24h 成交额 volCcy24h × 现价 > 10_000_000 USDT（过滤低流动性的杂波）

过滤：
  - 仅 USDT 本位永续（instId 以 -USDT-SWAP 结尾）
  - 剔除稳定币（USDT/USDC/DAI 等；BTC/ETH 及代币化股票/商品均纳入）

去重：每标的每根已完成 15m 柱只报一次（last_alert_bar_ts 状态文件）。
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
MIN_VOL_USDT = 10_000_000   # 24h 成交额下限（USDT）
BAR = "15m"                 # K 线周期
ATR_MULT = 1.5              # 波动放大倍数（当下 TR > ATR_MULT × ATR14）
ATR_PERIOD = 14             # ATR 周期（前 N 根已完成柱）
CANDLE_LIMIT = ATR_PERIOD + 2   # 15m 柱数（当前未完成 + 最新已完成 + ATR周期 + 1 根前收）
EXCLUDE_BASE = {"USDT", "USDC", "DAI", "TUSD", "BUSD", "FDUSD",
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


def fetch_candles(inst_id: str) -> list[list]:
    r = _SESSION.get(CANDLES_URL,
                     params={"instId": inst_id, "bar": BAR,
                             "limit": str(CANDLE_LIMIT)},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    p = r.json()
    if p.get("code") != "0":
        raise RuntimeError(f"candles code={p.get('code')} msg={p.get('msg')}")
    return p.get("data") or []   # 最新在前


def true_range(candles: list[list], i: int) -> float:
    hi = float(candles[i][2])
    lo = float(candles[i][3])
    prev_close = float(candles[i + 1][4])
    return max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))


def atr_expansion(candles: list[list]):
    """最新已完成 15m 柱 TR 相对 ATR14 的放大。返回 (ratio, bar_ts) 或 None。"""
    if len(candles) < CANDLE_LIMIT:
        return None
    trs = [true_range(candles, i) for i in range(1, 1 + ATR_PERIOD)]
    latest_tr = trs[0]
    atr14 = sum(trs) / len(trs)
    if atr14 <= 0:
        return None
    bar_ts = int(candles[1][0]) // 1000   # 最新已完成柱的开盘时间（epoch s）
    return (latest_tr / atr14, bar_ts)


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
    lines = [f"【山寨启动】{sym} 15m波动放大"]
    lines.append(f"现价 {fmt_num(sig['last'])} USDT（24h {sig['chg_24h'] * 100:+.2f}%）")
    lines.append(f"ATR放大 {sig['atr_ratio']:.2f}×（当前TR / ATR{ATR_PERIOD}）")
    lines.append(f"24h成交额 {fmt_vol(sig['vol_usdt'])} USDT")
    lines.append(f"时间 {sig['cst_time']}（北京）")
    return "\n".join(lines)


def main() -> int:
    if "--selftest" in sys.argv:
        print("【山寨启动扫描·投递测试】收到此消息即表示飞书群投递正常。"
              f"策略：USDT永续，15m当前TR>{ATR_MULT}xATR{ATR_PERIOD}且"
              f"24h成交额≥{MIN_VOL_USDT / 1e6:.0f}M USDT，剔除稳定币。")
        return 0

    try:
        tickers = fetch_tickers()
    except Exception as e:
        print(json.dumps({"status": "fetch_tickers_failed", "error": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 1

    now = datetime.now(CST)
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
            candles = fetch_candles(c["inst_id"])
        except Exception as e:
            print(json.dumps({"inst_id": c["inst_id"], "status": "candle_fetch_failed",
                              "error": str(e)}, ensure_ascii=False), file=sys.stderr)
            continue
        r = atr_expansion(candles)
        if r is None:
            continue
        ratio, bar_ts = r
        if ratio <= ATR_MULT:
            continue

        # 去重：同一根已完成 15m 柱只报一次
        state = load_state(c["base"])
        if state.get("last_alert_bar_ts") == bar_ts:
            continue

        chg_24h = (c["last"] - c["open24h"]) / c["open24h"] if c["open24h"] > 0 else 0.0
        sig = {
            "base": c["base"],
            "inst_id": c["inst_id"],
            "last": c["last"],
            "vol_usdt": c["vol_usdt"],
            "chg_24h": chg_24h,
            "atr_ratio": ratio,
            "cst_time": cst_time,
        }
        print(format_signal(sig))
        emitted += 1
        save_state(c["base"], {"last_alert_bar_ts": bar_ts,
                               "last_alert_ts": int(now.timestamp()),
                               "inst_id": c["inst_id"]})
        # 记入信号台账（供网页看板追踪 4h/8h/12h/当下收益率）
        append_signal({
            "symbol": c["base"],
            "inst_id": c["inst_id"],
            "signal_ts": int(now.timestamp()),
            "entry_price": c["last"],
            "vol_usdt": c["vol_usdt"],
            "chg_24h": chg_24h,
            "atr_ratio": ratio,
        })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
