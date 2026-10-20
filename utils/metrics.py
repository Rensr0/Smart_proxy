# -*- coding: utf-8 -*-
"""
运行指标时序 + 带宽统计（Redis DB 15 持久化）

键：
  metrics:series:{name}   ZSET  score=ts  member=ts:value  （保留最近 N 点）
  metrics:bw:peak         峰值带宽 bps
  metrics:bw:last         最近采样带宽 bps
  metrics:total_bytes     累计转发字节
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore

MAX_POINTS = 240  # 约 20 分钟（5s 一点）


class MetricsStore:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 15,
        password: str = "",
        max_points: int = MAX_POINTS,
    ):
        if redis is None:
            raise RuntimeError("未安装 redis")
        self.max_points = max_points
        self._lock = threading.Lock()
        self._bytes_window = 0
        self._last_bytes_ts = time.time()
        self.client = redis.Redis(
            host=host, port=port, db=db, password=password or None,
            socket_timeout=2.0, socket_connect_timeout=2.0, decode_responses=True,
        )
        self.client.ping()

    def add_bytes(self, n: int) -> None:
        """转发字节累计（可在多线程调用）。"""
        if n <= 0:
            return
        with self._lock:
            self._bytes_window += int(n)

    def record_series(self, name: str, value: float, ts: Optional[float] = None) -> None:
        ts = time.time() if ts is None else float(ts)
        key = f"metrics:series:{name}"
        member = f"{ts:.3f}:{float(value):.4f}"
        try:
            pipe = self.client.pipeline(transaction=False)
            pipe.zadd(key, {member: ts})
            # 修剪旧点
            pipe.zremrangebyrank(key, 0, -self.max_points - 1)
            pipe.expire(key, 86400 * 7)
            pipe.execute()
        except Exception:
            pass

    def record_sample(self, sample: Dict[str, float]) -> None:
        """写入一组指标；并计算/更新带宽。"""
        now = time.time()
        with self._lock:
            window_bytes = self._bytes_window
            self._bytes_window = 0
            last_ts = self._last_bytes_ts
            self._last_bytes_ts = now

        dt = max(0.001, now - last_ts)
        bw = window_bytes / dt  # bytes/s

        for k, v in sample.items():
            if v is None:
                continue
            self.record_series(k, float(v), now)

        self.record_series("bandwidth", bw, now)
        try:
            pipe = self.client.pipeline(transaction=False)
            pipe.set("metrics:bw:last", f"{bw:.2f}")
            # 峰值
            try:
                peak = float(self.client.get("metrics:bw:peak") or 0)
            except Exception:
                peak = 0.0
            if bw > peak:
                pipe.set("metrics:bw:peak", f"{bw:.2f}")
            pipe.incrbyfloat("metrics:total_bytes", window_bytes)
            pipe.execute()
        except Exception:
            pass

    def series(self, names: List[str], limit: int = 60) -> Dict[str, List[List[float]]]:
        """返回 {name: [[ts, value], ...]}。"""
        out: Dict[str, List[List[float]]] = {}
        for name in names:
            key = f"metrics:series:{name}"
            pts: List[List[float]] = []
            try:
                rows = self.client.zrange(key, -limit, -1)
                for row in rows:
                    try:
                        ts_s, _, val_s = row.partition(":")
                        pts.append([float(ts_s), float(val_s)])
                    except Exception:
                        continue
            except Exception:
                pass
            out[name] = pts
        return out

    def snapshot(self, names: Optional[List[str]] = None, limit: int = 60) -> Dict[str, Any]:
        names = names or [
            "pool_size", "ok_count", "avg_latency", "avg_score", "bandwidth",
        ]
        try:
            peak = float(self.client.get("metrics:bw:peak") or 0)
            last_bw = float(self.client.get("metrics:bw:last") or 0)
            total = float(self.client.get("metrics:total_bytes") or 0)
        except Exception:
            peak = last_bw = total = 0.0
        return {
            "series": self.series(names, limit=limit),
            "bandwidth": {
                "last_bps": round(last_bw, 2),
                "peak_bps": round(peak, 2),
                "total_bytes": int(total),
            },
        }


    def save_state(self, name: str, data: dict) -> None:
        try:
            self.client.set(f"metrics:state:{name}", json.dumps(data, ensure_ascii=False, default=str))
        except Exception:
            pass

    def load_state(self, name: str) -> dict:
        try:
            raw = self.client.get(f"metrics:state:{name}")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}


_metrics = None
_metrics_lock = threading.Lock()


def get_metrics(host="127.0.0.1", port=6379, db=15, password=""):
    """进程内单例 MetricsStore。"""
    global _metrics
    with _metrics_lock:
        if _metrics is None:
            _metrics = MetricsStore(host=host, port=port, db=db, password=password)
        return _metrics
