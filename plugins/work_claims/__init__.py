from __future__ import annotations

import json
import math
import shlex
from typing import Any

from . import core

_CLAIM_TOOLS = {"work_claim_acquire", "work_claim_status", "work_claim_renew", "work_claim_release"}
_ALWAYS_MUTATING = {
    "write_file", "patch", "skill_manage", "memory", "browser_exec", "execute_code",
    "setup_mcp", "project_create", "delegate_task",
}

_MAILBOX_READ_ONLY = (
    "/Users/rook/.hermes/profiles/sophie/integrations/google-workspace-mcp/.venv/bin/python",
    "/Users/rook/.hermes/profiles/sophie/integrations/google-workspace-mcp/run_hourly_triage.py",
)


def _terminal_is_read_only(command: Any) -> bool:
    """Recognise a deliberately small shell-free inspection allowlist."""
    if not isinstance(command, str) or not command.strip():
        return False
    if any(token in command for token in ("\n", ";", "&&", "||", "|", ">", "<", "`", "$(")):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    if tuple(words) == _MAILBOX_READ_ONLY:
        return True
    if words == ["python3", "scripts/validate_vault.py"]:
        return True
    if words[0] == "git" and len(words) >= 2:
        offset = 1
        if words[1] == "-C" and len(words) >= 4:
            offset = 3
        if any(word in {"--ext-diff", "--textconv", "--output"} for word in words[offset + 1:]):
            return False
        return words[offset] in {"status", "diff", "log", "show", "rev-parse"}
    if words[0] != "hermes" or len(words) < 2:
        return False
    family = words[1]
    sub = words[2] if len(words) > 2 else ""
    safe = {
        "sessions": {"list", "search", "export", "show"},
        "skills": {"list", "list-modified", "check", "inspect", "show"},
        "config": {"get", "show", "path", "check"},
        "plugins": {"list", "show", "doctor", "capabilities"},
        "cron": {"list", "status", "runs", "history"},
        "profile": {"list", "show", "info"},
        "kanban": {"list", "show", "stats", "runs", "boards"},
        "project": {"list"},
        "tools": {"", "list"},
        "gateway": {"status"},
        "doctor": {""},
        "status": {""},
    }
    return sub in safe.get(family, set())


def _session_id(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _required_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _required_pid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("pid must be a positive integer")
    return value


def _required_interval(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("renew_interval_seconds must be a positive number")
    interval = float(value)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("renew_interval_seconds must be a positive number")
    return interval


def _acknowledged() -> dict[str, Any]:
    return {"acknowledged": True, "consumer": "work-claims"}


def _is_mutating(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name == "terminal":
        return not _terminal_is_read_only(args.get("command"))
    if tool_name in _ALWAYS_MUTATING:
        if tool_name == "delegate_task" and args.get("action") == "list":
            return False
        return True
    if tool_name == "computer_use":
        return args.get("action") not in {"capture", "list_apps", "list_windows", "cua_browser_state", "wait"}
    if tool_name == "drive_preview":
        return args.get("action") in {"click", "type", "press", "hover"}
    if tool_name == "cronjob":
        return args.get("action") != "list"
    if tool_name == "process":
        return args.get("action") in {"kill", "write", "submit", "close"}
    return False


def _pre_tool_call(tool_name: str = "", args: Any = None, **kwargs: Any):
    """Authorize one tool call, and carry any containment rewrite it needs.

    A dispatcher-scoped worker is gated on *every* tool, not only the ones
    ``_is_mutating`` already recognises: its scope is default-deny, so an
    unclassified mutator must be denied rather than waved through. An
    ordinary session is untouched -- non-mutating calls return immediately,
    exactly as before.
    """
    if tool_name in _CLAIM_TOOLS:
        return None
    safe_args = args if isinstance(args, dict) else {}
    decision = core.pre_tool_decision(
        _session_id(kwargs), tool_name, safe_args,
        mutating=_is_mutating(tool_name, safe_args),
    )
    if not decision.allowed:
        return {
            "action": "block",
            "message": f"work-claims guard blocked {tool_name}: {decision.reason}",
        }
    if decision.modified_args:
        # The rewrite *is* the enforcement (the OS sandbox wrapper), so it
        # travels as a directive the host must apply, not as advice.
        return {"action": "modify", "args": dict(decision.modified_args)}
    return None


def _on_session_finalize(
    session_id: str = "", reason: str = "", **_: Any
) -> dict[str, Any]:
    """Run one atomic finalize decision and act only on an explicit release.

    The hook never releases a claim on its own judgement: it classifies the
    host's finalize *reason* into a durable terminal signal (or not), hands
    that to ``core.finalization_decision``, and completes the Kanban mirror
    only when the decision's disposition is an explicit release. A preserved
    claim -- a live execution turn, or any reason that is not proof the
    conversation ended -- is left untouched and audited.
    """
    session_id = _required_text("session_id", session_id)
    reason = _required_text("reason", reason)
    decision = core.finalization_decision(
        session_id,
        reason,
        core.is_durable_terminal_reason(reason),
        summary="Session finalized; claim released automatically",
    )
    if decision["disposition"] == core.RELEASE:
        core.mirror_released_claim(decision)
    return _acknowledged()


def _on_execution_turn_begin(
    session_id: str = "",
    turn_id: str = "",
    lease_id: str = "",
    holder_token: str = "",
    pid: int | None = None,
    boot_id: str = "",
    renew_interval_seconds: float = 0.0,
    **_: Any,
) -> dict[str, Any]:
    session_id = _required_text("session_id", session_id)
    turn_id = _required_text("turn_id", turn_id)
    lease_id = _required_text("lease_id", lease_id)
    holder_token = _required_text("holder_token", holder_token)
    pid = _required_pid(pid)
    boot_id = _required_text("boot_id", boot_id)
    renew_interval_seconds = _required_interval(renew_interval_seconds)
    core.on_execution_turn_begin(
        session_id=session_id,
        turn_id=turn_id,
        lease_id=lease_id,
        holder_token=holder_token,
        pid=pid,
        boot_id=boot_id,
        renew_interval_seconds=renew_interval_seconds,
    )
    return _acknowledged()


def _on_execution_turn_renew(
    session_id: str = "",
    turn_id: str = "",
    lease_id: str = "",
    holder_token: str = "",
    pid: int | None = None,
    boot_id: str = "",
    renew_interval_seconds: float = 0.0,
    **_: Any,
) -> dict[str, Any]:
    session_id = _required_text("session_id", session_id)
    turn_id = _required_text("turn_id", turn_id)
    lease_id = _required_text("lease_id", lease_id)
    holder_token = _required_text("holder_token", holder_token)
    pid = _required_pid(pid)
    boot_id = _required_text("boot_id", boot_id)
    renew_interval_seconds = _required_interval(renew_interval_seconds)
    core.on_execution_turn_renew(
        session_id=session_id,
        turn_id=turn_id,
        lease_id=lease_id,
        holder_token=holder_token,
        pid=pid,
        boot_id=boot_id,
        renew_interval_seconds=renew_interval_seconds,
    )
    return _acknowledged()


def _on_execution_turn_end(
    session_id: str = "",
    turn_id: str = "",
    lease_id: str = "",
    holder_token: str = "",
    pid: int | None = None,
    boot_id: str = "",
    renew_interval_seconds: float = 0.0,
    outcome: str = "",
    **_: Any,
) -> dict[str, Any]:
    session_id = _required_text("session_id", session_id)
    turn_id = _required_text("turn_id", turn_id)
    lease_id = _required_text("lease_id", lease_id)
    holder_token = _required_text("holder_token", holder_token)
    pid = _required_pid(pid)
    boot_id = _required_text("boot_id", boot_id)
    renew_interval_seconds = _required_interval(renew_interval_seconds)
    outcome = _required_text("outcome", outcome)
    core.on_execution_turn_end(
        session_id=session_id,
        turn_id=turn_id,
        lease_id=lease_id,
        holder_token=holder_token,
        pid=pid,
        boot_id=boot_id,
        renew_interval_seconds=renew_interval_seconds,
        outcome=outcome,
    )
    return _acknowledged()


def _handle_acquire(params: dict[str, Any], **kwargs: Any) -> str:
    result = core.acquire(
        _session_id(kwargs),
        params.get("summary", ""),
        params.get("targets") or [],
        workspace=params.get("workspace"),
        ttl_minutes=params.get("ttl_minutes", core.DEFAULT_TTL_MINUTES),
        create_worktree=params.get("create_worktree", True),
    )
    return json.dumps(result, sort_keys=True)


def _handle_status(params: dict[str, Any], **kwargs: Any) -> str:
    del params
    claim = core.active_claim(_session_id(kwargs))
    return json.dumps({"success": True, "active": bool(claim), "claim": claim}, sort_keys=True)


def _handle_renew(params: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps(core.renew(_session_id(kwargs), params.get("ttl_minutes", core.DEFAULT_TTL_MINUTES)), sort_keys=True)


def _handle_release(params: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps(core.release(_session_id(kwargs), params.get("summary", "Work verified and claim released")), sort_keys=True)


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def register(ctx):
    ctx.register_system_prompt_section(
        "work-claims.guardrails",
        """Before any material mutation, acquire one cross-session claim with work_claim_acquire. Use stable targets such as repo:/absolute/path, project:slug, external:codevolt-vps, or system:hermes-profile. Include every shared resource the work can change. If a Git primary checkout is supplied as workspace, the claim tool creates an isolated worktree; perform every file and terminal mutation using the returned absolute workspace. A target already held by another session fails closed. Renew long work and release only after verification. Kanban workers are already atomically claimed and do not need a second claim.""",
        max_chars=1200,
    )
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_hook("on_execution_turn_begin", _on_execution_turn_begin)
    ctx.register_hook("on_execution_turn_renew", _on_execution_turn_renew)
    ctx.register_hook("on_execution_turn_end", _on_execution_turn_end)
    ctx.register_tool(
        name="work_claim_acquire", toolset="work_claims",
        schema=_schema(
            "work_claim_acquire",
            "Atomically reserve shared targets for this Hermes session, mirror the claim to Kanban, and create an isolated Git worktree when needed.",
            {
                "summary": {"type": "string", "description": "Short bounded workstream description."},
                "targets": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 16, "description": "Shared resources: repo:/abs/path, project:name, external:name, or system:name."},
                "workspace": {"type": "string", "description": "Optional absolute Git workspace to isolate."},
                "ttl_minutes": {"type": "integer", "minimum": 15, "maximum": 720, "default": 240},
                "create_worktree": {"type": "boolean", "default": True},
            },
            ["summary", "targets"],
        ),
        handler=_handle_acquire,
    )
    ctx.register_tool(
        name="work_claim_status", toolset="work_claims",
        schema=_schema("work_claim_status", "Read this session's active cross-session work claim.", {}),
        handler=_handle_status,
    )
    ctx.register_tool(
        name="work_claim_renew", toolset="work_claims",
        schema=_schema("work_claim_renew", "Extend this session's active work-claim TTL.", {"ttl_minutes": {"type": "integer", "minimum": 15, "maximum": 720, "default": 240}}),
        handler=_handle_renew,
    )
    ctx.register_tool(
        name="work_claim_release", toolset="work_claims",
        schema=_schema("work_claim_release", "Release this session's work claim after verification and complete its Kanban mirror.", {"summary": {"type": "string"}}),
        handler=_handle_release,
    )
