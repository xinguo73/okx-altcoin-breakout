#!/usr/bin/env python3
"""OKX 山寨币启动扫描（15m 波动放大 + 24h成交额>4000万USDT 的 USDT 永续合约）。

信号定义（"启动" = 波动突然放大）：
  - 最新一根已完成 15m K 线的真实波幅 TR > ATR_MULT × ATR14
    （ATR14 = 前 14 根已完成 15m 柱的平均真实波幅）
  - 且 ATR14 占现价比例 < MAX_ATR_PCT%（之前波动较小，才是「启动」）
  - 且该柱收阳（close > open，向上启动，剔除砸盘阴线）
  - 且 24h 成交额 volCcy24h × 现价 > 40_000_000 USDT（过滤低流动性的杂波）
  - 相对强度 RS 作为后续确认指标（非硬过滤）：近 1 小时 5m 柱中 BTC 阴线时币不阴线的比例，RS≥0.7 看板高亮为强势
  - 且与 BTC 的相关性（近 4 小时 15m 收益率 Pearson）≤ 0.8：过滤掉「跟随 BTC」的纯 beta 品种

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
MIN_VOL_USDT = 40_000_000   # 24h 成交额下限（USDT）
BAR = "15m"                 # K 线周期
ATR_MULT = 2.0              # 波动放大倍数（当下 TR > ATR_MULT × ATR14）
ATR_PERIOD = 14             # ATR 周期（前 N 根已完成柱）
MAX_ATR_PCT = 1.5           # 之前ATR14占现价比例上限(%)：之前波动要小，才是「启动」
RS_WINDOW = 12              # 相对强度窗口（5m 柱数 = 1 小时）
RS_MIN = 0.7                # 强势阈值（RS≥此值看板绿色高亮；扫描器不按RS硬过滤）
RS_MIN_BTC_DOWN = 3         # BTC阴线柱少于此时无法判断RS（视为不适用）
MOMENTUM_KING_RS = 0.7      # 动量王：RS≥此值（相对强度强）
MOMENTUM_KING_CORR = 0.3    # 动量王：corr≤此值（不跟随BTC，独立行情）
CORR_WINDOW = 16            # 相关性窗口（15m 柱数 = 4 小时）
CORR_MAX = 0.8              # 相关性上限：corr > 此值 = 跟随BTC，过滤掉
COOLDOWN_HOURS = 12         # 同一币种冷却期（小时）：期内不重复报信号
CANDLE_LIMIT = max(ATR_PERIOD, CORR_WINDOW) + 2   # 15m 柱数（足够 ATR + 相关性）
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


def atr_expansion(candles: list[list], last: float):
    """最新已完成 15m 柱 TR 相对 ATR14 的放大。返回 (ratio, atr_pct, bar_ts) 或 None。"""
    if len(candles) < CANDLE_LIMIT:
        return None
    trs = [true_range(candles, i) for i in range(1, 1 + ATR_PERIOD)]
    latest_tr = trs[0]
    atr14 = sum(trs) / len(trs)
    if atr14 <= 0:
        return None
    bar_ts = int(candles[1][0]) // 1000   # 最新已完成柱的开盘时间（epoch s）
    return (latest_tr / atr14, atr14 / last * 100, bar_ts)


def fetch_5m(inst_id: str) -> list[list]:
    r = _SESSION.get(CANDLES_URL,
                     params={"instId": inst_id, "bar": "5m",
                             "limit": str(RS_WINDOW + 2)},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    p = r.json()
    if p.get("code") != "0":
        raise RuntimeError(f"5m candles code={p.get('code')}")
    return p.get("data") or []   # 最新在前


def rel_strength(coin_5m: list[list], btc_5m: list[list]):
    """BTC 阴线时币不阴线的比例。返回 (rs, btc_down, coin_held) 或 None(数据不足)。"""
    btc_down = 0
    coin_held = 0
    n = min(len(coin_5m), len(btc_5m), RS_WINDOW + 1)
    for j in range(1, n):   # 跳过 index 0（未完成柱）
        try:
            btc_down_bar = float(btc_5m[j][4]) < float(btc_5m[j][1])
            coin_hold = float(coin_5m[j][4]) >= float(coin_5m[j][1])
        except (IndexError, ValueError, TypeError):
            continue
        if btc_down_bar:
            btc_down += 1
            if coin_hold:
                coin_held += 1
    if btc_down < RS_MIN_BTC_DOWN:
        return None
    return (coin_held / btc_down, btc_down, coin_held)


def pearson(xs: list[float], ys: list[float]):
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


def btc_correlation(coin_candles: list[list], btc_close_map: dict[int, float]):
    """15m 收益率与 BTC 的 Pearson 相关性，窗口 CORR_WINDOW，结束于最新已完成柱。"""
    xs: list[float] = []
    ys: list[float] = []
    for j in range(1, 1 + CORR_WINDOW):
        if j + 1 >= len(coin_candles):
            break
        try:
            c0 = float(coin_candles[j + 1][4])
            c1 = float(coin_candles[j][4])
        except (IndexError, ValueError, TypeError):
            continue
        if c0 == 0:
            continue
        t0 = int(coin_candles[j + 1][0]) // 1000
        t1 = int(coin_candles[j][0]) // 1000
        b0 = btc_close_map.get(t0)
        b1 = btc_close_map.get(t1)
        if b0 is None or b1 is None or b0 == 0:
            continue
        xs.append(c1 / c0 - 1.0)
        ys.append(b1 / b0 - 1.0)
    if len(xs) < 8:
        return None
    return pearson(xs, ys)


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


def is_momentum_king(rs: float | None, corr: float | None) -> bool:
    """动量王 = RS≥0.7 且 corr≤0.3。回测 58% 胜率、+12h 均值 +3.27%（n=31，vs 基线 45%/-0.17%）。"""
    return (rs is not None and rs >= MOMENTUM_KING_RS and
            corr is not None and corr <= MOMENTUM_KING_CORR)


def format_signal(sig: dict) -> str:
    sym = sig["base"]
    tag = "🔥动量王 " if is_momentum_king(sig.get("rs"), sig.get("corr")) else ""
    lines = [f"【启动信号，ATR放大】{tag}{sym}"]
    lines.append(f"现价 {fmt_num(sig['last'])} USDT（24h {sig['chg_24h'] * 100:+.2f}%）")
    lines.append(f"ATR放大 {sig['atr_ratio']:.2f}× · 之前波动 {sig['atr_pct']:.2f}%")
    if sig.get("rs") is not None:
        lines.append(f"RS {sig['rs']:.2f}（BTC跌时扛跌 {sig['rs_coin_held']}/{sig['rs_btc_down']}）")
    if sig.get("corr") is not None:
        lines.append(f"相关性 {sig['corr']:.2f}（vs BTC 4h）")
    lines.append(f"24h成交额 {fmt_vol(sig['vol_usdt'])} USDT")
    lines.append(f"时间 {sig['cst_time']}（北京）")
    return "\n".join(lines)


def main() -> int:
    if "--selftest" in sys.argv:
        print("【山寨启动扫描·投递测试】收到此消息即表示飞书群投递正常。"
              f"策略：USDT永续，15m当前TR>{ATR_MULT}xATR{ATR_PERIOD}且之前波动<{MAX_ATR_PCT}%"
              f"且24h成交额≥{MIN_VOL_USDT / 1e6:.0f}M USDT且与BTC相关性≤{CORR_MAX}；RS为强势确认(≥{RS_MIN}高亮)。")
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

    # BTC 5m 基准（相对强度对比）
    try:
        btc_5m = fetch_5m("BTC-USDT-SWAP")
    except Exception:
        btc_5m = []

    # BTC 15m 基准（相关性对比）
    try:
        btc_candles = fetch_candles("BTC-USDT-SWAP")
        btc_close_map = {int(c[0]) // 1000: float(c[4]) for c in btc_candles}
    except Exception:
        btc_close_map = {}

    emitted = 0
    for c in candidates:
        try:
            candles = fetch_candles(c["inst_id"])
        except Exception as e:
            print(json.dumps({"inst_id": c["inst_id"], "status": "candle_fetch_failed",
                              "error": str(e)}, ensure_ascii=False), file=sys.stderr)
            continue
        r = atr_expansion(candles, c["last"])
        if r is None:
            continue
        ratio, atr_pct, bar_ts = r
        if ratio <= ATR_MULT:
            continue
        if atr_pct >= MAX_ATR_PCT:
            continue   # 之前波动就大，不算「启动」

        # 收阳：最新已完成柱必须 close > open（向上启动，剔除砸盘阴线）
        if float(candles[1][4]) <= float(candles[1][1]):
            continue   # 阴线，不是向上启动

        # 相关性 vs BTC（15m）：过滤掉「跟随 BTC」的品种（corr > CORR_MAX）
        corr_val = None
        if btc_close_map:
            corr_val = btc_correlation(candles, btc_close_map)
            if corr_val is not None and corr_val > CORR_MAX:
                continue   # 跟随 BTC，无独立行情

        # 相对强度 vs BTC（5m）：作为后续确认指标，不硬过滤
        rs_val = None; rs_btc_down = 0; rs_coin_held = 0
        if btc_5m:
            try:
                coin_5m = fetch_5m(c["inst_id"])
            except Exception:
                coin_5m = []
            r = rel_strength(coin_5m, btc_5m) if coin_5m else None
            if r is not None:
                rs_val, rs_btc_down, rs_coin_held = r

        # 去重：同一根已完成 15m 柱只报一次 + 冷却期内不重复报
        state = load_state(c["base"])
        if state.get("last_alert_bar_ts") == bar_ts:
            continue
        last_alert_ts = state.get("last_alert_ts")
        if last_alert_ts and (now.timestamp() - last_alert_ts) < COOLDOWN_HOURS * 3600:
            continue

        chg_24h = (c["last"] - c["open24h"]) / c["open24h"] if c["open24h"] > 0 else 0.0
        sig = {
            "base": c["base"],
            "inst_id": c["inst_id"],
            "last": c["last"],
            "vol_usdt": c["vol_usdt"],
            "chg_24h": chg_24h,
            "atr_ratio": ratio,
            "atr_pct": atr_pct,
            "rs": rs_val,
            "rs_btc_down": rs_btc_down,
            "rs_coin_held": rs_coin_held,
            "corr": corr_val,
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
            "atr_pct": atr_pct,
            "rs": rs_val,
            "corr": corr_val,
        })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
