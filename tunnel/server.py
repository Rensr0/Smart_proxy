# -*- coding: utf-8 -*-
"""
本地隧道代理（HTTP / HTTPS CONNECT）

业务侧配置：http://127.0.0.1:18888
- 每条连接自动取池内最优 IP（请求级换 IP，无粘性）
- 连上游超时/失败自动换下一个（最多 TUNNEL_MAX_FAILOVER）
- 仅绑定本机，避免变成开放代理
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from typing import Optional, Tuple

from crawler.proxy import ProxyEntry, ProxyPool

try:
    import config as app_config
except ImportError:  # pragma: no cover
    app_config = None  # type: ignore

logger = logging.getLogger("tunnel")


def _cfg(name: str, default):
    return getattr(app_config, name, default) if app_config is not None else default


class _AttemptError(Exception):
    """建连失败，附带本次实际尝试过的 entry 列表，避免误记未尝试 IP。"""

    def __init__(self, cause: Exception, attempted: list):
        super().__init__(str(cause))
        self.cause = cause
        self.attempted = list(attempted or [])


class TunnelStats:
    def __init__(self) -> None:
        self.connections = 0
        self.active = 0  # 当前在途连接
        self.success = 0
        self.failovers = 0
        self.failures = 0
        self.kicked = 0
        self.first_ok = 0
        self.first_fail = 0
        self.last_proxy = ""
        self.last_error = ""
        self.started_at = time.time()
        # 建连对冲
        self.hedge_attempts = 0      # 真正拉起对冲的次数
        self.hedge_saved = 0         # 对冲方获胜
        self.hedge_wasted = 0        # 主连接已胜，对冲白付
        self.hedge_skipped = 0       # 因预热/候选不足/预算跳过

    def to_dict(self) -> dict:
        return {
            "connections": self.connections,
            "active": self.active,
            "success": self.success,
            "failovers": self.failovers,
            "failures": self.failures,
            "kicked": self.kicked,
            "first_ok": self.first_ok,
            "first_fail": self.first_fail,
            "last_proxy": self.last_proxy,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "uptime": round(time.time() - self.started_at, 1),
            "hedge": {
                "attempts": self.hedge_attempts,
                "saved": self.hedge_saved,
                "wasted": self.hedge_wasted,
                "skipped": self.hedge_skipped,
            },
        }


class TunnelServer:
    def __init__(self, pool: ProxyPool):
        self.pool = pool
        self.host = _cfg("TUNNEL_HOST", "127.0.0.1")
        self.port = int(_cfg("TUNNEL_PORT", 18888))
        self.ports = (self.port,)
        self.connect_timeout = float(_cfg("TUNNEL_CONNECT_TIMEOUT", 1.5))
        self.max_failover = int(_cfg("TUNNEL_MAX_FAILOVER", 4))
        self.idle_timeout = float(_cfg("TUNNEL_IDLE_TIMEOUT", 120.0))
        self.stall_timeout = float(_cfg("TUNNEL_STALL_TIMEOUT", 4.0))
        self.stats = TunnelStats()
        # 自适应建连超时
        self.adaptive_timeout = bool(_cfg("ADAPTIVE_TIMEOUT_ENABLED", True))
        self.connect_timeout_min = float(_cfg("TUNNEL_CONNECT_TIMEOUT_MIN", 0.35))
        self.connect_timeout_max = float(_cfg("TUNNEL_CONNECT_TIMEOUT_MAX", 1.5))
        self.adaptive_alpha = float(_cfg("ADAPTIVE_ALPHA", 0.6))
        self.adaptive_beta = float(_cfg("ADAPTIVE_BETA_S", 0.25))
        self.adaptive_rem_ratio = float(_cfg("ADAPTIVE_REM_TIMEOUT_RATIO", 0.4))
        # 建连对冲
        self.hedge_enabled = bool(_cfg("HEDGE_ENABLED", True))
        self.hedge_delay = max(0.0, float(_cfg("HEDGE_DELAY_MS", 120)) / 1000.0)
        self.hedge_width = max(2, min(3, int(_cfg("HEDGE_WIDTH", 2))))
        self.hedge_max_inflight = max(0, int(_cfg("HEDGE_MAX_INFLIGHT", 32)))
        self._hedge_inflight = 0
        self._hedge_lock = threading.Lock()
        # 热 IP 预连接：TCP 提前建好，请求时省一次握手
        self._warm_sockets = {}
        self._warm_lock = threading.Lock()
        # 并发连接上限，防止 FD 被打满
        self._max_conns = int(_cfg("TUNNEL_MAX_CONNS", 300))
        self._conn_sem = threading.BoundedSemaphore(self._max_conns)
        self._server: Optional[asyncio.base_events.Server] = None
        self._servers: list = []
        self._task: Optional[asyncio.Task] = None

    # ---------- 上游连接 ----------

    def _ranked_usable(self) -> list:
        """
        候选链（秒用优先）：
        1. 热池：已验证且延迟快 —— 首试
        2. 其它已验证
        3. 未验证 —— 备用
        """
        ranked = self.pool.ranked_entries()
        usable = [e for e in ranked if e.remaining() >= 3.0]
        hot_max = float(_cfg("HOT_LATENCY_MAX_MS", 800.0))
        hot_min = int(_cfg("HOT_POOL_MIN", 5))
        hot = [e for e in usable if e.is_hot(hot_max)]
        if len(hot) >= hot_min:
            hot_ids = {e.address for e in hot}
            rest = [e for e in usable if e.address not in hot_ids]
            return hot + rest
        # 热池不够：仍用原顺序，但已验证优先
        verified = [e for e in usable if e.last_ok is True]
        unverified = [e for e in usable if e.last_ok is not True]
        return verified + unverified

    @staticmethod
    def _auth_header(entry: ProxyEntry) -> str:
        if entry.username and entry.password:
            raw = f"{entry.username}:{entry.password}".encode("utf-8")
            return "Basic " + base64.b64encode(raw).decode("ascii")
        return ""

    def _connect_timeout_for(self, entry: ProxyEntry) -> float:
        """
        自适应建连超时：
        - 历史延迟 EWMA 推算
        - 再用剩余 TTL 封顶，避免在将死 IP 上干等
        兜底 TUNNEL_CONNECT_TIMEOUT。
        """
        base = self.connect_timeout
        if not self.adaptive_timeout:
            return base
        try:
            rem = float(entry.remaining())
        except Exception:
            rem = 0.0
        if rem <= 0:
            return min(base, 0.15)
        lat = entry.ewma_latency_ms
        if lat is None:
            lat = entry.latency_ms
        if lat is None:
            lat = 1000.0
        try:
            t1 = self.adaptive_alpha * (float(lat) / 1000.0) + self.adaptive_beta
        except Exception:
            t1 = base
        t1 = min(max(t1, self.connect_timeout_min), self.connect_timeout_max)
        t2 = max(0.15, rem * self.adaptive_rem_ratio)
        # 自适应只用 t1/t2；TUNNEL_CONNECT_TIMEOUT 仅作关闭自适应时的回退
        return float(min(t1, t2))

    def _try_acquire_hedge_slot(self) -> bool:
        if self.hedge_max_inflight <= 0:
            return False
        with self._hedge_lock:
            if self._hedge_inflight >= self.hedge_max_inflight:
                return False
            self._hedge_inflight += 1
            return True

    def _release_hedge_slot(self) -> None:
        with self._hedge_lock:
            self._hedge_inflight = max(0, self._hedge_inflight - 1)

    async def _close_quiet(self, writer: Optional[asyncio.StreamWriter]) -> None:
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _open_upstream(
        self,
        entry: ProxyEntry,
        target_host: str,
        target_port: int,
        connect_req: bool = True,
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """经上游 HTTP 代理建立到目标的通道。connect_req=True 时发 CONNECT。"""
        timeout = self._connect_timeout_for(entry)
        # 优先使用预建 TCP（秒用）
        warm = None
        with self._warm_lock:
            warm = self._warm_sockets.pop(entry.address, None)
        if warm is not None:
            reader, writer = warm[1], warm[2]
            # 预建连接后续读写也按自适应超时兜底
            timeout = min(timeout, self.connect_timeout)
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(entry.host, entry.port),
                timeout=timeout,
            )
        try:
            if connect_req:
                headers = [
                    f"CONNECT {target_host}:{target_port} HTTP/1.1",
                    f"Host: {target_host}:{target_port}",
                ]
                auth = self._auth_header(entry)
                if auth:
                    headers.append(f"Proxy-Authorization: {auth}")
                headers.append("Proxy-Connection: keep-alive")
                writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
                await writer.drain()

                # 读响应头
                status_line = await asyncio.wait_for(
                    reader.readline(), timeout=timeout
                )
                if not status_line:
                    raise ConnectionError("上游无响应")
                # 吃掉剩余 header
                while True:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=timeout
                    )
                    if line in (b"\r\n", b"\n", b""):
                        break
                # 状态行必须是 2xx
                try:
                    code = int(status_line.split()[1])
                except (IndexError, ValueError):
                    code = 0
                if not (200 <= code < 300):
                    raise ConnectionError(f"上游 CONNECT 失败: {status_line[:80]!r}")
            return reader, writer
        except BaseException:
            # 含 CancelledError：对冲败者被取消时也要关掉 socket，防 FD 泄漏
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raise

    @staticmethod
    def _is_hard_fail(exc: BaseException) -> bool:
        """明确不可用（拒连/复位/网不可达/CONNECT 非成功）→ 可立刻踢。"""
        import errno
        if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
            return True
        if isinstance(exc, OSError) and exc.errno in (
            errno.ECONNREFUSED, errno.ECONNRESET,
            errno.EHOSTUNREACH, errno.ENETUNREACH,
        ):
            return True
        if isinstance(exc, asyncio.TimeoutError):
            return False
        msg = str(exc)
        if "CONNECT" in msg and "200" not in msg:
            return True
        return False

    def _kick(self, entry: ProxyEntry, reason: str) -> None:
        """踢出：幂等；池水位过低时只降权不删，先保供给。"""
        if entry.kicked:
            return
        # 池子空一半以上就不再踢，避免高峰掏空
        try:
            target = max(1, int(self.pool.config.proxy_count))
            ratio = float(_cfg("KICK_PAUSE_RATIO", 0.5))
            if self.pool.size() < target * ratio:
                entry.kicked = False  # 仅占位，不标记
                logger.debug("池水位低，暂不踢出 %s", entry.address)
                return
        except Exception:
            pass
        entry.kicked = True
        try:
            self.pool.drop_entries([entry])
            self.stats.kicked += 1
            logger.info("踢出代理 %s (%s)", entry.auth_address, reason)
        except Exception as exc:
            logger.debug("踢出失败: %s", exc)

    def _pick_candidates(self, exclude=None) -> list:
        """
        候选顺序（请求级换 IP，无粘性）：
        1. 已验证高分 —— 主力
        2. 未验证 —— 备用
        连续请求避开 last_proxy；失败则沿序列继续换。
        """
        pool = self._ranked_usable()
        if not pool:
            return []
        ordered = list(pool)

        # 请求级换 IP：避免连续打同一出口
        last = self.stats.last_proxy
        if last:
            head = [e for e in ordered if e.auth_address != last]
            tail = [e for e in ordered if e.auth_address == last]
            ordered = head + tail

        # fail_count>0 不进首试：放到候选末尾
        clean = [e for e in ordered if not (e.fail_count or 0)]
        risky = [e for e in ordered if (e.fail_count or 0)]
        ordered = clean + risky

        if exclude:
            ordered = [e for e in ordered if e.address not in exclude]
        return ordered

    async def _open_upstream_hedged(
        self,
        candidates: list,
        target_host: str,
        target_port: int,
        connect_req: bool = True,
    ) -> Tuple[ProxyEntry, asyncio.StreamReader, asyncio.StreamWriter, list]:
        """
        建连对冲：主连接挂起 hedge_delay 后并行拉起备用出口，先成功者胜。
        成功返回 (entry, reader, writer, attempted)；
        失败抛 _AttemptError(attempted=实际尝试列表)。
        只对冲建连，relay 阶段仍是单路径。
        """
        primary = candidates[0]
        secondary = candidates[1] if len(candidates) > 1 else None

        async def _close_tasks(pairs, keep_task):
            for _ent, t in pairs:
                if t is keep_task:
                    continue
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except BaseException:
                        # CancelledError 不能冒泡，否则会当成对冲整体失败
                        pass
                else:
                    try:
                        _r, lw = t.result()
                        await self._close_quiet(lw)
                    except Exception:
                        pass

        # 单路径
        if (not self.hedge_enabled or secondary is None
                or self.hedge_delay <= 0 or self.hedge_max_inflight <= 0):
            try:
                r, w = await self._open_upstream(primary, target_host, target_port, connect_req)
                return primary, r, w, [primary]
            except Exception as exc:
                raise _AttemptError(exc, [primary]) from exc

        task_primary = asyncio.create_task(
            self._open_upstream(primary, target_host, target_port, connect_req)
        )
        try:
            done, _ = await asyncio.wait({task_primary}, timeout=self.hedge_delay)
        except Exception as exc:
            if not task_primary.done():
                task_primary.cancel()
                try:
                    await task_primary
                except Exception:
                    pass
            raise _AttemptError(exc, [primary]) from exc

        # 主连接已在 τ 内结束 → 单路径语义（只「实际尝试」primary）
        if task_primary in done:
            try:
                r, w = await task_primary
                return primary, r, w, [primary]
            except Exception as exc:
                raise _AttemptError(exc, [primary]) from exc

        # 仍挂起 → 启动对冲
        if not self._try_acquire_hedge_slot():
            self.stats.hedge_skipped += 1
            try:
                r, w = await task_primary
                return primary, r, w, [primary]
            except Exception as exc:
                raise _AttemptError(exc, [primary]) from exc

        self.stats.hedge_attempts += 1
        task_secondary = asyncio.create_task(
            self._open_upstream(secondary, target_host, target_port, connect_req)
        )
        # ProxyEntry 可变不可哈希，用列表保存 (entry, task)
        pairs = [(primary, task_primary), (secondary, task_secondary)]
        attempted = [primary, secondary]
        try:
            pending = {task_primary, task_secondary}
            while pending:
                finished, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for ent, t in pairs:
                    if t not in finished or t.cancelled():
                        continue
                    if t.exception() is None:
                        r, w = t.result()
                        await _close_tasks(pairs, keep_task=t)
                        if t is task_secondary:
                            self.stats.hedge_saved += 1
                            logger.debug("hedge_saved: %s", secondary.address)
                        else:
                            self.stats.hedge_wasted += 1
                        return ent, r, w, attempted

                if pending:
                    continue

                errors = [t.exception() for _, t in pairs
                          if not t.cancelled() and t.exception() is not None]
                raise _AttemptError(
                    errors[-1] if errors else ConnectionError("对冲双方均失败"),
                    attempted,
                )
        except _AttemptError:
            await _close_tasks(pairs, keep_task=None)
            raise
        except Exception as exc:
            await _close_tasks(pairs, keep_task=None)
            raise _AttemptError(exc, attempted) from exc
        except BaseException:
            # CancelledError 等：收 socket 后原样抛出，防 FD 泄漏
            await _close_tasks(pairs, keep_task=None)
            raise
        finally:
            self._release_hedge_slot()

    async def _open_upstream_with_failover(
        self,
        target_host: str,
        target_port: int,
        connect_req: bool = True,
    ) -> Tuple[ProxyEntry, asyncio.StreamReader, asyncio.StreamWriter]:
        """一批失败自动取下一批候选（含未验证备用），直到尝试预算用尽。"""
        last_err: Optional[Exception] = None
        tried = 0
        tried_ids: set = set()
        max_attempts = max(self.max_failover * 2, 12)

        def _note_fail(entries: list, exc: Exception) -> None:
            for e in entries:
                fails = e.mark_fail()
                self.pool.touch_entry(e)
                hard = self._is_hard_fail(exc)
                if fails >= int(_cfg("FAIL_KICK_THRESHOLD", 3)) or hard:
                    self._kick(e, f"fail#{fails} {exc}")
                else:
                    logger.debug("失败#%s %s: %s", fails, e.auth_address, exc)

        while tried < max_attempts:
            candidates = self._pick_candidates(exclude=tried_ids)
            if not candidates:
                break
            batch = candidates[: max(1, self.max_failover)]
            if not batch:
                break

            idx = 0
            while idx < len(batch) and tried < max_attempts:
                remaining = [e for e in batch[idx:] if e.address not in tried_ids]
                if not remaining:
                    break

                try_hedge = (
                    self.hedge_enabled
                    and len(remaining) >= 2
                    and self.hedge_delay > 0
                )

                if try_hedge:
                    width = min(self.hedge_width, len(remaining))
                    group = remaining[:width]
                    # 只有主连接必定发起；对冲备胎要等真正拉起后才计入 tried
                    # 避免「主连接 τ 内失败」时把未尝试的备胎也跳过
                    tried_ids.add(group[0].address)
                    tried += 1
                    is_first_attempt = (tried == 1)
                    try:
                        entry, reader, writer, attempted = await self._open_upstream_hedged(
                            group, target_host, target_port, connect_req=connect_req
                        )
                        self.stats.last_proxy = entry.auth_address
                        if is_first_attempt and entry.address == group[0].address:
                            self.stats.first_ok += 1
                        return entry, reader, writer
                    except _AttemptError as exc:
                        last_err = exc.cause
                        self.stats.failovers += 1
                        if is_first_attempt:
                            self.stats.first_fail += 1
                        got = list(exc.attempted or [group[0]])
                        for e in got:
                            tried_ids.add(e.address)
                        tried += max(0, len(got) - 1)  # 主连接已计 1
                        _note_fail(got, exc.cause)
                        # 只推进实际消耗掉的候选：未尝试的备胎留给下一轮
                        idx += max(1, len(got))
                        continue
                    except Exception as exc:
                        last_err = exc
                        self.stats.failovers += 1
                        if is_first_attempt:
                            self.stats.first_fail += 1
                        for e in group:
                            tried_ids.add(e.address)
                        tried += max(0, width - 1)
                        _note_fail(group, exc)
                        idx += width
                        continue

                # 单路径（对冲关闭 / 仅剩 1 个）
                entry = remaining[0]
                tried_ids.add(entry.address)
                tried += 1
                try:
                    reader, writer = await self._open_upstream(
                        entry, target_host, target_port, connect_req=connect_req
                    )
                    self.stats.last_proxy = entry.auth_address
                    if tried == 1:
                        self.stats.first_ok += 1
                    return entry, reader, writer
                except Exception as exc:
                    last_err = exc
                    self.stats.failovers += 1
                    if tried == 1:
                        self.stats.first_fail += 1
                    _note_fail([entry], exc)
                    idx += 1
                    continue

        self.stats.last_error = str(last_err)
        raise ConnectionError(f"全部候选 IP 失败: {last_err}")

    # ---------- 数据转发 ----------

    async def _pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> str:
        """
        单向转发。
        返回 ok|eof|error|stall
        stall：超过 stall_timeout 无任何数据，立即断开便于客户端快速重试。
        """
        stall = self.stall_timeout if self.stall_timeout and self.stall_timeout > 0 else None
        try:
            while True:
                if stall:
                    try:
                        data = await asyncio.wait_for(r.read(65536), timeout=stall)
                    except asyncio.TimeoutError:
                        return "stall"
                else:
                    data = await r.read(65536)
                if not data:
                    return "eof"
                try:
                    from utils.metrics import get_metrics
                    import config as _appcfg
                    get_metrics(
                        host=getattr(_appcfg, "REDIS_HOST", "127.0.0.1"),
                        port=int(getattr(_appcfg, "REDIS_PORT", 6379)),
                        db=int(getattr(_appcfg, "REDIS_DB", 15)),
                        password=getattr(_appcfg, "REDIS_PASSWORD", ""),
                    ).add_bytes(len(data))
                except Exception:
                    pass
                w.write(data)
                await w.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            return "error"
        except asyncio.CancelledError:
            return "ok"
        except Exception:
            return "error"
        finally:
            try:
                w.close()
            except Exception:
                pass

    async def _relay(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
        entry: Optional[ProxyEntry] = None,
    ) -> str:
        t1 = asyncio.create_task(self._pipe(c_reader, u_writer))
        t2 = asyncio.create_task(self._pipe(u_reader, c_writer))
        done, pending = await asyncio.wait(
            {t1, t2}, return_when=asyncio.FIRST_COMPLETED, timeout=self.idle_timeout or None
        )
        error = False
        for t in pending:
            t.cancel()
        for t in done:
            if t.cancelled():
                continue
            try:
                if t.result() in ("error", "stall"):
                    error = True
            except Exception:
                error = True
        # 成功：整段转发无 error 才记 ok，避免「又成功又失败」
        if entry is not None:
            if error:
                fails = entry.mark_fail()
                self.pool.touch_entry(entry)
                if fails >= int(_cfg("FAIL_KICK_THRESHOLD", 3)):
                    self._kick(entry, f"relay error x{fails}")
            else:
                entry.mark_success()
                self.pool.touch_entry(entry)
        try:
            c_writer.close()
        except Exception:
            pass
        try:
            u_writer.close()
        except Exception:
            pass
        return "error" if error else "ok"

    # ---------- 请求处理 ----------

    async def _read_headers(self, reader: asyncio.StreamReader) -> Tuple[bytes, list]:
        request_line = await reader.readline()
        headers = []
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            headers.append(line)
        return request_line, headers

    async def _handle_client(
        self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter
    ) -> None:
        if not self._conn_sem.acquire(blocking=False):
            try:
                await self._reply(c_writer, 503, "Too Many Connections")
            except Exception:
                pass
            try:
                c_writer.close()
            except Exception:
                pass
            return
        self.stats.connections += 1
        self.stats.active += 1
        try:
            request_line, headers = await self._read_headers(c_reader)
            if not request_line:
                c_writer.close()
                return

            parts = request_line.decode("latin-1", errors="replace").split()
            if len(parts) < 2:
                await self._reply(c_writer, 400, "Bad Request")
                return
            method, target = parts[0].upper(), parts[1]

            if method == "CONNECT":
                await self._handle_connect(c_reader, c_writer, target, headers)
            else:
                await self._handle_http(c_reader, c_writer, method, target, headers)

        except Exception as exc:
            self.stats.failures += 1
            self.stats.last_error = str(exc)
            logger.debug("客户端处理失败: %s", exc)
            try:
                await self._reply(c_writer, 502, "Bad Gateway")
            except Exception:
                pass
            try:
                c_writer.close()
            except Exception:
                pass
        finally:
            self.stats.active = max(0, self.stats.active - 1)
            try:
                self._conn_sem.release()
            except Exception:
                pass
            try:
                c_writer.close()
            except Exception:
                pass

    async def _handle_connect(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        target: str,
        headers: list,
    ) -> None:
        if ":" in target:
            host, _, port_s = target.rpartition(":")
            port = int(port_s or 443)
        else:
            host, port = target, 443

        t0 = time.perf_counter()
        entry, u_reader, u_writer = await self._open_upstream_with_failover(
            host, port, connect_req=True
        )
        logger.debug("CONNECT %s:%s via %s", host, port, entry.auth_address)
        c_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await c_writer.drain()
        rel = await self._relay(c_reader, c_writer, u_reader, u_writer, entry=entry)
        if rel == "ok":
            entry.update_latency((time.perf_counter() - t0) * 1000.0)
            self.stats.success += 1

    async def _handle_http(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        method: str,
        target: str,
        headers: list,
    ) -> None:
        # 解析目标：绝对 URL 或 Host 头
        host = ""
        port = 80
        path = target
        if target.startswith("http://") or target.startswith("https://"):
            from urllib.parse import urlparse
            u = urlparse(target)
            host = u.hostname or ""
            port = u.port or (443 if u.scheme == "https" else 80)
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
        else:
            for h in headers:
                if h.lower().startswith(b"host:"):
                    hv = h.split(b":", 1)[1].decode("latin-1").strip()
                    if ":" in hv:
                        host, _, ps = hv.rpartition(":")
                        port = int(ps)
                    else:
                        host, port = hv, 80
                    break
        if not host:
            await self._reply(c_writer, 400, "Bad Request")
            return

        # 读 body（Content-Length）
        body = b""
        content_length = 0
        for h in headers:
            if h.lower().startswith(b"content-length:"):
                try:
                    content_length = int(h.split(b":", 1)[1].strip())
                except ValueError:
                    content_length = 0
        if content_length > 0:
            body = await c_reader.readexactly(content_length)

        t0 = time.perf_counter()
        entry, u_reader, u_writer = await self._open_upstream_with_failover(
            host, port, connect_req=False
        )

        # 经上游代理：请求行用绝对 URL
        abs_url = target if target.startswith("http") else f"http://{host}:{port}{path}"
        out_headers = [f"{method} {abs_url} HTTP/1.1"]
        auth = self._auth_header(entry)
        has_host = False
        for h in headers:
            hs = h.decode("latin-1", errors="replace")
            if hs.lower().startswith("host:"):
                has_host = True
            if hs.lower().startswith("proxy-authorization"):
                continue
            out_headers.append(hs.rstrip("\r\n"))
        if not has_host:
            out_headers.append(f"Host: {host}:{port}")
        if auth:
            out_headers.append(f"Proxy-Authorization: {auth}")
        out_headers.append("Proxy-Connection: keep-alive")

        raw = ("\r\n".join(out_headers) + "\r\n\r\n").encode("latin-1", errors="replace") + body
        u_writer.write(raw)
        await u_writer.drain()

        rel = await self._relay(c_reader, c_writer, u_reader, u_writer, entry=entry)
        if rel == "ok":
            entry.update_latency((time.perf_counter() - t0) * 1000.0)
            self.stats.success += 1

    @staticmethod
    async def _reply(w: asyncio.StreamWriter, code: int, msg: str) -> None:
        body = msg.encode("utf-8")
        w.write(
            f"HTTP/1.1 {code} {msg}\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode("ascii") + body
        )
        await w.drain()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle_client, host=self.host, port=self.port
            )
            self._servers = [self._server]
        except OSError as exc:
            if exc.errno in (10048, 48, 98):
                raise OSError(
                    f"隧道端口 {self.host}:{self.port} 被占用，请先结束占用进程"
                    f"（netstat -ano | findstr {self.port}）"
                ) from exc
            raise
        logger.info(
            "隧道代理已启动 http://%s:%s  failover≤%s timeout=%.1fs",
            self.host, self.port, self.max_failover, self.connect_timeout,
        )
        asyncio.create_task(self._warm_loop())

    async def _warm_loop(self) -> None:
        """后台给高分热 IP 预建 TCP，压低首包延迟。"""
        while True:
            try:
                tops = [e for e in self._ranked_usable()[:8] if e.is_hot(800.0)]
                now = time.time()
                with self._warm_lock:
                    for addr in list(self._warm_sockets):
                        rec = self._warm_sockets.pop(addr, None)
                        if rec is None:
                            continue
                        if (now - rec[3]) > 25.0:
                            # 关键：关掉预热 socket，避免 FD 泄漏
                            try:
                                rec[2].close()
                            except Exception:
                                pass
                        else:
                            self._warm_sockets[addr] = rec
                    have = set(self._warm_sockets)
                for e in tops:
                    if e.address in have:
                        continue
                    with self._warm_lock:
                        if len(self._warm_sockets) >= 16:
                            break
                    try:
                        r, w = await asyncio.wait_for(
                            asyncio.open_connection(e.host, e.port), timeout=1.0
                        )
                        with self._warm_lock:
                            self._warm_sockets[e.address] = (e, r, w, time.time())
                    except Exception:
                        continue
            except Exception:
                logger.debug("warm loop skip")
            await asyncio.sleep(2.0)

    async def stop(self) -> None:
        for srv in (self._servers or ([self._server] if self._server else [])):
            try:
                srv.close()
                await srv.wait_closed()
            except Exception:
                pass
        self._servers = []
        self._server = None
        logger.info("隧道代理已停止")

    def run_in_thread(self) -> None:
        """在独立线程跑 asyncio 循环，与 Flask 并存。"""
        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _main() -> None:
                await self.start()
                while True:
                    await asyncio.sleep(3600)

            try:
                loop.run_until_complete(_main())
            except Exception:
                logger.exception("隧道线程异常")

        import threading
        t = threading.Thread(target=_runner, name="TunnelServer", daemon=True)
        t.start()
        self._task = None  # noqa: placeholder
        self._thread = t  # type: ignore

    def status(self) -> dict:
        data = self.stats.to_dict()
        data.update({
            "enabled": True,
            "host": self.host,
            "port": self.port,
            "max_failover": self.max_failover,
            "connect_timeout": self.connect_timeout,
            "adaptive_timeout": self.adaptive_timeout,
            "hedge_enabled": self.hedge_enabled,
            "hedge_delay_ms": int(self.hedge_delay * 1000),
            "hedge_width": self.hedge_width,
        })
        return data
