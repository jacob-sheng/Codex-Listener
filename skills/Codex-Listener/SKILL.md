---
name: codex
description: Delegate coding tasks to OpenAI Codex via codex-listener
allowed-tools: Bash(python3 *)
---

# Codex Skill

Delegate coding tasks to OpenAI Codex CLI through the codex-listener daemon.

## IMPORTANT RULES

1. **Submit 后必须立即收口。** `submit.py` 成功返回 JSON 后，当前轮必须直接回复用户“已提交 + task_id + 下一步”，然后停止；不要空转。
2. **No polling.** Do NOT call `status.py` or `list_tasks.py` after submitting unless the user explicitly asks you to check a task's status.
3. **Notification is automatic.** The daemon will notify the user through the configured messaging channel(s) (Feishu/Telegram) when the task finishes. You will NOT receive the result — just move on.
4. **Use official submit path only.** Always use `scripts/submit.py` (or `POST /tasks` for manual HTTP). Do NOT use `/submit` or any other unofficial endpoint.
5. **Status source of truth.** Task status must come only from `/tasks` APIs via `scripts/status.py` or `scripts/list_tasks.py`.
6. **No inferred excuses.** If status query fails, report the raw JSON error and stop guessing. Do NOT claim "权限受限/系统拦截" unless the tool output explicitly says so.
7. **No shell fallbacks for status.** Do NOT append `2>/dev/null`, `|| echo`, or pipes that change script output.
8. **Do not use artifacts as status proxy.** Do NOT inspect `.codex/sessions` or output files to infer task state unless the user explicitly asks to verify deliverables.
9. **Use explicit sandbox when needed.** Default sandbox follows server-side model defaults (currently `workspace-write`). For system-level tasks, explicitly pass `--sandbox danger-full-access`.
10. **System tasks must include acceptance checks in the same prompt.** For installs/services/users/permissions, require "execute + verify + report verification output".
11. **Complex tasks must enter PlanMode first.** Trigger PlanMode if any of: write/delete files >=2, any delete/overwrite/batch replace, estimated steps >=5, or system-level changes (packages/services/permissions/env).
12. **Use Plan Bridge for multi-turn planning.** Submit stage-A with `--workflow-mode plan_bridge`; if result is `bridge_stage=needs_input`, collect user answers and continue with `--resume-session` (stage-B). Do not execute implementation until plan is ready.
13. **Plan Bridge output contract is strict.** Planning tasks must end with exactly one JSON object: `{"bridge":"planmode.v1","stage":"needs_input","questions":[...]}` or `{"bridge":"planmode.v1","stage":"plan_ready","plan_markdown":"..."}`.
14. **Tool 成功后必须有可见结论。** 任一 `submit.py` / `status.py` / `list_tasks.py` 成功返回 JSON 后，要么继续发起下一次必要脚本调用，要么立刻给出非空用户回复；禁止“结果拿到了但回答为空”。
15. **JSON 结果优先直出。** 如果脚本输出已经足够回答问题，直接基于 JSON 给结论；若用户要求“原样返回 JSON”，就直接返回 JSON 主体，不再改写。
16. **Plan Bridge 跟进看最新叶子任务。** 当用户跟进一条 plan_bridge 任务链的进展时，优先用 `list_tasks.py` 找该链的最新叶子 descendant，并以叶子任务状态作答，同时简要说明与根任务关系。
17. **提交后停手。** 如果 `submit.py` 返回了 `task_id`、`workflow_mode`、`next_action`，正确做法就是确认已提交、给出 `task_id`、提示等待通知或去 `/tasks`；不要在同一轮再虚构任务号、追加状态查询或把自己绕进空回复。

## Prerequisites

The daemon must be running:

```bash
codex-listener start
```

All scripts are in the `scripts/` directory relative to this skill.

## Workflow

```bash
# 1. Submit a task (canonical path: POST /tasks via submit.py)
python3 scripts/submit.py --prompt "fix the type error in auth.py" --cwd /path/to/project
# Returns: {"task_id": "a1b2c3d4", "status": "pending", ...}

# 2. 立刻回复用户：已提交、task_id、下一步（优先复用 `user_message` / `next_action`）。
# 3. Done. Move on to other work. The user will be notified through their configured channels when codex finishes.
# The submit script returns `next_action=wait_for_notification`; treat that as the default happy path.
```

Plan Bridge (two-stage):

```bash
# Stage A: ask planning questions only
python3 scripts/submit.py \
  --workflow-mode plan_bridge \
  --prompt "Complex task: do planning only. If information is missing, return {\"bridge\":\"planmode.v1\",\"stage\":\"needs_input\",\"questions\":[...]}; if the plan is ready, return {\"bridge\":\"planmode.v1\",\"stage\":\"plan_ready\",\"plan_markdown\":\"...\"}. Do not execute implementation." \
  --cwd /home/Hera/.nanobot/workspace

# Stage B: continue same session after user answers
python3 scripts/submit.py \
  --workflow-mode plan_bridge \
  --resume-session <session_id> \
  --parent-task-id <task_id> \
  --prompt "User answers: ... Continue planning only and return one canonical planmode.v1 JSON object." \
  --cwd /home/Hera/.nanobot/workspace
```

System-task prompt template (required):

```text
Install tmux on Debian/Ubuntu, then verify with:
1) tmux -V
2) dpkg -l tmux
Return the exact verification output in your final response.
```

## Scripts

### Submit a task

```bash
python3 scripts/submit.py --prompt "fix the bug in auth.py" --cwd /path/to/project
python3 scripts/submit.py --prompt "refactor this module" --model o3-mini --cwd .
python3 scripts/submit.py --prompt "quick fix" --reasoning-effort low --cwd .
python3 scripts/submit.py --prompt "install tmux and verify with tmux -V + dpkg -l tmux" --cwd /home/Hera/.nanobot/workspace
python3 scripts/submit.py --prompt "code-only task" --sandbox workspace-write --cwd /home/Hera/.nanobot/workspace
python3 scripts/submit.py --workflow-mode plan_bridge --prompt "ask questions first" --cwd /home/Hera/.nanobot/workspace
python3 scripts/submit.py --workflow-mode plan_bridge --resume-session <session_id> --parent-task-id <task_id> --prompt "answers: ..." --cwd /home/Hera/.nanobot/workspace
```

The script above sends requests to `POST /tasks`. Do not hand-write `/submit`.

Options: `--prompt` (required), `--model`, `--cwd`, `--sandbox`, `--reasoning-effort` (high/medium/low, default: high), `--workflow-mode`, `--resume-session`, `--parent-task-id`

### Cancel a task

```bash
python3 scripts/cancel.py --task-id <id>
```

### Health check

```bash
python3 scripts/health.py
```

### Check task status (only when user asks)

```bash
python3 scripts/status.py --task-id <id>
python3 scripts/list_tasks.py
```

Status handling:
- If success: report the conclusion directly from JSON in the same turn.
- Prefer a compact answer: current status, next step, and any key `error` / `next_action` / `user_message`.
- If the user asked for raw JSON, return the JSON body directly.
- If error: copy the `error` field verbatim, then run `python3 scripts/health.py` once and report result.
- Do not switch to guessed narratives.

Plan Bridge handling:
- If `bridge_stage=needs_input`: ask user for answers. Preferred reply format is `/plan-reply <task_id> <answer>`.
- If the user asks “现在呢 / 跑完了吗 / 执行完成了吗” for a plan_bridge chain:
  - First run `python3 scripts/list_tasks.py`
  - Find the latest leaf descendant for that chain
  - Answer based on the leaf task, and briefly mention the root task only as context
- Telegram 常用入口优先使用 `/tasks`，它会展示：
  - 待回答（`needs_input`）
  - 可执行（`plan_ready`）
  - 进行中（`pending/running`）
  - 最近完成/失败
- Telegram 菜单命令可无参选任务：
  - `/plan-reply` 仅显示可回复的 `needs_input`
  - `/plan-run` 仅显示可执行的 `plan_ready`
  - `/plan-cancel` 仅显示可取消任务列表（`pending/running`）
- Plan 执行权限选择：
  - `/plan-run` 先选权限模式（Sandbox 或 Full Access）再二次确认执行
  - 文本兼容：`/plan-run <task_id> sandbox|full`
  - 禁止依赖后端默认 sandbox；执行任务必须显式传权限
- 普通任务权限闸门（listener 侧）：
  - 普通任务若未显式提供 sandbox，listener 会返回 `needs_input` 权限问题
  - 在 Telegram 里用 `/plan-reply <task_id> sandbox|full`（或按钮预填）完成权限选择
  - 仅在权限确定后才会创建真实执行任务
- Natural-language reply is allowed only when there is exactly one pending `needs_input` task; otherwise require explicit `/plan-reply`.
- Replying directly to a Codex-Listener notification message can auto-bind its `Task <task_id>`.
- Continue by resubmitting with `--resume-session <session_id>` and `--parent-task-id <task_id>`.
- 正常提交后不要立刻再跑 `status.py` / `list_tasks.py`；让通知通道回推结果。
- Telegram button semantics:
  - `✍️ 回复问题`: only pre-fills `/plan-reply <task_id> `, user must still send the final answer text.
  - `✅ 执行计划`: requires second confirmation before creating execution task.
  - `📝 继续修改`: route back to `/plan-reply <task_id> ...`.
  - `❌ 取消`: cancel current execution intent and do not auto-submit implementation.

Result-closing templates:

- `submit.py` success:
  - `已提交任务 <task_id>。<user_message 或 next_action 对应的人话说明>`
  - 若 `next_action=wait_for_notification`：明确写“等待通知即可，不要立刻轮询状态”。
- `status.py` success:
  - `任务 <task_id> 当前状态：<status>。下一步：<next step>`
- `list_tasks.py` success:
  - 优先回答用户最关心的 1 个结论；若是任务链跟进，则回答最新叶子任务状态。
  - 不要把列表读完后又不给结论。
- Raw JSON mode:
  - 用户明确要求“原样返回 JSON”时，直接粘贴脚本输出 JSON，不追加其它脚本。

## Output Format

All scripts output a single JSON object to stdout. Exit code 0 = success, 1 = error.

Submitted task:
```json
{"task_id": "a1b2c3d4", "status": "pending", ...}
```

Daemon not running:
```json
{"error": "codex-listener is not running. Start it with: codex-listener start"}
```
