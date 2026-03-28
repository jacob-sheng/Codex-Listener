"""Telegram Bot notification via Bot API (stdlib only)."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from functools import partial

from codex_listener.config import TelegramConfig

logger = logging.getLogger(__name__)
NOTIFIER_NAME = "Codex-Listener"
MAX_ASSISTANT_ESCAPED_LEN = 2800


def _build_api_url(token: str, method: str) -> str:
    """Build Telegram Bot API URL."""
    return f"https://api.telegram.org/bot{token}/{method}"


def _send_message(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict[str, object] | None = None,
    proxy: str | None = None,
) -> bool:
    """Send a text message to a specific chat."""
    url = _build_api_url(token, "sendMessage")
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    body = json.dumps(payload).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # Set up proxy if provided
    if proxy:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(proxy_handler)
        urllib.request.install_opener(opener)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            logger.warning(
                "Telegram send failed to %s: %s",
                chat_id,
                data.get("description"),
            )
            return False
        return True
    except urllib.error.HTTPError as e:
        detail = str(e)
        try:
            payload = json.loads(e.read().decode())
            if isinstance(payload, dict):
                detail = payload.get("description", detail)
        except Exception:
            pass
        logger.warning(
            "Telegram send request failed to %s: %s",
            chat_id,
            detail,
        )
        return False
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.warning("Telegram send request failed to %s: %s", chat_id, e)
        return False


def _escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special_chars = ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


def _escape_and_truncate_markdown_v2(text: str, max_len: int) -> str:
    """Escape text for MarkdownV2 and cap message size safely."""
    escaped = _escape_markdown_v2(text)
    if len(escaped) <= max_len:
        return escaped
    suffix = "\\.\\.\\."
    cut = max(0, max_len - len(suffix))
    return escaped[:cut] + suffix


def _looks_like_permission_gate(bridge_questions: list[str] | None) -> bool:
    """Heuristic: detect permission gate prompts from question content."""
    if not bridge_questions:
        return False
    for question in bridge_questions:
        q = str(question or "").lower()
        if ("sandbox" in q and "full" in q) or ("权限" in question):
            return True
    return False


def _preview_text(text: str | None, *, max_len: int = 700, max_lines: int = 8) -> str | None:
    """Return a short preview suitable for chat notifications."""
    if not text:
        return None
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return None
    preview = "\n".join(lines[:max_lines]).strip()
    if len(preview) > max_len:
        preview = preview[: max_len - 3].rstrip() + "..."
    elif len(lines) > max_lines:
        preview += "\n..."
    return preview


def _build_bridge_markdown_lines(
    task_id: str,
    bridge_stage: str | None,
    bridge_questions: list[str] | None,
    bridge_plan: str | None,
) -> list[str]:
    """Build human-oriented markdown lines for plan bridge tasks."""
    if bridge_stage == "needs_input":
        lines = ["*下一步：补充信息后继续规划*", ""]
        if bridge_questions:
            for index, question in enumerate(bridge_questions, 1):
                lines.append(f"{index}\\. {_escape_markdown_v2(str(question).strip())}")
        else:
            lines.append(_escape_markdown_v2("请补充必要信息后继续。"))
        lines.extend(
            [
                "",
                f"直接回复：`/plan\\-reply {_escape_markdown_v2(task_id)} <你的回答>`",
            ]
        )
        return lines

    if bridge_stage == "plan_ready":
        lines = ["*计划已准备好，等待你决定是否执行*", ""]
        preview = _preview_text(bridge_plan, max_len=900, max_lines=10)
        if preview:
            lines.append(_escape_markdown_v2(preview))
            lines.append("")
        lines.append(
            f"可直接点按钮，或发送：`/plan\\-run {_escape_markdown_v2(task_id)} sandbox|full`"
        )
        return lines

    return [
        "*计划结果已生成，但还未被标准化*",
        _escape_markdown_v2("可查看任务状态或继续回复这条通知来补充计划。"),
    ]


def _build_bridge_plain_lines(
    task_id: str,
    bridge_stage: str | None,
    bridge_questions: list[str] | None,
    bridge_plan: str | None,
) -> list[str]:
    """Build plain-text user-oriented lines for plan bridge tasks."""
    if bridge_stage == "needs_input":
        lines = ["下一步：补充信息后继续规划", ""]
        if bridge_questions:
            lines.extend(f"{index}. {question}" for index, question in enumerate(bridge_questions, 1))
        else:
            lines.append("请补充必要信息后继续。")
        lines.extend(["", f"直接回复：/plan-reply {task_id} <你的回答>"])
        return lines

    if bridge_stage == "plan_ready":
        lines = ["计划已准备好，等待你决定是否执行", ""]
        preview = _preview_text(bridge_plan, max_len=1200, max_lines=10)
        if preview:
            lines.extend(preview.splitlines())
            lines.append("")
        lines.append(f"可直接点按钮，或发送：/plan-run {task_id} sandbox|full")
        return lines

    return [
        "计划结果已生成，但还未被标准化。",
        "可查看任务状态或继续回复这条通知来补充计划。",
    ]


def _build_reply_markup(
    task_id: str,
    bridge_stage: str | None,
    bridge_questions: list[str] | None,
) -> dict[str, object] | None:
    """Build Telegram inline keyboard for Plan Bridge stages."""
    if bridge_stage == "needs_input":
        if _looks_like_permission_gate(bridge_questions):
            return {
                "inline_keyboard": [
                    [
                        {
                            "text": "🧱 沙箱",
                            "switch_inline_query_current_chat": f"/plan-reply {task_id} sandbox",
                        },
                        {
                            "text": "⚠️ Full",
                            "switch_inline_query_current_chat": f"/plan-reply {task_id} full",
                        },
                    ]
                ]
            }
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✍️ 回复问题",
                        "switch_inline_query_current_chat": f"/plan-reply {task_id} ",
                    }
                ]
            ]
        }

    if bridge_stage == "plan_ready":
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ 执行计划",
                        "callback_data": f"pb1|exec|{task_id}",
                    }
                ],
                [
                    {
                        "text": "📝 继续修改",
                        "switch_inline_query_current_chat": f"/plan-reply {task_id} ",
                    },
                    {
                        "text": "❌ 取消",
                        "callback_data": f"pb1|cancel|{task_id}",
                    },
                ],
            ]
        }

    return None


def _build_message(
    task_id: str,
    status: str,
    workflow_mode: str | None,
    assistant_message: str | None,
    error_reason: str | None,
    total_tokens: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
    completed_at: str | None,
    bridge_stage: str | None,
    bridge_questions: list[str] | None,
    bridge_plan: str | None,
) -> str:
    """Build Telegram message with Markdown formatting."""
    is_ok = status == "completed"
    status_emoji = "✅" if is_ok else "❌"
    notifier = _escape_markdown_v2(NOTIFIER_NAME)
    
    lines = [
        f"{status_emoji} *{notifier} Task {_escape_markdown_v2(task_id)}*",
        "",
        f"*Status:* {_escape_markdown_v2(status)}",
    ]
    
    if completed_at:
        lines.append(f"*Completed:* {_escape_markdown_v2(completed_at)}")
    if status == "failed" and error_reason:
        lines.append(f"*Failure Reason:* {_escape_markdown_v2(error_reason)}")
    
    lines.append("")
    lines.append("─" * 30)
    lines.append("")
    
    is_plan_bridge = workflow_mode == "plan_bridge"
    if is_plan_bridge:
        lines.extend(
            _build_bridge_markdown_lines(
                task_id=task_id,
                bridge_stage=bridge_stage,
                bridge_questions=bridge_questions,
                bridge_plan=bridge_plan,
            )
        )
    elif assistant_message:
        escaped_truncated = _escape_and_truncate_markdown_v2(
            assistant_message,
            MAX_ASSISTANT_ESCAPED_LEN,
        )
        lines.append("*Codex Response:*")
        lines.append(escaped_truncated)
    else:
        lines.append("*Codex Response:* \\(none\\)")
    
    lines.append("")
    lines.append("─" * 30)
    lines.append("")
    
    # Token usage
    if total_tokens is not None:
        parts = [f"{total_tokens:,} total"]
        if input_tokens is not None:
            parts.append(f"{input_tokens:,} in")
        if output_tokens is not None:
            parts.append(f"{output_tokens:,} out")
        if reasoning_tokens:
            parts.append(f"{reasoning_tokens:,} reasoning")
        token_text = " / ".join(parts)
        lines.append(f"📊 Token usage: {_escape_markdown_v2(token_text)}")
    
    return "\n".join(lines)


def _build_plain_message(
    task_id: str,
    status: str,
    workflow_mode: str | None,
    assistant_message: str | None,
    error_reason: str | None,
    total_tokens: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
    completed_at: str | None,
    bridge_stage: str | None,
    bridge_questions: list[str] | None,
    bridge_plan: str | None,
) -> str:
    """Build plain-text fallback message for Telegram."""
    is_ok = status == "completed"
    status_emoji = "SUCCESS" if is_ok else "FAILED"
    lines = [
        f"{NOTIFIER_NAME} Task {task_id}",
        f"Status: {status_emoji} ({status})",
    ]
    if completed_at:
        lines.append(f"Completed: {completed_at}")
    if status == "failed" and error_reason:
        lines.append(f"Failure reason: {error_reason}")

    is_plan_bridge = workflow_mode == "plan_bridge"
    lines.append("")
    if is_plan_bridge:
        lines.extend(
            _build_bridge_plain_lines(
                task_id=task_id,
                bridge_stage=bridge_stage,
                bridge_questions=bridge_questions,
                bridge_plan=bridge_plan,
            )
        )
    else:
        lines.append("Codex Response:")
        if assistant_message:
            truncated = assistant_message[:2800]
            if len(assistant_message) > 2800:
                truncated += "\n..."
            lines.append(truncated)
        else:
            lines.append("(none)")

    if total_tokens is not None:
        parts = [f"{total_tokens:,} total"]
        if input_tokens is not None:
            parts.append(f"{input_tokens:,} in")
        if output_tokens is not None:
            parts.append(f"{output_tokens:,} out")
        if reasoning_tokens:
            parts.append(f"{reasoning_tokens:,} reasoning")
        lines.append("")
        lines.append(f"Token usage: {' / '.join(parts)}")

    return "\n".join(lines)


def _do_send(
    config: TelegramConfig,
    task_id: str,
    status: str,
    workflow_mode: str | None,
    assistant_message: str | None,
    error_reason: str | None,
    total_tokens: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
    completed_at: str | None,
    bridge_stage: str | None,
    bridge_questions: list[str] | None,
    bridge_plan: str | None,
) -> None:
    """Synchronous: send message to all recipients."""
    reply_markup = _build_reply_markup(task_id, bridge_stage, bridge_questions)

    markdown_message = _build_message(
        task_id=task_id,
        status=status,
        workflow_mode=workflow_mode,
        assistant_message=assistant_message,
        error_reason=error_reason,
        total_tokens=total_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        completed_at=completed_at,
        bridge_stage=bridge_stage,
        bridge_questions=bridge_questions,
        bridge_plan=bridge_plan,
    )
    plain_message = _build_plain_message(
        task_id=task_id,
        status=status,
        workflow_mode=workflow_mode,
        assistant_message=assistant_message,
        error_reason=error_reason,
        total_tokens=total_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        completed_at=completed_at,
        bridge_stage=bridge_stage,
        bridge_questions=bridge_questions,
        bridge_plan=bridge_plan,
    )

    for chat_id in config.allow_from:
        ok = _send_message(
            config.token,
            chat_id,
            markdown_message,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
            proxy=config.proxy,
        )
        if not ok:
            # Fallback to plain text for markdown parse errors or long entities.
            ok = _send_message(
                config.token,
                chat_id,
                plain_message,
                parse_mode=None,
                reply_markup=reply_markup,
                proxy=config.proxy,
            )
        if ok:
            logger.info("Telegram notification sent to %s", chat_id)


async def send_telegram_notification(
    config: TelegramConfig,
    task_id: str,
    status: str,
    workflow_mode: str | None = None,
    assistant_message: str | None = None,
    error_reason: str | None = None,
    total_tokens: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    completed_at: str | None = None,
    bridge_stage: str | None = None,
    bridge_questions: list[str] | None = None,
    bridge_plan: str | None = None,
) -> None:
    """Async wrapper: run Telegram API calls in thread executor to avoid blocking."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        partial(
            _do_send,
            config=config,
            task_id=task_id,
            status=status,
            workflow_mode=workflow_mode,
            assistant_message=assistant_message,
            error_reason=error_reason,
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            completed_at=completed_at,
            bridge_stage=bridge_stage,
            bridge_questions=bridge_questions,
            bridge_plan=bridge_plan,
        ),
    )
