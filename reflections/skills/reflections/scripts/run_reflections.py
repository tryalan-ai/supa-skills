#!/usr/bin/env python3
"""Run local-session reflections: discover, classify, sample, suggest."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from analyze_sessions import (  # noqa: E402
    SESSION_FORMATS,
    load_cache,
    parse_session,
    save_cache,
    select_samples,
    summarize,
)
from compact_sessions import compact_session  # noqa: E402
from discover import DEFAULT_SOURCES, discover_session_files  # noqa: E402
from render_html import html_report  # noqa: E402


def pct(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{round(100 * count / total)}%"


SETUP_PATTERN_TOKENS = ("auth", "login", "credential")
ENVIRONMENT_PATTERN_TOKENS = (
    "env",
    "secret",
    "sandbox",
    "provision",
    "host",
    "dependenc",
    "not installed",
    "command not found",
    "modulenotfound",
    "cannot find module",
)


def suggestion_kind_for_pattern(pattern: dict) -> str:
    """Map a failure pattern to Prompt/Tooling/Setup/Environment.

    Environment is missing packages, missing variables, and similar host gaps.
    Sandbox provisioning is one subset, not the whole bucket. Auth/login stays Setup.
    """
    blob = f"{pattern.get('pattern', '')} {pattern.get('category', '')}".lower()
    if any(token in blob for token in SETUP_PATTERN_TOKENS):
        return "setup"
    if any(token in blob for token in ENVIRONMENT_PATTERN_TOKENS):
        return "environment"
    return "tooling"


def build_suggestions(summary: dict) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    total = int(summary.get("sessionCount") or 0)
    classes = Counter(summary.get("classificationCounts") or {})
    rates = summary.get("rates") or {}
    patterns = summary.get("topFailurePatterns") or []
    failed_tools = summary.get("topFailedTools") or []

    def share(name: str) -> float:
        return (classes.get(name, 0) / total) if total else 0.0

    if share("research-only") >= 0.15:
        suggestions.append(
            {
                "kind": "prompt",
                "title": "Many sessions never become implementation loops",
                "why": f"{classes.get('research-only', 0)}/{total} sessions never edited or validated. Compact samples mix genuine Q&A with implementation tasks that stalled in discovery.",
                "change": "For implementation asks, require an exit predicate before kickoff. Leave true questions as research-only.",
            }
        )
    if share("asked-human") >= 0.08:
        suggestions.append(
            {
                "kind": "prompt",
                "title": "Agents stop to ask instead of looping",
                "why": f"{classes.get('asked-human', 0)} sessions asked the human before any edit.",
                "change": "Encode default branches in skills (one assumption + proceed) and reserve questions for irreversible or multi-valid implementations.",
            }
        )
    if share("blocked") >= 0.08:
        suggestions.append(
            {
                "kind": "environment",
                "title": "Sessions block before implementation",
                "why": f"{classes.get('blocked', 0)} sessions look blocked with no edits.",
                "change": "Inspect blocked samples for missing packages, env vars, or host access. Fix the environment once rather than re-prompting each session.",
            }
        )
    if classes.get("setup-failed"):
        suggestions.append(
            {
                "kind": "environment",
                "title": "CLI failed before the first turn",
                "why": f"{classes.get('setup-failed', 0)} sessions never started (login or runtime setup failed).",
                "change": "Fix missing packages, env vars, or host CLI login so the next session can actually start.",
            }
        )
    if share("code-generator") >= 0.08:
        suggestions.append(
            {
                "kind": "tooling",
                "title": "Edits without post-edit validation",
                "why": f"{classes.get('code-generator', 0)} sessions edited code and never ran a check.",
                "change": "Make the kickoff skill name the focused test/type-check command before the first edit. Treat missing validation as an incomplete session.",
            }
        )
    if share("debug-loop") >= 0.05:
        suggestions.append(
            {
                "kind": "tooling",
                "title": "High-failure debug loops",
                "why": f"{classes.get('debug-loop', 0)} sessions had many failed tools without a clean finish.",
                "change": "Add a no-progress circuit breaker (2 identical failures → stop) and prefer one narrow sensor over repeated shell retries.",
            }
        )
    edit_validation = float(rates.get("editSessionsWithValidationAfterEdit") or 0)
    if 0 < edit_validation < 0.85:
        suggestions.append(
            {
                "kind": "tooling",
                "title": "Validation after edit is leaky",
                "why": f"Only {edit_validation:.0%} of edit sessions ran a check after the first edit.",
                "change": "Standardize validation commands (type-check, focused test, lint) in AGENTS.md and loop skills.",
            }
        )
    for pattern in patterns:
        sessions = int(pattern.get("sessions") or 0)
        kind = suggestion_kind_for_pattern(pattern)
        min_sessions = 3 if kind == "environment" else 5
        if sessions >= min_sessions or (total and sessions / total >= 0.05):
            suggestions.append(
                {
                    "kind": kind,
                    "title": f"Recurring: {pattern.get('category')}",
                    "why": f"{sessions} sessions, {pattern.get('mentions')} mentions.",
                    "change": "Fix the underlying harness or docs for this pattern so the next loop does not re-hit it.",
                }
            )
    if failed_tools:
        top_name, top_count = failed_tools[0]
        suggestions.append(
            {
                "kind": "tooling",
                "title": f"Noisiest failed tool: {top_name}",
                "why": f"{top_count} failed calls.",
                "change": "If this is exploratory shell, add a cheaper discovery path (index or script). If it is a real command, document the working invocation.",
            }
        )
    styles = Counter((summary.get("autonomy") or {}).get("styleCounts") or {})
    if total and styles.get("babysat", 0) / total >= 0.12:
        suggestions.append(
            {
                "kind": "prompt",
                "title": "High share of short, interrupted loops",
                "why": f"{styles.get('babysat', 0)}/{total} sessions look babysat (many follow-ups, short unattended spans).",
                "change": "Give the next implementation ask a single exit predicate and leave it running. Do not send a follow-up until the agent reports or the sensor fails.",
            }
        )
    if total and styles.get("delegated", 0) / total >= 0.5:
        suggestions.append(
            {
                "kind": "prompt",
                "title": "Keep long-horizon work on the agents that already run unattended",
                "why": f"{styles.get('delegated', 0)}/{total} sessions already run long unattended streaks.",
                "change": "Route multi-hour implementation to those agents. Use others for short Q&A. Give each implementation task an exit predicate so the work stays bounded.",
            }
        )
    if share("task-finisher") >= 0.4:
        suggestions.append(
            {
                "kind": "prompt",
                "title": "Keep finisher sessions on a bounded loop",
                "why": f"{classes.get('task-finisher', 0)} sessions already edit, validate, and report.",
                "change": "Give implementation tasks a clear exit predicate and validation gate so finishers do not regress into research-only or asked-human.",
            }
        )
    if not suggestions:
        suggestions.append(
            {
                "kind": "tooling",
                "title": "Not enough signal yet",
                "why": "Classification is sparse or sessions are empty.",
                "change": "Widen the scan (drop --repo, raise --max-sessions) or inspect parser coverage for the empty provider.",
            }
        )
    return suggestions[:10]


def markdown_report(summary: dict, digests: list[dict], suggestions: list[dict[str, str]]) -> str:
    lines = [
        "# Local session reflections",
        "",
        f"Sessions analyzed: {summary.get('sessionCount', 0)}",
        f"Parse errors skipped: {summary.get('skippedCount', 0)}",
        "",
        "## Classifications",
    ]
    total = int(summary.get("sessionCount") or 0)
    for name, count in sorted((summary.get("classificationCounts") or {}).items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {name}: {count} ({pct(count, total)})")
    lines.extend(["", "## By agent"])
    for provider, bucket in (summary.get("byProvider") or {}).items():
        lines.append(
            f"- {provider}: {bucket['sessionCount']} sessions, "
            f"{bucket['editCalls']} edits, {bucket['failedToolCalls']} failed tools, "
            f"{dict(bucket.get('classificationCounts') or {})}"
        )
    lines.extend(["", "## Autonomy (inter-human spans)"])
    autonomy = summary.get("autonomy") or {}
    for name, count in sorted((autonomy.get("styleCounts") or {}).items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- style {name}: {count} ({pct(count, total)})")
    lines.append(f"- tools per human turn: {autonomy.get('toolsPerHumanTurn')}")
    lines.append(f"- max unattended streak: {autonomy.get('maxUnattendedStreak')}")
    lines.append(f"- turn duration p50s (timed spans only): {autonomy.get('turnDurationP50s')}")
    lines.append(
        f"- stop reasons: clarify={autonomy.get('stopClarify', 0)} "
        f"interrupt={autonomy.get('stopInterrupt', 0)} finish={autonomy.get('stopFinish', 0)}"
    )
    lines.extend(["", "## By agent (autonomy)"])
    for provider, bucket in (summary.get("byProvider") or {}).items():
        lines.append(
            f"- {provider}: humanTurns={bucket.get('humanTurns', 0)} "
            f"tools/human={bucket.get('toolsPerHumanTurn')} "
            f"maxStreak={bucket.get('maxUnattendedStreak')} "
            f"turnP50s={bucket.get('turnDurationP50s')} "
            f"stops clarify/interrupt/finish="
            f"{bucket.get('stopClarify', 0)}/{bucket.get('stopInterrupt', 0)}/{bucket.get('stopFinish', 0)} "
            f"styles={dict(bucket.get('autonomyStyles') or {})}"
        )
    lines.extend(["", "## Rates"])
    for name, value in (summary.get("rates") or {}).items():
        lines.append(f"- {name}: {value}")
    lines.extend(["", "## Top failed tools"])
    for name, count in summary.get("topFailedTools") or []:
        lines.append(f"- {name}: {count}")
    lines.extend(["", "## Recurring failure patterns"])
    for item in summary.get("topFailurePatterns") or []:
        lines.append(
            f"- {item.get('category')}: {item.get('sessions')} sessions, {item.get('mentions')} mentions"
        )
        for evidence in (item.get("evidence") or [])[:2]:
            lines.append(f"  - `{evidence}`")
    lines.extend(["", "## Suggestions to raise autonomy"])
    for item in suggestions:
        lines.append(f"### {item['title']}")
        lines.append(f"- Kind: {item['kind']}")
        lines.append(f"- Why: {item['why']}")
        lines.append(f"- Change: {item['change']}")
        lines.append("")
    lines.extend(["", "## Compact samples"])
    for digest in digests:
        metrics = digest.get("metrics") or {}
        lines.append(
            f"- {digest.get('sessionId')} [{digest.get('provider', '?')}] "
            f"{digest.get('classification')} {digest.get('autonomyStyle', '')} "
            f"tools={metrics.get('toolCalls', 0)} "
            f"edits={metrics.get('editCalls', 0)} failed={metrics.get('failedToolCalls', 0)}"
        )
        for message in (digest.get("userRequests") or [])[:2]:
            lines.append(f"  - user: {str(message)[:180]}")
        for message in (digest.get("assistantOutcomeMessages") or [])[:2]:
            lines.append(f"  - assistant: {str(message)[:180]}")
        for event in (digest.get("toolEvents") or [])[:6]:
            summary = event.get("command") or event.get("summary") or ""
            lines.append(f"  - {event.get('tool')}: {str(summary)[:160]}")
    return "\n".join(lines).rstrip() + "\n"


def compact_payload(digest: dict, stats) -> dict:
    return {
        **digest,
        "provider": getattr(stats, "provider", "unknown"),
        "classification": getattr(stats, "classification", digest.get("classification")),
        "sessionId": getattr(stats, "id", digest.get("sessionId")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Only sessions that mention this repo path")
    parser.add_argument("--providers", help="Comma list: grok,claude,codex,cursor,kimi")
    parser.add_argument("--max-sessions", type=int, help="Most recent N session files across all providers")
    parser.add_argument("--max-per-provider", type=int, help="Most recent N files per agent (keeps Grok from crowding others)")
    parser.add_argument("--skip-over-mb", type=float, help="Skip session files larger than this many MB")
    parser.add_argument("--sample-size", type=int, default=15)
    parser.add_argument("--format", default="auto", choices=SESSION_FORMATS)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--output",
        default="/tmp/alan-reflections/report.md",
        help="Markdown report path",
    )
    parser.add_argument("--report-json", help="Optional JSON summary path")
    parser.add_argument(
        "--output-html",
        default="/tmp/alan-reflections/report.html",
        help="Visual HTML report path",
    )
    args = parser.parse_args()

    providers = {part.strip() for part in args.providers.split(",")} if args.providers else None
    files = discover_session_files(
        repo=args.repo,
        max_sessions=args.max_sessions,
        max_per_provider=args.max_per_provider,
        providers=providers,
        skip_over_mb=args.skip_over_mb,
    )
    if not files:
        print("No local session files found. Check ~/.codex, ~/.claude/projects, ~/.cursor/projects, ~/.grok/sessions.", file=sys.stderr)
        return 1

    cache_path = Path("/tmp/alan-reflections/.reflections-cache.json")
    cache = None if args.no_cache else load_cache(cache_path)
    show_progress = not args.no_progress and sys.stderr.isatty()
    t0 = time.time()
    stats = []
    errors = 0
    total = len(files)
    if show_progress:
        print(f"[{time.strftime('%H:%M:%S')}] Analyzing {total} local sessions across {', '.join(sorted({s.root.name for s in DEFAULT_SOURCES if s.root.exists()})) or 'discovered roots'}...", file=sys.stderr)

    for idx, path in enumerate(files, start=1):
        size_mb = path.stat().st_size / (1024 * 1024)
        if show_progress and size_mb >= 25:
            print(f"\n  large file {size_mb:.1f}MB {path}", file=sys.stderr)
        try:
            stats.append(parse_session(path, args.repo, args.format, cache))
        except Exception as exc:
            errors += 1
            if show_progress:
                print(f"  [ERROR] {path}: {exc}", file=sys.stderr)
        if show_progress and (idx % max(1, total // 10) == 0 or idx == total):
            print(f"\r  Parsed {idx}/{total} ({round(time.time() - t0, 1)}s)...   ", file=sys.stderr, end="")
    if show_progress:
        print(file=sys.stderr)

    if cache is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            save_cache(cache_path, cache)
        except OSError as exc:
            print(f"Warning: could not write cache: {exc}", file=sys.stderr)

    samples = select_samples(stats, args.sample_size)
    summary = summarize(stats, samples)
    summary["skippedCount"] = errors
    suggestions = build_suggestions(summary)
    summary["suggestions"] = suggestions

    digests = []
    for item in samples:
        digest = compact_session(item, max_message_chars=600, max_events=16, session_format=args.format)
        if isinstance(digest, dict):
            digest["provider"] = item.provider
            digest["classification"] = item.classification
            digest["autonomyStyle"] = item.autonomy_style
            digest["sessionId"] = item.id
            digests.append(digest)
        else:
            digests.append({"sessionId": item.id, "provider": item.provider, "classification": item.classification, "events": []})

    report = markdown_report(summary, digests, suggestions)
    html = html_report(summary, digests)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report)
    html_path = Path(args.output_html)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html)
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(summary, indent=2, default=str))

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(report)
        print(f"Wrote {output}", file=sys.stderr)
        print(f"Wrote {html_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
