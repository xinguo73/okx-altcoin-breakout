# OKX Altcoin Breakout Scanner（山寨启动扫描）

每 5 分钟扫描 OKX USDT 本位永续合约，捕捉「启动」的山寨币，推送到飞书群。

## 信号定义（「启动」= 波动放大）

- 最新一根已完成 15m K 线真实波幅 TR > 2.0 × ATR14（前 14 根 15m 平均真实波幅）
- 之前 ATR14 占现价 < 1%（之前波动较小，才是「启动」）
- 24h 成交额 ≥ 1000 万 USDT（过滤低流动性杂波）
- 相对强度 RS（后续确认指标，非硬过滤）：近 1 小时 BTC 5m 阴线时币不阴线的比例，RS≥0.7 看板绿色高亮为强势
- 仅 USDT 本位永续（instId 以 `-USDT-SWAP` 结尾）
- 剔除稳定币（BTC/ETH 及代币化股票/商品 SNDK/SOXL/SPCX/XAU 等均纳入）

## 文件

- `okx_altcoin_breakout_monitor.py`  扫描主程序（纯标准库 + requests，无第三方依赖）
- 部署 wrapper：`~/.hermes/scripts/okx_altcoin_breakout_monitor.sh`（不在本仓库）

## 部署

- cron：`*/5 * * * *`，no_agent，deliver → Feishu `oc_1e6a033a32e1e4a611d7013ddc4f9fdb`（与 BTC 连阳监控同群）
- 去重：每标的每根已完成 15m 柱只报一次，状态存 `~/.hermes/data/crypto_signals/okx_breakout_*.json`

## 参数（脚本顶部）

| 参数 | 值 | 说明 |
|---|---|---|
| `MIN_VOL_USDT` | 10_000_000 | 24h 成交额下限（USDT） |
| `BAR` | 15m | K 线周期 |
| `ATR_MULT` | 2.0 | 波动放大倍数（TR > 2×ATR14） |
| `ATR_PERIOD` | 14 | ATR 周期（前 N 根） |
| `MAX_ATR_PCT` | 1.0 | 之前ATR14占现价上限(%)，之前波动要小 |
| `RS_WINDOW` | 12 | 相对强度窗口（5m柱数=1小时） |
| `RS_MIN` | 0.7 | 强势阈值（≥此值看板高亮，非硬过滤） |
| `EXCLUDE_BASE` | 稳定币 | 剔除名单 |

## 速率与限频

每 5 分钟：1 次 tickers + 约 60 次 15m 请求 ≈ 0.2 req/s，远低于 OKX 公共接口 20 req/2s 限频，无封号风险。

## 看板（Dashboard）

HTTP 网页看板，按周分标签页展示每个启动信号的：启动时间 / 启动价格 / 当前价格 / +4h / +8h / +12h / 当下收益率；顶部统计条（信号总数 + 各周期胜率）；点币名弹窗看 K 线（15m/1H/4H/1D，纯 SVG 自绘无外部依赖）。

- `ledger.py`  信号台账（scanner 写入，server 读取并追溯刷新收益）
- `dashboard_server.py`  看板 HTTP 服务（端口 8787，stdlib http.server，无第三方依赖）
- `deploy/okx-dashboard.service`  systemd 用户服务配置（需带 HTTP_PROXY 访问 OKX）

部署：systemd 用户服务 `okx-dashboard`（`~/.config/systemd/user/okx-dashboard.service`，Restart=always）。访问 http://<机器IP>:8787

收益口径：+4h/+8h/+12h 用 1H K 线在目标整点收盘价追溯计算（信号后 N 小时，收盘价/启动价-1）；当下收益率用实时 ticker last。
