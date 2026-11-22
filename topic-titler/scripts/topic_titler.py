#!/usr/bin/env python3
"""ZCode 话题自动命名：Stop Hook 后台用独立快模型维护稳定的 emoji 话题标题。

移植自 oil-oil/oil-codex-title（MIT），按 ZCode 的机制适配：
- 改名通道：短事务更新 ~/.zcode/cli/db/db.sqlite 的 session 表（title_source=generated）。
- 命名模型：GLM Flash 直连 chat completions（凭据复用本机 ZCode 配置，不硬编码密钥）。
- 钩子协议：ZCode 的 Stop 钩子总是内联执行（async 字段无运行时效果），
  因此 hook 进程读 stdin 后立即把工作转交给后台 worker 再退出，不阻塞会话。

Hook 始终只以空输出、退出码 0 结束；所有决策结果写本地审计日志。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = PLUGIN_ROOT / "prompts" / "naming.md"
ZCODE_HOME = Path(os.environ.get("ZCODE_HOME", str(Path.home() / ".zcode")))
DB_PATH = ZCODE_HOME / "cli" / "db" / "db.sqlite"
TASK_INDEX_PATH = ZCODE_HOME / "v2" / "tasks-index.sqlite"  # 桌面端话题列表数据源
DATA_DIR = ZCODE_HOME / "topic-titler"

DEFAULTS = {
    "enabled": True,
    "early_title": True,
    "model": "glm-4.5-flash",
    "api_base": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": None,
    "recent_turns": 5,
    "max_context_chars": 12000,
    "timeout_seconds": 60,
    "worker_settle_seconds": 2.0,
}
EMOJI = ("🎬", "🧩", "🔎", "📝", "📅", "🎨", "⚙️", "💬")
POLICY_VERSION = 2
CONFIRMATIONS = {"好", "好的", "可以", "收到", "谢谢", "继续", "ok", "okay", "thanks", "thank you", "continue"}
LOG_ROTATE_BYTES = 256 * 1024
USAGE_PATH = DATA_DIR / "logs" / "usage.json"


# ---------------------------------------------------------------- 基础工具

def read_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 文件必须是对象：{path}")
    return value


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def audit(session_id: str, result: dict) -> None:
    """决策审计日志：不含对话原文与提示词，只记录结论与用量。"""
    path = DATA_DIR / "logs" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
        os.replace(path, path.with_suffix(".previous.jsonl"))
    entry = {"time": int(time.time()), **result}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def bump_usage(usage: dict) -> None:
    if not usage:
        return
    try:
        total = read_json(USAGE_PATH, default={"total": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}})
        acc = total.setdefault("total", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        acc["calls"] += 1
        acc["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        acc["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        atomic_json(USAGE_PATH, total)
    except Exception:
        pass


def load_config() -> dict:
    config = dict(DEFAULTS)
    config.update(read_json(DATA_DIR / "config.json"))
    if not isinstance(config["enabled"], bool):
        raise ValueError("enabled 必须是布尔值")
    for key, low, up in (("recent_turns", 3, 8), ("max_context_chars", 4000, 20000),
                         ("timeout_seconds", 10, 110), ("worker_settle_seconds", 0.5, 10)):
        if not isinstance(config[key], (int, float)) or not low <= config[key] <= up:
            raise ValueError(f"{key} 必须在 {low}～{up} 之间")
    if not str(config["model"]).strip():
        raise ValueError("model 不能为空")
    return config


@contextmanager
def session_lock(session_id: str):
    """同会话后台任务互斥；拿不到锁直接放弃本轮（不排队）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    path = DATA_DIR / "locks" / f"{safe}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def state_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    return DATA_DIR / "threads" / f"{safe}.json"


# ---------------------------------------------------------------- 数据库

def db_connect(write: bool = False) -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"未找到 ZCode 会话数据库：{DB_PATH}")
    uri = f"file:{DB_PATH}" if write else f"file:{DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def read_session_row(con, session_id: str) -> Optional[dict]:
    row = con.execute(
        "SELECT id, title, title_source, title_message_id, directory, time_deleted "
        "FROM session WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "title": row[1] or "", "title_source": row[2] or "",
        "title_message_id": row[3], "directory": row[4] or "", "deleted": bool(row[5]),
    }


def read_history(con, session_id: str) -> list:
    """按用户消息切轮；过滤宿主注入（synthetic / runtime 元数据）。"""
    parts_by_msg: dict = {}
    for mid, data in con.execute(
        "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY sequence", (session_id,)
    ):
        try:
            part = json.loads(data)
        except ValueError:
            continue
        if part.get("type") == "text" and not part.get("synthetic"):
            text = (part.get("text") or "").strip()
            if text:
                parts_by_msg.setdefault(mid, []).append(text)

    turns: list = []
    current = None
    for mid, data in con.execute(
        "SELECT id, data FROM message WHERE session_id = ? ORDER BY sequence", (session_id,)
    ):
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if msg.get("synthetic"):
            continue
        meta = msg.get("metadata") or {}
        if meta.get("runtimeMessage") or meta.get("source") in ("hook_context", "environment"):
            continue
        texts = parts_by_msg.get(mid, [])
        if not texts:
            continue
        role = msg.get("role")
        if role == "user":
            current = {"user": texts, "assistant": [], "msg_ids": [mid]}
            turns.append(current)
        elif role == "assistant" and current is not None:
            current["assistant"].extend(texts)
            current["msg_ids"].append(mid)
    return [t for t in turns if any(u.strip() for u in t["user"])]


def project_hint(directory: str) -> str:
    if not directory:
        return ""
    path = Path(directory)
    if path == Path.home() or path.parent.name.lower() in ("users", "home"):
        return ""
    name = path.name
    if name.lower() in ("desktop", "documents", "downloads", "tmp", "project", "projects"):
        return ""
    return name[:64] if re.fullmatch(r"[\w .-]{1,64}", name) else ""


def conflicting_titles(con, session_id: str, candidate: str) -> list:
    rows = con.execute(
        "SELECT id FROM session WHERE id != ? AND title = ? AND time_deleted IS NULL LIMIT 5",
        (session_id, candidate),
    ).fetchall()
    return [r[0] for r in rows]


def sync_task_index(session_id: str, title: str, title_source: str) -> None:
    """同步桌面端话题列表数据源（tasks-index.sqlite 的 tasks 表）。

    外部直写 db.sqlite 不会触发 app 内部的 v4→task index 同步与
    task_title_changed 事件，不同步则标题显示有分钟级时滞，且可能被
    mirror/reconcile 对账的内存旧值覆盖。失败静默，不影响命名主流程。"""
    if not TASK_INDEX_PATH.exists():
        return
    try:
        con = sqlite3.connect(f"file:{TASK_INDEX_PATH}", uri=True, timeout=2)
        try:
            con.execute("PRAGMA busy_timeout=2000")
            if title_source == "custom":
                # 用户显式改名：解除旧的自动值并标记 overridden，UI 端即被尊重。
                con.execute(
                    "UPDATE tasks SET title = ?, cli_title = ?, cli_title_source = ?, "
                    "cli_updated_at = ?, title_overridden = 1 WHERE task_id = ?",
                    (title, title, title_source, int(time.time() * 1000), session_id),
                )
            else:
                # 自动命名：仅在用户未手动固定标题（title_overridden=0）时覆盖。
                con.execute(
                    "UPDATE tasks SET title = ?, cli_title = ?, cli_title_source = ?, "
                    "cli_updated_at = ? WHERE task_id = ? AND title_overridden = 0",
                    (title, title, title_source, int(time.time() * 1000), session_id),
                )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def rename_session_row(con, session_id: str, title: str, title_message_id: Optional[str],
                       title_source: str = "generated") -> None:
    """写回 session 表；对瞬时写冲突（readonly/locked）短退避重试后放行。"""
    for attempt in range(3):
        try:
            con.execute(
                "UPDATE session SET title = ?, title_source = ?, title_message_id = ?, "
                "time_title_updated = ? WHERE id = ?",
                (title, title_source, title_message_id, int(time.time() * 1000), session_id),
            )
            con.commit()
            sync_task_index(session_id, title, title_source)
            return
        except sqlite3.OperationalError as exc:
            if attempt == 2 or not ("readonly" in str(exc) or "locked" in str(exc)):
                raise
            time.sleep(1.0 + attempt)


def latest_session_id(con, cwd: Optional[str] = None) -> Optional[str]:
    if cwd:
        row = con.execute(
            "SELECT id FROM session WHERE time_deleted IS NULL AND directory LIKE ? "
            "ORDER BY time_updated DESC LIMIT 1", (cwd.rstrip("/") + "%",)
        ).fetchone()
        if row:
            return row[0]
    row = con.execute(
        "SELECT id FROM session WHERE time_deleted IS NULL ORDER BY time_updated DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- 快照与状态

def build_context(config: dict, session_row: dict, turns: list) -> tuple:
    effective = [
        {"user": "\n".join(t["user"])[:1200], "assistant": "\n".join(t["assistant"])[:500]}
        for t in turns
    ]
    recent = effective[-config["recent_turns"]:]
    total = sum(len(t["user"]) + len(t["assistant"]) for t in recent)
    if total > config["max_context_chars"]:
        ratio = max(0.2, config["max_context_chars"] / total)
        for t in recent:
            t["user"] = t["user"][:max(200, int(len(t["user"]) * ratio))]
            t["assistant"] = t["assistant"][:max(120, int(len(t["assistant"]) * ratio))]
    original = next((t["user"] for t in effective if t["user"]), "")[:800]
    context = {
        "current_title": session_row["title"],
        "project_hint": project_hint(session_row["directory"]),
        "original_goal": original,
        "recent_turns": recent,
    }
    # 指纹与模型上下文同用截断文本，长对话下保持稳定且体积可控。
    recent_raw = [{"user": t["user"], "assistant": t["assistant"]} for t in recent]
    return context, recent_raw


def fingerprint(recent_raw: list) -> str:
    signature = json.dumps(
        {"policy": POLICY_VERSION, "turns": recent_raw},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(signature.encode()).hexdigest()


def is_confirmation_turn(text: str) -> bool:
    return text.strip(" \t\r\n.!。！").casefold() in CONFIRMATIONS


def is_wellformed_title(title: str) -> bool:
    """核心结构把关：emoji 开头 + 恰好一个全角「｜」且两侧非空。"""
    if not title or not any(title.startswith(e + " ") for e in EMOJI):
        return False
    body = title.split(" ", 1)[1]
    if body.count("｜") != 1 or "|" in body:
        return False
    return all(part and part == part.strip() for part in body.split("｜"))


def validate_candidate(candidate, current_title: str) -> dict:
    if not isinstance(candidate, dict) or set(candidate) != {"action", "title", "reason"}:
        raise ValueError("模型输出字段无效")
    if candidate["action"] not in ("keep", "rename") or not all(
        isinstance(candidate[k], str) for k in ("title", "reason")
    ):
        raise ValueError("模型输出类型无效")
    if candidate["action"] == "keep":
        return {**candidate, "title": current_title}
    title = candidate["title"]
    if title != title.strip() or not 4 <= len(title) <= 48:
        raise ValueError("标题长度或空白无效")
    if not any(title.startswith(e + " ") for e in EMOJI):
        raise ValueError("标题缺少允许的类别 emoji")
    body = title.split(" ", 1)[1]
    if body.count("｜") != 1 or "|" in body:
        raise ValueError("标题必须采用对象｜目标结构")
    if any(not part or part != part.strip() for part in body.split("｜")):
        raise ValueError("标题对象与目标不能为空或带边缘空格")
    if any(e in body for e in EMOJI) or any(
        0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF for c in body
    ):
        raise ValueError("标题正文不能包含类别 emoji")
    if any(unicodedata.category(c).startswith("C") for c in title):
        raise ValueError("标题含控制字符")
    if any(x in title for x in ("\n", "\r", "`", "https://", "http://", "@", "/Users/", "sk-")):
        raise ValueError("标题含不允许的格式或私人信息")
    return {**candidate, "reason": candidate["reason"][:300]}


def confirmation_skip(turns: list, state: dict) -> int:
    """基线之后的纯确认轮（最多连续 2 轮）直接保留标题，不再调模型。"""
    if not state.get("last_fingerprint"):
        return 0
    last_user_id = state.get("last_user_msg_id")
    if not last_user_id:
        return 0
    start = next((i for i, t in enumerate(turns) if last_user_id in t["msg_ids"]), None)
    if start is None:
        return 0
    pending = turns[start + 1:]
    if not pending or len(pending) > 2:
        return 0
    for t in pending:
        joined = "\n".join(t["user"])
        if not is_confirmation_turn(joined):
            return 0
    return len(pending)


# ---------------------------------------------------------------- 命名模型

def find_api_key(config: dict):
    """密钥优先级：插件配置 > 环境变量 > ZCode config.json 里 bigmodel MCP 的明文 key。"""
    if config.get("api_key"):
        return str(config["api_key"]).strip(), "plugin-config"
    env_key = os.environ.get("TOPIC_TITLER_API_KEY")
    if env_key:
        return env_key.strip(), "environment"
    zcode_config = ZCODE_HOME / "cli" / "config.json"
    try:
        servers = (read_json(zcode_config).get("mcp") or {}).get("servers") or {}
    except Exception:
        return None, None
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        url = str(server.get("url") or "")
        if "bigmodel.cn" not in url:
            continue
        auth = str((server.get("headers") or {}).get("Authorization") or "")
        key = auth.split(" ", 1)[1] if " " in auth else auth
        if key.strip():
            return key.strip(), f"zcode-mcp:{name}"
    return None, None


def call_model(config: dict, context: dict, extra: Optional[dict] = None) -> tuple:
    key, source = find_api_key(config)
    if not key:
        raise RuntimeError("未找到可用的 API key（doctor 查看来源与配置方法）")
    payload_context = {**context, **(extra or {})}
    body = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps(payload_context, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
        "thinking": {"type": "disabled"},
    }
    request = urllib.request.Request(
        config["api_base"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=config["timeout_seconds"]) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    usage = data.get("usage") or {}
    usage = {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}
    usage["latency_ms"] = int((time.monotonic() - started) * 1000)
    usage["key_source"] = source
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=0)
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        raise ValueError("模型输出不是 JSON")
    return json.loads(match.group(0)), usage


# ---------------------------------------------------------------- 主流程

def process_session(session_id: str, config: dict, apply: bool) -> dict:
    with db_connect(write=apply) as con:
        session_row = read_session_row(con, session_id)
        if session_row is None:
            return {"status": "not_found"}
        if session_row["deleted"]:
            return {"status": "deleted"}
        turns = read_history(con, session_id)
        if not turns:
            return {"status": "empty", "title": session_row["title"]}
        context, recent_raw = build_context(config, session_row, turns)
        fp = fingerprint(recent_raw)
        last_user_msg_id = turns[-1]["msg_ids"][0]
        state = read_json(state_path(session_id))

        state["last_seen_title"] = state.get("last_seen_title") or ""
        if state.get("locked"):
            return {"status": "locked", "title": session_row["title"]}

        # 用户手动改名（title_source=custom 且与基线不一致）→ 锁定保护。
        if (
            session_row["title_source"] == "custom"
            and state["last_seen_title"]
            and state["last_seen_title"] != session_row["title"]
        ):
            if apply:
                state.update(locked=True, lock_reason="检测到手动改名",
                             last_seen_title=session_row["title"])
                atomic_json(state_path(session_id), state)
            return {"status": "manual_title", "title": session_row["title"]}
        # 宿主或本插件外的 generated 标题变化 → 接受为新基线。
        if state["last_seen_title"] and state["last_seen_title"] != session_row["title"]:
            state["last_seen_title"] = session_row["title"]
            state.pop("last_fingerprint", None)
            if apply:
                atomic_json(state_path(session_id), state)

        if state.get("last_fingerprint") == fp:
            return {"status": "unchanged", "title": session_row["title"]}

        skipped = confirmation_skip(turns, state)
        usage: dict = {}
        if skipped:
            candidate = {"action": "keep", "title": session_row["title"],
                         "reason": "新增内容仅为确认，保留稳定标题"}
        else:
            candidate, usage = call_model(config, context)
            bump_usage(usage)
        try:
            candidate = validate_candidate(candidate, session_row["title"])
        except ValueError as exc:
            result = {"status": "invalid_candidate", "error": str(exc), "usage": usage}
            if apply:
                audit(session_id, result)
            return result

        # 实测存在模型对不合结构标题误判 keep 的情形（首轮尤甚），落指纹前做结构把关。
        if candidate["action"] == "keep" and not skipped and not is_wellformed_title(session_row["title"]):
            candidate, retry_usage = call_model(config, context, {
                "naming_feedback": "当前标题不符合「类别 emoji + 对象｜目标」结构，不得 keep；必须按结构给出 rename 新标题。",
            })
            bump_usage(retry_usage)
            usage = {k: (usage.get(k, 0) or 0) + (retry_usage.get(k, 0) or 0) for k in usage.keys() | retry_usage.keys()}
            try:
                candidate = validate_candidate(candidate, session_row["title"])
            except ValueError as exc:
                result = {"status": "invalid_candidate", "error": str(exc), "usage": usage}
                if apply:
                    audit(session_id, result)
                return result

        conflicts = conflicting_titles(con, session_id, candidate["title"])
        if candidate["action"] == "rename" and conflicts and not skipped:
            candidate, retry_usage = call_model(config, context, {
                "conflicting_titles": conflicts,
                "naming_feedback": "候选与已有话题重名。用对话里真实的项目、模块或内容主题区分；无法区分就保留原名，不编造编号。",
            })
            bump_usage(retry_usage)
            candidate = validate_candidate(candidate, session_row["title"])
            usage = {k: (usage.get(k, 0) or 0) + (retry_usage.get(k, 0) or 0) for k in usage.keys() | retry_usage.keys()}
            if candidate["action"] == "rename" and conflicting_titles(con, session_id, candidate["title"]):
                return {"status": "ambiguous_title", "title": session_row["title"], "usage": usage}

        result = {"status": "preview", "action": candidate["action"], "title": candidate["title"],
                  "reason": candidate["reason"], "usage": usage}
        if skipped:
            result["skip_reason"] = "confirmation_only"
        if not apply:
            return result

        # 模型运行期间标题可能已被改动；写入前复核基线。
        fresh = read_session_row(con, session_id)
        if fresh is None or fresh["title"] != session_row["title"]:
            return {"status": "stale_result", "title": fresh["title"] if fresh else None}

        state.update(last_seen_title=session_row["title"], last_user_msg_id=last_user_msg_id,
                     updated_at=int(time.time()), policy_version=POLICY_VERSION)
        if candidate["action"] == "rename" and candidate["title"] != session_row["title"]:
            try:
                rename_session_row(con, session_id, candidate["title"], last_user_msg_id)
            except sqlite3.Error as exc:
                return {"status": "db_error", "error": str(exc)}
            verified = read_session_row(con, session_id)
            if not verified or verified["title"] != candidate["title"]:
                return {"status": "verify_failed", "title": verified["title"] if verified else None}
            state["last_seen_title"] = candidate["title"]
            result.update(status="renamed", title=candidate["title"], verification="db_metadata")
        else:
            result["status"] = "kept"
        state["last_fingerprint"] = fp
        atomic_json(state_path(session_id), state)
        audit(session_id, result)
        return result


# ---------------------------------------------------------------- 子命令

def cmd_hook() -> int:
    if os.environ.get("TOPIC_TITLER_WORKER") == "1":
        return 0
    try:
        raw = sys.stdin.read(1024 * 1024)
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if event.get("hook_event_name") != "Stop" or event.get("stop_hook_active"):
        return 0
    session_id = event.get("session_id")
    if not session_id:
        return 0
    try:
        if not load_config()["enabled"]:
            return 0
    except Exception:
        return 0
    _spawn_worker("worker", session_id)
    return 0


def _spawn_worker(subcommand: str, session_id: str) -> None:
    env = dict(os.environ, TOPIC_TITLER_WORKER="1")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), subcommand, "--session", session_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )


def cmd_early() -> int:
    if os.environ.get("TOPIC_TITLER_WORKER") == "1":
        return 0
    try:
        raw = sys.stdin.read(1024 * 1024)
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    # 入口诊断：区分「钩子未触发」「事件名不匹配」「payload 缺 session_id」（不含对话原文）
    audit("early-entry", {"status": "early_received", "event_keys": sorted(event.keys()),
                          "hook_event_name": event.get("hook_event_name"),
                          "has_session_id": bool(event.get("session_id"))})
    if event.get("hook_event_name") != "UserPromptSubmit":
        return 0
    session_id = event.get("session_id")
    if not session_id:
        audit("early-entry", {"status": "no_session_id"})
        return 0
    try:
        config = load_config()
        if not config["enabled"] or not config.get("early_title"):
            return 0
    except Exception:
        return 0
    _spawn_worker("early-worker", session_id)
    return 0


def cmd_worker(session_id: str) -> int:
    config = load_config()
    time.sleep(float(config["worker_settle_seconds"]))
    with session_lock(session_id) as acquired:
        if not acquired:
            return 0
        try:
            result = process_session(session_id, config, apply=True)
            if result["status"] not in ("renamed", "kept", "locked"):
                audit(session_id, result)
        except Exception as exc:  # 后台 worker 任何失败都不打扰用户
            try:
                audit(session_id, {"status": "error", "error_type": type(exc).__name__,
                                   "message": str(exc)[:200]})
            except Exception:
                pass
    return 0


def process_early(session_id: str, config: dict, apply: bool) -> dict:
    """首条消息提交后立即起初版标题；后续精炼与稳定仍由 Stop 钩子维护。"""
    with db_connect(write=apply) as con:
        session_row = read_session_row(con, session_id)
        if session_row is None or session_row["deleted"]:
            return {"status": "not_found"}
        if session_row["title_source"] == "custom":
            return {"status": "manual_title"}
        state = read_json(state_path(session_id))
        if state.get("locked") or state.get("last_seen_title"):
            return {"status": "already_tracked"}
        turns = read_history(con, session_id)
        if not turns:
            return {"status": "not_ready"}
        first = turns[0]
        # 仅拦截多轮；新话题首条即使助手已开跑（流式入库快），仍基于首条用户消息起初版标题。
        if len(turns) > 1:
            return {"status": "turn_finished"}
        joined = "\n".join(first["user"])
        if len(joined.strip()) < 6 or is_confirmation_turn(joined):
            return {"status": "thin_prompt"}
        context, _ = build_context(config, session_row, turns)
        candidate, usage = call_model(config, context)
        bump_usage(usage)
        try:
            candidate = validate_candidate(candidate, session_row["title"])
        except ValueError as exc:
            return {"status": "invalid_candidate", "error": str(exc), "usage": usage}
        # 与 Stop 一致：模型对不合结构标题误判 keep 时（首轮尤甚），带反馈强制重评一次。
        if candidate["action"] == "keep" and not is_wellformed_title(session_row["title"]):
            candidate, retry_usage = call_model(config, context, {
                "naming_feedback": "当前标题不符合「类别 emoji + 对象｜目标」结构，不得 keep；必须按结构给出 rename 新标题。",
            })
            bump_usage(retry_usage)
            usage = {k: (usage.get(k, 0) or 0) + (retry_usage.get(k, 0) or 0) for k in usage.keys() | retry_usage.keys()}
            try:
                candidate = validate_candidate(candidate, session_row["title"])
            except ValueError as exc:
                return {"status": "invalid_candidate", "error": str(exc), "usage": usage}
        if candidate["action"] != "rename":
            result = {"status": "kept", "title": session_row["title"], "usage": usage}
            if apply:
                audit(session_id, result)  # early 低频，落审计便于排查「为何没起初版标题」
            return result
        result = {"status": "preview", "title": candidate["title"],
                  "reason": candidate["reason"], "usage": usage}
        if not apply:
            return result
        # 模型运行期间标题可能已被改动；写入前复核基线。
        fresh = read_session_row(con, session_id)
        if fresh is None or fresh["title"] != session_row["title"]:
            return {"status": "stale_result"}
        try:
            rename_session_row(con, session_id, candidate["title"], first["msg_ids"][0])
        except sqlite3.Error as exc:
            return {"status": "db_error", "error": str(exc)}
        verified = read_session_row(con, session_id)
        if not verified or verified["title"] != candidate["title"]:
            return {"status": "verify_failed"}
        state.update(last_seen_title=candidate["title"], last_user_msg_id=first["msg_ids"][0],
                     updated_at=int(time.time()), policy_version=POLICY_VERSION)
        atomic_json(state_path(session_id), state)
        result.update(status="provisional", verification="db_metadata")
        audit(session_id, result)
        return result


def cmd_early_worker(session_id: str) -> int:
    try:
        config = load_config()
    except Exception:
        return 0
    time.sleep(float(config["worker_settle_seconds"]))
    with session_lock(session_id) as acquired:
        if not acquired:
            return 0
        try:
            result = process_early(session_id, config, apply=True)
            if result.get("status") in ("db_error", "stale_result", "not_ready", "turn_finished", "thin_prompt", "invalid_candidate"):
                audit(session_id, result)
        except Exception as exc:  # 后台 worker 任何失败都不打扰用户
            try:
                audit(session_id, {"status": "error", "phase": "early",
                                   "error_type": type(exc).__name__, "message": str(exc)[:200]})
            except Exception:
                pass
    return 0


def resolve_session(con, session_arg: Optional[str], cwd: Optional[str]) -> str:
    if session_arg:
        return session_arg
    found = latest_session_id(con, cwd)
    if not found:
        raise RuntimeError("未找到可操作的会话")
    return found


def cmd_preview(args) -> int:
    config = load_config()
    with db_connect() as con:
        session_id = resolve_session(con, args.session, args.cwd)
        result = process_session(session_id, config, apply=False)
    result["session"] = session_id
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in ("preview", "unchanged") else 1


def cmd_rename(args) -> int:
    config = load_config()
    if not args.title:
        # 预览路径只需只读连接。
        with db_connect() as con:
            session_id = resolve_session(con, args.session, args.cwd)
            result = process_session(session_id, config, apply=False)
        result["session"] = session_id
        result["hint"] = "省略 --title 时输出候选；加 --title <新标题> --apply 固定"
        print(json.dumps(result, ensure_ascii=False))
        return 0
    with db_connect(write=True) as con:
        session_id = resolve_session(con, args.session, args.cwd)
        if args.apply:
            # 用户显式指定 = 手动改名：写 title_source=custom 并锁定，避免模型下一轮覆盖。
            session_row = read_session_row(con, session_id)
            rename_session_row(con, session_id, args.title,
                               session_row and session_row["title_message_id"], title_source="custom")
            state = read_json(state_path(session_id))
            state.update(last_seen_title=args.title, last_fingerprint=None,
                         locked=True, lock_reason="用户指定标题")
            atomic_json(state_path(session_id), state)
            audit(session_id, {"status": "manual_rename", "title": args.title, "locked": True})
            print(json.dumps({"session": session_id, "title": args.title, "status": "renamed",
                              "locked": True, "hint": "恢复自动命名：unlock 子命令"},
                             ensure_ascii=False))
            return 0
        session_row = read_session_row(con, session_id)
        print(json.dumps({"session": session_id, "current_title": session_row and session_row["title"],
                          "candidate": args.title, "hint": "加 --apply 写入"}, ensure_ascii=False))
        return 0


def cmd_lock_unlock(args, lock: bool) -> int:
    with db_connect() as con:
        session_id = resolve_session(con, args.session, args.cwd)
        session_row = read_session_row(con, session_id)
    state = read_json(state_path(session_id))
    if lock:
        state.update(locked=True, lock_reason="用户设置",
                     last_seen_title=session_row and session_row["title"])
        state.pop("last_fingerprint", None)
    else:
        state.update(locked=False, lock_reason="")
    atomic_json(state_path(session_id), state)
    print(json.dumps({"session": session_id, "locked": lock}, ensure_ascii=False))
    return 0


def cmd_pause_resume(resume: bool) -> int:
    path = DATA_DIR / "config.json"
    changes = read_json(path)
    changes["enabled"] = resume
    atomic_json(path, changes)
    print(json.dumps({"enabled": resume}, ensure_ascii=False))
    return 0


def cmd_configure(args) -> int:
    path = DATA_DIR / "config.json"
    changes = read_json(path)
    if args.model:
        changes["model"] = args.model
    if args.api_key:
        changes["api_key"] = args.api_key
    if args.api_base:
        changes["api_base"] = args.api_base
    if args.recent_turns:
        changes["recent_turns"] = args.recent_turns
    atomic_json(path, changes)
    config = load_config()
    shown = {k: ("***" if k == "api_key" and config[k] else config[k]) for k in DEFAULTS}
    print(json.dumps(shown, ensure_ascii=False))
    return 0


def cmd_status() -> int:
    config = load_config()
    key, source = find_api_key(config)
    threads = list((DATA_DIR / "threads").glob("*.json")) if (DATA_DIR / "threads").exists() else []
    usage = read_json(USAGE_PATH, default={"total": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}})
    with db_connect() as con:
        session_id = latest_session_id(con)
        session_row = read_session_row(con, session_id) if session_id else None
        count = con.execute("SELECT count(*) FROM session WHERE time_deleted IS NULL").fetchone()[0]
    print(json.dumps({
        "config": {k: ("***" if k == "api_key" and config[k] else config[k]) for k in DEFAULTS},
        "api_key_source": source,
        "db_sessions": count,
        "tracked_sessions": len(threads),
        "latest_session": session_row,
        "usage": usage.get("total"),
    }, ensure_ascii=False))
    return 0


def cmd_doctor(args) -> int:
    report = {"python": sys.version.split()[0], "plugin_root": str(PLUGIN_ROOT),
              "prompt_exists": PROMPT_PATH.exists(), "db_path": str(DB_PATH)}
    config = load_config()
    report["config"] = {k: ("***" if k == "api_key" and config[k] else config[k]) for k in DEFAULTS}
    with db_connect() as con:
        con.execute("SELECT count(*) FROM session")
        session_id = latest_session_id(con)
        row = read_session_row(con, session_id) if session_id else None
        report["db"] = "ok"
        report["latest_session"] = row
        turns = read_history(con, session_id) if session_id else []
        report["latest_history_turns"] = len(turns)
    key, source = find_api_key(config)
    report["api_key_source"] = source
    if args.ping:
        candidate, usage = call_model(config, {
            "current_title": row["title"] if row else "", "project_hint": "",
            "original_goal": "只回复两个字：正常",
            "recent_turns": [{"user": "只回复两个字：正常", "assistant": ""}],
        })
        report["model_ping"] = {"model": config["model"], "latency_ms": usage.get("latency_ms"),
                                "reply": str(candidate)[:80]}
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description="独立模型驱动的 ZCode 话题命名")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="读取 Stop Hook stdin；后台化 worker 后立即退出")
    sub.add_parser("early", help="读取 UserPromptSubmit stdin；首条消息即时起初版标题")
    p = sub.add_parser("worker", help="内部：后台命名 worker")
    p.add_argument("--session", required=True)
    p = sub.add_parser("early-worker", help="内部：初版标题后台 worker")
    p.add_argument("--session", required=True)
    p = sub.add_parser("preview", help="预览最近会话（或指定会话）的候选标题")
    p.add_argument("--session")
    p.add_argument("--cwd")
    p = sub.add_parser("rename", help="查看/设置指定会话标题")
    p.add_argument("--session")
    p.add_argument("--cwd")
    p.add_argument("--title")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("lock", help="固定标题，暂停该会话自动命名")
    p.add_argument("--session")
    p.add_argument("--cwd")
    p = sub.add_parser("unlock", help="恢复该会话自动命名")
    p.add_argument("--session")
    p.add_argument("--cwd")
    sub.add_parser("pause", help="暂停全局自动命名")
    sub.add_parser("resume", help="恢复全局自动命名")
    p = sub.add_parser("configure", help="修改配置（模型/密钥/轮数）")
    p.add_argument("--model")
    p.add_argument("--api-key")
    p.add_argument("--api-base")
    p.add_argument("--recent-turns", type=int)
    sub.add_parser("status", help="查看配置与统计")
    p = sub.add_parser("doctor", help="只读体检；--ping 同时试连命名模型")
    p.add_argument("--ping", action="store_true")
    args = parser.parse_args()

    try:
        if args.command == "hook":
            return cmd_hook()
        if args.command == "early":
            return cmd_early()
        if args.command == "worker":
            return cmd_worker(args.session)
        if args.command == "early-worker":
            return cmd_early_worker(args.session)
        if args.command == "pause":
            return cmd_pause_resume(False)
        if args.command == "resume":
            return cmd_pause_resume(True)
        if args.command == "configure":
            return cmd_configure(args)
        if args.command == "status":
            return cmd_status()
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "preview":
            return cmd_preview(args)
        if args.command == "rename":
            return cmd_rename(args)
        if args.command == "lock":
            return cmd_lock_unlock(args, True)
        if args.command == "unlock":
            return cmd_lock_unlock(args, False)
        return 1
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "message": str(exc)[:300]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
