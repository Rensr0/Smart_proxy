# -*- coding: utf-8 -*-
"""
全局配置：改这里即可，不必翻业务代码。

常用：
  PROXY_API_URL     厂商提取链接
  POOL_SIZE         展示目标；补货到 POOL_MAX_SIZE
  TUNNEL_*          隧道
  REDIS_*           持久化（DB 15）
"""

from __future__ import annotations

import os


# ===================== 厂商提取 =====================

# 从环境变量读取，避免密钥入库。参考 .env.example
PROXY_API_URL = os.getenv("PROXY_API_URL", "")
API_NUM_PARAM = "num"
# 单次提取个数（覆盖 URL 中 num）
API_MAX_NUM = 10
API_RESPONSE_FORMAT = "json"  # auto | json | text
API_TEXT_SEP = "auto"
PROXY_USER = os.getenv("PROXY_USER", "")
PROXY_PASS = os.getenv("PROXY_PASS", "")


# ===================== 代理池 =====================

POOL_SIZE = 300          # 展示目标
POOL_MAX_SIZE = 350      # 补货水位（超配缓冲）
LEASE_SECONDS = 30.0     # 兜底存活；接口 expire 优先
DISCARD_BEFORE_SECONDS = 5.0

MAX_FETCH_PER_SECOND = 10.0   # 套餐硬顶 10/s
REFILL_INTERVAL = 1.0
REQUEST_TIMEOUT = 10.0
FETCH_RETRIES = 3

# 入池 TCP 预检：off | async（推荐）| sync
TCP_PRECHECK = "async"


# ===================== 目标站校验 =====================

VERIFY_WORKERS_MAX = 200
VERIFY_TIMEOUT = 3.0
VERIFY_INTERVAL = 1.0
DEFAULT_VERIFY_PROFILE = "baidu"
VERIFY_BAIDU_URL = "https://www.baidu.com/"
VERIFY_BAIDU_METHOD = "GET"
# 响应体含此片段视为可达；空字符串则只要 HTTP 成功即算通过
VERIFY_BAIDU_EXPECT = "百度"


# ===================== Redis =====================

REDIS_ENABLED = True
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "15"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "proxy:")


# ===================== Web =====================

SERVER_HOST = os.getenv("HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT", "1234"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ===================== 隧道代理 =====================

TUNNEL_ENABLED = True
TUNNEL_HOST = os.getenv("TUNNEL_HOST", "127.0.0.1")
TUNNEL_PORT = int(os.getenv("TUNNEL_PORT", "18888"))
# 建连快失败；总预算约 1.0s × 6 次 ≈ 6s
TUNNEL_CONNECT_TIMEOUT = 1.0
TUNNEL_MAX_FAILOVER = 6
TUNNEL_IDLE_TIMEOUT = 120.0
TUNNEL_STALL_TIMEOUT = 4.0
TUNNEL_MAX_CONNS = 300

# ---- 自适应建连超时（按历史延迟 + 剩余 TTL 封顶）----
ADAPTIVE_TIMEOUT_ENABLED = True
TUNNEL_CONNECT_TIMEOUT_MIN = 0.35
TUNNEL_CONNECT_TIMEOUT_MAX = 1.5
ADAPTIVE_ALPHA = 0.6
ADAPTIVE_BETA_S = 0.25
ADAPTIVE_REM_TIMEOUT_RATIO = 0.4   # 超时 ≤ rem × 0.4

# ---- 建连对冲（只对冲 CONNECT，不对冲业务字节流）----
HEDGE_ENABLED = True
HEDGE_DELAY_MS = 120               # 主连接挂起多久后启动对冲
HEDGE_WIDTH = 2                    # 含主连接，上限 3
HEDGE_MAX_INFLIGHT = 32            # 全局同时对冲建连上限


# ===================== 评分 / 质量 =====================

# IP 归属地（后台线程静默补全，不阻塞主流程）
GEO_ENABLED = True
GEO_API_URL = os.getenv(
    "GEO_API_URL", "https://ip.shixis.site/api/ip/location?ip={ip}"
)
GEO_INTERVAL = 0.5           # 空闲扫描间隔；有活干时连续领活不停
GEO_RATE_PER_SEC = 0.0       # 0=不限速
GEO_TIMEOUT = 3.0            # 单次请求超时，失败快速让位
GEO_RETRY_COOLDOWN = 45.0    # 失败后短冷却，尽快重试
GEO_MIN_REMAINING = 3.0      # 剩余寿命过短才跳过（尽量多补）
GEO_WORKERS = 8              # 多线程持续拉取

SCORE_OK_COUNT_CAP = 20
SCORE_OK_COUNT_WEIGHT = 2.0
LATENCY_EWMA_ALPHA = 0.3

# 热池：已验证且延迟 ≤ 阈值
HOT_LATENCY_MAX_MS = 800.0
HOT_LATENCY_MAX_MS_LOOSE = 1500.0   # 无热池时放宽
HOT_POOL_MIN = 5

# 超时/转发失败连败 N 次才踢；拒连立即踢
FAIL_KICK_THRESHOLD = 3
# 池水位低于 target*该比例时暂停踢出
KICK_PAUSE_RATIO = 0.5
