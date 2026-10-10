#!/usr/bin/env python3
"""OKX altcoin breakout signal dashboard — HTTP server.

Serves a web page grouped by week, showing each breakout signal's
启动时间 / 启动价格 / 当前价格 / +4h / +8h / +12h / 当下收益率,
plus a stats bar (信号总数 + 胜率) and clickable per-coin K-line chart.

On each page view it refreshes the ledger (current prices from the OKX
tickers endpoint, and retroactive 4h/8h/12h returns from 1H candles),
throttled to once per 60s. Pure stdlib + requests, no client-side deps.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import load_ledger, save_ledger, week_key, week_label, fmt_cst  # noqa: E402

import requests

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
HEADERS = {"User-Agent": "okx-altcoin-dashboard/1.0"}
REFRESH_INTERVAL = 60  # seconds between ledger refreshes
STRONG_RS = 0.7        # RS ≥ 此值标记为强势（绿色高亮）
SIGNAL_WINDOW_HOURS = 70  # 看板只展示近 N 小时的信号

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


def fetch_ohlcv(inst_id: str, bar: str, limit: int = 200) -> list[dict]:
    """OHLCV list oldest-first, for the K-line chart."""
    r = _SESSION.get(CANDLES_URL,
                     params={"instId": inst_id, "bar": bar, "limit": str(limit)},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    p = r.json()
    out: list[dict] = []
    if p.get("code") != "0":
        return out
    for c in p.get("data") or []:
        try:
            out.append({"time": int(c[0]) // 1000,
                        "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4])})
        except (IndexError, TypeError, ValueError):
            continue
    out.reverse()  # oldest-first for charting
    return out


def refresh_ledger(ledger: dict) -> bool:
    changed = False
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
    hourly_cache = {}
    for sig in ledger["signals"]:
        if sig.get("ret_12h") is not None:
            continue
        inst = sig["inst_id"]
        if inst not in hourly_cache:
            try:
                hourly_cache[inst] = fetch_hourly_candles(inst)
            except Exception:
                hourly_cache[inst] = {}
        candles = hourly_cache[inst]
        if not candles:
            continue
        t0 = sig["signal_ts"]
        for label, oh in (("4h", 4), ("8h", 8), ("12h", 12)):
            if sig.get(f"ret_{label}") is not None:
                continue
            target = ((t0 + oh * 3600) // 3600) * 3600
            close = candles.get(target)
            if close is None:
                continue
            sig[f"price_{label}"] = close
            sig[f"ret_{label}"] = (close - sig["entry_price"]) / sig["entry_price"]
            changed = True
    return changed


# ── rendering helpers ─────────────────────────────────────────────────────
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


def fmt_rs(rs):
    if rs is None:
        return '<span class="na">-</span>'
    cls = "rs-strong" if rs >= STRONG_RS else ("rs-mid" if rs >= 0.5 else "rs-weak")
    return f'<span class="{cls}">{rs:.2f}</span>'


def _row(sig):
    rs = sig.get("rs")
    strong = rs is not None and rs >= STRONG_RS
    cells = [
        f'<td class="sym">{sig["symbol"]} <span class="kl">K线▸</span></td>',
        f'<td>{fmt_cst(sig["signal_ts"])}</td>',
        f'<td class="num">{fmt_price(sig["entry_price"])}</td>',
        f'<td class="num">{fmt_price(sig.get("last_price"))}</td>',
        f'<td class="num">{fmt_rs(rs)}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_4h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_8h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_12h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_now"))}</td>',
    ]
    onclick = (f'onclick="openKline(\'{sig["inst_id"]}\', \'{sig["symbol"]}\', '
               f'{sig["signal_ts"]}, {sig["entry_price"]})"')
    cls = "clickable strong" if strong else "clickable"
    return f'<tr class="{cls}" {onclick}>' + "".join(cells) + "</tr>"


def _stats_block(signals):
    total = len(signals)
    parts = [f'<span class="stat total">总信号数 <b>{total}</b></span>']
    for label, key in (("4h", "ret_4h"), ("8h", "ret_8h"),
                       ("12h", "ret_12h"), ("当下", "ret_now")):
        vals = [s[key] for s in signals if s.get(key) is not None]
        n = len(vals)
        if n == 0:
            parts.append(f'<span class="stat"><b>{label}胜率</b> -</span>')
            continue
        wins = sum(1 for v in vals if v > 0)
        avg = sum(vals) / n
        parts.append(
            f'<span class="stat"><b>{label}胜率</b> {wins / n * 100:.0f}%'
            f' <span class="dim">({wins}/{n})</span>'
            f' · 均值 {avg * 100:+.2f}%</span>')
    return '<div class="stats">' + "".join(parts) + "</div>"


def _week_section(wk, signals, idx, active):
    label = week_label(signals[0]["signal_ts"])
    rows = "\n".join(_row(s) for s in signals)
    disp = "block" if active else "none"
    return f"""
<div class="week" id="wk{idx}" style="display:{disp}">
  <h2>{label} <span class="wkid">{wk}</span> <span class="cnt">{len(signals)} 个信号</span></h2>
  <table>
    <thead><tr>
      <th>币</th><th>启动时间</th><th class="num">启动价格</th><th class="num">当前价格</th><th class="num">RS</th>
      <th class="num">+4h收益率</th><th class="num">+8h收益率</th><th class="num">+12h收益率</th><th class="num">当下收益率</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


CSS = """
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
          margin: 0; background: #0f1420; color: #e6e9ef; }
  header { padding: 20px 24px 8px; }
  header h1 { margin: 0 0 4px; font-size: 20px; }
  header .sub { color: #8b93a7; font-size: 13px; }
  .stats { padding: 12px 24px 0; display: flex; flex-wrap: wrap; gap: 18px;
           font-size: 13px; color: #aab2c5; }
  .stats .stat b { color: #e6e9ef; }
  .stats .total { font-size: 14px; }
  .stats .dim { color: #6b7490; }
  .tabs { padding: 12px 24px 0; display: flex; flex-wrap: wrap; gap: 8px; }
  .tab { background: #1b2233; color: #aab2c5; border: 1px solid #2a3247;
          padding: 7px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; }
  .tab.active { background: #2f6fed; color: #fff; border-color: #2f6fed; }
  .week { padding: 16px 24px 24px; }
  .week h2 { font-size: 16px; margin: 0 0 10px; color: #dbe1ec; }
  .wkid, .cnt { color: #6b7490; font-size: 12px; font-weight: normal; margin-left: 8px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #232c40; }
  th { color: #8b93a7; font-weight: 600; background: #141b2b; }
  tr:hover td { background: #171f31; }
  tr.clickable { cursor: pointer; }
  .sym { font-weight: 600; color: #fff; }
  .kl { color: #2f6fed; font-size: 11px; font-weight: normal; }
  tr.strong td { background: #16301d; }
  tr.strong:hover td { background: #1d3a24; }
  .rs-strong { color: #2ecc71; font-weight: 600; }
  .rs-mid { color: #d9b64a; }
  .rs-weak { color: #ff5c5c; }
  .toggle-strong { background: #1b2233; color: #aab2c5; border: 1px solid #2a3247;
                   padding: 4px 10px; border-radius: 6px; cursor: pointer; font-size: 12px;
                   margin-left: 8px; vertical-align: middle; }
  .toggle-strong.active { background: #2ecc71; color: #0f1420; border-color: #2ecc71; }
  body.strong-only tr.clickable:not(.strong) { display: none; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .pos { color: #2ecc71; }
  .neg { color: #ff5c5c; }
  .na { color: #5b6377; }
  .empty { color: #8b93a7; padding: 24px; }
  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.62);
           z-index: 50; align-items: center; justify-content: center; }
  .modal-box { background: #0f1420; border: 1px solid #2a3247; border-radius: 10px;
               width: min(1000px, 94vw); max-height: 90vh; display: flex;
               flex-direction: column; }
  .modal-head { display: flex; align-items: center; gap: 12px;
                padding: 10px 14px; border-bottom: 1px solid #232c40; }
  #modal-title { font-weight: 600; flex: 1; }
  .bar-btn { background: #1b2233; color: #aab2c5; border: 1px solid #2a3247;
             padding: 4px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .bar-btn.active { background: #2f6fed; color: #fff; border-color: #2f6fed; }
  .close { background: none; border: none; color: #8b93a7; font-size: 16px; cursor: pointer; }
  .chart-body { height: 480px; padding: 8px; }
  .loading { color: #8b93a7; padding: 20px; }
"""

JS = """
var curInst = null, curBar = '1H', curTs = null, curPrice = null;
function openKline(inst, symbol, ts, price) {
  curInst = inst; curBar = '1H'; curTs = ts; curPrice = price;
  document.getElementById('modal').style.display = 'flex';
  var sig = (ts != null) ? ('信号 ' + fmtT(ts) + ' @ ' + fmtP(price)) : '';
  document.getElementById('modal-title').textContent = symbol + ' · ' + inst + ' ' + sig;
  syncBarButtons(); loadKline();
}
function setBar(bar) { curBar = bar; syncBarButtons(); loadKline(); }
function syncBarButtons() {
  var btns = document.querySelectorAll('.bar-btn');
  for (var i = 0; i < btns.length; i++)
    btns[i].classList.toggle('active', btns[i].getAttribute('data-bar') === curBar);
}
function loadKline() {
  var body = document.getElementById('chart-body');
  body.innerHTML = '<div class="loading">加载中…</div>';
  fetch('/kline?inst=' + encodeURIComponent(curInst) + '&bar=' + curBar + '&limit=200')
    .then(function (r) { return r.json(); })
    .then(function (d) { renderKline(d); })
    .catch(function (e) { body.textContent = '加载失败: ' + e; });
}
function closeModal() { document.getElementById('modal').style.display = 'none'; }
function fmtP(p) {
  if (p >= 1000) return p.toFixed(2);
  if (p >= 1) return p.toFixed(4);
  if (p >= 0.01) return p.toFixed(6);
  return p.toFixed(8);
}
function fmtT(ts) {
  var d = new Date(ts * 1000);
  var M = ('0' + (d.getMonth() + 1)).slice(-2), D = ('0' + d.getDate()).slice(-2);
  var h = ('0' + d.getHours()).slice(-2), m = ('0' + d.getMinutes()).slice(-2);
  return M + '-' + D + ' ' + h + ':' + m;
}
function renderKline(data) {
  var body = document.getElementById('chart-body');
  body.innerHTML = '';
  if (!data || !data.length) { body.textContent = '无数据'; return; }
  var W = 1000, H = 460, padL = 8, padR = 74, padT = 10, padB = 22;
  var pw = W - padL - padR, ph = H - padT - padB;
  var lo = Infinity, hi = -Infinity;
  for (var i = 0; i < data.length; i++) {
    if (data[i].low < lo) lo = data[i].low;
    if (data[i].high > hi) hi = data[i].high;
  }
  var range = (hi - lo) || 1;
  var n = data.length, step = pw / n, bw = Math.max(1, step * 0.62);
  function y(p) { return padT + (hi - p) / range * ph; }
  function x(i) { return padL + i * step + step / 2; }
  var NS = 'http://www.w3.org/2000/svg';
  var svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
  svg.style.display = 'block';
  for (var g = 0; g <= 4; g++) {
    var p = lo + range * g / 4, gy = y(p);
    var ln = document.createElementNS(NS, 'line');
    ln.setAttribute('x1', padL); ln.setAttribute('x2', padL + pw);
    ln.setAttribute('y1', gy); ln.setAttribute('y2', gy);
    ln.setAttribute('stroke', '#1f2737'); ln.setAttribute('stroke-width', '1');
    svg.appendChild(ln);
    var tx = document.createElementNS(NS, 'text');
    tx.setAttribute('x', padL + pw + 6); tx.setAttribute('y', gy + 4);
    tx.setAttribute('fill', '#6b7490'); tx.setAttribute('font-size', '11');
    tx.textContent = fmtP(p);
    svg.appendChild(tx);
  }
  for (var i = 0; i < n; i++) {
    var d = data[i], up = d.close >= d.open;
    var color = up ? '#2ecc71' : '#ff5c5c';
    var cx = x(i), oy = y(d.open), cy = y(d.close), hy = y(d.high), ly = y(d.low);
    var wk = document.createElementNS(NS, 'line');
    wk.setAttribute('x1', cx); wk.setAttribute('x2', cx);
    wk.setAttribute('y1', hy); wk.setAttribute('y2', ly);
    wk.setAttribute('stroke', color); wk.setAttribute('stroke-width', '1');
    svg.appendChild(wk);
    var top = Math.min(oy, cy), bh = Math.max(1, Math.abs(cy - oy));
    var rc = document.createElementNS(NS, 'rect');
    rc.setAttribute('x', cx - bw / 2); rc.setAttribute('y', top);
    rc.setAttribute('width', bw); rc.setAttribute('height', bh);
    rc.setAttribute('fill', color);
    svg.appendChild(rc);
  }
  if (curTs != null && curPrice != null && n >= 2) {
    var t0 = data[0].time, interval = data[1].time - data[0].time;
    var idx = interval > 0 ? (curTs - t0) / interval : 0;
    if (idx >= -0.5 && idx <= n + 0.5) {
      var sx = padL + idx * step + step / 2;
      sx = Math.max(padL, Math.min(padL + pw, sx));
      var sy = y(curPrice);
      var vl = document.createElementNS(NS, 'line');
      vl.setAttribute('x1', sx); vl.setAttribute('x2', sx);
      vl.setAttribute('y1', padT); vl.setAttribute('y2', padT + ph);
      vl.setAttribute('stroke', '#f5c542'); vl.setAttribute('stroke-width', '1.2');
      vl.setAttribute('stroke-dasharray', '4,4');
      svg.appendChild(vl);
      var hl = document.createElementNS(NS, 'line');
      hl.setAttribute('x1', padL); hl.setAttribute('x2', padL + pw);
      hl.setAttribute('y1', sy); hl.setAttribute('y2', sy);
      hl.setAttribute('stroke', '#f5c542'); hl.setAttribute('stroke-width', '1.2');
      hl.setAttribute('stroke-dasharray', '4,4');
      svg.appendChild(hl);
      var dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', sx); dot.setAttribute('cy', sy);
      dot.setAttribute('r', '4'); dot.setAttribute('fill', '#f5c542');
      svg.appendChild(dot);
      var lb = document.createElementNS(NS, 'text');
      lb.setAttribute('x', sx + 7); lb.setAttribute('y', sy - 7);
      lb.setAttribute('fill', '#f5c542'); lb.setAttribute('font-size', '12');
      lb.setAttribute('font-weight', '600');
      lb.textContent = '启动信号';
      svg.appendChild(lb);
    }
  }
  var idxs = [0, Math.floor(n / 2), n - 1];
  for (var k = 0; k < idxs.length; k++) {
    var ti = idxs[k];
    var tt = document.createElementNS(NS, 'text');
    tt.setAttribute('x', x(ti)); tt.setAttribute('y', H - 6);
    tt.setAttribute('fill', '#6b7490'); tt.setAttribute('font-size', '11');
    tt.setAttribute('text-anchor', ti === 0 ? 'start' : (ti === n - 1 ? 'end' : 'middle'));
    tt.textContent = fmtT(data[ti].time);
    svg.appendChild(tt);
  }
  body.appendChild(svg);
}
function show(i) {
  var tabs = document.querySelectorAll('.tab');
  var weeks = document.querySelectorAll('.week');
  tabs.forEach(function (t, n) { t.classList.toggle('active', n === i); });
  weeks.forEach(function (w, n) { w.style.display = (n === i) ? 'block' : 'none'; });
  try { localStorage.setItem('okx_tab', String(i)); } catch (e) {}
}
function toggleStrong() {
  var btn = document.getElementById('toggleStrongBtn');
  var on = document.body.classList.toggle('strong-only');
  if (btn) { btn.classList.toggle('active', on); btn.textContent = on ? '只看强势 ✓' : '只看强势'; }
  try { localStorage.setItem('okx_strong_only', on ? '1' : '0'); } catch (e) {}
}
try {
  var _t = parseInt(localStorage.getItem('okx_tab') || '0', 10);
  show(_t);
  if (localStorage.getItem('okx_strong_only') === '1') {
    document.body.classList.add('strong-only');
    var _b = document.getElementById('toggleStrongBtn');
    if (_b) { _b.classList.add('active'); _b.textContent = '只看强势 ✓'; }
  }
} catch (e) {}
setInterval(function () {
  var m = document.getElementById('modal');
  if (m && m.style.display === 'flex') return;  // 看K线时不打断
  location.reload();
}, 60000);
"""


def render_html(ledger: dict) -> str:
    cutoff = time.time() - SIGNAL_WINDOW_HOURS * 3600
    signals = [s for s in ledger["signals"] if s.get("signal_ts", 0) >= cutoff]
    weeks: dict[str, list] = {}
    for s in signals:
        weeks.setdefault(s.get("week") or week_key(s["signal_ts"]), []).append(s)
    for k in weeks:
        weeks[k].sort(key=lambda s: s["signal_ts"], reverse=True)   # 时间倒序，近期的在前
    week_keys = sorted(weeks.keys(), reverse=True)

    stats = _stats_block(signals)

    if not week_keys:
        tabs = ""
        sections = '<p class="empty">还没有信号。等第一个「山寨启动」信号出现后，这里会按周展示。</p>'
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

    updated = time.strftime("%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>山寨启动信号看板</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>山寨启动信号看板</h1>
  <div class="sub">近70h信号 · OKX USDT 永续 · 15m波动放大(TR&gt;2×ATR14, 之前波动&lt;1%) · 24h成交额≥1000万 USDT · 绿色行=RS≥0.7强势 · 每60秒自动刷新 · 数据更新 {updated}
  <button id="toggleStrongBtn" class="toggle-strong" onclick="toggleStrong()">只看强势</button></div>
</header>
{stats}
{tabs}
{sections}
<div class="modal" id="modal">
  <div class="modal-box">
    <div class="modal-head">
      <span id="modal-title"></span>
      <button class="bar-btn active" data-bar="1H" onclick="setBar('1H')">1H</button>
      <button class="bar-btn" data-bar="15m" onclick="setBar('15m')">15m</button>
      <button class="bar-btn" data-bar="4H" onclick="setBar('4H')">4H</button>
      <button class="bar-btn" data-bar="1D" onclick="setBar('1D')">1D</button>
      <button class="close" onclick="closeModal()">✕</button>
    </div>
    <div class="chart-body" id="chart-body"></div>
  </div>
</div>
<script>{JS}</script>
</body>
</html>"""


# ── HTTP server ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_dashboard()
        elif path == "/health":
            self._serve_text(200, "ok")
        elif path == "/kline":
            self._serve_kline()
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

    def _serve_kline(self):
        q = parse_qs(urlparse(self.path).query)
        inst = (q.get("inst") or [""])[0].strip()
        bar = (q.get("bar") or ["1H"])[0]
        try:
            limit = min(int((q.get("limit") or ["200"])[0]), 500)
        except ValueError:
            limit = 200
        if not inst or not inst.endswith("-USDT-SWAP"):
            self.send_error(400)
            return
        if bar not in ("15m", "1H", "4H", "1D"):
            bar = "1H"
        try:
            data = fetch_ohlcv(inst, bar, limit)
        except Exception as e:
            self.send_error(500, str(e))
            return
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
