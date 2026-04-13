from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from config import settings
from models import Message, MessageRole, Skill, SkillStatus
from server_pool import server_pool

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SKILLS_DIR = BASE_DIR / settings.skills_dir

META_FILE = ".skill_meta.json"
MESSAGES_FILE = ".messages.json"

# 多轮思考边界分隔符
THINKING_ROUND_SEP = "\n\n===THINKING_ROUND===\n\n"


def _ensure_skills_dir() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _dt_to_str(dt: datetime) -> str:
    return dt.isoformat()


def _str_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class SkillManager:
    """管理 Skill 的完整生命周期：创建 workspace、迭代、持久化。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        _ensure_skills_dir()
        self._load_all_skills()
        self._system_prompt = self._load_system_prompt()

    # ── 持久化 ──────────────────────────────────────────

    def _skill_meta_path(self, workspace: str) -> Path:
        return Path(workspace) / META_FILE

    def _messages_path(self, workspace: str) -> Path:
        return Path(workspace) / MESSAGES_FILE

    def _save_skill(self, skill: Skill) -> None:
        """保存 skill 元数据 + 消息到 workspace 目录。"""
        if not skill.workspace:
            return
        ws = Path(skill.workspace)

        # 保存元数据
        meta = {
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "status": skill.status.value,
            "workspace": skill.workspace,
            "auto_sent": skill.auto_sent,
            "created_at": _dt_to_str(skill.created_at),
            "updated_at": _dt_to_str(skill.updated_at),
        }
        (ws / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # 保存消息
        msgs = [
            {
                "id": m.id,
                "role": m.role.value,
                "content": m.content,
                "thinking": m.thinking,
                "timestamp": _dt_to_str(m.timestamp),
            }
            for m in skill.messages
        ]
        (ws / MESSAGES_FILE).write_text(json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_skill_from_dir(self, workspace: Path) -> Skill | None:
        """从 workspace 目录加载 skill。"""
        meta_path = workspace / META_FILE
        msgs_path = workspace / MESSAGES_FILE

        if not meta_path.exists():
            return None

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            # 启动时修正残留状态：creating/iterating → active
            saved_status = meta.get("status", "creating")
            if saved_status in ("creating", "iterating"):
                saved_status = "active"

            skill = Skill(
                id=meta["id"],
                name=meta["name"],
                description=meta.get("description", ""),
                status=SkillStatus(saved_status),
                workspace=meta.get("workspace", str(workspace)),
                auto_sent=meta.get("auto_sent", False),
                created_at=_str_to_dt(meta["created_at"]),
                updated_at=_str_to_dt(meta["updated_at"]),
            )

            # 加载消息
            if msgs_path.exists():
                msgs_data = json.loads(msgs_path.read_text(encoding="utf-8"))
                for m in msgs_data:
                    skill.messages.append(Message(
                        id=m["id"],
                        role=MessageRole(m["role"]),
                        content=m["content"],
                        thinking=m.get("thinking", ""),
                        timestamp=_str_to_dt(m["timestamp"]),
                    ))

            # 旧数据兼容：已有助手回复的 skill 自动标记 auto_sent=True
            if not skill.auto_sent and any(m.role == MessageRole.ASSISTANT for m in skill.messages):
                skill.auto_sent = True

            return skill
        except Exception as e:
            logger.error("Failed to load skill from %s: %s", workspace, e)
            return None

    def _load_all_skills(self) -> None:
        """启动时扫描 skills 目录，加载所有已有 skill。"""
        if not SKILLS_DIR.exists():
            return

        for workspace in SKILLS_DIR.iterdir():
            if not workspace.is_dir():
                continue
            if not (workspace / META_FILE).exists():
                continue
            skill = self._load_skill_from_dir(workspace)
            if skill:
                self._skills[skill.id] = skill
                logger.info("Loaded skill: %s (%s)", skill.name, skill.id)

    def _load_system_prompt(self) -> str:
        """加载系统提示词文件。"""
        prompt_path = BASE_DIR / "AGENT_SYSTEM_PROMPT.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return ""

    # ── 创建 ──────────────────────────────────────────

    async def create_skill(self, user_message: str) -> Skill:
        skill = Skill(
            name=_extract_skill_name(user_message),
            description=user_message[:100],
            status=SkillStatus.CREATING,
        )

        workspace = SKILLS_DIR / skill.name
        workspace.mkdir(parents=True, exist_ok=True)
        skill.workspace = str(workspace)

        skill.messages.append(
            Message(role=MessageRole.USER, content=user_message)
        )
        self._skills[skill.id] = skill
        self._save_skill(skill)
        return skill

    # ── 对话 ──────────────────────────────────────────

    async def chat_stream(self, skill_id: str, user_message: str):
        skill = self._skills.get(skill_id)
        if not skill:
            yield {"type": "error", "content": "Skill not found"}
            return

        if not skill.workspace:
            yield {"type": "error", "content": "Skill workspace not found"}
            return

        # 避免重复追加
        last = skill.messages[-1] if skill.messages else None
        if not (last and last.role == MessageRole.USER and last.content == user_message):
            skill.messages.append(
                Message(role=MessageRole.USER, content=user_message)
            )
        skill.status = SkillStatus.ITERATING

        full_response = ""
        full_thinking = ""
        thinking_has_content = False  # 追踪当前思考轮次是否已有内容
        try:
            async for chunk in self._stream_from_opencode(skill, user_message):
                if chunk.get("type") == "text":
                    full_response += chunk.get("content", "")
                    yield chunk
                elif chunk.get("type") == "thinking_round_start":
                    # 新一轮思考开始，在已有内容时插入分隔符
                    if full_thinking and thinking_has_content:
                        full_thinking += THINKING_ROUND_SEP
                    thinking_has_content = False
                elif chunk.get("type") == "thinking":
                    full_thinking += chunk.get("content", "")
                    thinking_has_content = True
                    yield chunk
                elif chunk.get("type") == "question":
                    # AI 提问，直接透传给前端（ws_chat 负责处理交互）
                    yield chunk
                elif chunk.get("type") == "tool_status":
                    yield chunk
                elif chunk.get("type") == "status":
                    yield chunk
                elif chunk.get("type") == "error":
                    yield chunk
                    return
        except Exception as e:
            logger.error("Stream error: %s", e, exc_info=True)
            yield {"type": "error", "content": f"流式读取错误: {e}"}
            return

        if full_response:
            skill.messages.append(
                Message(role=MessageRole.ASSISTANT, content=full_response, thinking=full_thinking)
            )

        skill.status = SkillStatus.ACTIVE
        self._save_skill(skill)
        yield {"type": "done", "content": "", "skill_id": skill.id}

    # ── 回答 OpenCode question ────────────────────────

    async def reply_question(self, skill_id: str, request_id: str, answers: list[list[str]]) -> bool:
        """回答 OpenCode 的 question 工具。"""
        import httpx

        server = server_pool.get_by_skill(skill_id)
        if not server:
            return False

        port = server.port
        base = f"http://127.0.0.1:{port}"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as c:
                r = await c.post(
                    f"{base}/question/{request_id}/reply",
                    json={"answers": answers},
                    timeout=15,
                )
                print(f"[Chat] Question reply: {r.status_code} {r.text[:100]}", flush=True)
                return r.status_code == 200
        except Exception as e:
            logger.error("Failed to reply question: %s", e)
            return False

    # ── SSE 流式 ──────────────────────────────────────

    async def _stream_from_opencode(self, skill: Skill, user_message: str):
        import httpx

        server = server_pool.get_by_skill(skill.id)
        if not server:
            yield {"type": "error", "content": "OpenCode 实例未启动"}
            return

        prompt = self._build_prompt(skill, user_message)
        port = server.port
        base = f"http://127.0.0.1:{port}"

        # 每次对话创建新 session，避免旧 prompt 残留干扰多轮对话
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as c:
                r = await c.post(f"{base}/session", json={}, timeout=15)
                r.raise_for_status()
                session_id = r.json()["id"]
            print(f"[Chat] New session created: {session_id}", flush=True)
        except Exception as e:
            yield {"type": "error", "content": f"创建 session 失败: {e}"}
            return

        yield {"type": "status", "content": "正在生成..."}
        print(f"[Chat] Streaming: port={port}, session={session_id}", flush=True)

        full_text = ""

        # Fix 2: SSE 活动超时，120 秒无事件则断开（heartbeat 每 10 秒一次保活）
        SSE_INACTIVITY_TIMEOUT = 120
        last_event_time = time.monotonic()

        # 设置读超时（10分钟），question 等待可能较长
        sse_timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)
        sse_client = httpx.AsyncClient(timeout=sse_timeout)

        try:
            print(f"[Chat] Connecting to SSE first...", flush=True)

            async with sse_client.stream(
                "GET", f"{base}/event",
                headers={"Accept": "text/event-stream"},
            ) as response:
                print(f"[Chat] SSE connected, status={response.status_code}", flush=True)

                if response.status_code != 200:
                    yield {"type": "error", "content": f"SSE 连接失败: {response.status_code}"}
                    return

                print(f"[Chat] Sending prompt_async...", flush=True)
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as post_client:
                    r = await post_client.post(
                        f"{base}/session/{session_id}/prompt_async",
                        json={"parts": [{"type": "text", "text": prompt}]},
                    )
                    print(f"[Chat] prompt_async response: {r.status_code}", flush=True)
                    if r.status_code != 204:
                        yield {"type": "error", "content": f"prompt_async 返回 {r.status_code}"}
                        return

                # 按 part_id 追踪每个 part 的类型，避免多 part 并行时错乱
                part_types: dict[str, str] = {}  # part_id → part_type

                async for line in response.aiter_lines():
                    # Fix 2: 活动超时检测
                    if time.monotonic() - last_event_time > SSE_INACTIVITY_TIMEOUT:
                        print(f"[Chat] SSE inactivity timeout ({SSE_INACTIVITY_TIMEOUT}s), closing", flush=True)
                        break

                    if not line or not line.startswith("data: "):
                        continue

                    raw = line[6:]
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    evt_type = evt.get("type", "")
                    properties = evt.get("properties", {})

                    evt_session = properties.get("sessionID", "")
                    if evt_session and evt_session != session_id:
                        continue

                    if evt_type == "server.heartbeat":
                        last_event_time = time.monotonic()
                        continue

                    # ── question.asked 事件：AI 向用户提问 ──
                    if evt_type == "question.asked":
                        last_event_time = time.monotonic()
                        q_id = properties.get("id", "")
                        q_list = properties.get("questions", [])
                        # 构造前端友好的格式
                        questions = []
                        for q in q_list:
                            questions.append({
                                "question": q.get("question", ""),
                                "header": q.get("header", ""),
                                "multiple": q.get("multiple", False),
                                "options": q.get("options", []),
                            })
                        yield {
                            "type": "question",
                            "request_id": q_id,
                            "session_id": session_id,
                            "questions": questions,
                        }
                        print(f"[Chat] Question asked: id={q_id}, {len(questions)} questions", flush=True)
                        continue

                    if evt_type == "message.part.updated":
                        last_event_time = time.monotonic()
                        part = properties.get("part", {})
                        part_id = part.get("id", "")
                        part_type = part.get("type", "")

                        if part_id:
                            part_types[part_id] = part_type

                        if part_type == "step-start":
                            yield {"type": "thinking_round_start"}
                            yield {"type": "status", "content": "思考中"}
                        elif part_type == "tool-use" or part_type == "tool":
                            tool_name = part.get("name", part.get("tool", ""))
                            state = part.get("state", {})
                            tool_status = state.get("status", "")
                            if tool_status == "completed":
                                output = state.get("output", "")
                                short_output = output[:80] if output else "完成"
                                yield {"type": "tool_status", "tool": tool_name, "status": "completed", "detail": short_output}
                            elif tool_status == "running":
                                if tool_name == "question":
                                    yield {"type": "tool_status", "tool": tool_name, "status": "running", "detail": "等待用户输入"}
                                else:
                                    yield {"type": "tool_status", "tool": tool_name, "status": "running"}
                            elif tool_status == "pending":
                                yield {"type": "tool_status", "tool": tool_name, "status": "pending"}
                            else:
                                yield {"type": "status", "content": f"调用工具: {tool_name}"}

                        print(f"[Chat] Part updated: {part_type} id={part_id[:8] if part_id else '?'}", flush=True)

                    elif evt_type == "message.part.delta":
                        delta = properties.get("delta", "")
                        part_id = properties.get("partID", "")
                        if not delta:
                            continue

                        # 通过 partID 查找实际类型
                        resolved_type = part_types.get(part_id, "")

                        if resolved_type == "text":
                            full_text += delta
                            print(f"[Chat] Delta (text, id={part_id[:8] if part_id else '?'}): {len(delta)} chars", flush=True)
                            yield {"type": "text", "content": delta}
                        elif resolved_type == "reasoning":
                            print(f"[Chat] Delta (reasoning, id={part_id[:8] if part_id else '?'}): {len(delta)} chars → thinking", flush=True)
                            yield {"type": "thinking", "content": delta}
                        else:
                            # partID 未知时用 field 判断：field="text" → text
                            field = properties.get("field", "")
                            if field == "text":
                                full_text += delta
                                print(f"[Chat] Delta (field=text, id={part_id[:8] if part_id else '?'}): {len(delta)} chars", flush=True)
                                yield {"type": "text", "content": delta}
                            else:
                                print(f"[Chat] Delta (unknown, field={field}): {len(delta)} chars → skip", flush=True)

                    elif evt_type == "session.status":
                        status = properties.get("status", {})
                        if status.get("type") == "idle":
                            print(f"[Chat] Session idle, done. Total: {len(full_text)} chars", flush=True)
                            return

        except httpx.RemoteProtocolError as e:
            if full_text:
                print(f"[Chat] SSE closed, got {len(full_text)} chars", flush=True)
            else:
                logger.error("SSE closed without data: %s", e)
                yield {"type": "error", "content": f"SSE 连接错误: {e}"}
        except httpx.TimeoutException:
            if full_text:
                print(f"[Chat] SSE timeout but got {len(full_text)} chars", flush=True)
            else:
                yield {"type": "error", "content": "OpenCode 响应超时"}
        except Exception as e:
            logger.error("Stream error: %s", e, exc_info=True)
            if not full_text:
                yield {"type": "error", "content": f"通信错误: {e}"}
        finally:
            await sse_client.aclose()

    def _build_prompt(self, skill: Skill, user_message: str) -> str:
        is_first = len([m for m in skill.messages if m.role == MessageRole.USER]) <= 1

        # 系统提示词（来自 AGENT_SYSTEM_PROMPT.md）
        system_section = ""
        if self._system_prompt:
            system_section = (
                "<system-prompt>\n"
                f"{self._system_prompt}\n"
                "</system-prompt>\n\n"
            )

        if is_first:
            return (
                system_section +
                "请帮我在当前目录下创建一个 Claude Code Skill。\n\n"
                f"## 用户需求\n{user_message}\n\n"
                "## 要求\n"
                "1. 在当前目录创建 SKILL.md，包含 YAML frontmatter（name, description）\n"
                "2. description 要写得宽泛，包含触发条件\n"
                "3. 包含详细的工作流程和步骤\n"
                "4. 尽量为 Skill 编写 Python 辅助脚本，放在 scripts/ 目录下\n"
                "   - 优先用 Python 脚本实现可自动化的功能（数据处理、API 调用、文件操作等）\n"
                "   - 脚本需包含完整的参数解析、错误处理、类型注解\n"
                "   - 在 SKILL.md 中引用脚本的用法\n"
                "5. 先分析需求，有不明确的地方可以先提问确认，再动手创建文件\n"
            )
        else:
            last_assistant = ""
            for m in reversed(skill.messages):
                if m.role == MessageRole.ASSISTANT:
                    last_assistant = m.content[:300]
                    break

            return (
                system_section +
                "请帮我迭代优化当前目录下的 Skill。\n\n"
                f"## 当前 Skill\n名称：{skill.name}\n描述：{skill.description}\n\n"
                + (f"## 上次对话摘要\n{last_assistant}\n\n" if last_assistant else "")
                + f"## 用户的修改意见\n{user_message}\n\n"
                "请根据反馈直接修改 SKILL.md 文件。"
            )

    # ── CRUD ──────────────────────────────────────────

    def get_skill(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    async def delete_skill(self, skill_id: str) -> bool:
        skill = self._skills.get(skill_id)
        if not skill:
            return False

        # 删除 workspace 目录
        if skill.workspace:
            try:
                shutil.rmtree(skill.workspace, ignore_errors=True)
            except Exception as e:
                logger.error("Failed to delete workspace: %s", e)

        del self._skills[skill_id]
        return True

    def get_messages(self, skill_id: str) -> list[Message]:
        skill = self._skills.get(skill_id)
        return skill.messages if skill else []


def _extract_skill_name(message: str) -> str:
    import re
    cleaned = re.sub(r"skills?", "", message, flags=re.IGNORECASE)
    cleaned = cleaned.replace("技能", "").strip()
    cleaned = re.sub(r"[，。、！？,.!?\s]+$", "", cleaned)
    return cleaned[:30] if cleaned else uuid.uuid4().hex[:6]


skill_manager = SkillManager()
