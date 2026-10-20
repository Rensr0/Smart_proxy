# -*- coding: utf-8 -*-
"""
Redis 代理存储（默认 DB 15）

键设计（一个 IP 一个键，稳定可预测）：
  proxy:{host}:{port} -> JSON 字符串（完整代理信息）
  proxy:index         -> SET，所有成员为 "{host}:{port}"，便于全量扫描

持久化：
  - 写入时带 TTL（与代理剩余寿命一致）
  - 进程启动 load() 回载，池子可跨重启恢复
  - Redis 侧由 RDB/AOF 负责落盘
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("redis_store")

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore


DEFAULT_DB = 15
DEFAULT_PREFIX = "proxy:"


class RedisProxyStore:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = DEFAULT_DB,
        password: str = "",
        prefix: str = DEFAULT_PREFIX,
        socket_timeout: float = 2.0,
    ):
        if redis is None:
            raise RuntimeError("未安装 redis 包，请先 pip install redis")
        self.prefix = prefix
        self._lock = threading.RLock()
        self.client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password or None,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
            decode_responses=True,
        )
        self.db = db
        # 简单连通性检查
        self.client.ping()
        logger.info("Redis 已连接 db=%s prefix=%r", db, prefix)

    # ---------- key ----------

    def key_for(self, host: str, port: int) -> str:
        return f"{self.prefix}{host}:{port}"

    def index_key(self) -> str:
        return f"{self.prefix}index"

    @staticmethod
    def member_of(host: str, port: int) -> str:
        return f"{host}:{port}"

    # ---------- 写 ----------

    def save(self, data: Dict[str, Any], ttl: Optional[float] = None) -> None:
        """
        保存一个代理。data 需含 host/port。
        ttl 单位秒；<=0 或 None 时不设过期。
        """
        host = data.get("host")
        port = data.get("port")
        if not host or port is None:
            return
        # ttl<=0：已到期，删除，禁止 persist 成永久键
        if ttl is None or float(ttl) <= 0:
            self.delete(str(host), int(port))
            return
        key = self.key_for(str(host), int(port))
        member = self.member_of(str(host), int(port))
        payload = json.dumps(data, ensure_ascii=False, default=str)
        with self._lock:
            pipe = self.client.pipeline(transaction=False)
            pipe.set(key, payload)
            pipe.expire(key, max(1, int(ttl) + 1))
            pipe.sadd(self.index_key(), member)
            pipe.execute()

    def delete(self, host: str, port: int) -> None:
        key = self.key_for(str(host), int(port))
        member = self.member_of(str(host), int(port))
        with self._lock:
            pipe = self.client.pipeline(transaction=False)
            pipe.delete(key)
            pipe.srem(self.index_key(), member)
            pipe.execute()

    def save_many(self, items: List[Dict[str, Any]]) -> int:
        n = 0
        for d in items:
            try:
                self.save(d, ttl=d.get("_ttl"))
                n += 1
            except Exception as exc:
                logger.warning("redis save 失败: %s", exc)
        return n

    # ---------- 读 ----------

    def load(self) -> List[Dict[str, Any]]:
        """回载全部仍存活的代理。"""
        out: List[Dict[str, Any]] = []
        try:
            members = self.client.smembers(self.index_key())
        except Exception as exc:
            logger.warning("redis load index 失败: %s", exc)
            return out

        if not members:
            return out

        with self._lock:
            for member in members:
                try:
                    if ":" not in member:
                        continue
                    host, _, port_s = member.rpartition(":")
                    key = self.key_for(host, int(port_s))
                    raw = self.client.get(key)
                    if not raw:
                        # 键已过期，清掉索引
                        self.client.srem(self.index_key(), member)
                        continue
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        out.append(data)
                except Exception as exc:
                    logger.debug("redis load 单条失败 %s: %s", member, exc)
        logger.info("Redis 回载代理 %s 条 (db=%s)", len(out), self.db)
        return out

    def count(self) -> int:
        try:
            return int(self.client.scard(self.index_key()))
        except Exception:
            return 0

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


def dict_to_entry_payload(entry) -> Dict[str, Any]:
    """ProxyEntry -> 可序列化 dict（含 _ttl）。"""
    rem = max(0.0, entry.remaining())
    return {
        "host": entry.host,
        "port": entry.port,
        "expire_at": entry.expire_at,
        "username": entry.username,
        "password": entry.password,
        "source": entry.source,
        "latency_ms": entry.latency_ms,
        "ewma_latency_ms": getattr(entry, "ewma_latency_ms", None),
        "last_ok": entry.last_ok,
        "last_verify_at": entry.last_verify_at,
        "ok_count": int(getattr(entry, "ok_count", 0) or 0),
        "fail_count": int(getattr(entry, "fail_count", 0) or 0),
        "kicked": bool(getattr(entry, "kicked", False)),
        "geo_country": getattr(entry, "geo_country", "") or "",
        "geo_province": getattr(entry, "geo_province", "") or "",
        "geo_city": getattr(entry, "geo_city", "") or "",
        "geo_isp": getattr(entry, "geo_isp", "") or "",
        "_ttl": rem,
    }


def entry_from_payload(data: Dict[str, Any]):
    """dict -> ProxyEntry（跳过已过期）。"""
    from crawler.proxy import ProxyEntry

    try:
        expire_at = float(data.get("expire_at") or 0)
        host = str(data.get("host") or "")
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if not host or not port or expire_at <= time.time():
        return None
    return ProxyEntry(
        host=host,
        port=port,
        expire_at=expire_at,
        username=str(data.get("username") or ""),
        password=str(data.get("password") or ""),
        source=str(data.get("source") or "redis"),
        latency_ms=data.get("latency_ms"),
        ewma_latency_ms=data.get("ewma_latency_ms"),
        last_ok=data.get("last_ok"),
        last_verify_at=data.get("last_verify_at"),
        ok_count=int(data.get("ok_count") or 0),
        fail_count=int(data.get("fail_count") or 0),
        kicked=bool(data.get("kicked")),
        geo_country=str(data.get("geo_country") or ""),
        geo_province=str(data.get("geo_province") or ""),
        geo_city=str(data.get("geo_city") or ""),
        geo_isp=str(data.get("geo_isp") or ""),
    )
