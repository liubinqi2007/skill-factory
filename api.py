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


# ─── WebSocket Chat — 按需启停 OpenCode ─────────────────────

@api.websocket("/ws/chat/{skill_id}")
async def ws_chat(websocket: WebSocket, skill_id: str):
    """
    WebSocket 聊天端点：
    - 连接时启动 OpenCode（cwd = skill workspace）
    - 断开时释放 OpenCode（空闲 5 分钟后自动关闭）
    - 流式转发 OpenCode 响应
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

    # ── 启动 OpenCode ──
    # 只有需要 auto-send 时才显示启动状态
    auto_send_task = None
    need_auto_send = False
    async with _auto_send_lock:
        user_msgs = [m for m in skill.messages if m.role == MessageRole.USER]
        has_assistant = any(m.role == MessageRole.ASSISTANT for m in skill.messages)
        # 仅首次创建、仅1条用户消息、无助手回复、且未触发过auto-send时才自动发送
        if (skill_id not in _auto_sending
                and not skill.auto_sent
                and len(user_msgs) == 1
                and not has_assistant):
            need_auto_send = True
            skill.auto_sent = True
            skill_manager._save_skill(skill)  # 立即持久化，防止重启后丢失
            _auto_sending.add(skill_id)

    if need_auto_send:
        await _safe_send(websocket, {"type": "status", "content": "正在启动 AI 助手..."})

    # Fix 5: try/finally 从 start 之后立即开始，防止 slot 泄漏
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

            async def _auto_send():
                try:
                    async for chunk in skill_manager.chat_stream(skill_id, initial_msg):
                        if chunk.get("type") == "question":
                            await _handle_question(websocket, skill_id, chunk)
                        else:
                            await _safe_send(websocket, chunk)
                except Exception as e:
                    print(f"[WS] Auto-send error: {e}", flush=True)
                    await _safe_send(websocket, {"type": "error", "content": f"自动发送失败: {e}"})
                finally:
                    async with _auto_send_lock:
                        _auto_sending.discard(skill_id)
                    print(f"[WS] Auto-send finished for {skill_id}", flush=True)

            auto_send_task = asyncio.create_task(_auto_send())
        elif has_assistant or skill_id in _auto_sending:
            print(f"[WS] Skill already has response or being processed, skipping auto-send", flush=True)

        # ── 消息循环 ──
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

            # 如果 auto_send 还在运行，先取消它
            if auto_send_task and not auto_send_task.done():
                print(f"[WS] Cancelling auto_send for {skill_id}, user sent new message", flush=True)
                auto_send_task.cancel()
                try:
                    await auto_send_task
                except asyncio.CancelledError:
                    pass
                await _safe_send(websocket, {"type": "done", "content": "", "skill_id": skill_id})

            # UI: 发送生成状态
            await websocket.send_json({"type": "status", "content": "正在生成..."})

            # 流式转发 OpenCode 响应
            async for chunk in skill_manager.chat_stream(skill_id, user_message):
                if chunk.get("type") == "question":
                    await _handle_question(websocket, skill_id, chunk)
                else:
                    await _safe_send(websocket, chunk)

    except WebSocketDisconnect:
        print(f"[WS] Disconnected: skill_id={skill_id}", flush=True)
    except Exception as e:
        print(f"[WS] Error: {e}", flush=True)
    finally:
        if auto_send_task and not auto_send_task.done():
            auto_send_task.cancel()
        async with _auto_send_lock:
            _auto_sending.discard(skill_id)
        print(f"[WS] Releasing OpenCode for skill_id={skill_id}", flush=True)
        await server_pool.release(server.port)


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
