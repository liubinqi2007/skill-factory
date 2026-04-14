from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from models import StatusResponse, MessageRole

# Fix 6: 用 lock 保护 _auto_sending 的 check-then-act
_auto_sending: set[str] = set()
_auto_send_lock = asyncio.Lock()
from skill_manager import skill_manager
from server_pool import server_pool
from config import settings

logger = logging.getLogger(__name__)

api = FastAPI(title="Skill Factory API")

FRONTEND_DIR = Path(__file__).parent / "frontend"
if FRONTEND_DIR.exists():
    api.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ── Stream Context（后台流式 + 重连恢复）────────────────────

class StreamContext:
    """跟踪正在进行的流式生成，支持 WS 断开后继续运行。"""
    def __init__(self):
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.done: bool = False
        self.server = None  # ServerInstance reference

_stream_contexts: dict[str, StreamContext] = {}


async def _run_stream(skill_id: str, user_message: str, ctx: StreamContext):
    """后台任务：运行 chat_stream 并将 chunks 写入队列。WS 断开不中断。"""
    try:
        async for chunk in skill_manager.chat_stream(skill_id, user_message):
            await ctx.queue.put(chunk)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Stream worker error: %s", e, exc_info=True)
        await ctx.queue.put({"type": "error", "content": f"Stream error: {e}"})
    finally:
        ctx.done = True
        await ctx.queue.put(None)  # sentinel
        # 释放 OpenCode 服务
        if ctx.server:
            await server_pool.release(ctx.server.port)
        # 清理 context
        if skill_id in _stream_contexts and _stream_contexts[skill_id] is ctx:
            del _stream_contexts[skill_id]
        async with _auto_send_lock:
            _auto_sending.discard(skill_id)


# Fix 7: 优雅关闭钩子
@api.on_event("shutdown")
async def shutdown():
    print("[Shutdown] Stopping all OpenCode instances...", flush=True)
    await server_pool.stop_all()


# ─── HTML 入口 ───────────────────────────────────────────────

@api.get("/")
async def index():
    html_path = FRONTEND_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return JSONResponse({"error": "frontend not found"}, status_code=404)


# ─── Skill CRUD ──────────────────────────────────────────────

@api.get("/api/skills")
async def list_skills():
    skills = skill_manager.list_skills()
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "status": s.status.value,
            "message_count": len(s.messages),
            "workspace": s.workspace,
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat(),
        }
        for s in skills
    ]


@api.post("/api/skills")
async def create_skill(request: Request):
    body = await request.json()
    message = body.get("message", "")
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    skill = await skill_manager.create_skill(message)
    return {
        "id": skill.id,
        "name": skill.name,
        "status": skill.status.value,
        "workspace": skill.workspace,
    }


@api.get("/api/skills/{skill_id}")
async def get_skill(skill_id: str):
    skill = skill_manager.get_skill(skill_id)
    if not skill:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "status": skill.status.value,
        "workspace": skill.workspace,
        "messages": [
            {
                "id": m.id,
                "role": m.role.value,
                "content": m.content,
                "thinking": m.thinking,
                "tool_details": m.tool_details,
                "timestamp": m.timestamp.isoformat(),
            }
            for m in skill.messages
        ],
    }


@api.delete("/api/skills/{skill_id}")
async def delete_skill(skill_id: str):
    ok = await skill_manager.delete_skill(skill_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"deleted": True}


# ─── WebSocket Chat — 按需启停 OpenCode，支持断线恢复 ──────

@api.websocket("/ws/chat/{skill_id}")
async def ws_chat(websocket: WebSocket, skill_id: str):
    """
    WebSocket 聊天端点：
    - 连接时启动 OpenCode（cwd = skill workspace）
    - 断开时保留流式任务继续运行（内容增量持久化到磁盘）
    - 重连时恢复已生成的内容并继续接收新内容
    - 支持 question 工具的双向交互
    """
    await websocket.accept()
    print(f"[WS] Connected: skill_id={skill_id}", flush=True)

    skill = skill_manager.get_skill(skill_id)
    if not skill:
        await websocket.send_json({"type": "error", "content": "Skill not found"})
        await websocket.close()
        return

    workspace = skill.workspace
    if not workspace:
        await websocket.send_json({"type": "error", "content": "Workspace not found"})
        await websocket.close()
        return

    # ── 检查是否有进行中的流（断线恢复） ──
    existing_ctx = _stream_contexts.get(skill_id)
    if existing_ctx and not existing_ctx.done:
        print(f"[WS] Resuming stream for skill_id={skill_id}", flush=True)
        # 发送当前已累积的内容
        streaming_msg = skill_manager.get_streaming_message(skill_id)
        if streaming_msg:
            await _safe_send(websocket, {
                "type": "stream_resume",
                "content": streaming_msg.content,
                "thinking": streaming_msg.thinking,
                "tool_details": streaming_msg.tool_details,
            })

        # 排空旧 chunks（内容已通过 stream_resume 发送）
        # 同时检测 done 事件（流可能在断线期间已完成）
        stream_done = False
        while not existing_ctx.queue.empty():
            try:
                old_chunk = existing_ctx.queue.get_nowait()
                if old_chunk is not None and old_chunk.get("type") == "done":
                    stream_done = True
            except asyncio.QueueEmpty:
                break

        # 如果流已完成（排空时找到 done 或 worker 已标记完成）
        if stream_done or existing_ctx.done:
            await _safe_send(websocket, {"type": "done", "content": "", "skill_id": skill_id})
            return

        # 继续转发新 chunks（带超时保护 + 同时监听 WS 消息处理 question_reply）
        async def _forward_chunks():
            """从队列读取 chunks 并转发到 WS。"""
            while True:
                try:
                    chunk = await asyncio.wait_for(existing_ctx.queue.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    print(f"[WS] Resume timeout (60s no data), finalizing: skill_id={skill_id}", flush=True)
                    if skill_manager.is_streaming(skill_id):
                        skill.status = SkillStatus.ACTIVE
                        skill_manager._save_skill(skill)
                        skill_manager._streaming.pop(skill_id, None)
                    await _safe_send(websocket, {"type": "done", "content": "", "skill_id": skill_id})
                    await asyncio.sleep(0.3)
                    return "timeout"
                if chunk is None:
                    return "done"
                if chunk.get("type") == "question":
                    await _handle_question(websocket, skill_id, chunk)
                else:
                    await _safe_send(websocket, chunk)
            return "done"

        async def _receive_ws_messages():
            """监听 WS 消息，处理 question_reply。"""
            while True:
                try:
                    data = await websocket.receive_json()
                except Exception:
                    return
                if data.get("type") == "question_reply":
                    request_id = data.get("request_id", "")
                    answers = data.get("answers", [])
                    if request_id:
                        print(f"[WS] Resume question reply: request_id={request_id}", flush=True)
                        await skill_manager.reply_question(skill_id, request_id, answers)

        try:
            forward_task = asyncio.create_task(_forward_chunks())
            receive_task = asyncio.create_task(_receive_ws_messages())
            done, pending = await asyncio.wait(
                [forward_task, receive_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                pass  # result already handled
        except WebSocketDisconnect:
            print(f"[WS] Disconnected during resume: skill_id={skill_id}", flush=True)
        return

    # ── 正常连接 ──
    need_auto_send = False
    async with _auto_send_lock:
        user_msgs = [m for m in skill.messages if m.role == MessageRole.USER]
        has_assistant = any(m.role == MessageRole.ASSISTANT for m in skill.messages)
        if (skill_id not in _auto_sending
                and not skill.auto_sent
                and len(user_msgs) == 1
                and not has_assistant):
            need_auto_send = True
            skill.auto_sent = True
            skill_manager._save_skill(skill)
            _auto_sending.add(skill_id)

    if need_auto_send:
        await _safe_send(websocket, {"type": "status", "content": "正在启动 AI 助手..."})

    server = await server_pool.start(skill_id, workspace)
    if not server:
        await _safe_send(websocket, {"type": "error", "content": "AI 助手启动失败，可能并发数已满（最多3个）"})
        await websocket.close()
        return

    try:
        skill.opencode_port = server.port

        # ── 自动发送初始消息（仅全新、无回复的 Skill） ──
        if need_auto_send:
            initial_msg = user_msgs[0].content
            await websocket.send_json({"type": "status", "content": "AI 助手已就绪，正在分析需求..."})
            print(f"[WS] Auto-sending initial message: {initial_msg[:50]}...", flush=True)

            # 创建 StreamContext，后台 worker 运行流式生成
            ctx = StreamContext()
            ctx.server = server
            _stream_contexts[skill_id] = ctx
            ctx.task = asyncio.create_task(_run_stream(skill_id, initial_msg, ctx))

            # 从队列转发 chunks 到 WS
            try:
                while True:
                    chunk = await ctx.queue.get()
                    if chunk is None:
                        break
                    if chunk.get("type") == "question":
                        await _handle_question(websocket, skill_id, chunk)
                    else:
                        await _safe_send(websocket, chunk)
            except WebSocketDisconnect:
                # WS 断开但 worker 继续运行，内容持久化到磁盘
                print(f"[WS] Disconnected, stream worker continues: skill_id={skill_id}", flush=True)
            return

        elif has_assistant or skill_id in _auto_sending:
            print(f"[WS] Skill already has response or being processed, skipping auto-send", flush=True)

        # ── 消息循环（手动发送） ──
        while True:
            data = await websocket.receive_json()

            # 处理 question 回答
            if data.get("type") == "question_reply":
                request_id = data.get("request_id", "")
                answers = data.get("answers", [])
                if request_id:
                    print(f"[WS] Question reply: request_id={request_id}, answers={answers}", flush=True)
                    ok = await skill_manager.reply_question(skill_id, request_id, answers)
                    if not ok:
                        await _safe_send(websocket, {"type": "error", "content": "回答提交失败"})
                continue

            user_message = data.get("message", "")
            if not user_message:
                await _safe_send(websocket, {"type": "error", "content": "消息不能为空"})
                continue

            # 检查是否有正在进行的流式任务
            existing_ctx = _stream_contexts.get(skill_id)
            if existing_ctx and not existing_ctx.done:
                print(f"[WS] Cancelling existing stream for {skill_id}", flush=True)
                existing_ctx.task.cancel()
                try:
                    await existing_ctx.task
                except asyncio.CancelledError:
                    pass
                await _safe_send(websocket, {"type": "done", "content": "", "skill_id": skill_id})

            # UI: 发送生成状态
            await websocket.send_json({"type": "status", "content": "正在生成..."})

            # 创建 StreamContext，后台 worker 运行流式生成
            ctx = StreamContext()
            ctx.server = server
            _stream_contexts[skill_id] = ctx
            ctx.task = asyncio.create_task(_run_stream(skill_id, user_message, ctx))

            # 从队列转发 chunks 到 WS
            try:
                while True:
                    chunk = await ctx.queue.get()
                    if chunk is None:
                        break
                    if chunk.get("type") == "question":
                        await _handle_question(websocket, skill_id, chunk)
                    else:
                        await _safe_send(websocket, chunk)
            except WebSocketDisconnect:
                print(f"[WS] Disconnected during manual send, stream continues: skill_id={skill_id}", flush=True)
            return  # 退出消息循环

    except WebSocketDisconnect:
        print(f"[WS] Disconnected: skill_id={skill_id}", flush=True)
    except Exception as e:
        print(f"[WS] Error: {e}", flush=True)
    finally:
        # 只有没有活跃流式任务时才释放 OpenCode
        ctx = _stream_contexts.get(skill_id)
        if not ctx or ctx.done:
            print(f"[WS] Releasing OpenCode for skill_id={skill_id}", flush=True)
            await server_pool.release(server.port)
        async with _auto_send_lock:
            _auto_sending.discard(skill_id)


async def _safe_send(websocket: WebSocket, data: dict) -> None:
    """安全发送 WebSocket 消息，忽略已断开的连接。"""
    try:
        await websocket.send_json(data)
    except Exception:
        pass


async def _handle_question(websocket: WebSocket, skill_id: str, question_chunk: dict) -> None:
    """处理 AI 的 question 事件：发送给前端。"""
    await _safe_send(websocket, question_chunk)
    print(f"[WS] Question sent to frontend: {question_chunk.get('request_id', '')}", flush=True)


# ─── 历史消息 ────────────────────────────────────────────────

@api.get("/api/skills/{skill_id}/messages")
async def get_messages(skill_id: str):
    messages = skill_manager.get_messages(skill_id)
    return [
        {
            "id": m.id,
            "role": m.role.value,
            "content": m.content,
            "thinking": m.thinking,
            "tool_details": m.tool_details,
            "timestamp": m.timestamp.isoformat(),
        }
        for m in messages
    ]


# ─── 系统状态 ────────────────────────────────────────────────

@api.get("/api/status")
async def get_status():
    servers = server_pool.get_status()
    skills = skill_manager.list_skills()
    return StatusResponse(
        total_servers=settings.max_servers,
        active_servers=sum(1 for s in servers if s.in_use),
        skills_count=len(skills),
        servers=servers,
    ).model_dump()
