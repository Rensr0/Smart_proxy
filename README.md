# SXS · Smart Exit Selector

**Smart Exit Selector（SXS）** — 智能出口选择：动态代理池 + 本地 HTTP 隧道 + 运营驾驶舱。从厂商提取短效出口 IP，自动校验、评分、补货，并通过本地隧道代理转给业务侧；每次请求可换出口（Rotate）。

---

## 功能概览

| 模块 | 说明 |
|---|---|
| **代理池** | 按提取限速自动补货；TTL/评分/EWMA/热池；TCP 预检；Redis 跨重启恢复 |
| **质量校验** | 后台只测未验证 IP，目标站探活（默认百度首页），失败降权/踢出 |
| **本地隧道** | `127.0.0.1:18888` HTTP/HTTPS CONNECT；failover、自适应建连超时、建连对冲 |
| **归属地** | 优先使用厂商 JSON 中的省/市/运营商，避免再打 geo 接口 |
| **驾驶舱** | 池资产筛选排序、隧道效率、校验漏斗、指标释义；暗色/浅色主题 |

```
厂商提取 API ──► ProxyPool ──► TunnelServer :18888 ──► 业务侧
                    │                │
                    ▼                ▼
              Redis (DB 15)     Flask + 静态驾驶舱 :1234
```

---

## 快速开始

### 1. 环境要求

- Python 3.10+
- Redis（默认 `127.0.0.1:6379`，DB 15）
- 依赖：`flask` `requests` `urllib3` `redis`

```bash
pip install -r requirements.txt
```

### 2. 配置密钥（必做）

提取链接里的 `trade_no` / `secret` **不要写进仓库**。复制环境变量模板：

```bash
cp .env.example .env
# 编辑 .env，填入 PROXY_API_URL
```

Windows 也可直接设置系统环境变量 `PROXY_API_URL`。

```python
# config.py 会读取
PROXY_API_URL = os.getenv("PROXY_API_URL", "")
```

常用可选环境变量见 `.env.example`（Redis、监听地址、隧道端口等）。

### 3. 启动

```bash
python main.py
```

| 服务 | 地址 | 用途 |
|---|---|---|
| 驾驶舱 / API | `http://127.0.0.1:1234` | 管理与监控（只读为主） |
| 隧道代理 | `http://127.0.0.1:18888` | 业务侧配成 HTTP 代理 |

业务侧示例：

```python
proxies = {"http": "http://127.0.0.1:18888", "https": "http://127.0.0.1:18888"}
requests.get("https://example.com", proxies=proxies, timeout=10)
```

---

## 配置说明（`config.py`）

| 配置 | 默认 | 说明 |
|---|---|---|
| `PROXY_API_URL` | 环境变量 | 厂商提取链接（含密钥） |
| `API_MAX_NUM` | 10 | 单次提取个数 |
| `API_RESPONSE_FORMAT` | `json` | `json` / `text` / `auto` |
| `POOL_SIZE` / `POOL_MAX_SIZE` | 300 / 350 | 展示目标 / 补货水位 |
| `MAX_FETCH_PER_SECOND` | 10 | 提取限速（贴套餐） |
| `FAIL_KICK_THRESHOLD` | 3 | 软失败累计阈值；成功 −1 |
| `TUNNEL_PORT` | 18888 | 隧道监听端口 |
| `ADAPTIVE_TIMEOUT_*` | 开 | 建连超时按 EWMA + 剩余 TTL |
| `HEDGE_*` | 开 | 建连对冲（救场/空跑） |
| `VERIFY_*` | — | 目标站探活 profile |
| `GEO_*` | 开 | 厂商无归属地时后台补全 |

更完整的建连策略说明见 [docs/tunnel-stability.md](docs/tunnel-stability.md)。

---

## 核心行为

### 请求级换 IP（Rotate）

- 每次隧道建连从池内按质量分取候选，**避开上一次使用的出口**
- 无粘性；多条并发连接可共享同一出口 IP
- 失败自动 failover（含对冲），超时/拒连按策略降权或踢出

### 失败与踢出

| 类型 | 行为 |
|---|---|
| 软失败（超时等） | `fail_count + 1`；成功 `fail_count - 1`（不清零，抓 flapping） |
| 累计 ≥ `FAIL_KICK_THRESHOLD` | 踢出池、删 Redis |
| 硬失败（拒连/复位） | 立即踢出 |
| 低水位 | 只降权不踢，避免高峰掏空 |

### 建连优化

- **自适应超时**：`min(EWMA 推算, 剩余 TTL × 0.4)`，约 0.35～1.5s
- **建连对冲**：主连接挂起约 120ms 后并行备用出口，先通者胜（只对冲建连）
- **热池预建 TCP**：高分 IP 预握手，降低首包延迟

---

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 存活检查 |
| GET | `/api/status` | 池状态 + 代理列表 |
| GET | `/api/proxies` | 代理清单（`?limit=`） |
| GET | `/api/proxy` | 随机取一个（`?healthy=1` 先探活） |
| GET | `/api/tunnel` | 隧道统计（成功/切换/对冲等） |
| GET | `/api/metrics` | 时序与带宽 |
| POST | `/api/refresh` | 手动补货 `{"count": 10}` |
| POST | `/api/config` | 运行时改 `proxy_count` / `lease_seconds` 等 |

---

## 目录结构

```
sxs_proxy/
├── main.py              # Flask 装配与 API
├── config.py            # 全局配置（密钥走环境变量）
├── .env.example
├── crawler/
│   ├── proxy.py         # 代理池：解析/评分/补货/踢出
│   └── spider.py        # 目标站探活
├── tunnel/
│   └── server.py        # HTTP/CONNECT 隧道 + 对冲
├── utils/
│   ├── helper.py        # 提取链接改写、响应解析
│   ├── redis_store.py   # 代理持久化
│   ├── metrics.py       # 指标/带宽
│   └── geo.py           # 归属地补全
├── static/              # 驾驶舱前端
└── docs/tunnel-stability.md
```

---

## 运维提示

1. **密钥轮换**：若提取链接曾进过 git 或其它渠道，到厂商后台重置 `secret`。
2. **Redis**：代理与指标在 DB 15；清空会丢池内 IP，启动后自动重新提取。
3. **只绑本机**：隧道默认 `127.0.0.1`；Flask 默认监听可由 `HOST`/`PORT` 控制，生产建议限制来源。
4. **指标**：`/api/tunnel` 中 `hedge.saved`（救场）高、`hedge.wasted`（空跑）低为佳。
5. **容量**：稳态池大小 ≈ `提取速率 × IP 存活秒数`；10/s × 30～60s 约对应 300～600。

---

## 许可与使用

仅供授权场景与自用流量调度；请遵守目标站点服务条款与当地法规。
