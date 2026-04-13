from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess

from config import settings
from models import ServerInstance

logger = logging.getLogger(__name__)

# 空闲后多久关闭 OpenCode（秒）
IDLE_TIMEOUT = 300  # 5 分钟
# SIGTERM 后等待进程退出的超时（秒）
STOP_TIMEOUT = 5


class ServerPool:
    """按需启停 OpenCode 实例，空闲延迟关闭，控制并发上限。"""

    def __init__(self) -> None:
        self._instances: dict[int, ServerInstance] = {}
        self._lock = asyncio.Lock()
        # Fix 1: 用计数器替代 semaphore，避免复用时重复释放
        self._available = settings.max_servers
        # 预注册可用端口
        for port in settings.opencode_ports:
            self._instances[port] = ServerInstance(port=port)
        # 空闲关闭任务：port → asyncio.Task
        self._idle_tasks: dict[int, asyncio.Task] = {}

    def _acquire_slot(self) -> bool:
        """占用一个并发槽位（必须在 self._lock 内调用）。"""
        if self._available <= 0:
            return False
        self._available -= 1
        return True

    def _release_slot(self) -> None:
        """释放一个并发槽位（必须在 self._lock 内调用）。"""
        self._available += 1

    async def start(self, skill_id: str, cwd: str) -> ServerInstance | None:
        """获取一个可用实例。优先复用空闲实例，否则启动新实例。"""
        print(f"[Pool] start() called: skill_id={skill_id}, cwd={cwd}", flush=True)

        server_to_start = None

        async with self._lock:
            # 1. 尝试复用该 skill 之前的空闲实例
            for port, server in self._instances.items():
                if server.skill_id == skill_id and server.opencode_pid:
                    idle_task = self._idle_tasks.pop(port, None)
                    if idle_task and not idle_task.done():
                        idle_task.cancel()

                    if self._is_alive(server):
                        server.in_use = True
                        print(f"[Pool] Reusing idle instance: port={port} for skill={skill_id}", flush=True)
                        return server
                    else:
                        # 进程已死，清理（slot 已在 _idle_shutdown 中释放，不重复释放）
                        print(f"[Pool] Process dead, cleaning up port={port}", flush=True)
                        server.opencode_pid = None
                        server.skill_id = None
                        server.in_use = False

            # 2. 没有可复用的，获取空闲端口准备启动
            if not self._acquire_slot():
                print(f"[Pool] No available slots (max={settings.max_servers})", flush=True)
                return None

            for port, server in self._instances.items():
                if not server.in_use and not server.opencode_pid:
                    server.in_use = True
                    server.skill_id = skill_id
                    server_to_start = server
                    break

            if not server_to_start:
                # 有 slot 但没有空闲端口
                self._release_slot()
                return None

        # 阶段2：锁外启动进程（可能耗时30秒，不阻塞其他请求）
        ok = await self._do_start(server_to_start, cwd)
        if ok:
            return server_to_start
        else:
            async with self._lock:
                server_to_start.in_use = False
                server_to_start.skill_id = None
                self._release_slot()
            return None

    async def release(self, port: int) -> None:
        """释放实例：标记空闲，启动延迟关闭计时器。"""
        async with self._lock:
            server = self._instances.get(port)
            if not server or not server.in_use:
                return

            server.in_use = False
            print(f"[Pool] Released port {port}, scheduling idle shutdown in {IDLE_TIMEOUT}s", flush=True)

            idle_task = asyncio.create_task(self._idle_shutdown(port))
            self._idle_tasks[port] = idle_task

    async def stop(self, port: int) -> None:
        """立即停止 OpenCode 实例。"""
        async with self._lock:
            idle_task = self._idle_tasks.pop(port, None)
            if idle_task and not idle_task.done():
                idle_task.cancel()

            server = self._instances.get(port)
            if server and (server.in_use or server.opencode_pid):
                was_in_use = server.in_use
                await self._do_stop(server)
                server.in_use = False
                server.skill_id = None
                if was_in_use:
                    self._release_slot()
                print(f"[Pool] Stopped port {port}", flush=True)

    async def stop_all(self) -> None:
        """停止所有实例（优雅关闭用）。"""
        async with self._lock:
            for port, server in self._instances.items():
                if server.opencode_pid:
                    idle_task = self._idle_tasks.pop(port, None)
                    if idle_task and not idle_task.done():
                        idle_task.cancel()
                    await self._do_stop(server)
                    server.in_use = False
                    server.skill_id = None
                    print(f"[Pool] Stopped port {port} (shutdown)", flush=True)
            self._available = settings.max_servers

    async def _idle_shutdown(self, port: int) -> None:
        """空闲超时后自动关闭。"""
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
            async with self._lock:
                server = self._instances.get(port)
                if server and not server.in_use and server.opencode_pid:
                    print(f"[Pool] Idle timeout, shutting down port {port}", flush=True)
                    await self._do_stop(server)
                    server.skill_id = None
                    self._release_slot()
                self._idle_tasks.pop(port, None)
        except asyncio.CancelledError:
            pass

    def _is_alive(self, server: ServerInstance) -> bool:
        """检查进程是否还活着。"""
        if not server.opencode_pid:
            return False
        try:
            os.kill(server.opencode_pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def _do_start(self, server: ServerInstance, cwd: str) -> bool:
        """启动 OpenCode 进程，轮询等待就绪。"""
        try:
            proc = subprocess.Popen(
                ["opencode", "serve", "--port", str(server.port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
            )
            # 立即记录 pid，防止后续异常导致进程泄漏
            server.opencode_pid = proc.pid

            import httpx
            # 轮询等待就绪（最多 30 秒，每 500ms 检查一次）
            for attempt in range(60):
                await asyncio.sleep(0.5)
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(3, connect=1)) as c:
                        r = await c.post(
                            f"http://127.0.0.1:{server.port}/session",
                            json={}, timeout=3,
                        )
                        r.raise_for_status()
                        break
                except Exception:
                    if attempt == 59:
                        raise
                    continue

            print(f"[Pool] OpenCode started: port={server.port}, pid={proc.pid}, cwd={cwd}", flush=True)
            return True
        except FileNotFoundError:
            logger.warning("opencode CLI not found")
            self._kill_process(server)
            return False
        except asyncio.CancelledError:
            # 被 cancel 时也要清理进程
            logger.warning("OpenCode start cancelled, killing process port=%s", server.port)
            self._kill_process(server)
            raise
        except Exception as e:
            logger.error("Failed to start OpenCode: %s", e)
            self._kill_process(server)
            return False

    async def _do_stop(self, server: ServerInstance) -> None:
        """Fix 3: SIGTERM 后等待进程退出，超时 SIGKILL。"""
        pid = server.opencode_pid
        if not pid:
            return

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            server.opencode_pid = None
            return

        # 等待进程退出（最多 5 秒）
        for _ in range(STOP_TIMEOUT * 10):
            if not self._is_alive(server):
                break
            await asyncio.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning("Force killed OpenCode pid=%d", pid)
            except ProcessLookupError:
                pass

        server.opencode_pid = None
        print(f"[Pool] OpenCode stopped: port={server.port}, pid={pid}", flush=True)

    def _kill_process(self, server: ServerInstance) -> None:
        """强制杀掉进程（启动失败时用）。"""
        if server.opencode_pid:
            try:
                os.kill(server.opencode_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            server.opencode_pid = None

    def get_status(self) -> list[ServerInstance]:
        return list(self._instances.values())

    def get_by_skill(self, skill_id: str) -> ServerInstance | None:
        """根据 skill_id 查找正在使用的实例。"""
        for server in self._instances.values():
            if server.skill_id == skill_id and server.in_use:
                return server
        return None


server_pool = ServerPool()
