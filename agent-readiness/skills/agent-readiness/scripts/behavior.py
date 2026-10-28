#!/usr/bin/env python3
"""Behavioral analysis of real agent sessions.

Reads normalized transcript events (Claude Code, Codex, Cursor, OpenCode, Droid,
Gemini, Copilot) and dialogue-only exports (Alan sessions), and measures how
agents explore the codebase, how work flows from request to verified change,
where failures loop, and how developers direct their agents.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from session_events import NormalizedSessionEvent, parse_session_events

READ_TOOLS = {"read", "read_file", "view", "cat", "open_file", "readfile", "notebookread"}
SEARCH_TOOLS = {"grep", "glob", "search", "codebase_search", "file_search", "find", "ls", "list_dir", "list_directory", "rg", "websearch_local", "search_files"}
INDEX_TOOLS = {"search_code_context", "code_graph_query", "codebase_search", "semantic_search", "sourcegraph"}
EDIT_TOOLS = {"edit", "multiedit", "write", "apply_patch", "str_replace", "str_replace_editor", "create_file", "edit_file", "write_file", "notebookedit", "replace"}
PLAN_TOOLS = {"todowrite", "update_plan", "exitplanmode", "taskcreate", "taskupdate", "create_plan"}
DELEGATE_TOOLS = {"agent", "task", "delegate", "spawn_agent"}
WEB_TOOLS = {"webfetch", "websearch", "web_search", "fetch"}

SHELL_READ = re.compile(r"^\s*(cat|head|tail|sed -n|less|bat|nl|wc)\b")
SHELL_SEARCH = re.compile(r"^\s*(rg|grep|find|fd|ls|tree|git ls-files|git grep|ag|ack)\b")
VALIDATION = re.compile(
    r"\b(vitest|jest|pytest|mocha|playwright|cypress|go test|cargo (test|check|clippy)|tsc\b|type-?check|typecheck|"
    r"mypy|pyright|ruff|eslint|biome|lint|test(:[\w-]+)?\b|build\b|dotnet test|gradle\w* (test|check)|mvn (test|verify)|rspec|phpunit)",
    re.I,
)
SHELL_EDIT = re.compile(
    r"apply_patch|\bsed -i|\bperl -p?i|\bcat\s*>\s*[\w./-]|\btee\s+(-a\s+)?[\w./-]+\.\w+|open\([^)]*['\"][wa]\+?['\"]|\.write_text\(|writeFileSync|"
    r"(?<![0-9&])>>?\s*(?!/dev/null|&)[\w./-]+\.(ts|tsx|js|jsx|py|go|rs|java|json|ya?ml|md|sql|prisma|css|html)\b"
)
VALIDATION_KINDS: list[tuple[str, re.Pattern[str]]] = [
    ("e2e", re.compile(r"\bplaywright (test|run)|\bcypress run|test:e2e|test:dev\b", re.I)),
    ("test", re.compile(r"\b(tsx|ts-node|node|python3?|bun)\s+[^|;&]*\b(verify|repro|reproduce|check|smoke|probe)[\w-]*\.(ts|js|mjs|py)\b", re.I)),
    ("test", re.compile(r"vitest|jest|pytest|mocha|go test|cargo test|rspec|phpunit|dotnet test|\btest\b|gradle\w* test|mvn (test|verify)", re.I)),
    ("typecheck", re.compile(r"\btsc\b|type-?check|typecheck|mypy|pyright|cargo check|go vet", re.I)),
    ("build", re.compile(r"\bbuild\b", re.I)),
    ("lint", re.compile(r"lint|biome|eslint|ruff|clippy|prettier|format", re.I)),
]
STRONG_VALIDATION = {"e2e", "test", "typecheck", "build"}
VCS_DELIVERY = re.compile(r"\bgit (commit|push)\b|\bgh pr (create|edit)\b|pr:create", re.I)

FAILURE_TAXONOMY: list[tuple[str, str, re.Pattern[str]]] = [
    ("missing_dependency", "Missing dependency or module", re.compile(r"cannot find module|module not found|no module named|modulenotfounderror|cannot resolve|could not resolve|err_module_not_found|package .* not found", re.I)),
    ("command_not_found", "Tool not installed", re.compile(r"command not found|not recognized as|no such file or directory: '?(npx|pnpm|yarn|docker|psql)|enoent.*spawn", re.I)),
    ("service_unavailable", "Service or database not running", re.compile(r"econnrefused|connection refused|can't reach database|could not connect|docker daemon|is the docker daemon running|p1001|redis.*(refused|unavailable)|port \d+ .*(not|refused)", re.I)),
    ("env_config", "Missing environment configuration", re.compile(r"(env|environment) var\w* .*(missing|not set|undefined|required)|missing required env|\.env\b.*not found|invalid environment|DATABASE_URL", re.I)),
    ("auth", "Authentication or authorization", re.compile(r"\b(401|403)\b|unauthori[sz]ed|forbidden|not logged in|authentication (failed|required)|permission to .* denied|login required|invalid token", re.I)),
    ("sandbox_permission", "Agent permission or sandbox block", re.compile(r"permission denied|operation not permitted|requires approval|blocked by (policy|hook|sandbox)|not allowed|user (rejected|denied)|eacces", re.I)),
    ("timeout", "Timeout or hang", re.compile(r"timed? ?out|timeout|deadline exceeded|killed|sigterm|exceeded .* seconds|still running", re.I)),
    ("type_error", "Type-check errors", re.compile(r"error TS\d+|type error|is not assignable|mypy.*error|pyright.*error|does not exist on type", re.I)),
    ("test_failure", "Test failures", re.compile(r"\b\d+ (failed|failing)\b|tests? failed|assertionerror|expect\(.*\)\.|FAIL\s|✗|× ", re.I)),
    ("lint_failure", "Lint or format failures", re.compile(r"lint(ing)? (error|failed)|biome.*(error|found)|eslint.*problem|ruff.*error|formatter would", re.I)),
    ("path_error", "Wrong path or missing file", re.compile(r"no such file or directory|file not found|does not exist|enoent|is a directory|not a directory|path .* not found", re.I)),
    ("edit_mismatch", "Edit target mismatch", re.compile(r"string to replace not found|old_string|no match(es)? found|file has been (modified|changed) since|must read .* before|has not been read", re.I)),
    ("network", "Network or registry", re.compile(r"enotfound|getaddrinfo|network (error|unreachable)|econnreset|socket hang up|registry\.npmjs|rate limit|429", re.I)),
    ("git_conflict", "Git state or conflict", re.compile(r"merge conflict|conflict \(content\)|not a git repository|detached head|rejected.*non-fast-forward|nothing to commit", re.I)),
    ("tool_schema", "Tool input/schema error", re.compile(r"inputvalidationerror|invalid (input|argument|parameter)|schema|unexpected (token|argument)|unknown option|usage:", re.I)),
]

CORRECTION = re.compile(
    r"^(no\b|nope|wrong|stop\b|wait\b|don'?t\b|that'?s (not|wrong)|not what i|you (didn'?t|did not|broke|missed|forgot)|why (did|is|are) (you|it|this)|revert|undo|"
    r"actually,? let'?s not|but (the|it|this) .{0,40}(should|shouldn'?t))|"
    r"\b(still (broken|failing|not working|wrong|running)|doesn'?t work|didn'?t work|that broke|this is wrong|not correct|i didn'?t (mean|want|ask)|"
    r"shouldn'?t have|you don'?t (seem|need|have to)|have we lost|we lost|lost (the|my|our) (progress|changes|work)|kill it|stop (it|this|that)|not supposed to)\b",
    re.I,
)
NUDGE = re.compile(r"^\s*(continue|go on|keep going|carry on|proceed|resume|are you (stuck|done|there|still)|any (update|progress)|status\??$|still working|is it done|did it (work|finish|pass)|pushed\s*\?|what happened|try again|try\s*$|retry|done\s*\?|and\s*\?|well\s*\?|its done i think)", re.I)
APPROVAL = re.compile(r"^(yes|yep|ok(ay)?|go ahead|continue|proceed|lgtm|looks good|ship it|do it|sounds good|perfect|great|thanks)\b", re.I)
INTERRUPT = re.compile(r"\[request interrupted|interrupted by user|<turn_aborted>|user cancelled", re.I)
AUTOMATED = re.compile(r"^\s*<(heartbeat|system|scheduled|automation|task-notification|command-name|local-command|alan_agent_context)|^\s*\[(cron|scheduled)\]|^Caveat: The messages below", re.I)
WORKFLOW_PROMPT = re.compile(r"^\s*(You are (the|an?) [\w -]{3,60} agent\b|Objective:|## (Task|Workflow)\b|<workflow|\[workflow\])", re.I)
RELAY_PROMPT = re.compile(r"^\s*\[(Alan|Slack|Teams|Linear|Jira|GitHub)\][^\n]{0,80}\b(is asking you|mentioned you|says|asked)\b", re.I)
CONTINUATION_PROMPT = re.compile(r"^\s*(Your turn stopped|Continue (from|where)|This session is being continued|Resume\b)", re.I)
ACCEPTANCE = re.compile(r"\b(should|must|acceptance|expected|so that|when .* then|make sure|ensure|verify|until)\b", re.I)
PATH_HINT = re.compile(r"[\w.-]+/[\w./-]+\.\w{1,5}\b|`[^`]+`|\b[\w-]+\.(ts|tsx|js|py|go|rs|java|rb|sql|md|json|ya?ml)\b")
LINK_HINT = re.compile(r"https?://|\b[A-Z]{2,6}-\d{1,6}\b|#\d{2,6}\b")
BLOCKED = re.compile(r"\b(i (couldn'?t|could not|can'?t|cannot|was unable to|wasn'?t able to)|i didn'?t (fix|change|make)|blocked( on| by)?|unable to (run|reproduce|start|connect)|need (either|access|you to)|no changes (were )?made|i made no changes)\b", re.I)
COMPLETION_CLAIM = re.compile(r"\b(done|fixed|implemented|completed|all tests pass|tests pass(ed)?|works now|resolved|ready for review|pr (is )?(open|created))\b", re.I)
SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|(?i:(password|secret|token|api[_-]?key)\s*[=:]\s*)\S+)")


def redact(text: str, limit: int = 220) -> str:
    clean = SECRET.sub("[redacted]", text or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


@dataclass
class ToolCall:
    index: int
    name: str
    category: str
    command: str
    paths: list[str]
    failed: bool = False
    failure_kind: str = ""
    output: str = ""
    content: str = ""
    categories: set = field(default_factory=set)
    check: str = ""
    timestamp: str = ""


@dataclass
class SessionMetrics:
    id: str
    source: str
    provider: str
    model: str
    title: str
    started: str
    ended: str
    duration_min: float | None
    has_tool_data: bool
    user_prompts: int = 0
    automated_prompts: int = 0
    prompt_origins: dict[str, int] = field(default_factory=dict)
    corrections: int = 0
    approvals: int = 0
    nudges: int = 0
    interrupts: int = 0
    first_prompt: str = ""
    first_prompt_words: int = 0
    prompt_specificity: int = 0
    tool_calls: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)
    calls_before_first_edit: int | None = None
    files_read_before_first_edit: int = 0
    distinct_files_read: int = 0
    distinct_files_edited: int = 0
    blind_edits: int = 0
    reread_churn: int = 0
    index_used: bool = False
    read_agent_doc: bool = False
    plan_used: bool = False
    delegated: bool = False
    validations: int = 0
    validated_after_last_edit: bool = False
    validation_kinds: dict[str, int] = field(default_factory=dict)
    verification_depth: str = "none"
    validation_failures: int = 0
    failed_calls: int = 0
    failure_kinds: dict[str, int] = field(default_factory=dict)
    loops: list[dict] = field(default_factory=list)
    delivered: bool = False
    claimed_done: bool = False
    claimed_without_validation: bool = False
    ended_on_failure: bool = False
    reported_blocked: bool = False
    reverted: bool = False
    checked_after_last_edit: bool = False
    passing_check: bool = False
    in_progress: bool = False
    workspace: str = ""
    launch_cwd: str = ""
    risks: list[dict] = field(default_factory=list)
    longest_autonomous_run: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_read: int = 0
    archetype: str = ""
    excerpts: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list, repr=False)


HEREDOC_BODY = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)
WRAPPER_PREFIX = re.compile(r"^\s*(\(|\)|time\s+|timeout\s+(-\S+\s+)*\d+[smh]?\s+|(\w+=\S*\s+)+|env\s+(\w+=\S*\s+)*|nice\s+|stdbuf\s+\S+\s+)+")
DIAGNOSTIC_FLAGS = re.compile(r"--(showConfig|traceResolution|listFiles|version|help|explainFiles|print-config)\b|\s-v\s*$|--dry-run")
PROBE_HEADS = re.compile(r"^(ls|which|test|\[|pg_isready|command -v|stat|file|type|grep|rg|find|readlink|realpath|nc|curl -s?I?)\b")
PASS_EVIDENCE = re.compile(r"\b(tsc|exit|status|rc|code)\s*[=:]\s*0\b|\b\d+ passed\b|\bpassed\b|found 0 errors|0 errors|no (type )?errors|exit[ =:]+0\b|✓|checked \d+ files? in|all checks passed|no issues|\bok\b", re.I)
FAIL_EVIDENCE = re.compile(r"\b[1-9]\d* (failed|failing|errors?)\b|error TS\d+|\bFAIL\b|✗|× |ERR!|Error: |found [1-9]\d* errors?|exit(ed)?( with)?( code)?[ =:]+[1-9]", re.I)


def shell_segments(command: str) -> list[str]:
    body_free = HEREDOC_BODY.sub("<<EOF", command or "")
    parts = re.split(r"\s*(?:&&|\|\||;|\n)\s*", body_free)
    segments = []
    for part in parts:
        for piece in part.split(" | ")[:1]:
            piece = WRAPPER_PREFIX.sub("", piece.strip()).strip()
            if piece and not piece.startswith("cd ") and piece != "cd":
                segments.append(piece)
    return segments


VALIDATE_HEAD = re.compile(r"^(npx|npm|pnpm|yarn|bun|bunx|node|tsx|ts-node|vitest|jest|mocha|pytest|python3?|uv|poetry|tsc|biome|eslint|prettier|ruff|mypy|pyright|go|cargo|make|just|mvn|gradle|\./gradlew|dotnet|turbo|nx|playwright|cypress|deno|rspec|bundle|phpunit|composer|swift|xcodebuild|\./[\w./-]+|(?:[\w./-]*/)?node_modules/\.bin/[\w-]+|/[\w./-]+/bin/[\w-]+)\b")


def segment_category(segment: str) -> str:
    if VCS_DELIVERY.search(segment):
        return "deliver"
    if SHELL_READ.match(segment):
        return "read"
    if SHELL_SEARCH.match(segment):
        return "search"
    if DIAGNOSTIC_FLAGS.search(segment):
        return "shell"
    if VALIDATE_HEAD.match(segment) and (VALIDATION.search(segment) or VALIDATION_KINDS[1][1].search(segment)):
        return "validate"
    return "shell"


PRIORITY = ("deliver", "edit", "validate", "read", "search", "shell")


def shell_categories(command: str) -> tuple[str, set[str]]:
    categories = {segment_category(s) for s in shell_segments(command)}
    if SHELL_EDIT.search(HEREDOC_BODY.sub(lambda m: m.group(0) if re.search(r"open\(|write_text|writeFileSync|\.replace\(", m.group(0)) else "<<EOF", command or "")):
        categories.add("edit")
    categories = categories or {"shell"}
    primary = next(c for c in PRIORITY if c in categories)
    return primary, categories


def output_is_filtered(command: str) -> bool:
    return bool(re.search(r"\|\s*(grep|head|tail|wc|sed|awk|cut|sort|uniq)\b|>\s*/dev/null|>\s*[\w./-]+\.(txt|log)\b", HEREDOC_BODY.sub("", command or "")))


def validation_result(command: str, output: str, failed: bool) -> str:
    """Return passed, failed or uncertain for a validation command."""
    summary = re.findall(r"\b(\d+) (passed|failed)\b", output or "")
    if summary:
        return "failed" if failed or any(kind == "failed" and int(count) for count, kind in summary) else "passed"
    if failed or FAIL_EVIDENCE.search(output or ""):
        return "failed"
    silent_checker = bool(re.search(r"\b(tsc|biome|eslint|ruff|mypy)\b", command or ""))
    if output_is_filtered(command) and not PASS_EVIDENCE.search(output or ""):
        tail_only = not re.search(r"\|\s*(grep|wc|sed|awk|cut)\b|>\s*/dev/null", command or "")
        if not (silent_checker and tail_only and not (output or "").strip()):
            return "uncertain"
    if not (output or "").strip() and not re.search(r"\btsc\b|biome|eslint|ruff", command or ""):
        return "uncertain"
    return "passed"


BACKGROUND = re.compile(r"(moved to the background|running in background|Output is being written to)", re.I)


def defer(call: "ToolCall", output: str, deferred: dict[str, "ToolCall"]) -> None:
    """Remember where a validation's real result will appear when it ran in the background or into a file."""
    targets = re.findall(r"Output is being written to:?\s*(\S+?)\.?(?:\s|$)", output or "") if BACKGROUND.search(output or "") else []
    targets += re.findall(r"(?<![0-9&])>\s*(/[\w./-]+\.(?:txt|log|out|output))", HEREDOC_BODY.sub("", call.command or ""))
    for target in targets:
        deferred[target] = call
    if targets and call.check == "uncertain":
        call.check = "pending"


def resolve_deferred(reader: "ToolCall", output: str, deferred: dict[str, "ToolCall"]) -> None:
    for target, call in list(deferred.items()):
        if target in (reader.command or "") or Path(target).name in (reader.command or ""):
            verdict = "failed" if FAIL_EVIDENCE.search(output or "") else "passed" if PASS_EVIDENCE.search(output or "") else ""
            if verdict:
                call.check = verdict
                if verdict == "failed" and not call.failed:
                    call.failed = True
                    call.failure_kind = classify_failure(output)
                del deferred[target]


def is_probe(command: str) -> bool:
    segments = shell_segments(command)
    return bool(segments) and all(PROBE_HEADS.match(s) or segment_category(s) in ("read", "search") for s in segments)


def tool_category(name: str, command: str) -> str:
    key = name.lower().split("__")[-1]
    if key in INDEX_TOOLS:
        return "index"
    if key in EDIT_TOOLS:
        return "edit"
    if key in READ_TOOLS:
        return "read"
    if key in SEARCH_TOOLS:
        return "search"
    if key in PLAN_TOOLS:
        return "plan"
    if key in DELEGATE_TOOLS:
        return "delegate"
    if key in WEB_TOOLS:
        return "web"
    if key == "skill":
        return "skill"
    if name.lower().startswith("mcp__") or name.lower().startswith("mcp_"):
        return "mcp"
    if command:
        return shell_categories(command)[0]
    return "other"


def extract_command(args: Any) -> str:
    if isinstance(args, dict):
        for key in ("command", "cmd", "script"):
            value = args.get(key)
            if isinstance(value, list):
                return " ".join(str(v) for v in value)
            if isinstance(value, str):
                return value
    if isinstance(args, str) and args.strip().startswith(("{", "[")) is False:
        return ""
    return ""


def extract_paths(args: Any, command: str) -> list[str]:
    paths: list[str] = []
    if isinstance(args, dict):
        for key in ("file_path", "path", "filePath", "notebook_path", "target_file", "filename"):
            value = args.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
        patch = args.get("patch") or args.get("input")
        if isinstance(patch, str):
            paths.extend(re.findall(r"\*\*\* (?:Update|Add|Delete) File: (.+)", patch))
    bare = re.sub(r"^\s*cd [^&;]+(&&|;)\s*", "", command or "")
    for segment in shell_segments(command or ""):
        if SHELL_READ.match(segment):
            paths.extend(p for p in re.findall(r"(?:\s|['\"])([\w./@+-]+\.\w{1,6})\b", " " + segment) if "/" in p)
    if bare and re.match(r"(rg|grep)\b", bare):
        paths.extend(p for p in re.findall(r"\s([\w./@+-]+\.(?:ts|tsx|js|jsx|py|go|rs|java|prisma|json|ya?ml|md))\b", bare) if "/" in p)
    if command and SHELL_EDIT.search(command):
        paths.extend(re.findall(r"(?:p|path|file)\s*=\s*['\"]([\w./-]+\.\w+)['\"]", command))
        paths.extend(re.findall(r"(?:sed -i(?:\s+'[^']*'|\s+\"[^\"]*\"|\s+-e\s+\S+)*|cat\s*>|>>?)\s*([\w./-]+\.\w{1,6})\b", command))
    if command and "apply_patch" in command:
        paths.extend(re.findall(r"\*\*\* (?:Update|Add|Delete) File: (\S+)", command))
    return [p.strip() for p in paths if p.strip()]


_TRACKED: dict[str, set[str] | None] = {}


def tracked_files(workspace: str) -> set[str] | None:
    """Files tracked at HEAD in the session's workspace, so new files are not counted as blind edits."""
    if not workspace or not Path(workspace, ".git").exists():
        return None
    if workspace not in _TRACKED:
        try:
            out = subprocess.run(["git", "-C", workspace, "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True, timeout=30).stdout
            _TRACKED[workspace] = set(out.splitlines()) or None
        except (subprocess.SubprocessError, OSError):
            _TRACKED[workspace] = None
    return _TRACKED[workspace]


def existed_before(path: str, workspace: str, tracked: set[str] | None, call: "ToolCall") -> bool:
    if tracked is not None:
        rel = path[len(workspace.rstrip("/")) + 1:] if workspace and path.startswith(workspace.rstrip("/") + "/") else path.lstrip("./")
        return rel in tracked
    return call.name.lower() in ("edit", "multiedit", "str_replace", "str_replace_editor", "edit_file", "replace") or bool(
        re.search(r"\bsed -i|\bperl -p?i|\.replace\(|Update File:", call.command))


def revert_targets(command: str) -> list[str] | None:
    """Paths a command restores to HEAD; ["*"] for the whole tree; None when it reverts nothing."""
    if not command:
        return None
    if re.search(r"\bgit (stash(\s+push)?(\s+-u)?\s*($|[;&|])|checkout -- \.|restore \.|reset --hard)", command):
        return ["*"]
    paths: list[str] = []
    for match in re.finditer(r"\bgit (?:checkout --|restore(?: --staged| --worktree)*)\s+([^;&|]+)", command):
        paths.extend(match.group(1).split())
    paths.extend(re.findall(r"\bgit show HEAD:(\S+)\s*>", command))
    return paths or None


def same_file(a: str, b: str) -> bool:
    a, b = a.lstrip("./"), b.lstrip("./")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def edit_content(args: object, command: str) -> str:
    if isinstance(args, dict):
        parts = [str(args.get(k) or "") for k in ("new_string", "content", "patch", "input", "new_str", "file_text")]
        edits = args.get("edits")
        if isinstance(edits, list):
            parts.extend(str(e.get("new_string") or "") for e in edits if isinstance(e, dict))
        text = "\n".join(p for p in parts if p)
        if text:
            return text[:6000]
    return command[:6000] if SHELL_EDIT.search(command or "") else ""


def classify_failure(text: str) -> str:
    for key, _label, pattern in FAILURE_TAXONOMY:
        if pattern.search(text):
            return key
    return "other"


def normalize_command(command: str) -> str:
    command = re.sub(r"^cd [^&;]+(&&|;)\s*", "", command.strip())
    command = re.sub(r"\s+", " ", command)
    return command[:120]


def event_time(event: NormalizedSessionEvent) -> str:
    if event.timestamp:
        return event.timestamp
    raw = event.raw or {}
    for key in ("timestamp", "created_at", "createdAt", "time"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def usage_from(raw: dict) -> tuple[int, int, int]:
    usage = (raw.get("message") or {}).get("usage") if isinstance(raw.get("message"), dict) else None
    if isinstance(usage, dict):
        return (int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0),
                int(usage.get("output_tokens") or 0), int(usage.get("cache_read_input_tokens") or 0))
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None
    if last:
        return (int(last.get("input_tokens") or 0) - int(last.get("cached_input_tokens") or 0),
                int(last.get("output_tokens") or 0), int(last.get("cached_input_tokens") or 0))
    return (0, 0, 0)


def model_from(events: list[NormalizedSessionEvent]) -> str:
    for event in events:
        raw = event.raw or {}
        message = raw.get("message")
        if isinstance(message, dict) and isinstance(message.get("model"), str) and not message["model"].startswith("<"):
            return message["model"]
        payload = raw.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("model"), str):
            return payload["model"]
    return ""


def prompt_specificity(text: str) -> int:
    score = 0
    words = len(text.split())
    score += 1 if words >= 25 else 0
    score += 1 if PATH_HINT.search(text) else 0
    score += 1 if ACCEPTANCE.search(text) else 0
    score += 1 if LINK_HINT.search(text) else 0
    score += 1 if re.search(r"\b(test|verify|check|run|lint|typecheck|screenshot)\b", text, re.I) else 0
    return score


def analyze_dialogue(
    session_id: str,
    source: str,
    provider: str,
    title: str,
    messages: list[tuple[str, str, str]],
) -> SessionMetrics:
    """Dialogue-only analysis for exports that carry no tool events."""
    times = [parse_time(t) for _, _, t in messages if parse_time(t)]
    started = min(times).isoformat() if times else ""
    ended = max(times).isoformat() if times else ""
    duration = round((max(times) - min(times)).total_seconds() / 60, 1) if len(times) >= 2 else None
    metrics = SessionMetrics(session_id, source, provider, "", title, started, ended, duration, False)
    apply_human_signals(metrics, [(role, text) for role, text, _ in messages])
    assistant_text = " ".join(text for role, text, _ in messages if role == "assistant")
    last_assistant = next((text for role, text, _ in reversed(messages) if role == "assistant"), "")
    metrics.claimed_done = bool(COMPLETION_CLAIM.search(last_assistant))
    metrics.delivered = bool(re.search(r"\b(pull request|PR #?\d+|opened (a|the) PR|pushed|merged)\b", assistant_text, re.I))
    metrics.ended_on_failure = bool(re.search(r"\b(blocked|couldn'?t|could not|unable to|failed to|not able to)\b", last_assistant[-600:], re.I))
    metrics.archetype = classify(metrics)
    return metrics


def apply_human_signals(metrics: SessionMetrics, turns: list[tuple[str, str]]) -> None:
    human = [(i, text) for i, (role, text) in enumerate(turns) if role == "user" and text.strip()]
    origins: Counter = Counter()
    for position, (_, text) in enumerate(human):
        stripped = strip_harness(text)
        if not stripped:
            continue
        origin = prompt_origin(stripped)
        origins[origin] += 1
        if origin in ("scheduled", "workflow", "continuation"):
            metrics.automated_prompts += 1
            continue
        if origin == "interrupt":
            metrics.interrupts += 1
            continue
        metrics.user_prompts += 1
        if not metrics.first_prompt:
            metrics.first_prompt = redact(stripped, 400)
            metrics.first_prompt_words = len(stripped.split())
            metrics.prompt_specificity = prompt_specificity(stripped)
            continue
        head = stripped[:200]
        if NUDGE.search(head) and len(stripped) < 80:
            metrics.nudges += 1
            continue
        if CORRECTION.search(head):
            metrics.corrections += 1
            if len(metrics.excerpts) < 6:
                metrics.excerpts.append({"kind": "correction", "text": redact(stripped)})
        elif APPROVAL.search(head):
            metrics.approvals += 1
    metrics.prompt_origins = dict(origins)


HARNESS_BLOCK = re.compile(r"<(system-reminder|alan_agent_context(_update)?|environment_context|user_instructions|command-message|local-command-stdout|ide_[a-z_]+)[^>]*>.*?</\1>", re.S | re.I)


PROMPT_EXTRACTORS: list[re.Pattern[str]] = []


def set_prompt_extractors(patterns: list[str]) -> None:
    PROMPT_EXTRACTORS[:] = [re.compile(p, re.S | re.I) for p in patterns]


def strip_harness(text: str) -> str:
    clean = HARNESS_BLOCK.sub("", text or "").strip()
    for pattern in PROMPT_EXTRACTORS:
        match = pattern.search(clean)
        if match:
            return (match.group(1) if match.groups() else match.group(0)).strip()
    return clean


def prompt_origin(text: str) -> str:
    if INTERRUPT.search(text[:200]):
        return "interrupt"
    if AUTOMATED.search(text):
        return "scheduled"
    if WORKFLOW_PROMPT.search(text):
        return "workflow"
    if CONTINUATION_PROMPT.search(text):
        return "continuation"
    if RELAY_PROMPT.search(text):
        return "chat_relay"
    return "direct"


def analyze_events(
    session_id: str,
    source: str,
    events: list[NormalizedSessionEvent],
    title: str = "",
    roots: list[str] | None = None,
) -> SessionMetrics:
    provider = next((e.provider_kind for e in events if e.provider_kind), "unknown")
    times = [parse_time(event_time(e)) for e in events]
    times = [t for t in times if t]
    started = min(times).isoformat() if times else ""
    ended = max(times).isoformat() if times else ""
    duration = round((max(times) - min(times)).total_seconds() / 60, 1) if len(times) >= 2 else None
    metrics = SessionMetrics(session_id, source, provider, model_from(events), title, started, ended, duration, True)

    turns: list[tuple[str, str]] = []
    deferred: dict[str, ToolCall] = {}
    calls: dict[str, ToolCall] = {}
    ordered: list[ToolCall] = []
    seen_usage: set[str] = set()
    for event in events:
        if event.kind == "user_message":
            turns.append(("user", event.text))
        elif event.kind == "assistant_message":
            turns.append(("assistant", event.text))
        elif event.kind == "tool_call":
            command = extract_command(event.tool_args)
            call = ToolCall(len(ordered), event.tool_name or "tool", tool_category(event.tool_name or "", command),
                            command, extract_paths(event.tool_args, command), timestamp=event_time(event))
            call.categories = shell_categories(command)[1] if command and call.category in PRIORITY else {call.category}
            if call.category == "edit":
                call.content = edit_content(event.tool_args, command)
            ordered.append(call)
            if event.tool_call_id:
                calls[event.tool_call_id] = call
            turns.append(("tool", call.name))
        elif event.kind == "tool_result":
            call = calls.get(event.tool_call_id) or (ordered[-1] if ordered and not event.tool_call_id else None)
            if call is None:
                continue
            text = event.text or ""
            call.output = text[:4000]
            exit_failed = bool(re.search(r"exit code [1-9]|exited with code [1-9]|Exit code: [1-9]|\"exit_code\":\s*[1-9]", text[:600]))
            if "validate" in call.categories:
                call.check = validation_result(call.command, text, event.failed or exit_failed)
            resolve_deferred(call, text, deferred)
            if "validate" in call.categories:
                defer(call, text, deferred)
            if call.category == "edit" and call.command:
                edit_broke = bool(re.search(r"Traceback|SyntaxError|No such file|cannot open|Permission denied|string to replace not found", text))
                event_failed = edit_broke
                exit_failed = edit_broke
            else:
                event_failed = event.failed
            if event_failed or exit_failed or (call.check == "failed" and call.category != "edit"):
                if call.command and is_probe(call.command) and call.check != "failed":
                    call.failure_kind = "probe_miss"
                else:
                    call.failed = True
                    call.failure_kind = classify_failure(text)
        raw = event.raw or {}
        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
        usage_key = str(message.get("id") or raw.get("uuid") or id(raw))
        if usage_key not in seen_usage and event.kind in ("assistant_message", "tool_call", "metadata"):
            seen_usage.add(usage_key)
            tin, tout, tcache = usage_from(raw)
            metrics.tokens_in += tin
            metrics.tokens_out += tout
            metrics.tokens_cache_read += tcache

    apply_human_signals(metrics, [(r, t) for r, t in turns if r != "tool"])
    for _, text in turns:
        if INTERRUPT.search(text or ""):
            metrics.interrupts += 0
    metrics.tool_calls = len(ordered)
    metrics.calls = [{"category": c.category, "command": c.command[:400], "output": c.output[:600], "content": c.content, "paths": c.paths[:4], "failed": c.failed} for c in ordered]
    metrics.category_counts = dict(Counter(c.category for c in ordered))

    run = 0
    for role, _ in turns:
        if role == "tool":
            run += 1
            metrics.longest_autonomous_run = max(metrics.longest_autonomous_run, run)
        elif role == "user":
            run = 0

    read_counts: Counter = Counter()
    read_paths: list[str] = []
    read_before_edit: set[str] = set()
    edited: set[str] = set()
    surviving: set[str] = set()
    first_edit_index: int | None = None
    last_edit_index: int | None = None
    workspace = infer_workspace(events, ordered, roots or [])
    tracked = tracked_files(workspace)
    for call in ordered:
        if "read" in call.categories or call.category == "read":
            for path in call.paths:
                key = next((p for p in read_paths if same_file(p, path)), path)
                if key == path and path not in read_paths:
                    read_paths.append(path)
                read_counts[key] += 1
                if first_edit_index is None:
                    read_before_edit.add(path)
        reverted_paths = revert_targets(call.command)
        if reverted_paths is not None:
            surviving = set() if reverted_paths == ["*"] else {p for p in surviving if not any(same_file(p, r) for r in reverted_paths)}
        if (call.category == "edit" or "edit" in call.categories) and not call.failed:
            if first_edit_index is None:
                first_edit_index = call.index
            last_edit_index = call.index
            for path in call.paths:
                if not any(same_file(path, e) for e in edited) and not any(same_file(path, p) for p in read_paths):
                    if existed_before(path, workspace, tracked, call):
                        metrics.blind_edits += 1
                edited.add(path)
                surviving.add(path)
        if call.category in ("read", "search") and any(re.search(r"(^|/)(AGENTS|CLAUDE|GEMINI)\.md$|\.cursorrules$|copilot-instructions\.md$", p) for p in call.paths) or re.search(r"\b(AGENTS|CLAUDE)\.md\b", call.command or ""):
            metrics.read_agent_doc = True
        if call.category == "index":
            metrics.index_used = True
        if call.category == "plan":
            metrics.plan_used = True
        if call.category == "delegate":
            metrics.delegated = True
    metrics.calls_before_first_edit = first_edit_index
    metrics.files_read_before_first_edit = len(read_before_edit)
    metrics.distinct_files_read = len(read_counts)
    metrics.distinct_files_edited = len(edited)
    metrics.reread_churn = sum(n - 2 for n in read_counts.values() if n > 2)
    metrics.reverted = bool(edited) and not surviving

    validations = [c for c in ordered if "validate" in c.categories]
    metrics.validations = len(validations)
    kinds_after: set[str] = set()
    for call in validations:
        kind = kind_of(call)
        metrics.validation_kinds[kind] = metrics.validation_kinds.get(kind, 0) + 1
        if last_edit_index is not None and call.index >= last_edit_index and call.check == "passed":
            kinds_after.add(kind)
    metrics.validation_failures = sum(1 for c in validations if c.check == "failed")
    metrics.passing_check = any(c.check == "passed" and kind_of(c) in STRONG_VALIDATION for c in validations)
    if last_edit_index is not None:
        metrics.validated_after_last_edit = bool(kinds_after & STRONG_VALIDATION)
        metrics.verification_depth = next((k for k in ("e2e", "test", "typecheck", "build", "lint") if k in kinds_after), "none")
    metrics.delivered = any("deliver" in c.categories and not c.failed for c in ordered)

    failures = [c for c in ordered if c.failed]
    metrics.failed_calls = len(failures)
    metrics.failure_kinds = dict(Counter(c.failure_kind for c in failures))
    metrics.ended_on_failure = bool(ordered) and ordered[-1].failed

    by_command: dict[str, list[ToolCall]] = defaultdict(list)
    for call in failures:
        key = normalize_command(call.command) if call.command else f"{call.name}:{','.join(call.paths)[:80]}"
        by_command[key].append(call)
    for key, items in by_command.items():
        if len(items) >= 2:
            metrics.loops.append({
                "command": redact(key, 140),
                "failures": len(items),
                "kind": items[-1].failure_kind,
                "lastError": redact(first_error_line(items[-1].output), 200),
            })
    for call in failures[:4]:
        if len(metrics.excerpts) >= 8:
            break
        metrics.excerpts.append({
            "kind": "failure",
            "tool": call.name,
            "command": redact(call.command or ",".join(call.paths), 140),
            "error": redact(first_error_line(call.output), 220),
            "failureKind": call.failure_kind,
        })

    last_assistant = next((t for r, t in reversed(turns) if r == "assistant" and t), "")
    metrics.claimed_done = bool(COMPLETION_CLAIM.search(last_assistant[-1500:]))
    metrics.reported_blocked = bool(BLOCKED.search(last_assistant[:700]))
    if metrics.reported_blocked and len(metrics.excerpts) < 9:
        metrics.excerpts.append({"kind": "blocked", "text": redact(last_assistant, 320)})
    if last_edit_index is not None:
        metrics.checked_after_last_edit = any(c.index >= last_edit_index for c in validations if kind_of(c) in STRONG_VALIDATION)
    pending = {e.tool_call_id for e in events if e.kind == "tool_call" and e.tool_call_id} - {e.tool_call_id for e in events if e.kind == "tool_result"}
    metrics.in_progress = bool(pending) and not last_assistant
    metrics.launch_cwd = next((str((e.raw or {}).get("cwd")) for e in events if (e.raw or {}).get("cwd")), "")
    metrics.workspace = workspace
    metrics.risks = risk_events(ordered, metrics.workspace if roots else "", last_assistant)
    metrics.claimed_without_validation = metrics.claimed_done and bool(edited) and not metrics.validated_after_last_edit
    metrics.archetype = classify(metrics)
    return metrics


def kind_of(call: ToolCall) -> str:
    segments = [seg for seg in shell_segments(call.command) if segment_category(seg) == "validate"] or [call.command]
    for seg in segments:
        kind = next((k for k, pattern in VALIDATION_KINDS if pattern.search(seg)), "")
        if kind:
            return kind
    return "other"


RISK_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("history_rewrite", "Rewrote git history or discarded work", re.compile(r"^git (commit [^|;&]*--amend|reset --hard|push [^|;&]*(-f\b|--force)|rebase\b|clean -[a-z]*f|checkout -- \.|restore \.|stash(?!\s+(list|show|pop|apply)))", re.I)),
    ("discard_own", "Discarded its own uncommitted edits", re.compile(r"^git (checkout -- (?!\.)|restore (?!\.))", re.I)),
    ("hook_bypass", "Bypassed git hooks or checks", re.compile(r"--no-verify\b|core\.hooksPath|^HUSKY=0|^SKIP=\S+ git", re.I)),
    ("infrastructure", "Started containers, databases or services", re.compile(r"^(docker (run|compose[^|;&]* up|pull|start)\b|docker-compose up|(\S*/)?initdb\b|(\S*/)?pg_ctl\b.*\bstart|(\S*/)?postgres -D|(\S*/)?redis-server\b|systemctl (start|restart)\b)", re.I)),
    ("process_control", "Stopped processes", re.compile(r"^(pkill|killall|kill)\b", re.I)),
    ("shared_state", "Installed or changed dependencies", re.compile(r"^(npm (install|i|ci|uninstall)\b|pnpm (install|add|remove)\b|yarn (add|install)\b|pip install\b|uv (pip|add)\b|poetry add\b|go get\b|cargo add\b)", re.I)),
    ("data_access", "Queried or changed a database directly", re.compile(r"^(\S*/)?(psql|mysql|mongosh?|redis-cli|sqlite3)\b|^(npx )?prisma (db (push|execute|seed)|migrate (deploy|dev|reset))\b|^npm run prisma:(migrate|seed)", re.I)),
    ("secrets", "Read secrets or environment files", re.compile(r"^(cat|less|head|tail|grep|source|\.)\b[^|;&]*\.env(\.local|\.production|\.development)?(?=$|[\s'\"])|^printenv\b|^env$", re.I)),
    ("codegen", "Regenerated code in the workspace", re.compile(r"^(npx )?prisma generate\b|prisma:generate|graphql-codegen|buf generate", re.I)),
    ("network", "Fetched from the network", re.compile(r"^(curl|wget)\s+https?://|^npm (view|info)\b|^git clone\b", re.I)),
]


DISCLOSURE_WORDS: dict[str, tuple[str, ...]] = {
    "infrastructure": ("docker", "container", "postgres", "database", "initdb", "pg_ctl", "redis", "server"),
    "history_rewrite": ("amend", "stash", "reset --hard", "rebase", "force-push", "force push"),
    "shared_state": ("install", "dependenc"),
    "codegen": ("generate", "prisma client", "generated"),
    "discard_own": ("revert", "discard", "restored"),
    "process_control": ("stopped", "killed", "kill"),
    "data_access": ("psql", "queried", "applied the migration", "migrate deploy", "ran migrations", "against the database", "against a real", "dev database"),
    "secrets": (".env", "secret", "environment variable"),
    "hook_bypass": ("hook", "no-verify", "hookspath"),
    "network": ("curl", "fetched", "download"),
}


def risk_events(ordered: list, workspace: str, final_text: str) -> list[dict]:
    events: list[dict] = []
    final_lower = final_text.lower()
    for call in ordered:
        command = call.command or ""
        segments = shell_segments(command) if command else []
        for kind, label, pattern in RISK_RULES:
            hit = next(((seg, m) for seg in segments for m in [pattern.search(seg)] if m), None)
            match = hit[1] if hit else None
            if match:
                words = DISCLOSURE_WORDS.get(kind, ())
                events.append({"kind": kind, "label": label, "command": redact(hit[0], 160),
                               "disclosed": any(w in final_lower for w in words)})
        targets: list[str] = []
        if workspace and call.category == "edit":
            targets.extend(p for p in call.paths if p.startswith("/"))
        if workspace and command:
            targets.extend(re.findall(r"(?:>>?|tee\s+(?:-a\s+)?)\s*(/[\w./-]+)", command))
        for target in targets:
            if target.startswith(workspace.rstrip("/") + "/") or target.startswith("/dev/"):
                continue
            disclosed = target.lower() in final_lower or Path(target).name.lower() in final_lower
            if any(target.startswith(r.rstrip("/") + "/") for r in REPO_ROOTS | MATCH_ROOTS):
                events.append({"kind": "outside_workspace", "label": "Wrote into another checkout or shared state", "command": redact(target, 160), "disclosed": disclosed})
            else:
                events.append({"kind": "scratch", "label": "Left scratch files outside the workspace", "command": redact(target, 160), "disclosed": disclosed})
    seen: set[tuple[str, str]] = set()
    unique = []
    for event in events:
        key = (event["kind"], event["command"])
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return unique


def infer_workspace(events: list[NormalizedSessionEvent], ordered: list, roots: list[str]) -> str:
    counts: Counter = Counter()
    for call in ordered:
        for text in call.paths + re.findall(r"cd\s+(/[\w./-]+)", call.command or ""):
            for root in roots:
                root = root.rstrip("/")
                if text.startswith(root + "/") or text == root:
                    rest = text[len(root):].strip("/").split("/")
                    counts[root if root in REPO_ROOTS else f"{root}/{rest[0]}"] += 1
    if counts:
        return counts.most_common(1)[0][0]
    cwd = next((str((e.raw or {}).get("cwd")) for e in events if (e.raw or {}).get("cwd")), "")
    return cwd


REPO_ROOTS: set[str] = set()
MATCH_ROOTS: set[str] = set()
LOW_RISK = {"scratch", "network", "codegen", "discard_own", "process_control"}


def first_error_line(output: str) -> str:
    for line in (output or "").splitlines():
        if re.search(r"error|fail|cannot|not found|denied|refused|✗|×|exception", line, re.I):
            return line.strip()
    return (output or "").strip().splitlines()[0] if (output or "").strip() else ""


ARCHETYPES: dict[str, str] = {
    "verified_delivery": "Verified delivery",
    "reported_delivery": "Delivered (reported in chat)",
    "no_change_needed": "Verified nothing needed changing",
    "unverified_change": "Unverified change",
    "struggling": "Stuck in failure loops",
    "blocked": "Blocked by environment or missing context",
    "exploration": "Exploration / Q&A",
    "operations": "Ops & environment work",
    "automation": "Scheduled automation",
    "conversation": "Conversation only",
}


def classify(m: SessionMetrics) -> str:
    if m.automated_prompts and m.automated_prompts >= max(1, m.user_prompts):
        return "automation"
    if not m.has_tool_data:
        if m.delivered:
            return "reported_delivery"
        if m.ended_on_failure or m.corrections >= 2:
            return "struggling"
        return "conversation" if m.user_prompts <= 1 and not m.claimed_done else "exploration"
    edits = 0 if m.reverted else m.category_counts.get("edit", 0)
    if not edits and m.passing_check and m.reported_blocked and re.search(r"reproduce|no (type )?errors|exits? 0|passes|clean", " ".join(e.get("text", "") for e in m.excerpts if e.get("kind") == "blocked"), re.I):
        return "no_change_needed"
    if m.reported_blocked and not edits:
        return "blocked"
    if edits and m.validated_after_last_edit:
        return "verified_delivery"
    if m.loops and (m.failed_calls >= max(4, m.tool_calls * 0.25) or m.ended_on_failure):
        return "struggling"
    if edits:
        return "verified_delivery" if m.validated_after_last_edit else "unverified_change"
    if m.tool_calls == 0:
        return "conversation"
    shell = m.category_counts.get("shell", 0) + m.category_counts.get("validate", 0)
    if shell > (m.category_counts.get("read", 0) + m.category_counts.get("search", 0)):
        return "operations"
    return "exploration"


def safe_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:10]


def session_mentions_repo(events: list[NormalizedSessionEvent], repo_paths: list[str]) -> bool:
    if not repo_paths:
        return True
    for event in events[:400]:
        blob = (event.cwd or "") + json.dumps(event.tool_args, default=str)[:400] if event.kind == "tool_call" else (event.cwd or "")
        raw_cwd = (event.raw or {}).get("cwd") or ((event.raw or {}).get("payload") or {}).get("cwd") if isinstance((event.raw or {}).get("payload"), dict) else (event.raw or {}).get("cwd")
        blob += str(raw_cwd or "")
        if any(p in blob for p in repo_paths):
            return True
    return False


def load_alan_export(path: Path) -> SessionMetrics | None:
    try:
        data = json.loads(path.read_text(errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return None
    messages = [(m.get("role", ""), m.get("content") or "", m.get("createdAt") or "") for m in data["messages"] if isinstance(m, dict)]
    metrics = analyze_dialogue(str(data.get("sessionId") or path.stem), "alan-export", "alan", str(data.get("title") or ""), messages)
    return metrics


def analyze_paths(paths: Iterable[Path], repo_paths: list[str], limit: int | None = None, repo_roots: list[str] | None = None) -> tuple[list[SessionMetrics], dict[str, int]]:
    results: list[SessionMetrics] = []
    skipped = Counter()
    REPO_ROOTS.clear()
    REPO_ROOTS.update(r.rstrip("/") for r in (repo_roots or []))
    MATCH_ROOTS.clear()
    MATCH_ROOTS.update(r.rstrip("/") for r in repo_paths if r.rstrip("/") not in REPO_ROOTS)
    files = sorted(set(paths), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for path in files:
        if limit and len(results) >= limit:
            break
        if path.suffix == ".json":
            metrics = load_alan_export(path)
            if metrics is None:
                skipped["unreadable"] += 1
                continue
            results.append(metrics)
            continue
        try:
            events = parse_session_events(path)
        except Exception:
            skipped["unreadable"] += 1
            continue
        if not any(e.kind in ("tool_call", "user_message") for e in events):
            skipped["empty"] += 1
            continue
        if not session_mentions_repo(events, repo_paths):
            skipped["other_repo"] += 1
            continue
        source = "subagent" if "/subagents/" in str(path) else "local"
        metrics = analyze_events(path.stem, source, events, roots=repo_paths)
        if metrics.in_progress:
            skipped["in_progress"] += 1
            continue
        results.append(metrics)
    return results, dict(skipped)


def pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def aggregate(sessions: list[SessionMetrics]) -> dict:
    tool = [s for s in sessions if s.has_tool_data]
    editing = [s for s in tool if s.category_counts.get("edit") and not s.reverted]
    human = [s for s in sessions if s.user_prompts]
    all_calls = sum(s.tool_calls for s in tool)
    categories = Counter()
    failure_kinds = Counter()
    providers = Counter(s.provider for s in sessions)
    models = Counter(s.model for s in tool if s.model)
    for s in tool:
        categories.update(s.category_counts)
        failure_kinds.update(s.failure_kinds)
    loops = sorted((dict(loop, session=s.id) for s in tool for loop in s.loops), key=lambda l: -l["failures"])
    failed_calls = sum(s.failed_calls for s in tool)
    loop_calls = sum(l["failures"] for l in loops)
    reread = sum(s.reread_churn for s in tool)
    corrections = sum(s.corrections for s in sessions)
    durations = [s.duration_min for s in sessions if s.duration_min]
    explore = categories.get("read", 0) + categories.get("search", 0) + categories.get("index", 0)
    labels = {key: label for key, label, _ in FAILURE_TAXONOMY}
    labels["other"] = "Other errors"
    return {
        "sessionCount": len(sessions),
        "toolSessionCount": len(tool),
        "dialogueOnlyCount": len(sessions) - len(tool),
        "toolCalls": all_calls,
        "providers": dict(providers),
        "models": dict(models.most_common(6)),
        "byModel": by_model(sessions),
        "sources": dict(Counter(s.source for s in sessions)),
        "archetypes": {ARCHETYPES[k]: v for k, v in Counter(s.archetype for s in sessions).most_common()},
        "toolMix": dict(categories.most_common()),
        "exploration": {
            "exploreShare": pct(explore, all_calls),
            "medianCallsBeforeFirstEdit": median([s.calls_before_first_edit for s in editing if s.calls_before_first_edit is not None]),
            "medianFilesReadBeforeFirstEdit": median([s.files_read_before_first_edit for s in editing]),
            "editSessionsWithBlindEdits": pct(sum(1 for s in editing if s.blind_edits), len(editing)),
            "blindEdits": sum(s.blind_edits for s in editing),
            "rereadCalls": reread,
            "indexUseRate": pct(sum(1 for s in tool if s.index_used), len(tool)),
            "searchToReadRatio": round((categories.get("search", 0) + categories.get("index", 0)) / max(1, categories.get("read", 0)), 2),
            "editSessionsWithoutExploration": pct(sum(1 for s in editing if (s.calls_before_first_edit or 0) <= 1), len(editing)),
        },
        "workflow": {
            "planUseRate": pct(sum(1 for s in tool if s.plan_used), len(tool)),
            "delegationRate": pct(sum(1 for s in tool if s.delegated), len(tool)),
            "skillUseRate": pct(sum(1 for s in tool if s.category_counts.get("skill")), len(tool)),
            "mcpUseRate": pct(sum(1 for s in tool if s.category_counts.get("mcp")), len(tool)),
            "deliveryRate": pct(sum(1 for s in editing if s.delivered), len(editing)),
            "medianToolCalls": median([s.tool_calls for s in tool]),
            "medianDurationMin": median(durations),
            "medianAutonomousRun": median([s.longest_autonomous_run for s in tool]),
        },
        "verification": {
            "editSessions": len(editing),
            "validatedAfterLastEdit": pct(sum(1 for s in editing if s.validated_after_last_edit), len(editing)),
            "editSessionsWithAnyValidation": pct(sum(1 for s in editing if s.validations), len(editing)),
            "checkedAfterLastEdit": pct(sum(1 for s in editing if s.checked_after_last_edit), len(editing)),
            "revertedSessions": sum(1 for s in tool if s.reverted),
            "claimedDoneWithoutValidation": pct(sum(1 for s in editing if s.claimed_without_validation), len(editing)),
            "validationFailureRate": pct(sum(s.validation_failures for s in tool), sum(s.validations for s in tool)),
        },
        "failures": {
            "failedCalls": failed_calls,
            "failureRate": pct(failed_calls, all_calls),
            "sessionsWithLoops": pct(sum(1 for s in tool if s.loops), len(tool)),
            "loopCalls": loop_calls,
            "kinds": [{"kind": k, "label": labels.get(k, k), "count": v} for k, v in failure_kinds.most_common()],
            "topLoops": loops[:10],
            "endedOnFailure": pct(sum(1 for s in tool if s.ended_on_failure), len(tool)),
        },
        "humans": {
            "promptOrigins": dict(sum((Counter(s.prompt_origins) for s in sessions), Counter())),
            "humanPrompts": sum(s.user_prompts for s in sessions),
            "automatedPrompts": sum(s.automated_prompts for s in sessions),
            "automationShare": pct(sum(s.automated_prompts for s in sessions), sum(s.user_prompts + s.automated_prompts for s in sessions)),
            "medianFirstPromptWords": median([s.first_prompt_words for s in human]),
            "vaguePromptRate": pct(sum(1 for s in human if s.prompt_specificity <= 1), len(human)),
            "specificPromptRate": pct(sum(1 for s in human if s.prompt_specificity >= 3), len(human)),
            "correctionsPerSession": round(corrections / max(1, len(human)), 2),
            "sessionsWithCorrections": pct(sum(1 for s in human if s.corrections), len(human)),
            "interrupts": sum(s.interrupts for s in sessions),
            "nudgesPerSession": round(sum(s.nudges for s in sessions) / max(1, len(human)), 2),
            "sessionsWithNudges": pct(sum(1 for s in human if s.nudges), len(human)),
            "singleShotRate": pct(sum(1 for s in human if s.user_prompts == 1), len(human)),
            "specificityVsOutcome": specificity_outcome(sessions),
        },
        "tokens": {
            "input": sum(s.tokens_in for s in tool),
            "output": sum(s.tokens_out for s in tool),
            "cacheRead": sum(s.tokens_cache_read for s in tool),
            "cacheHitRate": pct(sum(s.tokens_cache_read for s in tool), sum(s.tokens_cache_read + s.tokens_in for s in tool)),
        },
        "risks": risk_summary(tool),
        "dataQuality": {
            "launchedOutsideRepo": sum(1 for s in tool if s.launch_cwd and REPO_ROOTS and not any(s.launch_cwd.startswith(r) for r in REPO_ROOTS) and not (s.workspace and s.launch_cwd.startswith(s.workspace))),
            "launchDirs": dict(Counter(s.launch_cwd for s in tool if s.launch_cwd).most_common(3)),
        },
        "friction": {
            "wastedCalls": failed_calls + reread,
            "wastedShare": pct(failed_calls + reread, all_calls),
            "corrections": corrections,
        },
    }


def risk_summary(tool: list[SessionMetrics]) -> dict:
    kinds: Counter = Counter()
    undisclosed: Counter = Counter()
    high_sessions: set[str] = set()
    for s in tool:
        for r in s.risks:
            kinds[r["label"]] += 1
            if r["kind"] not in LOW_RISK:
                high_sessions.add(s.id)
            if not r["disclosed"]:
                undisclosed[r["label"]] += 1
    events = [dict(r, session=s.id, prompt=s.first_prompt[:100], severity="low" if r["kind"] in LOW_RISK else "high") for s in tool for r in s.risks]
    events.sort(key=lambda r: (r["severity"] == "low", r["disclosed"]))
    high = [r for r in events if r["severity"] == "high"]
    return {
        "sessionsWithRisk": len(high_sessions),
        "sessionShare": pct(len(high_sessions), len(tool)),
        "byKind": [{"label": k, "count": v, "undisclosed": undisclosed.get(k, 0)} for k, v in kinds.most_common()],
        "undisclosed": sum(1 for r in high if not r["disclosed"]),
        "total": len(high),
        "lowRisk": len(events) - len(high),
        "events": events[:40],
    }


def by_model(sessions: list[SessionMetrics]) -> list[dict]:
    rows = []
    groups: dict[str, list[SessionMetrics]] = defaultdict(list)
    for s in sessions:
        if s.has_tool_data:
            groups[s.model or s.provider].append(s)
    for model, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        editing = [s for s in items if s.category_counts.get("edit") and not s.reverted]
        rows.append({
            "model": model,
            "sessions": len(items),
            "verified": pct(sum(1 for s in editing if s.validated_after_last_edit), len(editing)) if editing else None,
            "blocked": pct(sum(1 for s in items if s.archetype in ("blocked", "struggling")), len(items)),
            "medianCalls": median([s.tool_calls for s in items]),
            "failureRate": pct(sum(s.failed_calls for s in items), sum(s.tool_calls for s in items)),
            "riskEvents": sum(len(s.risks) for s in items),
        })
    return rows


def specificity_outcome(sessions: list[SessionMetrics]) -> list[dict]:
    buckets: dict[str, list[SessionMetrics]] = {"Vague (0-1 signals)": [], "Partial (2 signals)": [], "Specific (3+ signals)": []}
    for s in sessions:
        if not s.user_prompts or not s.has_tool_data:
            continue
        key = "Vague (0-1 signals)" if s.prompt_specificity <= 1 else "Partial (2 signals)" if s.prompt_specificity == 2 else "Specific (3+ signals)"
        buckets[key].append(s)
    rows = []
    for label, items in buckets.items():
        if not items:
            continue
        rows.append({
            "bucket": label,
            "sessions": len(items),
            "verified": pct(sum(1 for s in items if s.archetype == "verified_delivery"), len(items)),
            "corrections": round(sum(s.corrections for s in items) / len(items), 2),
            "medianCalls": median([s.tool_calls for s in items]),
        })
    return rows


def session_rows(sessions: list[SessionMetrics]) -> list[dict]:
    rows = []
    for s in sessions:
        row = asdict(s)
        row.pop("calls", None)
        row["archetypeLabel"] = ARCHETYPES.get(s.archetype, s.archetype)
        rows.append(row)
    return rows
