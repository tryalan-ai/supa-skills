#!/usr/bin/env python3
"""Normalize local agent CLI transcript logs into provider-neutral events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

SessionFormat = Literal[
    "auto",
    "codex-jsonl",
    "generic-jsonl",
    "claude-jsonl",
    "copilot-jsonl",
    "cursor-jsonl",
    "droid-jsonl",
    "gemini-jsonl",
    "kimi-jsonl",
    "opencode-jsonl",
    "supatest-jsonl",
]

SESSION_FORMATS: tuple[SessionFormat, ...] = (
    "auto",
    "codex-jsonl",
    "generic-jsonl",
    "claude-jsonl",
    "copilot-jsonl",
    "cursor-jsonl",
    "droid-jsonl",
    "gemini-jsonl",
    "kimi-jsonl",
    "opencode-jsonl",
    "supatest-jsonl",
)

PROVIDER_KIND_BY_FORMAT: dict[str, str] = {
    "claude-jsonl": "claude_cli",
    "codex-jsonl": "codex_app_server",
    "copilot-jsonl": "copilot_cli",
    "cursor-jsonl": "cursor_agent_cli",
    "droid-jsonl": "droid_cli",
    "gemini-jsonl": "gemini_cli",
    "kimi-jsonl": "kimi_cli",
    "opencode-jsonl": "opencode_cli",
    "supatest-jsonl": "supatest_cli",
}


@dataclass
class NormalizedSessionEvent:
    kind: Literal[
        "metadata",
        "turn_context",
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "log",
    ]
    text: str = ""
    role: str = ""
    tool_name: str = ""
    tool_args: Any = None
    tool_call_id: str = ""
    failed: bool = False
    timestamp: str = ""
    cwd: str = ""
    session_id: str = ""
    provider_kind: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def parse_json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def compact_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    try:
        return json.dumps(value, ensure_ascii=False)[:limit]
    except TypeError:
        return str(value)[:limit]


def parse_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def get_value(record: dict[str, Any], path: Iterable[str]) -> Any:
    value: Any = record
    for segment in path:
        if not isinstance(value, dict):
            return None
        value = value.get(segment)
    return value


def get_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def extract_text(value: Any) -> str:
    return "\n".join(extract_text_parts(value)).strip()


def extract_text_parts(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(extract_text_parts(item))
        return dedupe_text_parts(parts)
    if not isinstance(value, dict):
        return []

    if get_string(value.get("type")) in {"tool_use", "tool_result", "function_call", "function_call_output", "thinking", "redacted_thinking", "reasoning", "think"}:
        return []

    parts: list[str] = []
    for key in (
        "text",
        "content",
        "input_text",
        "output_text",
        "message",
        "delta",
        "deltaContent",
        "finalText",
        "last_agent_message",
        "result",
        "output",
    ):
        parts.extend(extract_text_parts(value.get(key)))
    for path in (
        ("message", "content"),
        ("payload", "content"),
        ("payload", "message"),
        ("payload", "last_agent_message"),
        ("data", "content"),
        ("data", "message"),
        ("data", "deltaContent"),
        ("part", "text"),
    ):
        parts.extend(extract_text_parts(get_value(value, path)))
    return dedupe_text_parts(parts)


def dedupe_text_parts(parts: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = part.strip()
        if not text or text in seen:
            continue
        deduped.append(text)
        seen.add(text)
    return deduped


def extract_timestamp(record: dict[str, Any]) -> str:
    for value in (
        record.get("timestamp"),
        record.get("createdAt"),
        record.get("created_at"),
        get_value(record, ("payload", "timestamp")),
        get_value(record, ("message", "timestamp")),
        get_value(record, ("data", "timestamp")),
        get_value(record, ("data", "createdAt")),
    ):
        text = get_string(value)
        if text:
            return text
    return ""


def extract_role(record: dict[str, Any]) -> str:
    for value in (
        record.get("role"),
        record.get("type"),
        get_value(record, ("message", "role")),
        get_value(record, ("payload", "role")),
        get_value(record, ("data", "role")),
    ):
        role = get_string(value)
        if role in {"user", "assistant"}:
            return role
    record_type = get_string(record.get("type"))
    if record_type in {"user.message", "user.prompt"}:
        return "user"
    if record_type in {"assistant", "assistant.message", "assistant.message_delta", "text", "completion"}:
        return "assistant"
    return ""


def expand_session_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    records = [record]
    set_messages = get_value(record, ("$set", "messages"))
    if isinstance(set_messages, list):
        records.extend(item for item in set_messages if isinstance(item, dict))
    return records


def detect_format(path: Path, record: dict[str, Any] | None) -> SessionFormat:
    normalized = str(path).replace("\\", "/")
    path_parts = {part.lower() for part in normalized.split("/") if part}
    if "/.codex/" in normalized or record_has_codex_shape(record):
        return "codex-jsonl"
    if "/.claude/" in normalized or "claude" in path_parts or record_has_claude_shape(record):
        return "claude-jsonl"
    if "/session-state/" in normalized and normalized.endswith("/events.jsonl"):
        return "copilot-jsonl"
    if "/agent-transcripts/" in normalized:
        return "cursor-jsonl"
    if "/.gemini/" in normalized or "gemini" in path_parts or re.search(r"/chats/session-[^/]+\.jsonl$", normalized):
        return "gemini-jsonl"
    if "/.kimi" in normalized or "kimi" in path_parts or re.search(r"/sessions/.+/(context|wire)\.jsonl$", normalized):
        return "kimi-jsonl"
    if "/opencode" in normalized or "/.opencode/" in normalized or "opencode" in path_parts:
        return "opencode-jsonl"
    if "/.supatest/" in normalized:
        return "supatest-jsonl"
    if "/.factory/" in normalized or "/.droid/" in normalized:
        return "droid-jsonl"
    return "generic-jsonl"


def record_has_codex_shape(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    event_type = get_string(record.get("type"))
    if event_type in {"session_meta", "turn_context", "event_msg", "response_item"}:
        return True
    payload = record.get("payload")
    return isinstance(payload, dict) and get_string(payload.get("type")) in {
        "message",
        "function_call",
        "function_call_output",
        "custom_tool_call",
    }


def record_has_claude_shape(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"} for block in content)


def normalize_format(value: str) -> SessionFormat:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "codex": "codex-jsonl",
        "claude": "claude-jsonl",
        "copilot": "copilot-jsonl",
        "cursor": "cursor-jsonl",
        "droid": "droid-jsonl",
        "gemini": "gemini-jsonl",
        "kimi": "kimi-jsonl",
        "opencode": "opencode-jsonl",
        "supatest": "supatest-jsonl",
        "generic": "generic-jsonl",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SESSION_FORMATS:
        allowed = ", ".join(SESSION_FORMATS)
        raise ValueError(f"Unsupported session format '{value}'. Expected one of: {allowed}")
    return normalized  # type: ignore[return-value]


def parse_session_events(path: Path, session_format: str = "auto") -> list[NormalizedSessionEvent]:
    requested_format = normalize_format(session_format)
    first_record = first_json_record(path)
    detected_format = detect_format(path, first_record) if requested_format == "auto" else requested_format
    provider_kind = PROVIDER_KIND_BY_FORMAT.get(detected_format, "generic_cli")
    events: list[NormalizedSessionEvent] = []

    with path.open(errors="ignore") as handle:
        for line in handle:
            if '"encrypted_content"' in line:
                continue
            record = parse_json_object(line)
            if not record:
                continue
            if detected_format == "codex-jsonl":
                events.extend(parse_codex_record(record, provider_kind))
            else:
                for expanded in expand_session_records(record):
                    events.extend(parse_provider_record(expanded, provider_kind))
    return events


def first_json_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(errors="ignore") as handle:
            for line in handle:
                record = parse_json_object(line)
                if record:
                    return record
    except OSError:
        return None
    return None


def parse_codex_record(record: dict[str, Any], provider_kind: str) -> list[NormalizedSessionEvent]:
    event_type = get_string(record.get("type"))
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    events: list[NormalizedSessionEvent] = []

    if event_type == "session_meta":
        events.append(
            NormalizedSessionEvent(
                kind="metadata",
                timestamp=get_string(record.get("timestamp")),
                cwd=get_string(record.get("cwd")),
                session_id=get_string(record.get("id")),
                provider_kind=provider_kind,
                raw=record,
            )
        )
        return events

    if event_type == "turn_context":
        events.append(
            NormalizedSessionEvent(
                kind="turn_context",
                cwd=get_string(payload.get("cwd")),
                provider_kind=provider_kind,
                raw=record,
            )
        )
        return events

    if event_type == "event_msg":
        msg_type = get_string(payload.get("type"))
        if msg_type == "user_message":
            events.append(
                NormalizedSessionEvent(
                    kind="user_message",
                    text=compact_text(payload.get("message"), 50_000),
                    provider_kind=provider_kind,
                    raw=record,
                )
            )
        elif msg_type in {"mcp_tool_call_end", "tool_call_end"}:
            invocation = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
            result = compact_text(payload.get("result"), 30_000)
            events.append(
                NormalizedSessionEvent(
                    kind="tool_result",
                    text=result,
                    tool_name=get_string(invocation.get("tool")) or get_string(invocation.get("name")) or "tool",
                    failed=looks_failed(result),
                    provider_kind=provider_kind,
                    raw=record,
                )
            )
        return events

    if event_type != "response_item":
        return events

    item_type = get_string(payload.get("type"))
    if item_type in {"agent_message", "assistant_message", "task_complete", "agent_update", "agent_result"}:
        text = extract_text(payload)
        if text:
            events.append(
                NormalizedSessionEvent(
                    kind="assistant_message",
                    text=text,
                    role="assistant",
                    provider_kind=provider_kind,
                    raw=record,
                )
            )
    elif item_type in {"function_call", "custom_tool_call", "tool_search_call", "web_search_call"}:
        tool_name = (
            get_string(payload.get("name"))
            or get_string(payload.get("namespace"))
            or get_string(payload.get("type"))
            or "tool"
        )
        events.append(
            NormalizedSessionEvent(
                kind="tool_call",
                tool_name=tool_name,
                tool_args=parse_maybe_json(payload.get("arguments") or payload.get("input") or payload.get("action")),
                tool_call_id=get_string(payload.get("call_id")),
                provider_kind=provider_kind,
                raw=record,
            )
        )
    elif item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output", "web_search_end"}:
        output = compact_text(payload.get("output") or payload.get("result") or payload, 30_000)
        events.append(
            NormalizedSessionEvent(
                kind="tool_result",
                text=output,
                tool_call_id=get_string(payload.get("call_id")),
                failed=looks_failed(output),
                provider_kind=provider_kind,
                raw=record,
            )
        )
    elif item_type == "message":
        role = get_string(payload.get("role"))
        text = extract_text(payload.get("content"))
        if role == "user" and text:
            events.append(
                NormalizedSessionEvent(kind="user_message", text=text, provider_kind=provider_kind, raw=record)
            )
        elif role == "assistant" and text:
            events.append(
                NormalizedSessionEvent(
                    kind="assistant_message", text=text, provider_kind=provider_kind, raw=record
                )
            )
    return events


def parse_provider_record(record: dict[str, Any], provider_kind: str) -> list[NormalizedSessionEvent]:
    events: list[NormalizedSessionEvent] = []
    timestamp = extract_timestamp(record)
    session_id = (
        get_string(record.get("sessionId"))
        or get_string(record.get("sessionID"))
        or get_string(record.get("session_id"))
        or get_string(get_value(record, ("payload", "id")))
        or get_string(get_value(record, ("payload", "session_id")))
    )
    cwd = (
        get_string(record.get("cwd"))
        or get_string(get_value(record, ("payload", "cwd")))
        or infer_workspace_from_text(extract_text(record))
    )
    if session_id or cwd:
        events.append(
            NormalizedSessionEvent(
                kind="metadata",
                timestamp=timestamp,
                cwd=cwd,
                session_id=session_id,
                provider_kind=provider_kind,
                raw=record,
            )
        )

    events.extend(parse_provider_messages(record, provider_kind, timestamp))
    events.extend(parse_provider_tool_events(record, provider_kind))
    return events


def parse_provider_messages(
    record: dict[str, Any], provider_kind: str, timestamp: str
) -> list[NormalizedSessionEvent]:
    if provider_kind == "opencode_cli" and get_string(record.get("type")) == "text":
        part = record.get("part") if isinstance(record.get("part"), dict) else {}
        content = extract_text(part.get("text"))
        if content:
            return [
                NormalizedSessionEvent(
                    kind="assistant_message",
                    text=content,
                    role="assistant",
                    timestamp=timestamp,
                    provider_kind=provider_kind,
                    raw=record,
                )
            ]

    if provider_kind == "copilot_cli" and get_string(record.get("type")) in {
        "assistant.message_delta",
        "assistant.message",
    }:
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        content = extract_text(data)
        if content:
            return [
                NormalizedSessionEvent(
                    kind="assistant_message",
                    text=content,
                    role="assistant",
                    timestamp=timestamp,
                    provider_kind=provider_kind,
                    raw=record,
                )
            ]

    if provider_kind == "droid_cli" and get_string(record.get("type")) == "completion":
        content = extract_text(record)
        if content:
            return [
                NormalizedSessionEvent(
                    kind="assistant_message",
                    text=content,
                    role="assistant",
                    timestamp=timestamp,
                    provider_kind=provider_kind,
                    raw=record,
                )
            ]

    if provider_kind == "cursor_agent_cli" and get_string(record.get("type")) == "result":
        content = extract_text(record.get("result"))
        if content:
            return [
                NormalizedSessionEvent(
                    kind="assistant_message",
                    text=content,
                    role="assistant",
                    timestamp=timestamp,
                    provider_kind=provider_kind,
                    raw=record,
                )
            ]

    role = extract_role(record)
    if role not in {"user", "assistant"}:
        return []

    content = (
        extract_text(get_value(record, ("message", "content")))
        or extract_text(get_value(record, ("payload", "content")))
        or extract_text(get_value(record, ("data", "content")))
        or extract_text(record.get("content"))
        or extract_text(record.get("text"))
        or extract_text(record.get("message"))
    )
    if not content and provider_kind == "copilot_cli":
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        content = extract_text(data.get("content")) or extract_text(data.get("message")) or extract_text(data.get("prompt"))
    if not content:
        return []
    return [
        NormalizedSessionEvent(
            kind="user_message" if role == "user" else "assistant_message",
            text=content,
            role=role,
            timestamp=timestamp,
            provider_kind=provider_kind,
            raw=record,
        )
    ]


def parse_provider_tool_events(record: dict[str, Any], provider_kind: str) -> list[NormalizedSessionEvent]:
    record_type = get_string(record.get("type"))
    events: list[NormalizedSessionEvent] = []

    if provider_kind == "claude_cli":
        events.extend(parse_claude_tool_events(record))
    elif provider_kind == "kimi_cli":
        events.extend(parse_kimi_tool_events(record))
    elif provider_kind == "opencode_cli":
        events.extend(parse_opencode_tool_events(record))
    elif provider_kind == "copilot_cli":
        events.extend(parse_copilot_tool_events(record))
    elif provider_kind == "cursor_agent_cli":
        events.extend(parse_cursor_tool_events(record))
    elif provider_kind == "gemini_cli":
        events.extend(parse_simple_tool_events(record, "tool_use", "tool_result", "tool_name", "tool_id"))
    elif provider_kind == "droid_cli":
        events.extend(parse_simple_tool_events(record, "tool_call", "tool_result", "toolName", "id"))
    elif record_type in {"tool_use", "tool_call", "tool", "tool.execute"}:
        events.append(
            NormalizedSessionEvent(
                kind="tool_call",
                tool_name=tool_name_from_record(record),
                tool_args=record.get("parameters") or record.get("input") or record.get("data") or record,
                tool_call_id=get_string(record.get("id")) or get_string(record.get("tool_id")),
                provider_kind=provider_kind,
                raw=record,
            )
        )
    elif record_type in {"tool_result", "tool.result"}:
        text = tool_result_text(record)
        events.append(
            NormalizedSessionEvent(
                kind="tool_result",
                text=text,
                tool_call_id=get_string(record.get("id")) or get_string(record.get("tool_id")),
                failed=record.get("is_error") is True or record.get("isError") is True or looks_failed(text),
                provider_kind=provider_kind,
                raw=record,
            )
        )
    return events


def parse_claude_tool_events(record: dict[str, Any]) -> list[NormalizedSessionEvent]:
    events: list[NormalizedSessionEvent] = []
    record_type = get_string(record.get("type"))
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), list) else []
    if record_type == "assistant":
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            events.append(
                NormalizedSessionEvent(
                    kind="tool_call",
                    tool_name=get_string(block.get("name")) or "Tool",
                    tool_args=block.get("input") or {},
                    tool_call_id=get_string(block.get("id")),
                    provider_kind="claude_cli",
                    raw=record,
                )
            )
    elif record_type == "user":
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = extract_text(block.get("content"))
            events.append(
                NormalizedSessionEvent(
                    kind="tool_result",
                    text=text,
                    tool_call_id=get_string(block.get("tool_use_id")),
                    failed=block.get("is_error") is True or looks_failed(text),
                    provider_kind="claude_cli",
                    raw=record,
                )
            )
    return events


def parse_kimi_tool_events(record: dict[str, Any]) -> list[NormalizedSessionEvent]:
    events: list[NormalizedSessionEvent] = []
    role = get_string(record.get("role"))
    content = record.get("content") if isinstance(record.get("content"), list) else []
    tool_calls = record.get("tool_calls") if isinstance(record.get("tool_calls"), list) else []
    if role == "assistant":
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            events.append(
                NormalizedSessionEvent(
                    kind="tool_call",
                    tool_name=get_string(fn.get("name")) or "Tool",
                    tool_args=parse_maybe_json(fn.get("arguments")) if fn else {},
                    tool_call_id=get_string(tool_call.get("id")),
                    provider_kind="kimi_cli",
                    raw=record,
                )
            )
    elif role == "tool":
        text = "\n".join(extract_text(block.get("text")) for block in content if isinstance(block, dict))
        events.append(
            NormalizedSessionEvent(
                kind="tool_result",
                text=text,
                tool_call_id=get_string(record.get("tool_call_id")),
                failed=looks_failed(text),
                provider_kind="kimi_cli",
                raw=record,
            )
        )
    return events


def parse_opencode_tool_events(record: dict[str, Any]) -> list[NormalizedSessionEvent]:
    record_type = get_string(record.get("type"))
    part = record.get("part") if isinstance(record.get("part"), dict) else {}
    if record_type in {"tool_use", "tool.execute", "tool"}:
        state = record.get("state") if isinstance(record.get("state"), dict) else {}
        events = [
            NormalizedSessionEvent(
                kind="tool_call",
                tool_name=tool_name_from_record(record, part),
                tool_args=state.get("input") or part or record,
                tool_call_id=get_string(part.get("id")) or get_string(record.get("id")) or get_string(record.get("callID")),
                provider_kind="opencode_cli",
                raw=record,
            )
        ]
        status = get_string(state.get("status"))
        if record_type == "tool" and status in {"completed", "error"}:
            text = tool_result_text(record, part)
            events.append(
                NormalizedSessionEvent(
                    kind="tool_result",
                    text=text,
                    tool_call_id=get_string(record.get("id")) or get_string(record.get("callID")),
                    failed=status == "error" or looks_failed(text),
                    provider_kind="opencode_cli",
                    raw=record,
                )
            )
        return events
    if record_type in {"tool_result", "tool.result"}:
        text = tool_result_text(record, part)
        return [
            NormalizedSessionEvent(
                kind="tool_result",
                text=text,
                tool_call_id=get_string(part.get("id")) or get_string(record.get("id")),
                failed=part.get("is_error") is True or part.get("isError") is True or looks_failed(text),
                provider_kind="opencode_cli",
                raw=record,
            )
        ]
    return []


def parse_simple_tool_events(
    record: dict[str, Any],
    call_type: str,
    result_type: str,
    name_key: str,
    id_key: str,
) -> list[NormalizedSessionEvent]:
    record_type = get_string(record.get("type"))
    if record_type == call_type:
        return [
            NormalizedSessionEvent(
                kind="tool_call",
                tool_name=get_string(record.get(name_key)) or "Tool",
                tool_args=record.get("parameters") or {},
                tool_call_id=get_string(record.get(id_key)),
                raw=record,
            )
        ]
    if record_type == result_type:
        text = tool_result_text(record)
        return [
            NormalizedSessionEvent(
                kind="tool_result",
                text=text,
                tool_call_id=get_string(record.get(id_key)),
                failed=looks_failed(text),
                raw=record,
            )
        ]
    return []


def parse_copilot_tool_events(record: dict[str, Any]) -> list[NormalizedSessionEvent]:
    record_type = get_string(record.get("type"))
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    if record_type == "tool.execution_start":
        return [
            NormalizedSessionEvent(
                kind="tool_call",
                tool_name=get_string(data.get("toolName")) or "Copilot Tool",
                tool_args=data.get("arguments") or {},
                tool_call_id=get_string(data.get("toolCallId")),
                provider_kind="copilot_cli",
                raw=record,
            )
        ]
    if record_type == "tool.execution_complete":
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        text = (
            extract_text(result.get("content"))
            or extract_text(result.get("detailedContent"))
            or extract_text(data.get("error"))
            or compact_text(data, 3000)
        )
        success = data.get("success") if isinstance(data.get("success"), bool) else True
        return [
            NormalizedSessionEvent(
                kind="tool_result",
                text=text,
                tool_call_id=get_string(data.get("toolCallId")),
                failed=(not success) or looks_failed(text),
                provider_kind="copilot_cli",
                raw=record,
            )
        ]
    return []


def parse_cursor_tool_events(record: dict[str, Any]) -> list[NormalizedSessionEvent]:
    if get_string(record.get("type")) != "tool_call":
        return []
    tool_call = record.get("tool_call") if isinstance(record.get("tool_call"), dict) else {}
    raw_name = ""
    payload: dict[str, Any] = {}
    for key, value in tool_call.items():
        if isinstance(value, dict):
            raw_name = key
            payload = value
            break
    if not raw_name:
        return []
    tool_id = get_string(record.get("call_id"))
    subtype = get_string(record.get("subtype"))
    tool_name = "Bash" if raw_name == "terminalToolCall" else raw_name.removesuffix("ToolCall") or "Cursor Tool"
    if subtype == "completed":
        text = tool_result_text({"result": payload.get("result")})
        return [
            NormalizedSessionEvent(
                kind="tool_result",
                text=text,
                tool_name=tool_name,
                tool_call_id=tool_id,
                failed=looks_failed(text),
                provider_kind="cursor_agent_cli",
                raw=record,
            )
        ]
    return [
        NormalizedSessionEvent(
            kind="tool_call",
            tool_name=tool_name,
            tool_args=payload.get("args") or payload,
            tool_call_id=tool_id,
            provider_kind="cursor_agent_cli",
            raw=record,
        )
    ]


def tool_name_from_record(record: dict[str, Any], part: dict[str, Any] | None = None) -> str:
    part = part or {}
    return (
        get_string(record.get("tool"))
        or get_string(record.get("toolName"))
        or get_string(record.get("tool_name"))
        or get_string(record.get("name"))
        or get_string(part.get("name"))
        or get_string(part.get("tool"))
        or "Tool"
    )


def tool_result_text(record: dict[str, Any], part: dict[str, Any] | None = None) -> str:
    part = part or {}
    state = record.get("state") if isinstance(record.get("state"), dict) else {}
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    return (
        extract_text(record.get("output"))
        or extract_text(part.get("output"))
        or extract_text(state.get("output"))
        or extract_text(record.get("value"))
        or extract_text(record.get("content"))
        or extract_text(record.get("message"))
        or extract_text(record.get("error"))
        or extract_text(error.get("message"))
        or compact_text(record, 3000)
    )


def infer_workspace_from_text(text: str) -> str:
    match = re.search(r"Workspace Path:\s*([^\n]+)", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"Workspace Directories:[\s\S]*?\n\s*-\s+([^\n]+)", text)
    if match:
        return match.group(1).strip()
    return ""


def looks_failed(text: str) -> bool:
    return re.search(
        r"(Exit code:\s*[1-9]|Traceback|ModuleNotFoundError|timed out|permission denied|No such file|\"Err\")",
        text,
        re.IGNORECASE,
    ) is not None
