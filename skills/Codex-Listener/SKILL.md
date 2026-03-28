---
name: codex
description: Delegate coding tasks to OpenAI Codex via codex-listener
allowed-tools: Bash(python3 *)
---

# Codex Skill

Delegate coding tasks to OpenAI Codex CLI through the codex-listener daemon.

## Rules

1. **提交后收口**：`submit.py` 成功后，当前轮只需回复"已提交 + task_id + next_action"即停止。不空转、不追加 status 查询、不虚构任务号。
2. **No polling**：除非用户主动问，不调用 `status.py` / `list_tasks.py`。
3. **通知自动推送**：完成通知走配置的消息通道（Telegram/微信），你不会收到结果——直接 move on。
4. **仅用 `scripts/submit.py`**：不用 `/submit` 或其他非官方路径。状态唯一来源是 `/tasks` API。
5. **不编造错误原因**：查询失败时原样返回 error，不猜测"权限受限"。不加 `2>/dev/null`、`|| echo` 等管道篡改输出。不检查 `.codex/sessions` 推断状态。
6. **系统任务含验收**：安装/服务/权限类 prompt 须包含"执行 + 验证 + 返回验证输出"。需要 full access 时显式传 `--sandbox danger-full-access`。
7. **复杂任务先 PlanMode**：写/删 ≥2 / 系统级改动 → 用 `--workflow-mode plan_bridge`。
8. **Plan Bridge 输出契约**：planning 任务须以 `{"bridge":"planmode.v1","stage":"needs_input"|"plan_ready",...}` 结尾。
9. **Plan Bridge 跟进**：用户问链进展时，先 `list_tasks.py` 找最新叶子 descendant，以叶子状态作答。
10. **JSON 直出**：脚本输出足够回答时直接引用；用户要原始 JSON 就直接贴。

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

# 2. 立刻回复用户：已提交、task_id、下一步（复用 next_action）。
# 3. Done. Move on. The user will be notified through configured channels.
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

Options: `--prompt` (required), `--model`, `--cwd`, `--sandbox`, `--reasoning-effort` (high/medium/low), `--workflow-mode`, `--resume-session`, `--parent-task-id`

### Cancel / Health / Status

```bash
python3 scripts/cancel.py --task-id <id>
python3 scripts/health.py
python3 scripts/status.py --task-id <id>
python3 scripts/list_tasks.py
```

Status handling:
- Success: report conclusion directly from JSON. Prefer compact: status + next step + key error/next_action.
- Error: copy `error` verbatim, then `health.py` once.
- User wants raw JSON: paste it directly.

## Plan Bridge Handling

- `bridge_stage=needs_input`: ask user for answers. Preferred: `/plan-reply <task_id> <answer>`.
- 用户跟进链进展 → `list_tasks.py` → 找最新叶子 → 以叶子状态回答。
- 提交后不立刻轮询；让通知通道回推。

### Telegram 交互

- `/tasks` 展示全局任务概览（待回答/可执行/进行中/已完成）
- `/plan-reply` 仅显示 `needs_input` 任务
- `/plan-run` 先选权限（Sandbox / Full Access）再确认执行
- `/plan-cancel` 显示可取消列表
- 普通任务未提供 sandbox → listener 返回 `needs_input` 权限问题
- 回复 Codex-Listener 通知消息可自动绑定 task_id
- Natural-language reply 仅在恰好一条 pending `needs_input` 时生效
- 按钮语义：`✍️ 回复问题` = 预填 plan-reply / `✅ 执行计划` = 需二次确认 / `❌ 取消` = 终止

## Result Templates

- `submit.py` → `已提交任务 <task_id>。<next_action 人话说明>`
- `status.py` → `任务 <task_id> 当前状态：<status>。下一步：<next>`
- `list_tasks.py` → 回答最关心的 1 个结论；链跟进回答最新叶子。

## Output Format

All scripts output JSON to stdout. Exit 0 = success, 1 = error.
