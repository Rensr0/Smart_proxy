# -*- coding: utf-8 -*-
"""
IP 归属地后台补全。

- 独立线程，不阻塞提 IP / 校验 / 隧道
- 自建接口不限速，有活连续领，尽量补满
- 失败短冷却后重试；结果写回 ProxyEntry 并落 Redis
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("geo")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


class GeoEnricher:
    def __init__(self, pool, config=None):
        self.pool = pool
        self.config = config
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._fail_until: Dict[str, float] = {}
        self._claimed: Set[str] = set()
        self._next_slot = 0.0
        self._sessions: Dict[int, Any] = {}
        self.stats = {
            "lookups": 0,
            "ok": 0,
            "fail": 0,
            "skip": 0,
        }

    # ---------- config ----------

    def _cfg(self, name: str, default):
        cfg = self.config
        if cfg is not None and hasattr(cfg, name):
            return getattr(cfg, name)
        try:
            import config as app_config

            return getattr(app_config, name, default)
        except Exception:
            return default

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("GEO_ENABLED", True)) and requests is not None

    @property
    def api_url(self) -> str:
        return str(
            self._cfg("GEO_API_URL", "https://ip.shixis.site/api/ip/location?ip={ip}")
        )

    @property
    def rate_per_sec(self) -> float:
        """0 或负数表示不限速。"""
        try:
            return float(self._cfg("GEO_RATE_PER_SEC", 0.0))
        except Exception:
            return 0.0

    @property
    def timeout(self) -> float:
        try:
            return max(0.5, float(self._cfg("GEO_TIMEOUT", 3.0)))
        except Exception:
            return 3.0

    @property
    def cooldown(self) -> float:
        try:
            return max(5.0, float(self._cfg("GEO_RETRY_COOLDOWN", 45.0)))
        except Exception:
            return 45.0

    @property
    def interval(self) -> float:
        try:
            return max(0.1, float(self._cfg("GEO_INTERVAL", 0.5)))
        except Exception:
            return 0.5

    @property
    def workers(self) -> int:
        try:
            return max(1, min(32, int(self._cfg("GEO_WORKERS", 8))))
        except Exception:
            return 8

    @property
    def min_remaining(self) -> float:
        try:
            return max(0.0, float(self._cfg("GEO_MIN_REMAINING", 3.0)))
        except Exception:
            return 3.0

    # ---------- cooldown / claim / rate ----------

    def _prune_cooldown(self) -> None:
        now = time.time()
        with self._lock:
            dead = [h for h, until in self._fail_until.items() if until <= now]
            for h in dead:
                self._fail_until.pop(h, None)

    def _in_cooldown(self, host: str) -> bool:
        now = time.time()
        with self._lock:
            until = self._fail_until.get(host)
            if until is None:
                return False
            if now < until:
                return True
            self._fail_until.pop(host, None)
            return False

    def _mark_fail(self, host: str) -> None:
        with self._lock:
            self._fail_until[host] = time.time() + self.cooldown

    def _mark_ok(self, host: str) -> None:
        with self._lock:
            self._fail_until.pop(host, None)

    def _geo_done(self, entry) -> bool:
        """省份或运营商任一有值即算补全（列表/地图够用）。"""
        return bool(
            getattr(entry, "geo_province", "") or getattr(entry, "geo_isp", "")
        )

    def _claim_next(self):
        """取一个待处理且未被认领的条目；无则返回 None。"""
        self._prune_cooldown()
        entries = self.pool.snapshot_entries()
        min_rem = self.min_remaining
        with self._lock:
            for e in entries:
                host = e.host
                if host in self._claimed:
                    continue
                if self._geo_done(e):
                    continue
                try:
                    if e.remaining() < min_rem:
                        continue
                except Exception:
                    continue
                if host in self._fail_until and self._fail_until[host] > time.time():
                    continue
                self._claimed.add(host)
                return e
        return None

    def _release(self, host: str) -> None:
        with self._lock:
            self._claimed.discard(host)

    def _wait_rate(self) -> None:
        rate = self.rate_per_sec
        if rate <= 0:
            return
        gap = 1.0 / rate
        with self._lock:
            now = time.time()
            slot = max(now, self._next_slot)
            self._next_slot = slot + gap
            delay = slot - now
        if delay > 0:
            self._stop.wait(delay)

    def _session(self):
        """每个 worker 一个 Session，复用连接、降低开销。"""
        if requests is None:
            return None
        tid = threading.get_ident()
        s = self._sessions.get(tid)
        if s is None:
            s = requests.Session()
            self._sessions[tid] = s
        return s

    # ---------- lookup ----------

    @staticmethod
    def _is_unknown(val: str) -> bool:
        v = (val or "").strip()
        return (not v) or v in ("未知", "unknown", "None", "null", "-")

    def lookup(self, ip: str) -> Optional[Dict[str, str]]:
        if not ip or requests is None:
            return None
        url = self.api_url.replace("{ip}", ip)
        try:
            sess = self._session() or requests
            resp = sess.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        node = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(node, dict):
            return None

        country = str(node.get("国家") or node.get("country") or "").strip()
        province = str(node.get("省份") or node.get("province") or "").strip()
        city = str(node.get("城市") or node.get("city") or "").strip()
        isp = str(node.get("运营商") or node.get("isp") or "").strip()

        if self._is_unknown(country):
            country = ""
        if self._is_unknown(province):
            province = ""
        if self._is_unknown(city):
            city = ""
        if self._is_unknown(isp):
            isp = ""

        # 至少要有省或运营商，否则视为无效，稍后重试
        if not province and not isp:
            return None
        return {
            "country": country,
            "province": province,
            "city": city,
            "isp": isp,
        }

    def _apply(self, entry, geo: Dict[str, str]) -> bool:
        try:
            return bool(self.pool.set_entry_geo(entry.host, entry.port, geo))
        except Exception as exc:
            logger.debug("set_entry_geo 失败 %s: %s", entry.address, exc)
            return False

    def process_one(self, entry) -> str:
        if self._geo_done(entry):
            self.stats["skip"] += 1
            return "skip"
        self.stats["lookups"] += 1
        self._wait_rate()
        if self._stop.is_set():
            return "skip"
        geo = self.lookup(entry.host)
        if not geo:
            self.stats["fail"] += 1
            self._mark_fail(entry.host)
            return "fail"
        if self._apply(entry, geo):
            self.stats["ok"] += 1
            self._mark_ok(entry.host)
            return "ok"
        self.stats["fail"] += 1
        self._mark_fail(entry.host)
        return "fail"

    def _worker(self) -> None:
        logger.info("geo worker 启动")
        while not self._stop.is_set():
            host = None
            try:
                if not self.enabled:
                    self._stop.wait(1.0)
                    continue
                entry = self._claim_next()
                if entry is None:
                    # 没活干稍歇；有活则在 finally 后立刻再领，不空转等待
                    self._stop.wait(self.interval)
                    continue
                host = entry.host
                self.process_one(entry)
            except Exception as exc:
                logger.debug("geo worker 异常: %s", exc)
                self._stop.wait(0.2)
            finally:
                if host:
                    self._release(host)
        logger.info("geo worker 退出")

    def start(self) -> None:
        if not self.enabled:
            logger.info("geo 补全已禁用")
            return
        if self._threads:
            return
        self._stop.clear()
        n = self.workers
        for i in range(n):
            t = threading.Thread(target=self._worker, name=f"geo-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info(
            "geo 补全已启动 workers=%s rate=%s/s min_rem=%ss",
            n,
            self.rate_per_sec,
            self.min_remaining,
        )

    def stop(self) -> None:
        self._stop.set()
        self._threads.clear()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            pending = len(self._fail_until)
            claimed = len(self._claimed)
        d = dict(self.stats)
        d["enabled"] = self.enabled
        d["cooldown_hosts"] = pending
        d["claimed"] = claimed
        d["workers"] = self.workers
        return d
