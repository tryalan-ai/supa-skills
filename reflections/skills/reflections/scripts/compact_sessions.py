#!/usr/bin/env python3
"""Create compact qualitative digests from selected agent session JSONL logs."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from analyze_sessions import (
    EDIT_TOOL_NAMES,
    FAILURE_RE,
    VALIDATION_RE,
    WRITE_SHELL_RE,
    SessionStats,
    command_from_args,
    compact_text,
    extract_human_query,
    extract_text_content,
    is_host_context_dump,
    is_read_only_shell_command,
    iter_session_files,
    parse_arguments,
    parse_session,
    select_samples,
)
from session_events import SESSION_FORMATS, iter_session_events


REPORT_HINT_RE = re.compile(
    r"(Changed files|Validation|Residual risk|Tests? run|Verification|blocked|cannot|can't)",
    re.IGNORECASE,
)


def truncate(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def call_command(args: Any) -> str:
    command = command_from_args(args)
    if command:
        return command
    if isinstance(args, dict):
        for key in ("path", "instruction", "code_edit", "query", "search_query"):
            value = args.get(key)
            if isinstance(value, str):
                return value
    return compact_text(args, 500)


def call_is_edit(tool_name: str, args: Any, command: str) -> bool:
    is_shell = bool(command_from_args(args)) or tool_name == "shell_command" or "shell_command" in tool_name
    return tool_name in EDIT_TOOL_NAMES or "edit_file" in tool_name or (is_shell and not is_read_only_shell_command(command) and WRITE_SHELL_RE.search(command) is not None)


def resolve_requested_sessions(sessions_dir: Path, session_ids: list[str]) -> list[Path]:
    if not session_ids:
        return []
    files = sorted(sessions_dir.glob("**/*.jsonl"))
    selected: list[Path] = []
    seen: set[Path] = set()
    for session_id in session_ids:
        matches = [path for path in files if session_id in path.stem or session_id in str(path)]
        for match in matches:
            if match not in seen:
                selected.append(match)
                seen.add(match)
    return selected


def choose_sessions(args: argparse.Namespace) -> tuple[list[SessionStats], list[SessionStats]]:
    sessions_dir = Path(args.sessions_dir)
    requested_paths = resolve_requested_sessions(sessions_dir, args.session_id)
    if requested_paths:
        stats = [parse_session(path, args.repo, args.format) for path in requested_paths]
        for item in stats:
            item.sample_reasons.append("requested")
        return stats, stats

    files = iter_session_files(sessions_dir, args.repo, args.max_sessions)
    all_stats = [parse_session(path, args.repo, args.format) for path in files]
    samples = select_samples(all_stats, args.sample_size)
    return all_stats, samples


def compact_session(
    stats: SessionStats, max_message_chars: int, max_events: int, session_format: str = "auto"
) -> dict[str, Any]:
    tool_counts: Counter[str] = Counter()
    failed_tool_counts: Counter[str] = Counter()
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    report_messages: list[str] = []
    tool_events: list[dict[str, Any]] = []
    call_index: dict[str, dict[str, Any]] = {}

    session_path = Path(stats.path)
    if session_path.is_file() and session_path.stat().st_size > 40 * 1024 * 1024:
        return {
            "sessionId": stats.id,
            "timestamp": stats.timestamp,
            "classification": stats.classification,
            "sampleReasons": stats.sample_reasons,
            "path": stats.path,
            "metrics": {
                "toolCalls": stats.tool_calls,
                "editCalls": stats.edit_calls,
                "validationCalls": stats.validation_calls,
                "validationAfterEdit": stats.validation_after_edit,
                "failedToolCalls": stats.failed_tool_calls,
                "finalReportMarkers": stats.final_report_markers,
                "manualHandoffMarkers": stats.manual_handoff_markers,
                "blockedMarkers": stats.blocked_markers,
            },
            "failurePatterns": dict(stats.failure_patterns),
            "failurePatternEvidence": stats.failure_pattern_evidence,
            "toolCounts": {},
            "failedToolCounts": {},
            "validationCommands": stats.validation_commands,
            "userRequests": ["(compact skipped: session file over 40MB)"],
            "assistantOutcomeMessages": [],
            "toolEvents": [],
        }

    for event in iter_session_events(session_path, session_format):
        if event.kind == "user_message":
            query = extract_human_query(event.text)
            if query:
                user_messages.append(truncate(query, max_message_chars))
            continue

        if event.kind == "assistant_message":
            if not event.text or is_host_context_dump(event.text):
                continue
            compacted = truncate(event.text, max_message_chars)
            if REPORT_HINT_RE.search(event.text):
                report_messages.append(compacted)
            else:
                assistant_messages.append(compacted)
            continue

        if event.kind == "tool_call":
            tool_name = event.tool_name or "tool"
            args = parse_arguments(event.tool_args)
            command = call_command(args)
            is_validation = VALIDATION_RE.search(command) is not None
            is_edit = call_is_edit(tool_name, args, command)
            tool_counts[tool_name] += 1
            tool_event = {
                "tool": tool_name,
                "kind": "tool_call",
                "edit": bool(is_edit),
                "validation": bool(is_validation),
                "command": truncate(command, 260),
            }
            if event.tool_call_id:
                call_index[event.tool_call_id] = tool_event
            if len(tool_events) < max_events:
                tool_events.append(tool_event)
            continue

        if event.kind == "tool_result":
            tool_event = call_index.get(event.tool_call_id or "")
            tool_name = event.tool_name or (tool_event["tool"] if tool_event else "tool")
            failed = event.failed or FAILURE_RE.search(event.text) is not None
            if failed:
                failed_tool_counts[tool_name] += 1
            if len(tool_events) < max_events and (failed or (tool_event and tool_event.get("validation"))):
                tool_events.append(
                    {
                        "tool": tool_name,
                        "kind": "tool_result",
                        "failed": failed,
                        "summary": truncate(event.text, 260),
                    }
                )
            continue

    assistant_sample = report_messages[:4]
    if len(assistant_sample) < 4:
        assistant_sample.extend(assistant_messages[-(4 - len(assistant_sample)) :])

    return {
        "sessionId": stats.id,
        "timestamp": stats.timestamp,
        "classification": stats.classification,
        "sampleReasons": stats.sample_reasons,
        "path": stats.path,
        "metrics": {
            "toolCalls": stats.tool_calls,
            "editCalls": stats.edit_calls,
            "validationCalls": stats.validation_calls,
            "validationAfterEdit": stats.validation_after_edit,
            "failedToolCalls": stats.failed_tool_calls,
            "finalReportMarkers": stats.final_report_markers,
            "manualHandoffMarkers": stats.manual_handoff_markers,
            "blockedMarkers": stats.blocked_markers,
        },
        "failurePatterns": dict(stats.failure_patterns),
        "failurePatternEvidence": stats.failure_pattern_evidence,
        "toolCounts": dict(tool_counts),
        "failedToolCounts": dict(failed_tool_counts),
        "validationCommands": stats.validation_commands,
        "userRequests": user_messages[:6],
        "assistantOutcomeMessages": assistant_sample,
        "toolEvents": tool_events,
    }


def markdown_digest(digests: list[dict[str, Any]]) -> str:
    lines = ["# Compact Session Digests", ""]
    for digest in digests:
        metrics = digest["metrics"]
        reasons = ", ".join(digest["sampleReasons"]) or "selected"
        lines.extend(
            [
                f"## {digest['sessionId']} - {digest['classification']} ({reasons})",
                "",
                f"- Timestamp: {digest['timestamp'] or 'unknown'}",
                f"- Tool calls: {metrics['toolCalls']} total, {metrics['failedToolCalls']} failed",
                f"- Edits/validation: {metrics['editCalls']} edits, {metrics['validationCalls']} validations, validationAfterEdit={metrics['validationAfterEdit']}",
                f"- Final report markers: {metrics['finalReportMarkers']}; manual handoff markers: {metrics['manualHandoffMarkers']}; blocked markers: {metrics['blockedMarkers']}",
            ]
        )
        if digest["validationCommands"]:
            lines.append("- Validation commands:")
            for command in digest["validationCommands"][:4]:
                lines.append(f"  - `{command}`")
        if digest["userRequests"]:
            lines.append("- User requests:")
            for message in digest["userRequests"][:3]:
                lines.append(f"  - {message}")
        if digest["assistantOutcomeMessages"]:
            lines.append("- Assistant outcome signals:")
            for message in digest["assistantOutcomeMessages"][:3]:
                lines.append(f"  - {message}")
        if digest["failedToolCounts"]:
            failed = ", ".join(f"{name}={count}" for name, count in digest["failedToolCounts"].items())
            lines.append(f"- Failed tools: {failed}")
        if digest.get("failurePatterns"):
            patterns = ", ".join(f"{name}={count}" for name, count in digest["failurePatterns"].items())
            lines.append(f"- Failure patterns: {patterns}")
            for name, evidence_items in digest.get("failurePatternEvidence", {}).items():
                for evidence in evidence_items[:2]:
                    lines.append(f"  - {name}: {evidence}")
        if digest["toolEvents"]:
            lines.append("- Compact tool events:")
            for event in digest["toolEvents"][:8]:
                markers = []
                if event.get("edit"):
                    markers.append("edit")
                if event.get("validation"):
                    markers.append("validation")
                if event.get("failed"):
                    markers.append("failed")
                marker_text = f" [{', '.join(markers)}]" if markers else ""
                summary = event.get("command") or event.get("summary") or ""
                lines.append(f"  - {event['tool']}{marker_text}: {summary}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".codex" / "sessions"))
    parser.add_argument("--repo", default=str(Path.cwd()))
    parser.add_argument("--sample-size", type=int, default=15)
    parser.add_argument("--max-sessions", type=int)
    parser.add_argument("--session-id", action="append", default=[])
    parser.add_argument("--max-message-chars", type=int, default=900)
    parser.add_argument("--max-events", type=int, default=24)
    parser.add_argument(
        "--format",
        default="auto",
        choices=SESSION_FORMATS,
        help="Session transcript format. auto detects supported CLI JSONL formats.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    args = parser.parse_args()

    all_stats, selected = choose_sessions(args)
    digests = [
        compact_session(item, args.max_message_chars, args.max_events, args.format)
        for item in selected
    ]
    payload = {
        "sessionCount": len(all_stats),
        "digestCount": len(digests),
        "digests": digests,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(markdown_digest(digests))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
