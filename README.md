# OKX Altcoin Breakout Scanner（山寨启动扫描）

每 5 分钟扫描 OKX USDT 本位永续合约，捕捉「启动」的山寨币，推送到飞书群。

## 信号定义（「启动」= 今日首次突破）

- 现价 > 前 5 日最高价（不含今日）
- 且昨日尚未突破其前 5 日最高价 → 今日才首次突破（抓启动那一刻，而非连涨中的每日新高）
- 24h 成交额 ≥ 5000 万 USDT
- 仅 USDT 本位永续（instId 以 `-USDT-SWAP` 结尾）
- 剔除 BTC / ETH / 稳定币

## 文件

- `okx_altcoin_breakout_monitor.py`  扫描主程序（纯标准库 + requests，无第三方依赖）
- 部署 wrapper：`~/.hermes/scripts/okx_altcoin_breakout_monitor.sh`（不在本仓库）

## 部署

- cron：`*/5 * * * *`，no_agent，deliver → Feishu `oc_1e6a033a32e1e4a611d7013ddc4f9fdb`（与 BTC 连阳监控同群）
- 去重：每标的每次「启动事件」只报一次，状态存 `~/.hermes/data/crypto_signals/okx_breakout_*.json`

## 参数（脚本顶部）

| 参数 | 值 | 说明 |
|---|---|---|
| `MIN_VOL_USDT` | 50_000_000 | 24h 成交额下限（USDT） |
| `BREAKOUT_DAYS` | 5 | 创 N 日新高 |
| `CANDLE_LIMIT` | 7 | 日线根数（今日+昨日+前5日+冗余） |
| `EXCLUDE_BASE` | BTC/ETH/稳定币 | 剔除名单 |

## 速率与限频

每 5 分钟：1 次 tickers + 约 106 次日线请求 ≈ 0.36 req/s，远低于 OKX 公共接口 20 req/2s 限频，无封号风险。

## 看板（Dashboard）

HTTP 网页看板，按周分标签页展示每个启动信号的：启动时间 / 启动价格 / 当前价格 / +4h / +8h / +12h / 当下收益率；顶部统计条（信号总数 + 各周期胜率）；点币名弹窗看 K 线（15m/1H/4H/1D，纯 SVG 自绘无外部依赖）。

- `ledger.py`  信号台账（scanner 写入，server 读取并追溯刷新收益）
- `dashboard_server.py`  看板 HTTP 服务（端口 8787，stdlib http.server，无第三方依赖）
- `deploy/okx-dashboard.service`  systemd 用户服务配置（需带 HTTP_PROXY 访问 OKX）

部署：systemd 用户服务 `okx-dashboard`（`~/.config/systemd/user/okx-dashboard.service`，Restart=always）。访问 http://<机器IP>:8787

收益口径：+4h/+8h/+12h 用 1H K 线在目标整点收盘价追溯计算（信号后 N 小时，收盘价/启动价-1）；当下收益率用实时 ticker last。
