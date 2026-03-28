"""Codex process lifecycle and state management."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
import signal
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
import textwrap

import yaml

from codex_listener.models import TaskCreate, TaskStatus

logger = logging.getLogger(__name__)

PERMISSION_GATE_TIMEOUT_SECONDS = 900
PERMISSION_GATE_SESSION_PREFIX = "perm:"
PERMISSION_GATE_QUESTION = "请选择本次权限：回复 sandbox 或 full。"
PERMISSION_GATE_INVALID_QUESTION = (
    "未识别权限输入，请回复 sandbox 或 full。"
    "如再次无效将默认使用 sandbox。"
)
PLAN_BRIDGE_PROTOCOL = textwrap.dedent(
    """
    You are operating in Plan Bridge mode.
    Do not execute commands, edit files, or start implementation.
    Your final answer must be exactly one JSON object, without markdown fences.

    If more information or confirmation is required, return:
    {"bridge":"planmode.v1","stage":"needs_input","questions":["question 1","question 2"]}

    If the plan is ready for execution, return:
    {"bridge":"planmode.v1","stage":"plan_ready","plan_markdown":"human-readable markdown plan"}

    Rules:
    - Always set bridge to "planmode.v1".
    - Use only one of the two stages above.
    - questions must be a JSON array of concise strings.
    - plan_markdown must be concise markdown for humans to review in chat.
    - Do not include extra prose before or after the JSON object.
    """
).strip()


@dataclass
class PermissionGateContext:
    """In-memory context for a pending permission selection."""

    prompt: str
    model: str
    cwd: str
    full_auto: bool
    reasoning_effort: str
    invalid_replies: int = 0
    timeout_task: asyncio.Task[None] | None = None


class TaskManager:
    """Manages Codex CLI subprocess lifecycle and task state."""

    def __init__(
        self,
        max_concurrent: int = 4,
        max_completed: int = 50,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_completed = max_completed
        self._tasks: dict[str, TaskStatus] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._completed: OrderedDict[str, TaskStatus] = OrderedDict()
        self._bg_tasks: dict[str, asyncio.Task[None]] = {}
        self._permission_gates: dict[str, PermissionGateContext] = {}

    @property
    def active_count(self) -> int:
        return sum(
            1 for t in self._tasks.values() if t.status in ("pending", "running")
        )

    def _gen_task_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def get_task(self, task_id: str) -> TaskStatus | None:
        task = self._tasks.get(task_id) or self._completed.get(task_id)
        if task is not None:
            self._maybe_backfill_bridge_payload(task)
        return task

    def list_tasks(self, status_filter: str | None = None) -> list[TaskStatus]:
        all_tasks = list(self._tasks.values()) + list(self._completed.values())
        for task in all_tasks:
            self._maybe_backfill_bridge_payload(task)
        if status_filter:
            all_tasks = [t for t in all_tasks if t.status == status_filter]
        return sorted(all_tasks, key=lambda t: t.created_at, reverse=True)

    async def create_task(self, req: TaskCreate) -> TaskStatus:
        """Create and start a new Codex task."""
        if self._is_permission_reply(req):
            return await self._handle_permission_reply(req)

        if self._is_stale_permission_reply(req):
            raise RuntimeError(
                "Permission gate is no longer active. Please resubmit the original task."
            )

        if self._should_open_permission_gate(req):
            return await self._create_permission_gate(req, parent_task_id=req.parent_task_id)

        return await self._enqueue_execution_task(req)

    async def _enqueue_execution_task(self, req: TaskCreate) -> TaskStatus:
        """Create a pending task and launch codex execution in background."""
        if self.active_count >= self.max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self.max_concurrent}) reached. "
                "Cancel or wait for a task to finish."
            )

        task_id = self._gen_task_id()
        now = datetime.now(timezone.utc)
        task = TaskStatus(
            task_id=task_id,
            status="pending",
            created_at=now,
            workflow_mode=req.workflow_mode,
            parent_task_id=req.parent_task_id,
        )
        self._tasks[task_id] = task

        bg = asyncio.create_task(self._run_task(task_id, req))
        self._bg_tasks[task_id] = bg
        return task

    async def cancel_task(self, task_id: str) -> TaskStatus | None:
        """Cancel a running or pending task."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if task.status not in ("pending", "running"):
            return task

        proc = self._processes.get(task_id)
        if proc and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
                logger.info("Sent SIGTERM to task %s (pid %d)", task_id, proc.pid)
            except ProcessLookupError:
                pass

        # The _run_task coroutine will handle cleanup when the process exits.
        # But if it was still pending (never started), mark it directly.
        if task.status == "pending":
            task.status = "failed"
            task.error = "Cancelled before starting"
            task.completed_at = datetime.now(timezone.utc)
            self._archive_task(task_id)

        return task

    async def shutdown(self) -> None:
        """Cancel all running tasks and wait for them to finish."""
        for task_id in list(self._tasks):
            await self.cancel_task(task_id)

        for gate_task_id in list(self._permission_gates):
            self._close_permission_gate(gate_task_id)

        # Wait for all background tasks to finish
        bg_tasks = list(self._bg_tasks.values())
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)

    def _is_permission_reply(self, req: TaskCreate) -> bool:
        """Check whether this request is a reply for a permission gate task."""
        parent = (req.parent_task_id or "").strip()
        if not parent:
            return False
        if parent not in self._permission_gates:
            return False
        return req.workflow_mode == "plan_bridge"

    def _is_stale_permission_reply(self, req: TaskCreate) -> bool:
        """Check whether request looks like a reply to an expired permission gate."""
        if req.workflow_mode != "plan_bridge":
            return False
        parent = (req.parent_task_id or "").strip()
        if not parent:
            return False
        parent_task = self.get_task(parent)
        if parent_task is None:
            return False
        session_id = str(parent_task.session_id or "")
        return session_id.startswith(PERMISSION_GATE_SESSION_PREFIX)

    def _should_open_permission_gate(self, req: TaskCreate) -> bool:
        """Decide whether this request must pass permission selection first."""
        if req.workflow_mode != "normal":
            return False
        if req.resume_session_id:
            return False
        sandbox = (req.sandbox or "").strip()
        return sandbox == ""

    def _extract_user_answer(self, prompt: str) -> str:
        """Best-effort extraction of user answer from nanobot-generated prompts."""
        text = (prompt or "").strip()
        if not text:
            return ""
        markers = ("用户回答：", "User answer:", "User response:", "Answer:")
        for marker in markers:
            idx = text.rfind(marker)
            if idx >= 0:
                return text[idx + len(marker):].strip()
        return text

    def _parse_permission_choice(self, answer: str) -> str | None:
        """Parse sandbox/full choice from user answer text."""
        raw = (answer or "").strip()
        if not raw:
            return None
        lower = raw.lower()
        has_full = bool(
            re.search(r"\b(full|danger-full-access|danger_full_access)\b", lower)
            or "全权限" in raw
            or "高权限" in raw
        )
        has_sandbox = bool(
            re.search(r"\b(sandbox|workspace-write|workspace_write|workspace)\b", lower)
            or "沙箱" in raw
        )
        if has_full and not has_sandbox:
            return "danger-full-access"
        if has_sandbox and not has_full:
            return "workspace-write"
        return None

    def _close_permission_gate(self, task_id: str) -> None:
        """Remove permission gate context and cancel its timeout watcher."""
        ctx = self._permission_gates.pop(task_id, None)
        if not ctx or not ctx.timeout_task:
            return
        if not ctx.timeout_task.done():
            ctx.timeout_task.cancel()

    async def _expire_permission_gate(self, task_id: str) -> None:
        """Fail gate task if user does not choose permission in time."""
        try:
            await asyncio.sleep(PERMISSION_GATE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return

        ctx = self._permission_gates.pop(task_id, None)
        if ctx is None:
            return

        task = self.get_task(task_id)
        if task is None:
            return
        if task.status != "completed" or task.bridge_stage != "needs_input":
            return

        task.status = "failed"
        task.error = (
            f"Permission selection timed out after {PERMISSION_GATE_TIMEOUT_SECONDS} seconds."
        )
        task.bridge_stage = "none"
        task.bridge_questions = None
        task.completed_at = datetime.now(timezone.utc)
        task.output = "权限选择超时，任务已取消。"
        await self._notify(task)

    async def _create_permission_gate(
        self,
        req: TaskCreate,
        *,
        parent_task_id: str | None,
        invalid_replies: int = 0,
        question: str = PERMISSION_GATE_QUESTION,
    ) -> TaskStatus:
        """Create a synthetic needs_input task to ask user for permission mode."""
        task_id = self._gen_task_id()
        now = datetime.now(timezone.utc)
        task = TaskStatus(
            task_id=task_id,
            status="completed",
            output=question,
            created_at=now,
            completed_at=now,
            workflow_mode="plan_bridge",
            parent_task_id=parent_task_id,
            session_id=f"{PERMISSION_GATE_SESSION_PREFIX}{task_id}",
            bridge_stage="needs_input",
            bridge_questions=[question],
            bridge_plan=None,
        )

        self._tasks[task_id] = task
        self._archive_task(task_id)

        ctx = PermissionGateContext(
            prompt=req.prompt,
            model=req.model,
            cwd=req.cwd,
            full_auto=req.full_auto,
            reasoning_effort=req.reasoning_effort,
            invalid_replies=invalid_replies,
        )
        timeout_task = asyncio.create_task(
            self._expire_permission_gate(task_id),
            name=f"permission-gate-timeout-{task_id}",
        )
        ctx.timeout_task = timeout_task
        self._permission_gates[task_id] = ctx

        await self._notify(task)
        return task

    async def _handle_permission_reply(self, req: TaskCreate) -> TaskStatus:
        """Handle /plan-reply style continuation for permission gate tasks."""
        parent_task_id = (req.parent_task_id or "").strip()
        ctx = self._permission_gates.get(parent_task_id)
        if ctx is None:
            raise RuntimeError(f"Permission gate context not found for task {parent_task_id}")

        answer = self._extract_user_answer(req.prompt)
        sandbox = self._parse_permission_choice(answer)

        if sandbox is None and ctx.invalid_replies == 0:
            self._close_permission_gate(parent_task_id)
            follow_req = TaskCreate(
                prompt=ctx.prompt,
                model=ctx.model,
                cwd=ctx.cwd,
                sandbox=None,
                full_auto=ctx.full_auto,
                reasoning_effort=ctx.reasoning_effort,
                workflow_mode="normal",
                parent_task_id=parent_task_id,
            )
            return await self._create_permission_gate(
                follow_req,
                parent_task_id=parent_task_id,
                invalid_replies=1,
                question=PERMISSION_GATE_INVALID_QUESTION,
            )

        if sandbox is None:
            sandbox = "workspace-write"
            logger.warning(
                "Permission reply still invalid for gate %s, fallback to sandbox",
                parent_task_id,
            )

        self._close_permission_gate(parent_task_id)
        run_req = TaskCreate(
            prompt=ctx.prompt,
            model=ctx.model,
            cwd=ctx.cwd,
            sandbox=sandbox,
            full_auto=ctx.full_auto,
            reasoning_effort=ctx.reasoning_effort,
            workflow_mode="normal",
            parent_task_id=parent_task_id,
        )
        return await self._enqueue_execution_task(run_req)

    async def _run_task(self, task_id: str, req: TaskCreate) -> None:
        """Spawn codex subprocess and monitor its output."""
        task = self._tasks[task_id]
        cmd = self._build_command(req)

        logger.info("Starting task %s: %s", task_id, " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=req.cwd,
            )
        except FileNotFoundError:
            task.status = "failed"
            task.error = "codex CLI not found. Is it installed and on PATH?"
            task.completed_at = datetime.now(timezone.utc)
            self._archive_task(task_id)
            await self._notify(task)
            return
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            task.completed_at = datetime.now(timezone.utc)
            self._archive_task(task_id)
            await self._notify(task)
            return

        self._processes[task_id] = proc
        task.status = "running"
        task.pid = proc.pid

        # Read JSONL output and extract the final agent message/session id
        output, session_id = await self._read_codex_output(proc)

        exit_code = await proc.wait()
        task.exit_code = exit_code
        task.completed_at = datetime.now(timezone.utc)

        if exit_code == 0:
            task.status = "completed"
            task.output = output
            task.session_id = session_id
        else:
            task.status = "failed"
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            stderr_text = stderr_bytes.decode(errors="replace").strip()
            task.error = (
                output or stderr_text or f"Exited with code {exit_code}"
            )

        summary = self._enrich_task_from_session(task)
        if req.workflow_mode == "plan_bridge":
            bridge_payload = self._extract_bridge_payload(task.output)
            if bridge_payload:
                self._apply_bridge_payload(task, bridge_payload)

        logger.info(
            "Task %s finished: status=%s exit_code=%s",
            task_id,
            task.status,
            exit_code,
        )

        self._archive_task(task_id)
        self._processes.pop(task_id, None)
        self._bg_tasks.pop(task_id, None)

        await self._notify(task, summary=summary)

    async def _notify(self, task: TaskStatus, summary: object | None = None) -> None:
        """Send notifications (Feishu/Telegram) with session details after task completion."""
        from codex_listener.config import get_feishu_config, get_telegram_config, get_qq_config
        from codex_listener.channels.feishu import send_feishu_notification
        from codex_listener.channels.telegram import send_telegram_notification
        from codex_listener.session_parser import get_session_summary

        feishu_cfg = get_feishu_config()
        telegram_cfg = get_telegram_config()
        qq_cfg = get_qq_config()

        if feishu_cfg is None and telegram_cfg is None and qq_cfg is None:
            return

        is_permission_timeout_failure = (
            task.status == "failed"
            and bool(task.error)
            and str(task.error).startswith("Permission selection timed out")
            and str(task.session_id or "").startswith(PERMISSION_GATE_SESSION_PREFIX)
        )

        # Parse the session JSONL to get detailed results unless this is a synthetic timeout failure.
        if summary is None and not is_permission_timeout_failure:
            summary = get_session_summary(task.created_at, task.completed_at)
        logger.info(
            "Notify task %s: summary=%s assistant_msg_len=%s",
            task.task_id,
            summary is not None,
            len(summary.last_assistant_message)
            if summary and summary.last_assistant_message
            else 0,
        )

        if is_permission_timeout_failure:
            assistant_msg = (
                "失败原因：权限选择超时，15 分钟内未回复 sandbox/full，"
                "任务已自动取消。"
            )
        else:
            assistant_msg = summary.last_assistant_message if summary else task.output
        completed_at = (
            summary.completed_at
            if summary
            else (task.completed_at.isoformat() if task.completed_at else None)
        )

        # Send Feishu notification
        if feishu_cfg is not None:
            logger.info("Sending Feishu notification for task %s", task.task_id)
            try:
                await send_feishu_notification(
                    config=feishu_cfg,
                    task_id=task.task_id,
                    status=task.status,
                    assistant_message=assistant_msg,
                    total_tokens=(
                        summary.total_tokens if summary else None
                    ),
                    input_tokens=(
                        summary.input_tokens if summary else None
                    ),
                    output_tokens=(
                        summary.output_tokens if summary else None
                    ),
                    reasoning_tokens=(
                        summary.reasoning_tokens if summary else None
                    ),
                    completed_at=completed_at,
                )
            except Exception:
                logger.exception(
                    "Feishu notification failed for task %s", task.task_id,
                )

        # Send Telegram notification
        if telegram_cfg is not None:
            logger.info("Sending Telegram notification for task %s", task.task_id)
            try:
                await send_telegram_notification(
                    config=telegram_cfg,
                    task_id=task.task_id,
                    status=task.status,
                    workflow_mode=task.workflow_mode,
                    assistant_message=assistant_msg,
                    error_reason=task.error if task.status == "failed" else None,
                    total_tokens=(
                        summary.total_tokens if summary else None
                    ),
                    input_tokens=(
                        summary.input_tokens if summary else None
                    ),
                    output_tokens=(
                        summary.output_tokens if summary else None
                    ),
                    reasoning_tokens=(
                        summary.reasoning_tokens if summary else None
                    ),
                    completed_at=completed_at,
                    bridge_stage=task.bridge_stage,
                    bridge_questions=task.bridge_questions,
                    bridge_plan=task.bridge_plan,
                )
            except Exception:
                logger.exception(
                    "Telegram notification failed for task %s", task.task_id,
                )

        # Send QQ notification
        if qq_cfg is not None:
            logger.info("Sending QQ notification for task %s", task.task_id)
            try:
                try:
                    from codex_listener.channels.qq import send_qq_notification
                except ModuleNotFoundError as e:
                    logger.warning(
                        "QQ notification skipped for task %s: missing dependency (%s)",
                        task.task_id,
                        e,
                    )
                else:
                    await send_qq_notification(
                        config=qq_cfg,
                        task_id=task.task_id,
                        status=task.status,
                        assistant_message=assistant_msg,
                        total_tokens=(
                            summary.total_tokens if summary else None
                        ),
                        input_tokens=(
                            summary.input_tokens if summary else None
                        ),
                        output_tokens=(
                            summary.output_tokens if summary else None
                        ),
                        reasoning_tokens=(
                            summary.reasoning_tokens if summary else None
                        ),
                        completed_at=completed_at,
                    )
            except Exception:
                logger.exception(
                    "QQ notification failed for task %s", task.task_id,
                )

    async def _read_codex_output(
        self, proc: asyncio.subprocess.Process,
    ) -> tuple[str | None, str | None]:
        """Read codex --json stdout, returning (last_message, session_id)."""
        if proc.stdout is None:
            return None, None

        last_message: str | None = None
        session_id: str | None = None

        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            text = line.decode(errors="replace").strip()
            if not text:
                continue

            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("Non-JSON line from codex: %s", text[:200])
                continue

            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id.strip():
                    session_id = thread_id.strip()

            # Extract final message from codex JSONL events.
            # Different codex versions emit either `item.completed` with embedded
            # message content, or `response_item` assistant messages.
            if (
                event.get("type") == "item.completed"
                and isinstance(event.get("item"), dict)
            ):
                item = event["item"]
                msg = ""
                if item.get("type") == "message":
                    content_parts = item.get("content", [])
                    texts = [
                        p.get("text", "")
                        for p in content_parts
                        if isinstance(p, dict) and p.get("type") == "output_text"
                    ]
                    msg = "\n".join(texts).strip()
                elif item.get("type") == "agent_message":
                    msg = str(item.get("text", "")).strip()
                if msg:
                    last_message = msg
                continue

            if (
                event.get("type") == "response_item"
                and isinstance(event.get("payload"), dict)
                and event["payload"].get("type") == "message"
                and event["payload"].get("role") == "assistant"
            ):
                content_parts = event["payload"].get("content", [])
                texts = [
                    p.get("text", "")
                    for p in content_parts
                    if isinstance(p, dict) and p.get("type") == "output_text"
                ]
                msg = "\n".join(texts).strip()
                if msg:
                    last_message = msg

        return last_message, session_id

    def _enrich_task_from_session(self, task: TaskStatus) -> object | None:
        """Fill task fields from session summary when available."""
        from codex_listener.session_parser import get_session_summary

        summary = get_session_summary(task.created_at, task.completed_at)
        if summary is None:
            return None

        if not task.session_id and summary.session_id:
            task.session_id = summary.session_id

        if (not task.output) and summary.last_assistant_message:
            task.output = summary.last_assistant_message

        return summary

    def _extract_bridge_payload(self, text: str | None) -> dict[str, object] | None:
        """Extract bridge payload from assistant output text."""
        if not text:
            return None

        candidates: list[dict[str, object]] = []
        decoder = json.JSONDecoder()

        # 1) Direct JSON message
        raw = text.strip()
        if raw.startswith("{") and raw.endswith("}"):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    candidates.append(obj)
            except json.JSONDecodeError:
                pass

        # 2) JSON fenced blocks
        for block in re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE):
            try:
                obj = json.loads(block.strip())
                if isinstance(obj, dict):
                    candidates.append(obj)
            except json.JSONDecodeError:
                continue

        # 3) planmode.v1 fenced YAML blocks
        for block in re.findall(r"```planmode\.v1\s*([\s\S]*?)```", text, re.IGNORECASE):
            try:
                obj = yaml.safe_load(block.strip())
            except yaml.YAMLError:
                continue
            if isinstance(obj, dict):
                candidates.append(obj)

        # 4) Any raw JSON objects in free text
        idx = 0
        while idx < len(text):
            start = text.find("{", idx)
            if start < 0:
                break
            try:
                obj, consumed = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                idx = start + 1
                continue
            if isinstance(obj, dict):
                candidates.append(obj)
            idx = start + consumed

        for obj in candidates:
            normalized = self._normalize_bridge_payload(obj)
            if normalized:
                return normalized

        return None

    def _normalize_bridge_payload(
        self,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Normalize historical bridge payload variants to the canonical shape."""
        if payload.get("bridge") == "planmode.v1":
            stage = str(payload.get("stage") or "").strip().lower()
            if stage in {"needs_input", "plan_ready"}:
                return {
                    "bridge": "planmode.v1",
                    "stage": stage,
                    "questions": payload.get("questions"),
                    "plan_markdown": payload.get("plan_markdown", payload.get("plan")),
                }

        nested = payload.get("planmode.v1")
        if isinstance(nested, dict):
            return self._normalize_legacy_bridge_payload(nested)

        return self._normalize_legacy_bridge_payload(payload)

    def _normalize_legacy_bridge_payload(
        self,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Best-effort normalization for historical JSON/YAML plan bridge outputs."""
        stage = self._infer_bridge_stage(payload)
        if stage is None:
            return None

        if stage == "needs_input":
            questions = self._extract_bridge_questions(payload)
            return {
                "bridge": "planmode.v1",
                "stage": "needs_input",
                "questions": questions,
            }

        return {
            "bridge": "planmode.v1",
            "stage": "plan_ready",
            "plan_markdown": self._extract_bridge_plan_markdown(payload),
        }

    def _infer_bridge_stage(self, payload: dict[str, object]) -> str | None:
        """Infer plan bridge stage from canonical or legacy payload fields."""
        explicit = str(payload.get("stage") or payload.get("status") or "").strip().lower()
        if explicit in {"needs_input", "plan_ready"}:
            return explicit

        questions = self._extract_bridge_questions(payload)
        if questions:
            return "needs_input"

        if bool(payload.get("ready_to_execute")):
            return "plan_ready"

        if bool(payload.get("execute_after_confirmation")):
            return "plan_ready"

        if any(payload.get(key) for key in ("plan_markdown", "plan", "steps", "acceptance_criteria", "pass_criteria")):
            return "plan_ready"

        return None

    def _extract_bridge_questions(self, payload: dict[str, object]) -> list[str]:
        """Collect human questions from legacy plan payloads."""
        questions: list[str] = []
        for key in ("questions", "pending_user_confirmation", "inputs_needed"):
            questions.extend(self._collect_named_strings(payload, key))

        unique: list[str] = []
        seen: set[str] = set()
        for question in questions:
            normalized = " ".join(question.split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return unique

    def _collect_named_strings(self, node: object, name: str) -> list[str]:
        """Recursively collect string-like values from matching keys."""
        found: list[str] = []

        if isinstance(node, dict):
            for key, value in node.items():
                if key == name:
                    found.extend(self._coerce_string_list(value))
                if isinstance(value, (dict, list)):
                    found.extend(self._collect_named_strings(value, name))
            return found

        if isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    found.extend(self._collect_named_strings(item, name))
            return found

        return found

    def _coerce_string_list(self, value: object) -> list[str]:
        """Best-effort conversion of structured values to human-readable strings."""
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, list):
            items: list[str] = []
            for item in value:
                items.extend(self._coerce_string_list(item))
            return items
        if isinstance(value, dict):
            lines: list[str] = []
            for key, item in value.items():
                nested = self._coerce_string_list(item)
                if nested:
                    if len(nested) == 1:
                        lines.append(f"{key}: {nested[0]}")
                    else:
                        lines.append(f"{key}: {'; '.join(nested)}")
            return lines
        if isinstance(value, (int, float, bool)):
            return [str(value)]
        return []

    def _extract_bridge_plan_markdown(self, payload: dict[str, object]) -> str:
        """Convert legacy structured plans into concise markdown."""
        plan_markdown = payload.get("plan_markdown")
        if isinstance(plan_markdown, str) and plan_markdown.strip():
            return plan_markdown.strip()

        raw_plan = payload.get("plan")
        if isinstance(raw_plan, str) and raw_plan.strip():
            return raw_plan.strip()

        lines: list[str] = []

        goal = self._first_string(payload, "goal", "summary", "objective", "title")
        if goal:
            lines.append(f"目标：{goal}")

        assumptions = self._collect_top_level_items(payload.get("assumptions"))
        if assumptions:
            lines.append("")
            lines.append("假设：")
            lines.extend(f"- {item}" for item in assumptions[:5])

        constraints = self._collect_top_level_items(payload.get("constraints"))
        if constraints:
            lines.append("")
            lines.append("约束：")
            lines.extend(f"- {item}" for item in constraints[:5])

        risks = self._collect_top_level_items(payload.get("risks") or payload.get("uncertainties"))
        if risks:
            lines.append("")
            lines.append("风险/不确定性：")
            lines.extend(f"- {item}" for item in risks[:5])

        steps = payload.get("steps") or payload.get("plan")
        if isinstance(steps, list) and steps:
            lines.append("")
            lines.append("步骤：")
            for index, step in enumerate(steps[:8], 1):
                lines.extend(self._render_plan_step(step, index))

        acceptance = self._collect_top_level_items(
            payload.get("acceptance_criteria") or payload.get("pass_criteria")
        )
        if acceptance:
            lines.append("")
            lines.append("验收标准：")
            lines.extend(f"- {item}" for item in acceptance[:6])

        if not lines:
            return json.dumps(payload, ensure_ascii=False, indent=2)
        return "\n".join(lines).strip()

    def _first_string(self, payload: dict[str, object], *keys: str) -> str | None:
        """Return the first non-empty string for given keys."""
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _collect_top_level_items(self, value: object) -> list[str]:
        """Flatten a top-level structured section into short bullet items."""
        if value is None:
            return []
        if isinstance(value, list):
            items: list[str] = []
            for item in value:
                items.extend(self._collect_top_level_items(item))
            return items
        if isinstance(value, dict):
            items: list[str] = []
            for key, item in value.items():
                nested = self._collect_top_level_items(item)
                if nested:
                    if len(nested) == 1:
                        items.append(f"{key}: {nested[0]}")
                    else:
                        items.append(f"{key}: {'; '.join(nested)}")
                elif isinstance(item, (str, int, float, bool)):
                    items.append(f"{key}: {item}")
            return items
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, (int, float, bool)):
            return [str(value)]
        return []

    def _render_plan_step(self, step: object, index: int) -> list[str]:
        """Render one structured plan step into short markdown lines."""
        if isinstance(step, str):
            text = step.strip()
            return [f"{index}. {text}"] if text else []

        if not isinstance(step, dict):
            return [f"{index}. {json.dumps(step, ensure_ascii=False)}"]

        step_id = step.get("id")
        name = self._first_string(step, "name", "title")
        label = name or f"步骤 {step_id or index}"
        purpose = self._first_string(step, "purpose", "goal", "description")
        header = f"{index}. {label}"
        if purpose:
            header = f"{header} — {purpose}"
        lines = [header]

        inputs_needed = self._collect_top_level_items(step.get("inputs_needed"))
        if inputs_needed:
            lines.append(f"   - 需要输入：{'；'.join(inputs_needed[:3])}")

        commands = self._collect_top_level_items(step.get("commands") or step.get("command"))
        if commands:
            lines.append(f"   - 命令：{commands[0]}")

        decision = self._collect_top_level_items(step.get("decision"))
        if decision:
            lines.append(f"   - 分支：{'；'.join(decision[:2])}")

        expected = self._collect_top_level_items(
            step.get("expected") or step.get("output") or step.get("verify")
        )
        if expected:
            lines.append(f"   - 预期：{'；'.join(expected[:2])}")

        return lines

    def _maybe_backfill_bridge_payload(self, task: TaskStatus) -> None:
        """Retrofit bridge fields for historical tasks that were parsed as none."""
        if task.workflow_mode != "plan_bridge":
            return
        if task.bridge_stage != "none":
            return
        bridge_payload = self._extract_bridge_payload(task.output)
        if bridge_payload:
            self._apply_bridge_payload(task, bridge_payload)

    def _apply_bridge_payload(
        self,
        task: TaskStatus,
        payload: dict[str, object],
    ) -> None:
        """Map plan bridge payload to persisted task fields."""
        stage = payload.get("stage")
        if stage == "needs_input":
            qs = payload.get("questions")
            if isinstance(qs, list):
                task.bridge_questions = [str(q).strip() for q in qs if str(q).strip()]
            else:
                task.bridge_questions = []
            task.bridge_plan = None
            task.bridge_stage = "needs_input"
            return

        if stage == "plan_ready":
            plan = payload.get("plan_markdown")
            if plan is None:
                # Compatibility fallback: some models return `plan` instead of
                # `plan_markdown` in plan_ready payloads.
                plan = payload.get("plan")
            if isinstance(plan, dict):
                task.bridge_plan = json.dumps(plan, ensure_ascii=False, indent=2)
            elif plan is None:
                task.bridge_plan = ""
            else:
                task.bridge_plan = str(plan).strip()
            task.bridge_questions = None
            task.bridge_stage = "plan_ready"
            return

        task.bridge_stage = "none"
        task.bridge_questions = None
        task.bridge_plan = None

    def _build_command(self, req: TaskCreate) -> list[str]:
        """Build the codex exec command line."""
        sandbox = (req.sandbox or "workspace-write").strip() or "workspace-write"
        if req.resume_session_id:
            cmd = [
                "codex",
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "--model", req.model,
                "-c", f"model_reasoning_effort=\"{req.reasoning_effort}\"",
            ]
        else:
            cmd = [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--model", req.model,
                "--sandbox", sandbox,
                "-c", f"model_reasoning_effort=\"{req.reasoning_effort}\"",
            ]
        if req.full_auto:
            # NOTE: `codex exec resume` does not support `--sandbox`.
            # Keep behavior consistent by mapping sandbox intent to supported flags.
            if sandbox == "workspace-write":
                cmd.append("--full-auto")
            elif sandbox == "danger-full-access":
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                cmd.extend(["-c", "approval_policy=\"never\""])
        if req.resume_session_id:
            cmd.append(req.resume_session_id)
        prompt = req.prompt
        if req.workflow_mode == "plan_bridge":
            prompt = self._wrap_plan_bridge_prompt(prompt)
        cmd.append(prompt)
        return cmd

    def _wrap_plan_bridge_prompt(self, prompt: str) -> str:
        """Append a strict output contract for plan bridge tasks."""
        base_prompt = (prompt or "").strip()
        if not base_prompt:
            return PLAN_BRIDGE_PROTOCOL
        return f"{base_prompt}\n\n{PLAN_BRIDGE_PROTOCOL}"

    def _archive_task(self, task_id: str) -> None:
        """Move a finished task from active to completed history."""
        task = self._tasks.pop(task_id, None)
        if task is None:
            return
        self._completed[task_id] = task
        # Evict oldest if over limit
        while len(self._completed) > self.max_completed:
            evicted_task_id, _ = self._completed.popitem(last=False)
            self._close_permission_gate(evicted_task_id)
