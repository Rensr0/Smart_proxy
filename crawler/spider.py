# -*- coding: utf-8 -*-
"""
目标站业务：探活配置 + 校验逻辑。
当前内置百度首页探活（通用连通性），可按需扩展其他站点。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

import requests

from crawler.proxy import ProxyEntry, ProxyPool

try:
    import config as app_config
except ImportError:  # pragma: no cover
    app_config = None  # type: ignore

logger = logging.getLogger("spider")


@dataclass
class VerifyProfile:
    """目标站点探活描述。"""

    name: str
    url: str
    method: str = "GET"
    expect_fragment: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[dict] = None


VERIFY_PROFILES: Dict[str, VerifyProfile] = {
    "baidu": VerifyProfile(
        name="baidu",
        url=getattr(app_config, "VERIFY_BAIDU_URL", "https://www.baidu.com/"),
        method=getattr(app_config, "VERIFY_BAIDU_METHOD", "GET"),
        expect_fragment=getattr(app_config, "VERIFY_BAIDU_EXPECT", "百度"),
        headers={
            "User-Agent": getattr(app_config, "USER_AGENT", "Mozilla/5.0"),
        },
    ),
}


# 最近一次校验结果（供 API/前端读取）
_last_verify_lock = threading.Lock()
_last_verify: Dict[str, object] = {}
# 累计校验（进程内）
_verify_cum_ok = 0
_verify_cum_fail = 0


def _load_verify_persist() -> None:
    global _verify_cum_ok, _verify_cum_fail
    try:
        import config as app_config
        from utils.metrics import get_metrics
        m = get_metrics(
            host=app_config.REDIS_HOST,
            port=app_config.REDIS_PORT,
            db=app_config.REDIS_DB,
            password=app_config.REDIS_PASSWORD,
        )
        st = m.load_state("verify")
        _verify_cum_ok = int(st.get("cum_ok") or 0)
        _verify_cum_fail = int(st.get("cum_fail") or 0)
        if st:
            _set_last_verify(st.get("last") or {})
        logger.info("校验累计已恢复: ok=%s fail=%s", _verify_cum_ok, _verify_cum_fail)
    except Exception as exc:
        logger.debug("校验持久化恢复失败: %s", exc)


def _save_verify_persist(last: dict) -> None:
    try:
        import config as app_config
        from utils.metrics import get_metrics
        m = get_metrics(
            host=app_config.REDIS_HOST,
            port=app_config.REDIS_PORT,
            db=app_config.REDIS_DB,
            password=app_config.REDIS_PASSWORD,
        )
        m.save_state("verify", {
            "cum_ok": _verify_cum_ok,
            "cum_fail": _verify_cum_fail,
            "last": last,
        })
    except Exception:
        pass



def get_last_verify() -> Dict[str, object]:
    with _last_verify_lock:
        return dict(_last_verify)


def _set_last_verify(payload: Dict[str, object]) -> None:
    with _last_verify_lock:
        _last_verify.clear()
        _last_verify.update(payload)


def verify_one(
    pool: ProxyPool,
    entry_or_address: Union[str, ProxyEntry],
    profile: VerifyProfile,
    timeout: Optional[float] = None,
) -> "tuple[bool, float]":
    """
    用给定代理访问目标站。
    返回 (是否可达, 延迟ms)；失败时延迟为超时/耗时。
    """
    timeout = timeout or pool.config.verify_timeout
    headers = dict(profile.headers)
    headers.setdefault("User-Agent", pool.config.user_agent)

    kwargs = {
        "proxies": pool.build_proxies(entry_or_address),
        "timeout": timeout,
        "headers": headers,
        "verify": True,
    }

    method = profile.method.upper()
    started = time.perf_counter()
    try:
        if method == "GET":
            resp = requests.get(profile.url, **kwargs)
        elif method == "POST":
            body = profile.body
            if isinstance(body, dict):
                # 空 body 也只发一次，避免探活重复放大延迟
                resp = requests.post(profile.url, json=body if body else {}, **kwargs)
            elif isinstance(body, str):
                resp = requests.post(profile.url, data=body.encode("utf-8"), **kwargs)
            else:
                resp = requests.post(profile.url, **kwargs)
        else:
            resp = requests.request(method, profile.url, **kwargs)
    except requests.RequestException as exc:
        latency = (time.perf_counter() - started) * 1000.0
        logger.debug("verify %s via %s 失败: %s (%.0fms)", profile.name, entry_or_address, exc, latency)
        return False, latency

    latency = (time.perf_counter() - started) * 1000.0
    text = resp.text or ""
    ok = (not profile.expect_fragment) or (profile.expect_fragment in text)

    # 回写到条目上，便于排序
    # 失败不写 latency，保留未测状态，允许下次再测（连败达阈值再踢）
    if isinstance(entry_or_address, ProxyEntry):
        entry_or_address.last_ok = ok
        entry_or_address.last_verify_at = time.time()
        if ok:
            entry_or_address.latency_ms = latency

    logger.debug("verify %s via %s -> %s (%.0fms)", profile.name, entry_or_address, ok, latency)
    return ok, latency


def _verify_many(
    pool: ProxyPool,
    entries: List[ProxyEntry],
    profile: VerifyProfile,
) -> "tuple[list[ProxyEntry], list[ProxyEntry]]":
    """
    多线程校验指定条目，返回 (good, bad)。
    并发数 = 本批待验数量（上限 200）。
    """
    if not entries:
        return [], []
    good: List[ProxyEntry] = []
    bad: List[ProxyEntry] = []
    workers = max(1, min(len(entries), 200))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(verify_one, pool, e, profile): e for e in entries}
        for fut in as_completed(futs):
            entry = futs[fut]
            try:
                ok, _lat = fut.result()
                if ok:
                    entry.mark_success()
                    good.append(entry)
                else:
                    bad.append(entry)
                pool.touch_entry(entry)
            except Exception:
                bad.append(entry)
    return good, bad


def _drop_entries(pool: ProxyPool, bad: List[ProxyEntry]) -> None:
    if not bad:
        return
    pool.drop_entries(bad)


def verify_pool(
    pool: ProxyPool,
    profile: Optional[VerifyProfile] = None,
    limit: Optional[int] = None,
    drop_failures: bool = True,
) -> List[str]:
    """全量（或限量）并发校验池内代理，返回可用地址列表（按质量分排序）。"""
    profile = profile or VERIFY_PROFILES.get("baidu")
    if profile is None:
        raise ValueError("缺少 verify profile")

    targets = pool.ranked_entries()
    if limit is not None:
        targets = targets[:limit]

    good, bad = _verify_many(pool, targets, profile)
    if drop_failures:
        _drop_entries(pool, bad)

    # 按分数重排返回
    good.sort(key=lambda e: e.sort_key(), reverse=True)
    good_addrs = [e.auth_address for e in good]

    payload = {
        "profile": profile.name,
        "ok": len(good),
        "total": len(targets),
        "dropped": len(bad) if drop_failures else 0,
        "proxies": good_addrs,
    }
    _set_last_verify(payload)
    logger.info(
        "目标 [%s] 校验完成：可用 %s / 共 %s%s",
        profile.name, len(good), len(targets),
        f"，已踢出 {len(bad)}" if drop_failures and bad else "",
    )
    return good_addrs


def get_healthy_proxy(
    pool: ProxyPool,
    profile: Optional[VerifyProfile] = None,
    max_try: int = 8,
) -> str:
    """取一个能打通目标站的代理；优先排名靠前的。"""
    profile = profile or VERIFY_PROFILES.get("baidu")
    tried = set()
    for _ in range(max_try):
        entry = pool.get_entry(top_k=8)
        if not entry or entry.address in tried:
            continue
        tried.add(entry.address)
        ok, _lat = verify_one(pool, entry, profile)
        if ok:
            entry.mark_success()
            pool.touch_entry(entry)
            return entry.auth_address
    return ""


def verify_loop(
    pool: ProxyPool,
    profile: Optional[VerifyProfile] = None,
    interval: float = 2.0,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """
    校验循环：
    - 只验证「未验证过」的 IP，已测的不再重复测
    - 并发数 = 本轮未验证数量（上限 200）
    - 未验证中优先剩余时间长的
    - 连败达阈值才踢出；通过的按「延迟低 + 剩余久」排序
    """
    profile = profile or VERIFY_PROFILES.get("baidu")
    if profile is None:
        raise ValueError("缺少 verify profile")

    stop_event = stop_event or threading.Event()
    logger.info(
        "全量校验循环启动 profile=%s workers=%s interval=%ss",
        profile.name, pool.config.verify_workers, interval,
    )

    while not stop_event.is_set():
        try:
            entries = pool.ranked_entries()
            if entries:
                # 只验「未验证过」的：已测过的不再重复测，保留其延迟/分数直到过期
                unverified = [e for e in entries if e.latency_ms is None]
                # 未验证里也优先剩余时间长的
                unverified.sort(key=lambda e: e.remaining(), reverse=True)

                if unverified:
                    good, bad = _verify_many(pool, unverified, profile)
                    # 校验失败：连败达阈值才踢（与隧道一致）
                    thr = int(getattr(app_config, "FAIL_KICK_THRESHOLD", 3) if app_config else 3)
                    really_bad = []
                    for e in bad:
                        e.last_ok = False
                        if e.mark_fail() >= thr:
                            really_bad.append(e)
                        else:
                            pool.touch_entry(e)
                    if really_bad:
                        _drop_entries(pool, really_bad)
                    bad = really_bad
                else:
                    good, bad = [], []

                good.sort(key=lambda e: e.sort_key(), reverse=True)
                verified_n = sum(1 for e in pool.ranked_entries() if e.latency_ms is not None)
                global _verify_cum_ok, _verify_cum_fail
                _verify_cum_ok += len(good)
                _verify_cum_fail += len(bad)
                _payload = {
                    "profile": profile.name,
                    "ok": len(good),
                    "total": len(unverified),
                    "dropped": len(bad),
                    "loop": True,
                    "full": False,
                    "unverified_only": True,
                    "verified": verified_n,
                    "cum_ok": _verify_cum_ok,
                    "cum_fail": _verify_cum_fail,
                    "top": [e.auth_address for e in pool.ranked_entries()[:5]],
                }
                _set_last_verify(_payload)
                _save_verify_persist(_payload)
                logger.debug(
                    "校验循环: 本轮未验证 %s，通过 %s，踢出 %s，已测 %s，池内 %s",
                    len(unverified), len(good), len(bad), verified_n, pool.size(),
                )
        except Exception:
            logger.exception("校验循环异常")

        stop_event.wait(interval)

    logger.info("校验循环已停止")

