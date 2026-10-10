#!/usr/bin/env python3
"""OKX altcoin breakout signal dashboard — HTTP server.

Serves a web page grouped by week, showing each breakout signal's
启动时间 / 启动价格 / 当前价格 / +4h / +8h / +12h / 当下收益.

On each page view it refreshes the ledger (current prices from the OKX
tickers endpoint, and retroactive 4h/8h/12h returns from 1H candles),
throttled to once per 60s. Pure stdlib + requests.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import load_ledger, save_ledger, week_key, week_label, fmt_cst  # noqa: E402

import requests

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
HEADERS = {"User-Agent": "okx-altcoin-dashboard/1.0"}
REFRESH_INTERVAL = 60  # seconds between ledger refreshes

_SESSION = requests.Session()
_CACHE = {"html": None, "ts": 0.0}
_CACHE_LOCK = threading.Lock()


# ── data refresh ──────────────────────────────────────────────────────────
def fetch_tickers() -> dict[str, float]:
    """base symbol -> last price (USDT swaps only)."""
    r = _SESSION.get(TICKERS_URL, params={"instType": "SWAP"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    p = r.json()
    out: dict[str, float] = {}
    if p.get("code") != "0":
        return out
    for t in p.get("data") or []:
        inst = t.get("instId") or ""
        if not inst.endswith("-USDT-SWAP"):
            continue
        base = inst.split("-")[0].upper()
        try:
            out[base] = float(t["last"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_hourly_candles(inst_id: str) -> dict[int, float]:
    """open-time (epoch s, hour-aligned) -> close, last 100 hours."""
    r = _SESSION.get(CANDLES_URL,
                     params={"instId": inst_id, "bar": "1H", "limit": "100"},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    p = r.json()
    out: dict[int, float] = {}
    if p.get("code") != "0":
        return out
    for c in p.get("data") or []:
        try:
            out[int(c[0]) // 1000] = float(c[4])
        except (IndexError, TypeError, ValueError):
            continue
    return out


def refresh_ledger(ledger: dict) -> bool:
    changed = False
    # 1) current prices (one batch call)
    try:
        last_map = fetch_tickers()
    except Exception:
        last_map = {}
    for sig in ledger["signals"]:
        last = last_map.get(sig["symbol"])
        if last and last > 0 and sig.get("last_price") != last:
            sig["last_price"] = last
            sig["ret_now"] = (last - sig["entry_price"]) / sig["entry_price"]
            changed = True
    # 2) finalize 4h/8h/12h returns (retroactive, from 1H candles)
    for sig in ledger["signals"]:
        if sig.get("ret_12h") is not None:
            continue
        try:
            candles = fetch_hourly_candles(sig["inst_id"])
        except Exception:
            continue
        if not candles:
            continue
        t0 = sig["signal_ts"]
        for label, oh in (("4h", 4), ("8h", 8), ("12h", 12)):
            if sig.get(f"ret_{label}") is not None:
                continue
            target = ((t0 + oh * 3600) // 3600) * 3600
            close = candles.get(target)
            if close is None:
                continue  # not elapsed yet (or beyond 100h window)
            sig[f"price_{label}"] = close
            sig[f"ret_{label}"] = (close - sig["entry_price"]) / sig["entry_price"]
            changed = True
    return changed


# ── rendering ─────────────────────────────────────────────────────────────
def fmt_price(p):
    if p is None:
        return "-"
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    if p >= 0.01:
        return f"{p:.6f}".rstrip("0").rstrip(".")
    return f"{p:.8f}".rstrip("0").rstrip(".")


def fmt_ret(r, pending_label="待定"):
    if r is None:
        return f'<span class="na">{pending_label}</span>'
    pct = r * 100
    cls = "pos" if pct >= 0 else "neg"
    return f'<span class="{cls}">{pct:+.2f}%</span>'


def _row(sig):
    cells = [
        f'<td class="sym">{sig["symbol"]}</td>',
        f'<td>{fmt_cst(sig["signal_ts"])}</td>',
        f'<td class="num">{fmt_price(sig["entry_price"])}</td>',
        f'<td class="num">{fmt_price(sig.get("last_price"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_4h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_8h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_12h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_now"))}</td>',
    ]
    return "<tr>" + "".join(cells) + "</tr>"


def _week_section(wk, signals, idx, active):
    label = week_label(signals[0]["signal_ts"])
    rows = "\n".join(_row(s) for s in signals)
    disp = "block" if active else "none"
    return f"""
<div class="week" id="wk{idx}" style="display:{disp}">
  <h2>{label} <span class="wkid">{wk}</span> <span class="cnt">{len(signals)} 个信号</span></h2>
  <table>
    <thead><tr>
      <th>币</th><th>启动时间</th><th>启动价格</th><th>当前价格</th>
      <th>+4h收益</th><th>+8h收益</th><th>+12h收益</th><th>当下收益</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


def render_html(ledger: dict) -> str:
    signals = ledger["signals"]
    weeks: dict[str, list] = {}
    for s in signals:
        weeks.setdefault(s.get("week") or week_key(s["signal_ts"]), []).append(s)
    week_keys = sorted(weeks.keys(), reverse=True)

    if not week_keys:
        body = '<p class="empty">还没有信号。等第一个「山寨启动」信号出现后，这里会按周展示。</p>'
        tabs = ""
    else:
        tabs = "\n".join(
            f'<button class="tab{" active" if i == 0 else ""}" '
            f'onclick="show({i})">{week_label(weeks[k][0]["signal_ts"])}</button>'
            for i, k in enumerate(week_keys)
        )
        sections = "\n".join(
            _week_section(k, weeks[k], i, active=(i == 0))
            for i, k in enumerate(week_keys)
        )
        body = f'<div class="tabs">{tabs}</div>\n{sections}'

    updated = time.strftime("%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>山寨启动信号看板</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
          margin: 0; background: #0f1420; color: #e6e9ef; }}
  header {{ padding: 20px 24px 8px; }}
  header h1 {{ margin: 0 0 4px; font-size: 20px; }}
  header .sub {{ color: #8b93a7; font-size: 13px; }}
  .tabs {{ padding: 12px 24px 0; display: flex; flex-wrap: wrap; gap: 8px; }}
  .tab {{ background: #1b2233; color: #aab2c5; border: 1px solid #2a3247;
          padding: 7px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; }}
  .tab.active {{ background: #2f6fed; color: #fff; border-color: #2f6fed; }}
  .week {{ padding: 16px 24px 24px; }}
  .week h2 {{ font-size: 16px; margin: 0 0 10px; color: #dbe1ec; }}
  .wkid, .cnt {{ color: #6b7490; font-size: 12px; font-weight: normal; margin-left: 8px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #232c40; }}
  th {{ color: #8b93a7; font-weight: 600; background: #141b2b; }}
  tr:hover td {{ background: #171f31; }}
  .sym {{ font-weight: 600; color: #fff; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .pos {{ color: #2ecc71; }}
  .neg {{ color: #ff5c5c; }}
  .na {{ color: #5b6377; }}
  .empty {{ color: #8b93a7; padding: 24px; }}
</style>
</head>
<body>
<header>
  <h1>山寨启动信号看板</h1>
  <div class="sub">OKX USDT 永续 · 创5日新高且昨日未启动 · 24h成交额≥4000万 USDT · 数据更新 {updated}</div>
</header>
{body}
<script>
function show(i) {{
  var tabs = document.querySelectorAll('.tab');
  var weeks = document.querySelectorAll('.week');
  tabs.forEach(function(t, n) {{ t.classList.toggle('active', n === i); }});
  weeks.forEach(function(w, n) {{ w.style.display = (n === i) ? 'block' : 'none'; }});
}}
</script>
</body>
</html>"""


# ── HTTP server ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_dashboard()
        elif self.path == "/health":
            self._serve_text(200, "ok")
        else:
            self.send_error(404)

    def _serve_dashboard(self):
        with _CACHE_LOCK:
            if _CACHE["html"] is None or time.time() - _CACHE["ts"] > REFRESH_INTERVAL:
                ledger = load_ledger()
                try:
                    refresh_ledger(ledger)
                    save_ledger(ledger)
                except Exception:
                    pass  # serve whatever we have
                _CACHE["html"] = render_html(ledger)
                _CACHE["ts"] = time.time()
            body = _CACHE["html"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_text(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # quiet


def main():
    port = int(os.environ.get("OKX_DASHBOARD_PORT", "8787"))
    host = os.environ.get("OKX_DASHBOARD_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"OKX dashboard serving on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
