# -*- coding: utf-8 -*-
"""
通用动态 IP 代理池（适配多厂商提取链接）

- api_url 直接粘贴厂商提取链接
- 批量取 IP 自动改写 num=N（受套餐单次上限约束）
- 限速 10 次/秒，默认单 IP 存活 30s
"""

from __future__ import annotations

import datetime
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils.helper import (
    build_api_url,
    looks_like_host,
    parse_api_response,
    peek_api_error,
    peek_url_num,
)

try:
    import config as app_config
except ImportError:  # pragma: no cover
    app_config = None  # type: ignore

logger = logging.getLogger("proxy_pool")


def _cfg(name: str, default):
    return getattr(app_config, name, default) if app_config is not None else default

# 尾部到期时间：2026-10-20 15:36:07 / 2026-10-20T15:36:07
_EXPIRE_SUFFIX_RE = re.compile(
    r"^(?P<proxy>.+?):(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*$"
)


def _parse_expire_text(text: str, fallback: float) -> float:
    """解析到期时间文本为 unix 时间戳（本地时区）。失败返回 fallback。"""
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    return fallback


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class PoolConfig:
    """代理池运行参数（默认值来自根目录 config.py）。"""

    proxy_count: int = 300
    max_proxy_count: int = 350

    # ---- 通用提取链接 ----
    api_url: str = ""
    api_num_param: str = "num"
    api_max_num: Optional[int] = None
    api_response_format: str = "auto"  # auto | json | text
    text_sep: str = "auto"

    # ---- 生命周期 / 限速 ----
    lease_seconds: float = 30.0
    discard_before_seconds: float = 5.0
    max_fetch_per_second: float = 10.0

    refill_interval: float = 1.0
    request_timeout: float = 10.0
    fetch_retries: int = 3

    verify_workers: int = 200
    verify_timeout: float = 3.0

    proxy_user: str = ""
    proxy_pass: str = ""

    user_agent: str = ""
    api_headers: Dict[str, str] = field(default_factory=dict)

    # ---- Redis 持久化 ----
    redis_enabled: bool = True
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 15
    redis_password: str = ""
    redis_prefix: str = "proxy:"

    def __post_init__(self) -> None:
        """用根目录 config.py 填值，保证改 config 即生效。"""
        if app_config is None:
            return
        self.proxy_count = _cfg("POOL_SIZE", self.proxy_count)
        self.max_proxy_count = _cfg("POOL_MAX_SIZE", self.max_proxy_count)
        if not self.api_url:
            self.api_url = _cfg("PROXY_API_URL", "")
        self.api_num_param = _cfg("API_NUM_PARAM", self.api_num_param)
        if self.api_max_num is None:
            self.api_max_num = _cfg("API_MAX_NUM", None)
        self.api_response_format = _cfg("API_RESPONSE_FORMAT", self.api_response_format)
        self.text_sep = _cfg("API_TEXT_SEP", self.text_sep)
        self.lease_seconds = _cfg("LEASE_SECONDS", self.lease_seconds)
        self.discard_before_seconds = _cfg("DISCARD_BEFORE_SECONDS", self.discard_before_seconds)
        self.max_fetch_per_second = _cfg("MAX_FETCH_PER_SECOND", self.max_fetch_per_second)
        self.refill_interval = _cfg("REFILL_INTERVAL", self.refill_interval)
        self.request_timeout = _cfg("REQUEST_TIMEOUT", self.request_timeout)
        self.fetch_retries = _cfg("FETCH_RETRIES", self.fetch_retries)
        self.verify_workers = _cfg("VERIFY_WORKERS_MAX", self.verify_workers)
        self.verify_timeout = _cfg("VERIFY_TIMEOUT", self.verify_timeout)
        self.proxy_user = _cfg("PROXY_USER", self.proxy_user)
        self.proxy_pass = _cfg("PROXY_PASS", self.proxy_pass)
        if not self.user_agent:
            self.user_agent = _cfg("USER_AGENT", "")
        self.redis_enabled = _cfg("REDIS_ENABLED", self.redis_enabled)
        self.redis_host = _cfg("REDIS_HOST", self.redis_host)
        self.redis_port = _cfg("REDIS_PORT", self.redis_port)
        self.redis_db = _cfg("REDIS_DB", self.redis_db)
        self.redis_password = _cfg("REDIS_PASSWORD", self.redis_password)
        self.redis_prefix = _cfg("REDIS_PREFIX", self.redis_prefix)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ProxyEntry:
    host: str
    port: int
    expire_at: float
    username: str = ""
    password: str = ""
    source: str = "api"
    # 最近一次探活：延迟(ms) / 是否成功 / 时间戳
    latency_ms: Optional[float] = None
    last_ok: Optional[bool] = None
    last_verify_at: Optional[float] = None
    # 真实转发成功次数（隧道/业务），成功越多越可信
    ok_count: int = 0
    # 软污点计数：失败 +1，成功 -1（不清零）。达到阈值才踢出
    # 清零会被 flapping IP（失败→成功→失败…）洗白；不动则偶发失败永不修复
    fail_count: int = 0
    # 已踢出，避免重复 kick / 累加
    kicked: bool = False
    # 真实流量延迟滑动平均（比单次探活更稳）
    ewma_latency_ms: Optional[float] = None
    # IP 归属地（后台线程补全，可为空）
    geo_country: str = ""
    geo_province: str = ""
    geo_city: str = ""
    geo_isp: str = ""

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def auth_address(self) -> str:
        if self.username and self.password:
            return f"{self.username}:{self.password}@{self.host}:{self.port}"
        return self.address

    def remaining(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return self.expire_at - now

    def is_dead(self, now: Optional[float] = None, margin: float = 0.0) -> bool:
        return self.remaining(now) <= margin

    def quality_score(self, now: Optional[float] = None) -> float:
        """
        越大越好。
        - 剩余寿命：5s 内近零；5~12s 平滑升权（无断崖）
        - 延迟：EWMA 优先，无延迟样本按 1000ms 估
        - last_ok / ok_count / 新鲜度 / 热池 / 信任
        - fail_count 全分支降权
        """
        now = time.time() if now is None else now
        rem = max(0.0, self.remaining(now))

        # 临期平滑：0~3s 近零；3~12s smoothstep，避免 5s 断崖
        if rem <= 3.0:
            return rem * 0.05
        if rem < 12.0:
            x = (rem - 3.0) / 9.0
            expire_factor = x * x * (3.0 - 2.0 * x)  # smoothstep 0→1
        else:
            expire_factor = 1.0

        rem_capped = min(rem, 90.0)
        rem_score = rem_capped * 0.6

        weight = 1.0
        cap = 20
        hot_max = 800.0
        if app_config is not None:
            weight = float(getattr(app_config, "SCORE_OK_COUNT_WEIGHT", 1.0))
            cap = int(getattr(app_config, "SCORE_OK_COUNT_CAP", 20))
            hot_max = float(getattr(app_config, "HOT_LATENCY_MAX_MS", 800.0))
        ok_bonus = min(int(self.ok_count or 0), max(1, cap)) * weight

        fresh_bonus = 0.0
        if self.last_verify_at:
            age = max(0.0, now - float(self.last_verify_at))
            fresh_bonus = max(0.0, 12.0 * (1.0 - min(age, 60.0) / 60.0))

        hot_bonus = 8.0 if self.is_hot(hot_max) else 0.0
        fail_penalty = min(int(self.fail_count or 0), 3) * 25.0

        if self.last_ok is True:
            # 无延迟样本时按 1000ms 估（勿用 2000 过度惩罚）
            if self.ewma_latency_ms is not None:
                lat = float(self.ewma_latency_ms)
            elif self.latency_ms is not None:
                lat = float(self.latency_ms)
            else:
                lat = 1000.0
            lat_score = 100.0 * (1500.0 / (lat + 500.0))
            trust_bonus = 6.0 if self.ewma_latency_ms is not None else 0.0
            base = (
                100.0 + rem_score + lat_score + ok_bonus
                + fresh_bonus + hot_bonus + trust_bonus - fail_penalty
            )
            return max(0.0, base * expire_factor)

        # 未验证 / 失败过：压低，且同样扣 fail_penalty
        base = rem_capped * 0.15 + ok_bonus * 0.3 + fresh_bonus * 0.2 - fail_penalty
        return max(0.0, base * expire_factor)

    def mark_success(self) -> None:
        self.ok_count = int(self.ok_count or 0) + 1
        # 成功只「减刑」不「清零」：既留案底抓住 flapping，又允许逐步恢复
        self.fail_count = max(0, int(self.fail_count or 0) - 1)

    def mark_fail(self) -> int:
        """记一次失败；已踢出则不再累加。"""
        if self.kicked:
            return int(self.fail_count or 0)
        self.fail_count = int(self.fail_count or 0) + 1
        return self.fail_count

    def update_latency(self, ms: float) -> None:
        """真实流量延迟 -> EWMA，越用越准。"""
        if ms is None or ms < 0:
            return
        try:
            alpha = 0.3
            if app_config is not None:
                alpha = float(getattr(app_config, "LATENCY_EWMA_ALPHA", 0.3))
            alpha = min(1.0, max(0.05, alpha))
            # 离群超时（>15s）不污染 EWMA
            if float(ms) > 15000.0:
                return
            prev = self.ewma_latency_ms
            self.ewma_latency_ms = float(ms) if prev is None else (alpha * float(ms) + (1 - alpha) * float(prev))
            self.latency_ms = self.ewma_latency_ms
        except Exception:
            pass

    @property
    def effective_latency_ms(self) -> float:
        if self.ewma_latency_ms is not None:
            return float(self.ewma_latency_ms)
        if self.latency_ms is not None:
            return float(self.latency_ms)
        return 1000.0

    def is_hot(self, hot_max_ms: float = 800.0) -> bool:
        """热池：已验证且延迟够快。"""
        return bool(self.last_ok is True) and self.effective_latency_ms <= float(hot_max_ms)

    def sort_key(self, now: Optional[float] = None) -> float:
        """供排序：越大越靠前。"""
        return self.quality_score(now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "address": self.address,
            "auth_address": self.auth_address,
            "username": self.username,
            "remaining": round(self.remaining(), 1),
            "expire_at": self.expire_at,
            "latency_ms": self.latency_ms,
            "ewma_latency_ms": self.ewma_latency_ms,
            "hot": self.is_hot(),
            "last_ok": self.last_ok,
            "ok_count": int(self.ok_count or 0),
            "fail_count": int(self.fail_count or 0),
            "score": round(self.quality_score(), 2),
            "geo": {
                "country": self.geo_country or "",
                "province": self.geo_province or "",
                "city": self.geo_city or "",
                "isp": self.geo_isp or "",
            },
        }

    @staticmethod
    def parse(raw: str, default_ttl: float) -> Optional["ProxyEntry"]:
        """
        兼容：
          - ip:port
          - ip:port,expire
          - user:pass@ip:port
          - ip:port:user:pass
          - ip:port:user:pass:2026-10-20 15:36:07   # 91http time=1&pw=1
          - user:pass:ip:port
          - http://user:pass@ip:port
        时间戳里的冒号不能用 split(':') 切，先用正则摘掉尾部时间。
        """
        if not raw:
            return None
        raw = raw.strip().strip('"').strip("'")
        if not raw:
            return None

        expire_at = time.time() + default_ttl

        # 尾部到期时间：2026-10-20 15:36:07（优先，覆盖默认 TTL）
        m = _EXPIRE_SUFFIX_RE.match(raw)
        if m:
            raw = m.group("proxy").rstrip(": ")
            expire_at = _parse_expire_text(m.group("ts"), fallback=expire_at)

        if "," in raw and "@" not in raw.split(",")[0]:
            head, _, tail = raw.partition(",")
            if re.match(r"^[^,\s]+:\d+$", head.strip()) or "@" in head:
                raw = head
                tail = tail.strip()
                try:
                    val = float(tail)
                    expire_at = val if val > 1e10 else time.time() + val
                except ValueError:
                    pass

        username = password = ""

        if "://" in raw:
            parsed = urlparse(raw)
            if parsed.username:
                username = parsed.username or ""
                password = parsed.password or ""
            host = parsed.hostname or ""
            port = parsed.port or 0
            if host and port and looks_like_host(host):
                return ProxyEntry(host, int(port), expire_at, username, password)
            return None

        if "@" in raw:
            auth, _, hostpart = raw.rpartition("@")
            if ":" in auth:
                username, _, password = auth.partition(":")
            raw = hostpart

        parts = raw.split(":")
        if len(parts) == 4 and all(parts):
            a, b, c, d = parts
            # host:port:user:pass
            if b.isdigit() and 0 < int(b) < 65536 and not d.isdigit():
                username, password = c, d
                raw = f"{a}:{b}"
            # user:pass:host:port
            elif d.isdigit() and 0 < int(d) < 65536 and not b.isdigit():
                username, password = a, b
                raw = f"{c}:{d}"
            elif b.isdigit() and 0 < int(b) < 65536:
                username, password = c, d
                raw = f"{a}:{b}"

        if ":" not in raw:
            return None
        host, _, port_s = raw.rpartition(":")
        host = host.strip().strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            return None
        if not host or not (0 < port < 65536):
            return None
        if not looks_like_host(host):
            return None
        return ProxyEntry(
            host=host,
            port=port,
            expire_at=expire_at,
            username=username.strip(),
            password=password.strip(),
        )


# ---------------------------------------------------------------------------
# 代理池
# ---------------------------------------------------------------------------

def _tcp_alive(host: str, port: int, timeout: float = 0.6) -> bool:
    """入池前 TCP 预检：明显不通的直接丢，降低坏 IP 进池。"""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


class ProxyPool:
    def __init__(self, config: Optional[PoolConfig] = None):
        self.config = config or PoolConfig()

        self._lock = threading.RLock()
        self._alive: List[ProxyEntry] = []
        self._known: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._fetch_times: List[float] = []
        self._rate_lock = threading.Lock()

        if self.config.api_max_num is not None:
            self._api_max_num = max(1, int(self.config.api_max_num))
        else:
            self._api_max_num = peek_url_num(self.config.api_url, self.config.api_num_param)

        self._session = self._build_session()
        self.stats = {
            "fetched": 0,
            "added": 0,
            "dropped": 0,
            "fetch_fail": 0,
            "fetch_throttled": 0,
        }

        self._store = None  # RedisProxyStore
        if self.config.redis_enabled:
            self._init_store()

    def _init_store(self) -> None:
        try:
            from utils.redis_store import RedisProxyStore
            self._store = RedisProxyStore(
                host=self.config.redis_host,
                port=self.config.redis_port,
                db=self.config.redis_db,
                password=self.config.redis_password,
                prefix=self.config.redis_prefix,
            )
        except Exception as exc:
            logger.warning("Redis 不可用，跳过持久化: %s", exc)
            self._store = None

    def _store_save(self, entry: ProxyEntry) -> None:
        if not self._store:
            return
        try:
            from utils.redis_store import dict_to_entry_payload
            payload = dict_to_entry_payload(entry)
            self._store.save(payload, ttl=payload.get("_ttl"))
        except Exception as exc:
            logger.debug("redis save 失败: %s", exc)

    def _store_delete(self, host: str, port: int) -> None:
        if not self._store:
            return
        try:
            self._store.delete(host, port)
        except Exception as exc:
            logger.debug("redis delete 失败: %s", exc)

    def _load_from_store(self) -> int:
        if not self._store:
            return 0
        try:
            from utils.redis_store import entry_from_payload
            rows = self._store.load()
        except Exception as exc:
            logger.warning("redis 回载失败: %s", exc)
            return 0
        entries = []
        for row in rows:
            e = entry_from_payload(row)
            if e:
                entries.append(e)
        with self._lock:
            return self._add_locked(entries, persist=False)

    # ---------- session ----------

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=self.config.fetch_retries,
            backoff_factor=0.3,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    # ---------- 限速 ----------

    def _acquire_fetch_slot(self) -> bool:
        limit = max(1.0, float(self.config.max_fetch_per_second))
        while not self._stop.is_set():
            now = time.monotonic()
            with self._rate_lock:
                cutoff = now - 1.0
                self._fetch_times = [t for t in self._fetch_times if t > cutoff]
                if len(self._fetch_times) < limit:
                    self._fetch_times.append(now)
                    return True
                wait = self._fetch_times[0] + 1.0 - now
            time.sleep(max(0.005, min(wait, 0.1)))
        return False

    # ---------- API ----------

    def _fetch_proxy_list(self, count: int) -> List[ProxyEntry]:
        if count <= 0:
            return []
        entries: List[ProxyEntry] = []
        # 单次上限由 api_max_num 控制，外层限速 ≤10/s
        safety = count + 2 if self._api_max_num <= 1 else min(200, count * 2 + 5)
        attempts = 0
        while len(entries) < count and attempts < safety:
            attempts += 1
            batch = min(count - len(entries), self._api_max_num)
            got = self._fetch_once(batch)
            if not got:
                # 连续失败就停，避免空转打爆接口
                break
            entries.extend(got)
        self.stats["fetched"] += len(entries)
        return entries

    def _fetch_once(self, count: int) -> List[ProxyEntry]:
        if count <= 0:
            return []
        if not self._acquire_fetch_slot():
            self.stats["fetch_throttled"] += 1
            return []

        url = build_api_url(self.config.api_url, count, self.config.api_num_param)
        headers = {"User-Agent": self.config.user_agent}
        if self.config.api_headers:
            headers.update(self.config.api_headers)

        try:
            res = self._session.get(url, timeout=self.config.request_timeout, headers=headers)
            res.raise_for_status()
        except Exception as exc:
            self.stats["fetch_fail"] += 1
            logger.warning("获取代理失败: %s", exc)
            return []

        body_text = (res.text or "").strip()
        err = peek_api_error(body_text)
        if err:
            code, msg = err
            self.stats["fetch_fail"] += 1
            logger.warning("提取接口返回错误 code=%s msg=%s", code, msg)
            if count > 1 and ("数量" in str(msg) or "limit" in str(msg).lower() or code in (1005, "1005")):
                self._api_max_num = max(1, count // 2)
                self._last_qty_error_ts = time.time()
                logger.info("自动下调单次提取上限为 %s", self._api_max_num)
            return []

        try:
            raw_items = parse_api_response(
                res.content,
                fmt=self.config.api_response_format,
                text_sep=self.config.text_sep,
            )
        except Exception as exc:
            self.stats["fetch_fail"] += 1
            logger.warning("解析提取结果失败: %s", exc)
            return []

        ttl = self.config.lease_seconds
        entries: List[ProxyEntry] = []
        for item in raw_items:
            entry = self._to_entry(item, default_ttl=ttl)
            if entry:
                entries.append(entry)

        if not entries:
            self.stats["fetch_fail"] += 1
            logger.warning("提取结果为空或无法解析，body[:200]=%r", body_text[:200])
        return entries

    def _to_entry(self, item: Any, default_ttl: float) -> Optional[ProxyEntry]:
        if isinstance(item, ProxyEntry):
            return item

        if isinstance(item, dict):
            host = item.get("ip") or item.get("host") or item.get("proxy") or item.get("addr")
            port = item.get("port")
            # 91http json: http_user / http_pass；兼容其它厂商字段名
            username = str(
                item.get("http_user") or item.get("user") or item.get("username")
                or item.get("account") or ""
            )
            password = str(
                item.get("http_pass") or item.get("pass") or item.get("password")
                or item.get("pwd") or ""
            )
            expire_raw = (
                item.get("expire_time") or item.get("expire") or item.get("expire_at")
                or item.get("deadline") or item.get("ttl")
            )
            expire_at = time.time() + default_ttl
            if expire_raw is not None:
                if isinstance(expire_raw, str):
                    try:
                        val = float(expire_raw)
                        expire_at = val if val > 1e10 else time.time() + val
                    except ValueError:
                        # "2026-10-22 15:22:50"
                        expire_at = _parse_expire_text(expire_raw, fallback=expire_at)
                else:
                    try:
                        val = float(expire_raw)
                        expire_at = val if val > 1e10 else time.time() + val
                    except (TypeError, ValueError):
                        pass

            # 归属地：厂商 JSON 自带则直接入池，geo 后台可跳过
            geo_country = str(item.get("country") or item.get("geo_country") or "").strip()
            geo_province = str(item.get("province") or item.get("geo_province") or "").strip()
            geo_city = str(item.get("city") or item.get("geo_city") or "").strip()
            geo_isp = str(item.get("isp") or item.get("geo_isp") or "").strip()

            if host is None and port is None:
                for v in item.values():
                    if isinstance(v, str) and ":" in v:
                        e = ProxyEntry.parse(v, default_ttl=default_ttl)
                        if e:
                            return e
                return None

            if host is None or port is None:
                if isinstance(host, str) and ":" in host:
                    return ProxyEntry.parse(host, default_ttl=default_ttl)
                return None

            try:
                return ProxyEntry(
                    host=str(host).strip(),
                    port=int(port),
                    expire_at=expire_at,
                    username=username,
                    password=password,
                    geo_country=geo_country,
                    geo_province=geo_province,
                    geo_city=geo_city,
                    geo_isp=geo_isp,
                )
            except (TypeError, ValueError):
                return None

        return ProxyEntry.parse(str(item), default_ttl=default_ttl)

    # ---------- 池维护 ----------

    def _init_proxy(self) -> None:
        # 1) 先从 Redis 回载，实现跨重启持久化
        restored = self._load_from_store()
        if restored:
            logger.info("从 Redis 恢复 %s 个代理", restored)
        # 2) 再预热一小批，其余交给后台循环补到 POOL_MAX_SIZE
        warmup = min(self.config.proxy_count, max(5, int(self.config.max_fetch_per_second)))
        entries = self._filter_precheck(self._fetch_proxy_list(warmup))
        with self._lock:
            self._add_locked(entries)

    def _precheck_mode(self) -> str:
        raw = getattr(app_config, "TCP_PRECHECK", "async") if app_config else "async"
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "sync"):
            return "sync"
        if s in ("0", "false", "no", "off", "none"):
            return "off"
        return "async"

    def _async_precheck(self, entries: List[ProxyEntry]) -> None:
        """入池后异步 TCP 探测：不挡补货；连续失败则踢出。"""
        items = [e for e in entries if not e.is_dead(margin=self.config.discard_before_seconds)]
        if not items:
            return

        def _worker():
            from concurrent.futures import ThreadPoolExecutor, as_completed
            workers = min(16, max(2, len(items)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_tcp_alive, e.host, e.port, 0.45): e for e in items}
                for fut in as_completed(futs):
                    e = futs[fut]
                    try:
                        ok = bool(fut.result())
                    except Exception:
                        ok = False
                    if ok:
                        continue
                    fails = e.mark_fail()
                    if fails >= int(_cfg("FAIL_KICK_THRESHOLD", 3)):
                        self.drop_entries([e])
                    else:
                        self.touch_entry(e)

        threading.Thread(target=_worker, name="TcpPrecheck", daemon=True).start()

    def _filter_precheck(self, entries: List[ProxyEntry]) -> List[ProxyEntry]:
        """
        sync：入池前过滤（慢）
        async：直接放行，后台异步探测
        off：全放行
        """
        mode = self._precheck_mode()
        if mode == "off":
            return entries
        entries = [e for e in entries if not e.is_dead(margin=self.config.discard_before_seconds)]
        if mode == "async":
            return entries
        if len(entries) == 1:
            return entries if _tcp_alive(entries[0].host, entries[0].port, timeout=0.4) else []
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            out: List[ProxyEntry] = []
            workers = min(32, max(4, len(entries)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_tcp_alive, e.host, e.port, 0.4): e for e in entries}
                for fut in as_completed(futs):
                    e = futs[fut]
                    try:
                        if fut.result():
                            out.append(e)
                    except Exception:
                        continue
            return out
        except Exception:
            return entries

    def _add_locked(self, entries: List[ProxyEntry], persist: bool = True) -> int:
        added = 0
        new_entries: List[ProxyEntry] = []
        for e in entries:
            if e.address in self._known:
                if persist:
                    self._store_save(e)
                continue
            if e.is_dead(margin=self.config.discard_before_seconds):
                continue
            self._alive.append(e)
            self._known.add(e.address)
            added += 1
            if persist:
                self._store_save(e)
            new_entries.append(e)
        self.stats["added"] += added
        # async 模式：入池后异步质检，不阻塞
        if added and self._precheck_mode() == "async" and persist:
            self._async_precheck(new_entries)
        return added

    def _sweep_locked(self, now: float) -> int:
        keep: List[ProxyEntry] = []
        dropped = 0
        for e in self._alive:
            if e.is_dead(now, margin=self.config.discard_before_seconds):
                self._known.discard(e.address)
                self._store_delete(e.host, e.port)
                dropped += 1
            else:
                keep.append(e)
        self._alive = keep
        self.stats["dropped"] += dropped
        if dropped:
            self._known = {e.address for e in self._alive}
        return dropped

    def drop_entries(self, entries: List[ProxyEntry]) -> None:
        """外部探活失败时批量剔除并同步 Redis。"""
        if not entries:
            return
        bad = {e.address for e in entries}
        with self._lock:
            self._alive = [e for e in self._alive if e.address not in bad]
            self._known = {e.address for e in self._alive}
        for e in entries:
            self._store_delete(e.host, e.port)

    def touch_entry(self, entry: ProxyEntry) -> None:
        """探活后回写延迟等到 Redis。"""
        self._store_save(entry)

    def snapshot_entries(self) -> List[ProxyEntry]:
        """当前存活代理的浅列表（供后台线程扫描，不阻塞补货）。"""
        with self._lock:
            return list(self._alive)

    def set_entry_geo(self, host: str, port: int, geo: Dict[str, str]) -> bool:
        """写入归属地并落 Redis；找不到条目则忽略。落盘前再次确认仍在池中，
        避免踢出/过期后被后台线程误写回 Redis。"""
        if not host or not port:
            return False
        addr = f"{host}:{int(port)}"
        with self._lock:
            target = None
            for e in self._alive:
                if e.address == addr:
                    target = e
                    break
            if target is None:
                return False
            target.geo_country = str(geo.get("country") or "")
            target.geo_province = str(geo.get("province") or "")
            target.geo_city = str(geo.get("city") or "")
            target.geo_isp = str(geo.get("isp") or "")
            # 在锁内落盘，且仅当仍存活；ttl<=0 时 _store_save 会删键而不是复活
            self._store_save(target)
            return True

    def add_alive_proxy(self, add_count: int) -> int:
        entries = self._filter_precheck(self._fetch_proxy_list(add_count))
        with self._lock:
            return self._add_locked(entries)

    def get_proxy(self) -> str:
        entry = self.get_entry()
        return entry.auth_address if entry else ""

    def ranked_entries(self) -> List[ProxyEntry]:
        """按质量分降序：延迟低 + 剩余久 的排前面。"""
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            return sorted(self._alive, key=lambda e: e.sort_key(now), reverse=True)

    def get_entry(self, top_k: int = 5, min_remaining: float = 5.0) -> Optional[ProxyEntry]:
        """
        优先取「已验证通过」且排名靠前的。
        剩余寿命 < min_remaining 的不发放（拿到也用不了）。
        """
        ranked = self.ranked_entries()
        if not ranked:
            return None
        usable = [e for e in ranked if e.remaining() >= min_remaining]
        if not usable:
            return None
        verified = [e for e in usable if e.last_ok is True]
        pool = verified or usable
        if top_k <= 1:
            return pool[0]
        return random.choice(pool[: min(top_k, len(pool))])

    def snapshot(self) -> List[ProxyEntry]:
        with self._lock:
            self._sweep_locked(time.time())
            return list(self._alive)

    def size(self) -> int:
        with self._lock:
            return len(self._alive)

    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            items = sorted(self._alive, key=lambda e: e.sort_key(now), reverse=True)
            payload = [e.to_dict() for e in items]
            return {
                "size": len(self._alive),
                "target": self.config.proxy_count,
                "lease_seconds": self.config.lease_seconds,
                "api_max_num": self._api_max_num,
                "max_fetch_per_second": self.config.max_fetch_per_second,
                "redis": {
                    "enabled": self._store is not None,
                    "db": self.config.redis_db,
                    "count": self._store.count() if self._store else 0,
                },
                "stats": dict(self.stats),
                "proxies": payload,
            }

    # ---------- 后台循环 ----------

    def _maybe_restore_max_num(self) -> None:
        """数量错误降级后，冷却一段时间再尝试恢复，避免反复撞墙。"""
        want = _cfg("API_MAX_NUM", None) if app_config else None
        if not want:
            return
        want = max(1, int(want))
        if self._api_max_num >= want:
            return
        last_err = getattr(self, "_last_qty_error_ts", 0.0)
        if time.time() - float(last_err) < 60.0:
            return
        self._api_max_num = want

    def run(self) -> None:
        """
        补货循环：未满时连续提取，节奏由 10/s 限速器控制；
        池满才空闲等待，避免「取一批再睡 1s」导致实际只有 5~8/s。
        """
        idle_wait = max(0.2, float(self.config.refill_interval))
        while not self._stop.is_set():
            # 每轮重算，跟随 api_max_num 变化
            batch = max(1, int(self._api_max_num or 1))
            try:
                now = time.time()
                with self._lock:
                    self._sweep_locked(now)
                    # 补货到 MAX（超配缓冲），展示目标仍是 proxy_count
                    missing = self.config.max_proxy_count - len(self._alive)

                if missing <= 0:
                    self._stop.wait(idle_wait)
                    continue
                self._maybe_restore_max_num()

                fetch_n = min(missing, batch, self.config.max_proxy_count)
                entries = self._filter_precheck(self._fetch_proxy_list(fetch_n))
                with self._lock:
                    self._add_locked(entries)

                if not entries:
                    # 提取失败/限速退避，短等再试
                    self._stop.wait(0.15)
                # 拉到就立刻下一轮，不额外 sleep —— _acquire_fetch_slot 负责 ≤10/s

            except Exception:
                logger.exception("IP 池维护循环异常")
                self._stop.wait(0.5)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._init_proxy()
        self._thread = threading.Thread(target=self.run, name="ProxyPool", daemon=True)
        self._thread.start()
        logger.info("IP 池已启动，目标维持 %s 个，当前 %s", self.config.proxy_count, self.size())

    def stop(self, join_timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)
        self._session.close()

    # ---------- 请求代理 ----------

    def build_proxies(self, entry_or_address: Union[str, ProxyEntry]) -> Dict[str, str]:
        if isinstance(entry_or_address, ProxyEntry):
            e = entry_or_address
            if e.username and e.password:
                proxy_url = f"http://{e.username}:{e.password}@{e.host}:{e.port}"
            else:
                proxy_url = f"http://{e.host}:{e.port}"
        else:
            address = entry_or_address
            cfg = self.config
            if "@" in address:
                auth, _, hostport = address.rpartition("@")
                if ":" in auth:
                    user, _, pwd = auth.partition(":")
                    proxy_url = f"http://{user}:{pwd}@{hostport}"
                else:
                    proxy_url = f"http://{hostport}"
            elif cfg.proxy_user and cfg.proxy_pass:
                proxy_url = f"http://{cfg.proxy_user}:{cfg.proxy_pass}@{address}"
            else:
                proxy_url = f"http://{address}"
        return {"http": proxy_url, "https": proxy_url}

    def request(self, method: str, url: str, **kwargs):
        entry = self.get_entry()
        if not entry:
            raise RuntimeError("代理池为空")
        kwargs.setdefault("proxies", self.build_proxies(entry))
        kwargs.setdefault("timeout", self.config.verify_timeout)
        return self._session.request(method.upper(), url, **kwargs)
