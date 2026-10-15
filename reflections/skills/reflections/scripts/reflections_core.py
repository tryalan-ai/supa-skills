"""Small, evidence-first session analysis independent of Alan product data."""

from __future__ import annotations

import re
import hashlib
import json
from collections import Counter
from pathlib import Path
import statistics

from analyze_sessions import (
    APPLY_PATCH_RE,
    EDIT_TOOL_NAMES,
    SHELL_TOOL_NAMES,
    WRITE_SHELL_RE,
    command_from_args,
    extract_human_query,
    is_read_only_shell_command,
    parse_arguments,
    parse_epoch_seconds,
)
from discover import provider_for_path
from session_events import NormalizedSessionEvent, cursor_project_dir, iter_session_events


EXIT_RE = re.compile(r"^[ \t]*(?:exit[_ ]code|process exited with code)[ \t]*[:=]?[ \t]*(-?\d+)\b", re.I | re.M)
FAIL_RE = re.compile(r"\b(?:FAIL(?:ED)?|timed out|permission denied)\b", re.I)
PASS_RE = re.compile(r"\b(?:tests? passed|passed \d+ tests?|status: (?:success|completed))\b", re.I)
BIOME_ERROR_RE = re.compile(r"(?m)^(?:Found [1-9]\d* errors?\.|[ \t]*× Some errors were emitted while running checks\.)[ \t]*$")
CODE_SUFFIXES = {".c", ".cpp", ".css", ".go", ".html", ".java", ".js", ".json", ".jsx", ".py", ".rs", ".sh", ".sql", ".swift", ".toml", ".ts", ".tsx", ".vue", ".yaml", ".yml"}
SHELL_TARGET_RE = re.compile(r"(?:\bcat\s*>|\btee\s+)(?:\s*)([^\s;&|]+)")
FORMAT_WRITE_RE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z_0-9]*=\S+\s+)*(?:rtk\s+(?:proxy\s+)?)?(?:(?:pnpm\s+(?:exec\s+)?)|(?:npx\s+(?:@biomejs/)?)?)biome\s+check\s+--write\b", re.I)
PATCH_TARGET_RE = re.compile(r"\*\*\* (?:Add|Update|Delete) File:\s*([^\r\n\\]+)")
CHECK_COMMAND_RE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z_0-9]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)*(?:rtk\s+(?:proxy\s+|test\s+)?)?"
    r"(?P<check>pnpm\s+(?:--filter\s+\S+\s+)?(?:exec\s+)?(?:test|lint|type-check|design:check|vitest|tsc|biome\s+check)|"
    r"npx\s+(?:vitest|(?:@biomejs/)?biome\s+check|tsc|playwright)|"
    r"(?:\S*/)?node_modules/\.bin/(?:biome\s+check|vitest\b|tsc\b)|"
    r"node\s+\S*vitest(?:\.mjs)?\s+run\b|"
    r"(?:biome\s+check|vitest\s+run|tsc\s+--noEmit)|"
    r"bash\s+\S*test\S*\.sh\b|"
    r"(?:bash|sh)\s+-n\s+\S+|"
    r"test\s+!\s+-e\s+\S+|"
    r"(?:PGPASSWORD=\S+\s+)?psql\b[^;\n]*\s-v\s+ON_ERROR_STOP=1\b|"
    r"(?:pnpm|rtk\s+(?:proxy\s+)?pnpm)\s+db:migrate\b|"
    r"(?:bash\s+\S+\s+--\s+)?bun\s+\S*(?:eval|test)\S*\.ts\b|"
    r"python3?\s+(?:-m\s+(?:py_compile|unittest|pytest)\b|\S*test_[^\s]+\.py\b)|"
    r"(?:pytest|go\s+test|cargo\s+test|swift\s+test)\b|"
    r"ruby\s+-ryaml\s+-e\s+['\"]|"
    r"(?:bash\s+\S*infisical-run-dev\.sh\s+--\s+)?(?:pnpm\s+exec\s+tsx\s+\S*verify-[^\s]+\.ts|bun\s+\S*smoke-[^\s]+\.ts)\b|"
    r"xcodebuild\b[^;\n]*\btest\b|"
    r"gh\s+run\s+watch\b[^;\n]*--exit-status\b|"
    r"git\s+diff\s+--check)",
    re.I,
)
NESTED_EXEC_RE = re.compile(r'tools\.exec_command\(\{\s*(?:cmd|"cmd")\s*:\s*("(?:\\.|[^"\\])*")')
INLINE_EXIT_RE = re.compile(r"^(?:EXIT\s+|exit=)(-?\d+)$", re.I)
TERMINAL_COMMAND_RE = re.compile(r'^command:\s*("(?:\\.|[^"\\])*")\s*$', re.M)
PARALLEL_EXIT_MARKER_RE = re.compile(r"--- result ([1-9]\d*) exit=(-?\d+) ---")
GROK_CONTEXT_END = "If the index returns no useful path, say so and run one targeted local search in the most likely package. Do not enter an index-search loop or a repo-wide grep spiral."
TASK_CONTEXT_END = "so the task Prototypes panel renders the new version."
STANDALONE_CONTEXT_END = "Do not use create_document unless the user explicitly asks for a standalone team Document."


def check_matches(command: str) -> list[str]:
    matches = []
    for segment in shell_segments(command):
        segment = segment.strip()
        if "--write" in segment or "--fix" in segment:
            continue
        match = CHECK_COMMAND_RE.search(segment)
        if match:
            if match.group("check").startswith("ruby -ryaml -e") and not re.search(r"\b(?:abort|raise)\b", segment):
                continue
            matches.append(" ".join(match.group("check").split())[:120])
    return matches


def shell_segments(command: str) -> list[str]:
    kept_lines = []
    delimiter = ""
    for line in command.splitlines(keepends=True):
        if delimiter:
            if line.strip() == delimiter:
                delimiter = ""
                kept_lines.append("\n")
            continue
        kept_lines.append(line)
        marker = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1", line)
        if marker:
            delimiter = marker.group(2)
    source = "".join(kept_lines)
    segments = []
    buffer = []
    quote = ""
    escaped = False
    for char in source:
        if escaped:
            buffer.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            buffer.append(char)
            escaped = True
        elif char in ("'", '"'):
            buffer.append(char)
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif not quote and char in ";|&\n":
            segments.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    segments.append("".join(buffer))
    return segments


def is_check_command(command: str) -> bool:
    return bool(check_matches(command))


def check_label(command: str) -> str:
    matches = check_matches(command)
    if not matches:
        return "check"
    label = next((match for match in reversed(matches) if "git diff --check" not in match), matches[-1])
    return "ruby YAML assertion" if label.startswith("ruby -ryaml -e") else label


def check_signature(command: str) -> str:
    segments = [segment.strip() for segment in shell_segments(command) if check_matches(segment)]
    selected = next((segment for segment in reversed(segments) if "git diff --check" not in segment), segments[-1]) if segments else ""
    return hashlib.sha256(selected.encode()).hexdigest() if selected else ""


def check_exit_reliable(command: str) -> bool:
    if len(check_matches(command)) > 1:
        return "&&" in command and not any(token in command for token in ("|", ";", "\n"))
    captured_pipeline = re.search(r"\becho\s+['\"]?exit=\$\{pipestatus\[1\]\}['\"]?\s*$", command, re.I)
    if captured_pipeline:
        preceding = command[:captured_pipeline.start()]
        if preceding.count("|") == 1 and check_matches(preceding.split("|", 1)[0]):
            return True
    captured = re.search(r"\becho\s+['\"]?(?:EXIT\s+CODE:|exit=)\s*\$\?['\"]?\s*$", command, re.I)
    if captured and "|" not in command[:captured.start()]:
        preceding = re.sub(r"\d*>&\d+", "", command[:captured.start()])
        prior_segments = [segment.strip() for segment in shell_segments(preceding) if segment.strip()]
        if prior_segments and check_matches(prior_segments[-1]):
            return True
    segments = [segment.strip() for segment in shell_segments(command) if segment.strip()]
    return bool(segments and check_matches(segments[-1]))


def formatter_change_status(output: str) -> str:
    if re.search(r"\bFixed\s+[1-9]\d*\s+files?\b", output, re.I):
        return "changed"
    if re.search(r"\bNo fixes applied\b|\bFixed\s+0\s+files?\b", output, re.I):
        return "no_change"
    return "unknown"


def check_kind(command: str) -> str:
    label = check_label(command).lower()
    if "git diff --check" in label:
        return "diff_hygiene"
    if re.search(r"\b(?:vitest|pytest|playwright|unittest|test_[^\s]+\.py|go test|cargo test|swift test|eval[^\s]*\.ts)\b", label) or re.search(r"\b(?:pnpm|npx|python3?)\s+(?:exec\s+)?test\b", label):
        return "test"
    if re.search(r"\b(?:verify-[^\s]+\.ts|smoke-[^\s]+\.ts|ruby yaml assertion)\b", label):
        return "runtime_smoke"
    if re.search(r"\b(?:psql|db:migrate)\b", label):
        return "migration_run"
    if "gh run watch" in label:
        return "release_check"
    if label.startswith("test ! -e"):
        return "file_assertion"
    if re.search(r"\b(?:tsc|type-check|py_compile)\b", label):
        return "type_check"
    if re.search(r"\b(?:bash|sh)\s+-n\b", label):
        return "syntax_check"
    if re.search(r"\b(?:lint|biome\s+check|design:check)\b", label):
        return "lint"
    return "other_check"


def formatter_write_segments(command: str) -> list[tuple[str, re.Match[str]]]:
    return [(segment, match) for segment in shell_segments(command) if (match := FORMAT_WRITE_RE.match(segment))]


def edit_targets(args: object, tool_name: str) -> list[str]:
    if isinstance(args, dict):
        for key in ("file_path", "path", "target_file", "filename"):
            value = args.get(key)
            if isinstance(value, str) and value:
                return [value[:180]]
    if isinstance(args, str):
        patch_targets = [match.group(1).strip("'\"")[:180] for match in PATCH_TARGET_RE.finditer(args)]
        if patch_targets:
            return patch_targets
        match = SHELL_TARGET_RE.search(args)
        if match:
            return [match.group(1).strip("'\"")[:180]]
    return [tool_name]


def edit_scope(target: str, tool_name: str) -> str:
    if target == tool_name:
        return "unknown"
    normalized = target.replace("\\", "/")
    if normalized.startswith("$") or "/scratchpad/" in normalized or "/long-running-background-tasks/" in normalized:
        return "output"
    if (normalized.startswith("/tmp/") or normalized.startswith("/private/tmp/")) and "/src/" not in normalized and "/apps/" not in normalized and "/packages/" not in normalized:
        return "output"
    if "/apps/docs/content/" in normalized:
        return "output"
    if "/test-output/reports/" in normalized or normalized.startswith("test-output/reports/"):
        return "output"
    if Path(normalized).suffix.lower() in {".md", ".mdx", ".txt", ".pdf", ".png", ".jpg"}:
        return "output"
    if Path(normalized).suffix.lower() in CODE_SUFFIXES:
        return "code"
    return "unknown"


def is_host_request(text: str) -> bool:
    if re.fullmatch(r"<timestamp>[^<]*</timestamp>", text.strip(), re.S):
        return True
    return text.startswith((
        "Briefly inform the user about the task result and perform any follow-up actions",
        "You are Alan, the software factory agent",
        "## Alan platform tools",
        "## Memory (non-code context)",
        "<recommended_plugins>",
        "<environment_context>",
        "<system-reminder>",
        "<alan-selected-skill>",
        "<task-notification>",
        "<available_subagent_types>",
        "<dynamic_tools>",
        "Base directory for this skill:",
        "# Current User",
        "# AGENTS.md instructions for ",
    ))


def visible_user_query(text: str) -> str:
    query = extract_human_query(text)
    image_prefix = re.match(r"^(?:<image\b[^>]*>\s*</image>\s*)+", query, re.S)
    had_image = image_prefix is not None
    if image_prefix:
        query = query[image_prefix.end():]
    query = re.sub(r"^Codex could not read the local image at [^\n]*\n(?=You are Alan,)", "", query)
    if query.startswith(("You are Alan, the software factory agent", "## Alan platform tools", "## Memory (non-code context)", "## Codebase index")) and GROK_CONTEXT_END in query:
        query = query.rsplit(GROK_CONTEXT_END, 1)[-1].strip()
    if query.startswith("# Current User"):
        for marker in (TASK_CONTEXT_END, STANDALONE_CONTEXT_END):
            if marker in query:
                query = query.split(marker, 1)[-1].strip()
                break
    if query.startswith("<recommended_plugins>") and "</environment_context>" in query:
        query = query.rsplit("</environment_context>", 1)[-1].strip()
    if query.startswith("# AGENTS.md instructions for ") and "</environment_context>" in query:
        query = query.rsplit("</environment_context>", 1)[-1].strip()
    if query.startswith("<resume_context>") and "</resume_context>" in query:
        query = query.split("</resume_context>", 1)[-1].strip()
    query = query.split("<alan-selected-skill>", 1)[0].split("<relevantContext>", 1)[0].strip()
    return query or ("[attached image]" if had_image else "")


def local_session_id(path: Path) -> str:
    ids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(path), re.I)
    if "subagents" in path.parts:
        return f"{ids[-1] if ids else path.parent.parent.name}:{path.stem}"
    return ids[-1] if ids else hashlib.sha256(str(path).encode()).hexdigest()[:12]


def single_nested_command(code: str) -> str:
    calls = re.findall(r"tools\.[A-Za-z_]+\(", code)
    if calls != ["tools.exec_command("]:
        return ""
    match = NESTED_EXEC_RE.search(code)
    if not match:
        return ""
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""


def is_empty_search_result(command: str, exit_code: int, output: str) -> bool:
    return exit_code == 1 and not output.strip() and bool(re.match(r"^\s*(?:rtk\s+(?:proxy\s+)?)?(?:rg|grep)\b", command))


def nested_result_status(text: str, allow_inline_exit: bool = False, command: str = "") -> str:
    try:
        blocks = json.loads(text)
        if not isinstance(blocks, list):
            return "unknown"
        if any(isinstance(block, dict) and str(block.get("text", "")).startswith("Script failed") for block in blocks):
            return "failed"
        for block in blocks:
            block_text = block.get("text", "")
            inline_exit = INLINE_EXIT_RE.fullmatch(block_text.strip()) if allow_inline_exit else None
            if inline_exit is not None:
                return "passed" if int(inline_exit.group(1)) == 0 else "failed"
            try:
                payload = json.loads(block_text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("exit_code"), int):
                if is_empty_search_result(command, payload["exit_code"], str(payload.get("output", ""))):
                    return "observed"
                return "passed" if payload["exit_code"] == 0 else "failed"
    except (json.JSONDecodeError, AttributeError, TypeError):
        return "failed" if text.startswith("Script failed") else "unknown"
    return "failed" if text.startswith("Script failed") else "unknown"


def codex_exec_result_status(text: str, expected_commands: int, commands: list[str] | None = None) -> str:
    try:
        blocks = json.loads(text)
    except json.JSONDecodeError:
        return "failed" if text.startswith("Script failed") else "observed"
    if not isinstance(blocks, list):
        return "observed"
    if any(isinstance(block, dict) and str(block.get("text", "")).startswith("Script failed") for block in blocks):
        return "failed"
    exit_codes: list[int] = []
    rejected = False
    for block in blocks:
        block_text = block.get("text", "") if isinstance(block, dict) else ""
        try:
            payload = json.loads(block_text)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        result = payload.get("result", payload)
        if isinstance(result, dict) and result.get("status") == "rejected":
            rejected = True
        value = result.get("value", result) if isinstance(result, dict) else None
        if isinstance(value, dict) and type(value.get("exit_code")) is int:
            index = payload.get("i")
            command = commands[index] if commands and type(index) is int and 0 <= index < len(commands) else ""
            if not is_empty_search_result(command, value["exit_code"], str(value.get("output", ""))):
                exit_codes.append(value["exit_code"])
    if rejected or any(code != 0 for code in exit_codes):
        return "failed"
    if expected_commands and len(exit_codes) == expected_commands:
        return "passed"
    return "failed" if text.startswith("Script failed") else "observed"


def codex_parallel_command_statuses(text: str, commands: list[str], allow_exit_markers: bool = False) -> dict[int, str]:
    try:
        blocks = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(blocks, list):
        return {}
    statuses: dict[int, str] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if allow_exit_markers:
            marker = PARALLEL_EXIT_MARKER_RE.fullmatch(str(block.get("text", "")).strip())
            if marker:
                index = int(marker.group(1)) - 1
                if 0 <= index < len(commands):
                    statuses[index] = "passed" if int(marker.group(2)) == 0 else "failed"
                continue
        try:
            payload = json.loads(block.get("text", ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict) or type(payload.get("i")) is not int:
            continue
        index = payload["i"]
        if index < 0 or index >= len(commands):
            continue
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            continue
        if result.get("status") == "rejected":
            statuses[index] = "failed"
            continue
        value = result.get("value", result)
        if not isinstance(value, dict):
            continue
        code = value.get("exit_code")
        if type(code) is int:
            statuses[index] = "observed" if is_empty_search_result(commands[index], code, str(value.get("output", ""))) else ("passed" if code == 0 else "failed")
        elif value.get("output"):
            statuses[index] = "observed"
    return statuses


def terminal_result_command(text: str) -> str:
    match = TERMINAL_COMMAND_RE.search(text[:8000])
    if not match:
        return ""
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""


def result_status(text: str, failed: bool) -> str:
    if re.match(r"^\s*\[warn\] Linter process terminated abnormally\b", text, re.I):
        return "failed"
    exit_match = EXIT_RE.search(text)
    if exit_match:
        return "passed" if int(exit_match.group(1)) == 0 else "failed"
    captured_exit = re.search(r"(?:^|\n)\s*exit\s*=\s*(-?\d+)\s*$", text, re.I)
    if captured_exit:
        return "passed" if int(captured_exit.group(1)) == 0 else "failed"
    if BIOME_ERROR_RE.search(text):
        return "failed"
    if failed:
        return "failed"
    diagnostic = re.sub(r"\bFAIL\s*\(\s*0\s*\)|\b0\s+failed\b|\bfailed\s*:\s*0\b", "", text[:2000], flags=re.I)
    if FAIL_RE.search(diagnostic):
        return "unknown"
    if PASS_RE.search(text[:2000]):
        return "observed"
    return "observed" if text else "unknown"


def event_result_status(event: NormalizedSessionEvent, provider: str, tool_name: str) -> str:
    if provider == "codex" and tool_name == "wait_agent":
        try:
            result = json.loads(event.text)
            if isinstance(result, dict) and result.get("timed_out") is True:
                return "observed"
        except json.JSONDecodeError:
            pass
    if provider == "grok":
        update = event.raw.get("params", {}).get("update", {})
        status = update.get("status")
        if status in {"failed", "error"}:
            return "failed"
        raw_output = update.get("rawOutput")
        if tool_name in SHELL_TOOL_NAMES and isinstance(raw_output, dict) and type(raw_output.get("exit_code")) is int:
            if raw_output.get("timed_out") is True:
                return "failed"
            code = raw_output["exit_code"]
            output = raw_output.get("output")
            if is_empty_search_result(str(raw_output.get("command", "")), code, "" if output == [] else "output"):
                return "observed"
            return "passed" if code == 0 else "failed"
        if status == "completed":
            if tool_name not in SHELL_TOOL_NAMES:
                return "observed" if event.text else "unknown"
            if tool_name in SHELL_TOOL_NAMES and event.text.lstrip().startswith(("Traceback", "Error:")):
                return "failed"
            return result_status(event.text, False)
    if provider == "claude":
        content = event.raw.get("message", {}).get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") == event.tool_call_id:
                if block.get("is_error") is True:
                    return "failed"
                if tool_name not in SHELL_TOOL_NAMES:
                    return "observed" if event.text else "unknown"
                if tool_name in SHELL_TOOL_NAMES and event.text.lstrip().startswith(("Traceback", "Error:")):
                    return "failed"
                return result_status(event.text, False)
    if tool_name not in SHELL_TOOL_NAMES:
        return "failed" if event.failed else ("observed" if event.text else "unknown")
    return result_status(event.text, event.failed)


def open_failed_checks(checks: list[dict]) -> list[dict]:
    failed: dict[str, dict] = {}
    for check in checks:
        if check["status"] == "failed":
            failed[check["signature"]] = check
        elif check["status"] == "passed":
            failed.pop(check["signature"], None)
    return list(failed.values())


def linked_check_status(check: dict, status: str, output: str) -> str:
    explicit_biome_failure = status == "failed" and "biome check" in check["command"].lower() and bool(BIOME_ERROR_RE.search(output))
    if check["multipleChecks"] and status == "failed" and not explicit_biome_failure:
        return "unknown"
    if check["exitReliable"] or explicit_biome_failure or status not in {"passed", "failed"}:
        return status
    return "observed" if status == "passed" else "unknown"


def verification_state(code_edit_calls: int, last_code_edit: int, checks: list[dict], unparsed_indices: list[int], read_only: bool, formatter_records: list[dict] | None = None) -> str:
    if read_only or not code_edit_calls:
        return "not_applicable"
    latest_formatter = max((record["index"] for record in formatter_records or [] if record["changeStatus"] != "no_change" or record["executionStatus"] == "failed"), default=-1)
    if latest_formatter > last_code_edit and not any(check["index"] > latest_formatter and check["kind"] != "diff_hygiene" for check in checks):
        return "unresolved_write"
    after_edit = [check for check in checks if check["index"] > last_code_edit and check["kind"] != "diff_hygiene"]
    if not after_edit:
        if any(index > last_code_edit for index in unparsed_indices):
            return "unresolved"
        if any(check["index"] > last_code_edit and check["kind"] == "diff_hygiene" for check in checks):
            return "hygiene_only"
        return "stale" if any(check["kind"] != "diff_hygiene" for check in checks) else "unchecked"
    if open_failed_checks(after_edit):
        return "failed" if after_edit[-1]["status"] == "failed" else "unresolved_failure"
    latest_by_signature = {check["signature"]: check["status"] for check in after_edit}
    if "unknown" in latest_by_signature.values():
        return "unknown"
    if "observed" in latest_by_signature.values():
        return "observed"
    return "passed"


def finish_span(span: dict) -> dict:
    start = span["startEpoch"]
    last = span["lastAgentEpoch"]
    duration = round(last - start, 2) if start is not None and last is not None and last >= start else None
    return {
        "turn": span["turn"],
        "request": span["request"],
        "startToolIndex": span["startToolIndex"],
        "endToolIndex": span["endToolIndex"],
        "toolCalls": span["toolCalls"],
        "elapsedToLastAgentEventSeconds": duration,
        "editCalls": span["editCalls"],
        "codeEditCalls": span["codeEditCalls"],
        "lastEditTarget": span["lastEditTarget"],
        "lastCodeEditTarget": span["lastCodeEditTarget"],
        "lastCodeEditToolIndex": span["lastCodeEdit"],
        "validationCalls": len(span["checks"]),
        "verification": verification_state(span["codeEditCalls"], span["lastCodeEdit"], span["checks"], span["unparsedIndices"], span["readOnly"], span["formatterRecords"]),
        "lastCheck": {"command": span["checks"][-1]["command"], "kind": span["checks"][-1]["kind"], "status": span["checks"][-1]["status"]} if span["checks"] else None,
    }


def analyze_session(path: Path) -> dict:
    provider = provider_for_path(path)
    cursor_project = cursor_project_dir(path) if provider == "cursor" else None
    cursor_terminal_available = (cursor_project / "terminals").is_dir() if cursor_project else None
    human_turns = tool_calls = edit_calls = code_edit_calls = failed_tool_calls = unlinked_failed_results = 0
    validation_calls = 0
    last_edit = -1
    last_code_edit = -1
    last_code_edit_read_only = False
    read_only_code_edit_calls = 0
    last_read_only_code_target = ""
    validations: list[dict] = []
    pending: dict[str, dict] = {}
    pending_parallel: dict[str, list[tuple[int, dict]]] = {}
    parallel_exit_marker_ids: set[str] = set()
    pending_by_command: dict[str, list[dict]] = {}
    cursor_shell_commands: Counter[str] = Counter()
    unmatched_terminal_results = 0
    ambiguous_terminal_results = 0
    extraneous_terminal_results = 0
    last_call_id = ""
    request = ""
    final_report = False
    read_only_goal = False
    event_number = 0
    failure_tools: Counter[str] = Counter()
    unparsed_orchestration_calls = 0
    unparsed_indices: list[int] = []
    formatter_records: list[dict] = []
    pending_formatters: dict[str, dict] = {}
    nested_exec_ids: set[str] = set()
    nested_exit_logged: set[str] = set()
    codex_exec_command_counts: dict[str, int] = {}
    codex_exec_commands: dict[str, list[str]] = {}
    tool_names: dict[str, str] = {}
    tool_signatures: dict[str, tuple[str, str]] = {}
    failed_since_edit_or_instruction: set[str] = set()
    repeated_failures = 0
    repeated_failure_examples: list[str] = []
    last_human_query = ""
    last_human_ts: float | None = None
    last_human_activity = -1
    activity_number = 0
    span_tool_counts: list[int] = []
    current_span_tools = 0
    unattributed_tool_calls = 0
    unattributed_code_edit_calls = 0
    current_span: dict | None = None
    raw_spans: list[dict] = []

    for event in iter_session_events(path, "auto"):
        if event.kind == "user_message":
            query = visible_user_query(event.text)
            if query and not is_host_request(query):
                normalized = " ".join(query.split())
                ts = parse_epoch_seconds(event.timestamp)
                same_replay = normalized == last_human_query and (
                    (ts is not None and last_human_ts is not None and abs(ts - last_human_ts) < 2)
                    or (ts is None and activity_number == last_human_activity)
                )
                codex_resume_burst = provider == "codex" and ts is not None and last_human_ts is not None and 0 <= ts - last_human_ts < 0.5
                if same_replay or codex_resume_burst:
                    continue
                if human_turns:
                    span_tool_counts.append(current_span_tools)
                if current_span is not None:
                    current_span["endToolIndex"] = event_number
                    raw_spans.append(current_span)
                current_span_tools = 0
                human_turns += 1
                current_span = {
                    "turn": human_turns,
                    "request": normalized[:180],
                    "startToolIndex": event_number,
                    "endToolIndex": event_number,
                    "toolCalls": 0,
                    "startEpoch": ts,
                    "lastAgentEpoch": None,
                    "editCalls": 0,
                    "codeEditCalls": 0,
                    "lastEditTarget": None,
                    "lastCodeEditTarget": None,
                    "checks": [],
                    "lastEdit": -1,
                    "lastCodeEdit": -1,
                    "unparsedIndices": [],
                    "formatterRecords": [],
                    "readOnly": bool(re.search(r"\bREAD-ONLY\b|\bdo not edit files\b", normalized[:500], re.I)),
                }
                failed_since_edit_or_instruction.clear()
                last_human_query = normalized
                last_human_ts = ts
                last_human_activity = activity_number
                if not request:
                    request = " ".join(query.split())[:180]
                    read_only_goal = bool(re.search(r"\bREAD-ONLY\b|\bdo not edit files\b", query[:500], re.I))
        elif event.kind == "assistant_message":
            activity_number += 1
            if current_span is not None:
                agent_ts = parse_epoch_seconds(event.timestamp)
                if agent_ts is not None:
                    current_span["lastAgentEpoch"] = max(agent_ts, current_span["lastAgentEpoch"] or agent_ts)
            final_report = final_report or bool(re.search(r"\b(?:tests? run|validation|verification|changed files|pull request)\b", event.text, re.I))
        elif event.kind == "tool_call":
            activity_number += 1
            if current_span is not None:
                agent_ts = parse_epoch_seconds(event.timestamp)
                if agent_ts is not None:
                    current_span["lastAgentEpoch"] = max(agent_ts, current_span["lastAgentEpoch"] or agent_ts)
            event_number += 1
            tool_calls += 1
            current_span_tools += 1
            if current_span is not None:
                current_span["toolCalls"] += 1
            else:
                unattributed_tool_calls += 1
            name = event.tool_name or "tool"
            if event.tool_call_id:
                tool_names[event.tool_call_id] = name
            args = parse_arguments(event.tool_args)
            nested_command = single_nested_command(args) if name == "exec" and isinstance(args, str) else ""
            parallel_commands: list[str] = []
            if name == "exec":
                if event.tool_call_id and isinstance(args, str):
                    if "result ${i + 1} exit=${r.exit_code}" in args:
                        parallel_exit_marker_ids.add(event.tool_call_id)
                    all_tools = re.findall(r"tools\.[A-Za-z_]+\(", args)
                    codex_exec_command_counts[event.tool_call_id] = all_tools.count("tools.exec_command(")
                    encoded_commands = NESTED_EXEC_RE.findall(args)
                    if all_tools and all(tool == "tools.exec_command(" for tool in all_tools) and len(encoded_commands) == len(all_tools):
                        try:
                            codex_exec_commands[event.tool_call_id] = [json.loads(encoded) for encoded in encoded_commands]
                            if len(all_tools) > 1:
                                parallel_commands = codex_exec_commands[event.tool_call_id]
                        except json.JSONDecodeError:
                            pass
                if nested_command:
                    nested_exec_ids.add(event.tool_call_id)
                    if isinstance(args, str) and ".exit_code" in args:
                        nested_exit_logged.add(event.tool_call_id)
                elif not parallel_commands:
                    unparsed_orchestration_calls += 1
                    unparsed_indices.append(event_number)
                    if current_span is not None:
                        current_span["unparsedIndices"].append(event_number)
            command = nested_command or command_from_args(args)
            args_text = str(args)[:20_000]
            command_text = command or args_text
            if event.tool_call_id:
                signature_source = args if name == "exec" and isinstance(args, str) else command_text
                signature = hashlib.sha256(f"{name}\0{signature_source}".encode()).hexdigest()
                display_command = parallel_commands[0] if parallel_commands else command_text
                tool_signatures[event.tool_call_id] = (signature, f"{name}: {' '.join(display_command.split())[:120]}")
            is_shell = name in SHELL_TOOL_NAMES or "shell_command" in name or bool(command)
            if is_shell and formatter_write_segments(command_text):
                formatter = {"index": event_number, "changeStatus": "unknown", "executionStatus": "unknown"}
                formatter_records.append(formatter)
                if current_span is not None:
                    current_span["formatterRecords"].append(formatter)
                if event.tool_call_id:
                    pending_formatters[event.tool_call_id] = formatter
            if provider == "cursor" and name == "Shell" and command:
                cursor_shell_commands[command] += 1
            is_edit = bool(
                name in EDIT_TOOL_NAMES
                or "edit_file" in name
                or APPLY_PATCH_RE.search(command_text)
                or (is_shell and not is_read_only_shell_command(command_text) and WRITE_SHELL_RE.search(command_text))
            )
            if is_edit:
                edit_calls += 1
                last_edit = event_number
                failed_since_edit_or_instruction.clear()
                targets = edit_targets(args if name not in SHELL_TOOL_NAMES else command_text, name)
                code_targets = [target for target in targets if edit_scope(target, name) == "code"]
                target = code_targets[-1] if code_targets else targets[-1]
                if code_targets:
                    code_edit_calls += 1
                    last_code_edit = event_number
                    last_code_edit_read_only = current_span["readOnly"] if current_span is not None else read_only_goal
                    if last_code_edit_read_only:
                        read_only_code_edit_calls += 1
                        last_read_only_code_target = code_targets[-1]
                    if current_span is None:
                        unattributed_code_edit_calls += 1
                if current_span is not None:
                    current_span["editCalls"] += 1
                    current_span["lastEdit"] = event_number
                    current_span["lastEditTarget"] = target
                    if code_targets:
                        current_span["codeEditCalls"] += 1
                        current_span["lastCodeEdit"] = event_number
                        current_span["lastCodeEditTarget"] = target
            # Codex exec contains JavaScript orchestration, not one shell command.
            # Treat its embedded strings as unparsed rather than crediting a check.
            if is_shell and (name != "exec" or nested_command) and is_check_command(command_text):
                validation_calls += 1
                check = {"index": event_number, "status": "unknown", "command": check_label(command_text), "kind": check_kind(command_text), "signature": check_signature(command_text), "exitReliable": check_exit_reliable(command_text), "multipleChecks": len(check_matches(command_text)) > 1}
                validations.append(check)
                if current_span is not None:
                    current_span["checks"].append(check)
                key = event.tool_call_id or f"no-id-{event_number}"
                pending[key] = check
                if provider == "cursor" and not event.tool_call_id:
                    pending_by_command.setdefault(command_text, []).append(check)
            if parallel_commands and event.tool_call_id:
                for command_index, parallel_command in enumerate(parallel_commands):
                    if not is_check_command(parallel_command):
                        continue
                    validation_calls += 1
                    check = {"index": event_number, "status": "unknown", "command": check_label(parallel_command), "kind": check_kind(parallel_command), "signature": check_signature(parallel_command), "exitReliable": check_exit_reliable(parallel_command), "multipleChecks": len(check_matches(parallel_command)) > 1}
                    validations.append(check)
                    if current_span is not None:
                        current_span["checks"].append(check)
                    pending_parallel.setdefault(event.tool_call_id, []).append((command_index, check))
            last_call_id = event.tool_call_id or ""
        elif event.kind == "tool_result":
            linked_result = bool(event.tool_call_id and event.tool_call_id in tool_names)
            if provider == "cursor" and event.tool_name == "Shell" and not event.raw:
                terminal_command = terminal_result_command(event.text)
                if cursor_shell_commands[terminal_command] != 1:
                    unmatched_terminal_results += 1
                    if cursor_shell_commands[terminal_command] > 1:
                        ambiguous_terminal_results += 1
                    else:
                        extraneous_terminal_results += 1
                    continue
                linked_result = True
            tool_name = tool_names.get(event.tool_call_id, event.tool_name or "tool")
            if event.tool_call_id in nested_exec_ids:
                status = nested_result_status(event.text, event.tool_call_id in nested_exit_logged, codex_exec_commands.get(event.tool_call_id, [""])[0])
            elif provider == "codex" and tool_name == "exec":
                status = codex_exec_result_status(event.text, codex_exec_command_counts.get(event.tool_call_id, 0), codex_exec_commands.get(event.tool_call_id))
            else:
                status = event_result_status(event, provider, tool_name)
            if status == "failed":
                if linked_result:
                    failed_tool_calls += 1
                    failure_tools[tool_name] += 1
                    signature = tool_signatures.get(event.tool_call_id)
                    if signature:
                        if signature[0] in failed_since_edit_or_instruction:
                            repeated_failures += 1
                            if len(repeated_failure_examples) < 3:
                                repeated_failure_examples.append(signature[1])
                        failed_since_edit_or_instruction.add(signature[0])
                else:
                    unlinked_failed_results += 1
            key = event.tool_call_id or last_call_id
            check = pending.pop(key, None)
            if check is None and provider == "cursor" and event.tool_name == "Shell":
                command = terminal_result_command(event.text)
                matches = pending_by_command.get(command, [])
                if len(matches) == 1:
                    check = matches.pop()
            if check is not None:
                check["status"] = linked_check_status(check, status, event.text)
            formatter = pending_formatters.pop(event.tool_call_id, None)
            if formatter is not None:
                formatter["changeStatus"] = formatter_change_status(event.text)
                formatter["executionStatus"] = status
            if event.tool_call_id in pending_parallel:
                statuses = codex_parallel_command_statuses(event.text, codex_exec_commands[event.tool_call_id], event.tool_call_id in parallel_exit_marker_ids)
                for command_index, parallel_check in pending_parallel.pop(event.tool_call_id):
                    command_status = statuses.get(command_index, "unknown")
                    parallel_check["status"] = linked_check_status(parallel_check, command_status, "")

    after_last_edit = [check for check in validations if check["index"] > last_code_edit] if last_code_edit >= 0 else []
    substantive_after_edit = [check for check in after_last_edit if check["kind"] != "diff_hygiene"]
    if human_turns:
        span_tool_counts.append(current_span_tools)
    if current_span is not None:
        current_span["endToolIndex"] = event_number
        raw_spans.append(current_span)
    spans = [finish_span(span) for span in raw_spans]
    observed_durations = [span["elapsedToLastAgentEventSeconds"] for span in spans if span["elapsedToLastAgentEventSeconds"] is not None]
    verification = verification_state(code_edit_calls, last_code_edit, validations, unparsed_indices, last_code_edit_read_only, formatter_records)
    last_code_span = next((span for span in reversed(spans) if span["codeEditCalls"]), None)
    last_code_request = (last_code_span["request"] if last_code_span else request).lower()

    insights = []
    if read_only_code_edit_calls:
        insights.append({"kind": "read_only_code_edit", "evidence": f"{read_only_code_edit_calls} identified code edit calls during a read-only instruction; last target {last_read_only_code_target}", "suggestion": "Review code changes made during the read-only request and restore the intended scope."})
    if code_edit_calls and verification in ("stale", "unchecked", "hygiene_only"):
        suggestion = "Confirm that a focused check exercised the final code edit, or run one and record its result."
        if re.search(r"\bcommit(?:ting)?\b", last_code_request):
            suggestion = "Review code edits made during this commit request and run the affected check."
        elif re.search(r"\bmerg(?:e|ing)\b", last_code_request):
            suggestion = "Run the affected check after resolving the merge changes."
        later_goal = last_code_span is not None and spans[-1]["turn"] > last_code_span["turn"]
        if later_goal:
            suggestion = "A later request followed this edit. Confirm whether the earlier goal was validated; if it was meant to be complete, run the affected check."
        last_target = last_code_span["lastCodeEditTarget"] if last_code_span else "unknown target"
        timing = "earlier checks predate the final edit" if verification == "stale" else ("only diff hygiene follows the final edit" if verification == "hygiene_only" else "no recognized check in this session")
        turn = f"turn {last_code_span['turn']}: " if later_goal else ""
        insights.append({"kind": "verification_gap", "evidence": f"{turn}last code edit call targeted {last_target}; {timing}", "suggestion": suggestion})
    elif verification == "failed":
        failing = open_failed_checks(substantive_after_edit)
        target = last_code_span["lastCodeEditTarget"] if last_code_span else "unknown target"
        insights.append({"kind": "failing_check", "evidence": f"{failing[-1]['command']} failed after the code edit call to {target}", "suggestion": "Inspect the failure. Fix issues caused by this change and rerun the check; record evidence if the failure is unrelated."})
    elif verification == "unresolved_failure":
        failing = open_failed_checks(substantive_after_edit)
        insights.append({"kind": "unresolved_failure", "evidence": f"{failing[-1]['command']} failed and a later check did not explicitly clear it", "suggestion": "Confirm the earlier failed check with an explicit successful retry, or record why it is unrelated to this change."})
    elif verification == "unknown":
        if provider == "cursor" and cursor_terminal_available is False:
            unknown = next(check for check in reversed(substantive_after_edit) if check["status"] == "unknown")
            insights.append({"kind": "missing_terminal_sidecar", "evidence": f"{unknown['command']}; Cursor terminal directory unavailable", "suggestion": "Capture this check's result in the session transcript or retain its terminal sidecar."})
        elif (provider == "cursor" and ambiguous_terminal_results) or (provider == "codex" and any(index > last_code_edit for index in unparsed_indices)):
            unknown = next(check for check in reversed(substantive_after_edit) if check["status"] == "unknown")
            insights.append({"kind": "parser_link_gap", "evidence": f"{unknown['command']}; {ambiguous_terminal_results} ambiguous terminal results, {sum(index > last_code_edit for index in unparsed_indices)} later opaque orchestration calls", "suggestion": "Pair the check with its result before judging this session's verification."})
        else:
            unknown = next(check for check in reversed(substantive_after_edit) if check["status"] == "unknown")
            insights.append({"kind": "unknown_check_result", "evidence": unknown["command"], "suggestion": "Capture the check exit status before claiming verification."})
    elif verification == "observed":
        observed = next(check for check in reversed(substantive_after_edit) if check["status"] == "observed")
        insights.append({"kind": "missing_exit_status", "evidence": observed["command"], "suggestion": "Record the check exit status to confirm the observed non-failing result."})
    elif verification == "unresolved_write":
        last_formatter = next(record for record in reversed(formatter_records) if record["changeStatus"] != "no_change" or record["executionStatus"] == "failed")
        if last_formatter["executionStatus"] == "failed":
            evidence = "formatter failed after the last code edit; no later check is linked"
            suggestion = "Inspect the formatter error and rerun the affected check."
            kind = "formatter_failed_after_check"
        elif last_formatter["changeStatus"] == "changed":
            evidence = "formatter reported changed files after the last code edit; no later check is linked"
            suggestion = "Rerun the affected check after the formatter changed files."
            kind = "formatter_changed_after_check"
        else:
            evidence = "formatter ran with --write after the last code edit; a resulting file change and check ordering are not known"
            suggestion = "Capture the formatter result and confirm a check ran after any changes before treating earlier checks as current."
            kind = "formatter_result_unknown"
        insights.append({"kind": kind, "evidence": evidence, "suggestion": suggestion})
    if failed_tool_calls >= 4 and tool_calls and failed_tool_calls / tool_calls >= 0.2:
        insights.append({"kind": "tool_friction", "evidence": f"{failed_tool_calls}/{tool_calls} tool results failed", "suggestion": "Inspect repeated failed calls and fix the tool invocation or environment."})
    if repeated_failures:
        noun = "failure" if repeated_failures == 1 else "failures"
        insights.append({"kind": "repeat_failure", "evidence": f"{repeated_failures} repeated call {noun} without an intervening edit or human turn; {repeated_failure_examples[0]}", "suggestion": "After the second identical failure, change the command or resolve its prerequisite before retrying."})

    return {
        "sessionId": local_session_id(path),
        "provider": provider,
        "path": str(path),
        "request": request,
        "lastHumanRequest": spans[-1]["request"] if spans else "",
        "goalMode": "read_only" if last_code_edit_read_only or (read_only_goal and not code_edit_calls) else "unspecified",
        "metrics": {
            "humanTurns": human_turns,
            "toolsPerHumanTurn": round((tool_calls - unattributed_tool_calls) / human_turns, 2) if human_turns else None,
            "maxToolsBetweenHumanTurns": max(span_tool_counts, default=0),
            "medianElapsedToLastAgentEventSeconds": statistics.median(observed_durations) if observed_durations else None,
            "timedHumanSpans": len(observed_durations),
            "toolCalls": tool_calls,
            "unattributedToolCalls": unattributed_tool_calls,
            "editCalls": edit_calls,
            "codeEditCalls": code_edit_calls,
            "readOnlyCodeEditCalls": read_only_code_edit_calls,
            "unattributedCodeEditCalls": unattributed_code_edit_calls,
            "lastCodeEditToolIndex": last_code_edit,
            "validationCalls": validation_calls,
            "failedToolCalls": failed_tool_calls,
            "unlinkedFailedResults": unlinked_failed_results,
            "unparsedOrchestrationCalls": unparsed_orchestration_calls,
            "possibleFormatterWriteCalls": len(formatter_records),
            "confirmedFormatterChanges": sum(record["changeStatus"] == "changed" for record in formatter_records),
            "confirmedFormatterNoChanges": sum(record["changeStatus"] == "no_change" for record in formatter_records),
            "repeatIdenticalFailuresWithinTurn": repeated_failures,
            "unmatchedTerminalResults": unmatched_terminal_results,
            "ambiguousTerminalResults": ambiguous_terminal_results,
            "extraneousTerminalResults": extraneous_terminal_results,
            "cursorTerminalDirectoryAvailable": cursor_terminal_available,
        },
        "verification": verification,
        "reportMarkerSeen": final_report,
        "spans": spans,
        "checksAfterLastEdit": [{"command": check["command"], "kind": check["kind"], "status": check["status"]} for check in after_last_edit[-3:]],
        "topFailedTools": failure_tools.most_common(3),
        "insights": insights,
    }
