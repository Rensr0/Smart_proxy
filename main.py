# -*- coding: utf-8 -*-
"""
入口：启动代理池 + Flask API / 前端。
业务逻辑在 crawler/ 与 utils/，这里只做装配与 HTTP 接口。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import config as app_config
from crawler.proxy import PoolConfig, ProxyPool
from crawler.spider import (
    VERIFY_PROFILES,
    get_last_verify,
    get_healthy_proxy,
    verify_loop,
    _load_verify_persist,
)

try:
    from tunnel.server import TunnelServer
except ImportError:  # pragma: no cover
    TunnelServer = None  # type: ignore

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

# 全局代理池（模块级单例）
_pool: ProxyPool | None = None
_verify_stop = threading.Event()
_verify_thread: threading.Thread | None = None
_tunnel = None
_geo = None


def get_pool() -> ProxyPool:
    global _pool
    if _pool is None:
        # 参数全部来自根目录 config.py
        _pool = ProxyPool(PoolConfig())
        _pool.start()
    return _pool


def get_tunnel():
    """获取/启动隧道代理。"""
    global _tunnel
    if _tunnel is None and TunnelServer is not None and getattr(app_config, "TUNNEL_ENABLED", True):
        _tunnel = TunnelServer(get_pool())
        try:
            from utils.metrics import get_metrics
            m = get_metrics(
                host=app_config.REDIS_HOST,
                port=app_config.REDIS_PORT,
                db=app_config.REDIS_DB,
                password=app_config.REDIS_PASSWORD,
            )
            st = m.load_state("tunnel")
            if st:
                s = _tunnel.stats
                s.connections = int(st.get("connections") or 0)
                s.success = int(st.get("success") or 0)
                s.failovers = int(st.get("failovers") or 0)
                s.failures = int(st.get("failures") or 0)
                s.kicked = int(st.get("kicked") or 0)
                s.last_proxy = st.get("last_proxy") or ""
                s.last_error = st.get("last_error") or ""
                s.first_ok = int(st.get("first_ok") or 0)
                s.first_fail = int(st.get("first_fail") or 0)
                # 恢复启动时间，uptime 跨重启连续
                try:
                    started = float(st.get("started_at") or 0)
                    if started > 0:
                        s.started_at = started
                except Exception:
                    pass
                logger.info("隧道统计已恢复 connections=%s success=%s", s.connections, s.success)
        except Exception as exc:
            logger.debug("隧道统计恢复失败: %s", exc)
        _tunnel.run_in_thread()
    return _tunnel


def _start_metrics_loop(interval: float = 1.0) -> None:
    """定时采样写入 Redis（池子/延迟/分数/带宽）。"""
    def _runner():
        try:
            from utils.metrics import get_metrics
            m = get_metrics(
                host=app_config.REDIS_HOST,
                port=app_config.REDIS_PORT,
                db=app_config.REDIS_DB,
                password=app_config.REDIS_PASSWORD,
            )
        except Exception as exc:
            logger.warning("指标存储不可用: %s", exc)
            return
        while True:
            try:
                pool = get_pool()
                st = pool.status()
                proxies = st.get("proxies") or []
                ok_n = sum(1 for x in proxies if x.get("last_ok") is True)
                lats = [x.get("ewma_latency_ms") or x.get("latency_ms") for x in proxies]
                lats = [x for x in lats if x is not None]
                scores = [x.get("score") or 0 for x in proxies]
                m.record_sample({
                    "pool_size": st.get("size") or 0,
                    "ok_count": ok_n,
                    "avg_latency": (sum(lats) / len(lats)) if lats else 0,
                    "avg_score": (sum(scores) / len(scores)) if scores else 0,
                })
                # 隧道统计持久化
                try:
                    from crawler.spider import get_last_verify
                    tnl = get_tunnel()
                    if tnl is not None:
                        m.save_state("tunnel", tnl.stats.to_dict())
                    m.save_state("verify_last", get_last_verify())
                except Exception:
                    pass
            except Exception:
                logger.exception("指标采样异常")
            threading.Event().wait(interval)

    threading.Thread(target=_runner, name="MetricsLoop", daemon=True).start()


def _start_verify_loop(profile_name: str | None = None) -> None:
    global _verify_thread
    if _verify_thread and _verify_thread.is_alive():
        return
    name = profile_name or app_config.DEFAULT_VERIFY_PROFILE
    profile = VERIFY_PROFILES.get(name) or VERIFY_PROFILES["baidu"]
    _verify_stop.clear()
    _verify_thread = threading.Thread(
        target=verify_loop,
        kwargs={
            "pool": get_pool(),
            "profile": profile,
            "interval": float(getattr(app_config, "VERIFY_INTERVAL", 1.0)),
            "stop_event": _verify_stop,
        },
        name="VerifyLoop",
        daemon=True,
    )
    _verify_thread.start()
    logger.info("后台校验循环已启动")


def _profile_or_default(name: str | None):
    if not name:
        return VERIFY_PROFILES["baidu"]
    if name not in VERIFY_PROFILES:
        return None
    return VERIFY_PROFILES[name]


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "sxs", "name": "Smart Exit Selector"})


@app.get("/api/tunnel")
def api_tunnel():
    """隧道代理状态。"""
    t = get_tunnel()
    if t is None:
        return jsonify({"ok": False, "enabled": False, "error": "隧道未启用"})
    return jsonify({"ok": True, **t.status()})


@app.get("/api/metrics")
def api_metrics():
    """时序指标 + 带宽（供 sparkline）。"""
    try:
        from utils.metrics import get_metrics
        m = get_metrics(
            host=app_config.REDIS_HOST,
            port=app_config.REDIS_PORT,
            db=app_config.REDIS_DB,
            password=app_config.REDIS_PASSWORD,
        )
        return jsonify({"ok": True, **m.snapshot(limit=int(request.args.get("limit", 60)))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/status")
def api_status():
    pool = get_pool()
    data = pool.status()
    data["last_verify"] = get_last_verify()
    data["profiles"] = list(VERIFY_PROFILES.keys())
    if _geo is not None:
        try:
            data["geo"] = _geo.snapshot()
        except Exception:
            pass
    return jsonify(data)


@app.get("/api/proxies")
def api_proxies():
    pool = get_pool()
    limit = request.args.get("limit", default=50, type=int)
    items = pool.status()["proxies"]
    return jsonify({"total": len(items), "items": items[: max(0, limit)]})


@app.get("/api/proxy")
def api_get_proxy():
    """随机取一个代理。?healthy=1 时先探活目标站。"""
    pool = get_pool()
    profile = _profile_or_default(request.args.get("profile"))
    if profile is None:
        return jsonify({"ok": False, "error": "unknown profile"}), 400

    if request.args.get("healthy") in ("1", "true", "yes"):
        addr = get_healthy_proxy(pool, profile)
    else:
        addr = pool.get_proxy()

    if not addr:
        return jsonify({"ok": False, "error": "代理池为空"}), 503
    return jsonify({"ok": True, "proxy": addr})


@app.post("/api/refresh")
def api_refresh():
    """手动补 IP。body: {"count": 10}"""
    body = request.get_json(silent=True) or {}
    count = int(body.get("count") or 10)
    count = max(1, min(count, 50))
    pool = get_pool()
    added = pool.add_alive_proxy(count)
    return jsonify({"ok": True, "requested": count, "added": added, "size": pool.size()})


@app.post("/api/config")
def api_config():
    """
    运行时调整部分参数。
    body: {"proxy_count": 30, "lease_seconds": 30, "api_url": "..."}
    """
    body = request.get_json(silent=True) or {}
    pool = get_pool()
    cfg = pool.config
    changed = {}

    if "proxy_count" in body:
        cfg.proxy_count = max(1, min(int(body["proxy_count"]), 500))
        changed["proxy_count"] = cfg.proxy_count
    if "lease_seconds" in body:
        cfg.lease_seconds = max(5.0, float(body["lease_seconds"]))
        changed["lease_seconds"] = cfg.lease_seconds
    if "api_url" in body and body["api_url"]:
        cfg.api_url = str(body["api_url"]).strip()
        changed["api_url"] = cfg.api_url
        # 链接变更后按新 num 重算单次上限
        from utils.helper import peek_url_num
        pool._api_max_num = peek_url_num(cfg.api_url, cfg.api_num_param)
        changed["api_max_num"] = pool._api_max_num
    if "api_max_num" in body and body["api_max_num"] is not None:
        pool._api_max_num = max(1, int(body["api_max_num"]))
        changed["api_max_num"] = pool._api_max_num

    return jsonify({"ok": True, "changed": changed})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def main():
    global _geo
    host = app_config.SERVER_HOST
    port = app_config.SERVER_PORT
    # 预热代理池 + 隧道统计回载（须在 metrics 采样前，避免用空值覆盖 Redis）
    get_pool()
    _load_verify_persist()
    get_tunnel()
    _start_verify_loop()
    _start_metrics_loop()
    try:
        from utils.geo import GeoEnricher

        _geo = GeoEnricher(get_pool(), config=app_config)
        _geo.start()
    except Exception as exc:
        logger.warning("geo 补全未启动: %s", exc)
    logger.info("Flask 启动 http://%s:%s", host, port)
    if getattr(app_config, "TUNNEL_ENABLED", True):
        logger.info(
            "隧道代理: http://%s:%s  （业务侧配成 HTTP 代理即可）",
            getattr(app_config, "TUNNEL_HOST", "127.0.0.1"),
            getattr(app_config, "TUNNEL_PORT", 18888),
        )
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
