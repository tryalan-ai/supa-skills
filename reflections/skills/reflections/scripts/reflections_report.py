"""Aggregate and render standalone Reflections session evidence."""

from __future__ import annotations

from collections import Counter
import statistics

def aggregate(sessions: list[dict]) -> dict:
    verification = Counter(session["verification"] for session in sessions)
    insight_counts = Counter(insight["kind"] for session in sessions for insight in session["insights"])
    edit_sessions = sum(bool(session["metrics"]["editCalls"]) for session in sessions)
    implementation_edits = sum(bool(session["metrics"]["codeEditCalls"]) and session["goalMode"] != "read_only" for session in sessions)
    passed = verification["passed"]
    explicit_outcomes = passed + verification["failed"]
    edit_spans = [span for session in sessions for span in session["spans"] if span["codeEditCalls"]]
    span_verification = Counter(span["verification"] for span in edit_spans)
    check_kinds = Counter(check["kind"] for session in sessions for span in session["spans"] for check in ([span["lastCheck"]] if span["lastCheck"] else []))
    recommendations = []
    for kind, count in insight_counts.most_common():
        examples = [session for session in sessions if any(insight["kind"] == kind for insight in session["insights"])]
        recommendations.append({
            "kind": kind,
            "audience": "analyzer" if kind in {"missing_exit_status", "parser_link_gap", "unknown_check_result", "missing_terminal_sidecar", "formatter_result_unknown"} else "coding_workflow",
            "recurrence": "recurring" if count >= 3 else "isolated",
            "sessions": count,
            "sampleSessionIds": [session["sessionId"] for session in examples[:3]],
            "suggestion": next(insight["suggestion"] for insight in examples[0]["insights"] if insight["kind"] == kind),
        })
    by_provider = {}
    for provider in sorted({item["provider"] for item in sessions}):
        items = [item for item in sessions if item["provider"] == provider]
        by_provider[provider] = {
            "sessions": len(items),
            "editSessions": sum(bool(item["metrics"]["editCalls"]) for item in items),
            "codeEditSessions": sum(bool(item["metrics"]["codeEditCalls"]) for item in items),
            "humanTurns": sum(item["metrics"]["humanTurns"] for item in items),
            "toolCalls": sum(item["metrics"]["toolCalls"] for item in items),
            "unattributedToolCalls": sum(item["metrics"]["unattributedToolCalls"] for item in items),
            "medianToolsPerHumanTurn": round(statistics.median([item["metrics"]["toolsPerHumanTurn"] for item in items if item["metrics"]["toolsPerHumanTurn"] is not None]), 2) if any(item["metrics"]["humanTurns"] for item in items) else None,
            "medianLongestUnattendedStreak": statistics.median([item["metrics"]["maxToolsBetweenHumanTurns"] for item in items if item["metrics"]["humanTurns"]]) if any(item["metrics"]["humanTurns"] for item in items) else None,
            "timedHumanSpans": sum(item["metrics"]["timedHumanSpans"] for item in items),
            "medianElapsedToLastAgentEventSeconds": round(statistics.median([span["elapsedToLastAgentEventSeconds"] for item in items for span in item["spans"] if span["elapsedToLastAgentEventSeconds"] is not None]), 2) if any(item["metrics"]["timedHumanSpans"] for item in items) else None,
            "verificationCounts": dict(Counter(item["verification"] for item in items)),
            "failedToolCalls": sum(item["metrics"]["failedToolCalls"] for item in items),
            "unlinkedFailedResults": sum(item["metrics"]["unlinkedFailedResults"] for item in items),
            "unparsedOrchestrationCalls": sum(item["metrics"]["unparsedOrchestrationCalls"] for item in items),
            "possibleFormatterWriteCalls": sum(item["metrics"]["possibleFormatterWriteCalls"] for item in items),
            "confirmedFormatterChanges": sum(item["metrics"]["confirmedFormatterChanges"] for item in items),
            "unmatchedTerminalResults": sum(item["metrics"]["unmatchedTerminalResults"] for item in items),
            "ambiguousTerminalResults": sum(item["metrics"]["ambiguousTerminalResults"] for item in items),
            "extraneousTerminalResults": sum(item["metrics"]["extraneousTerminalResults"] for item in items),
            "sessionsMissingTerminalDirectory": sum(item["metrics"]["cursorTerminalDirectoryAvailable"] is False for item in items),
        }
    return {
        "sessionCount": len(sessions),
        "editSessions": edit_sessions,
        "implementationEditSessions": implementation_edits,
        "formatterChangeSessions": sum(session["metrics"]["confirmedFormatterChanges"] > 0 for session in sessions),
        "explicitOutcomeSessions": explicit_outcomes,
        "explicitOutcomeCoverage": round(explicit_outcomes / implementation_edits, 3) if implementation_edits else None,
        "totals": {key: sum(session["metrics"][key] for session in sessions) for key in ("humanTurns", "toolCalls", "unattributedToolCalls", "editCalls", "codeEditCalls", "possibleFormatterWriteCalls", "confirmedFormatterChanges", "confirmedFormatterNoChanges", "unattributedCodeEditCalls", "validationCalls", "failedToolCalls", "unlinkedFailedResults")},
        "unmatchedTerminalResults": sum(session["metrics"]["unmatchedTerminalResults"] for session in sessions),
        "ambiguousTerminalResults": sum(session["metrics"]["ambiguousTerminalResults"] for session in sessions),
        "extraneousTerminalResults": sum(session["metrics"]["extraneousTerminalResults"] for session in sessions),
        "autonomy": {
            "sessionsMissingHumanTurns": sum(session["metrics"]["humanTurns"] == 0 for session in sessions),
            "medianToolsPerHumanTurn": round(statistics.median([session["metrics"]["toolsPerHumanTurn"] for session in sessions if session["metrics"]["toolsPerHumanTurn"] is not None]), 2) if any(session["metrics"]["humanTurns"] for session in sessions) else None,
            "medianMaxToolsBetweenHumanTurns": statistics.median([session["metrics"]["maxToolsBetweenHumanTurns"] for session in sessions if session["metrics"]["humanTurns"]]) if any(session["metrics"]["humanTurns"] for session in sessions) else None,
            "timedHumanSpans": sum(session["metrics"]["timedHumanSpans"] for session in sessions),
            "medianElapsedToLastAgentEventSeconds": round(statistics.median([span["elapsedToLastAgentEventSeconds"] for session in sessions for span in session["spans"] if span["elapsedToLastAgentEventSeconds"] is not None]), 2) if any(session["metrics"]["timedHumanSpans"] for session in sessions) else None,
        },
        "passedCheckAfterLastEdit": passed,
        "observedAfterLastEdit": verification["observed"],
        "verificationCounts": dict(verification),
        "editSpanCount": len(edit_spans),
        "editSpanVerificationCounts": dict(span_verification),
        "lastCheckKindCounts": dict(check_kinds),
        "insightCounts": dict(insight_counts),
        "recommendations": recommendations,
        "byProvider": by_provider,
    }


def markdown_report(summary: dict, sessions: list[dict]) -> str:
    lines = ["# Standalone Reflections", "", f"Analyzed {summary['sessionCount']} local sessions; {summary['editSessions']} contain edits.", "", "## Aggregate", ""]
    if summary.get("unstableFiles") or summary.get("skippedFiles"):
        lines.append(f"- Incomplete scan: {len(summary.get('unstableFiles', []))} changed input files and {len(summary.get('skippedFiles', []))} file or parse errors. Do not compare this report with a complete run.")
    lines.append(f"- Identified code-edit sessions: {summary['implementationEditSessions']}; explicit final check outcomes in {summary['explicitOutcomeSessions']} ({summary['passedCheckAfterLastEdit']} passed, {summary['verificationCounts'].get('failed', 0)} failed). This is outcome visibility in logs, not a session success or efficiency score.")
    lines.append(f"- Formatter output reported changed files in {summary['formatterChangeSessions']} sessions. These remain separate from identified code-edit calls.")
    lines.append(f"- Check result observed without an explicit exit status: {summary['observedAfterLastEdit']} edit sessions.")
    unresolved = summary['verificationCounts'].get('unresolved', 0)
    lines.append(f"- Unresolved because of unparsed orchestration: {unresolved} edit {'session' if unresolved == 1 else 'sessions'}.")
    lines.append("- Post-edit check evidence states: " + ", ".join(f"{name} {count}" for name, count in summary["verificationCounts"].items()) + ".")
    lines.append(f"- Human-turn spans with identified code edits: {summary['editSpanCount']}; same-span verification: " + ", ".join(f"{name} {count}" for name, count in summary["editSpanVerificationCounts"].items()) + ".")
    lines.append("- Last check kind per turn (including non-edit turns): " + ", ".join(f"{name} {count}" for name, count in summary["lastCheckKindCounts"].items()) + ".")
    lines.append("- Event totals: " + ", ".join(f"{name} {count}" for name, count in summary["totals"].items()) + ".")
    lines.append("- Tool calls before a visible human instruction remain unattributed and are excluded from tools-per-human-turn denominators.")
    lines.append(f"- Cursor terminal candidates excluded: {summary['extraneousTerminalResults']} from unrelated commands and {summary['ambiguousTerminalResults']} matching repeated commands.")
    lines.append(f"- Inter-human spans: median per-session ratio {summary['autonomy']['medianToolsPerHumanTurn']} tools per visible human turn; median longest span {summary['autonomy']['medianMaxToolsBetweenHumanTurns']} tools; {summary['autonomy']['sessionsMissingHumanTurns']} sessions lack a visible human turn.")
    lines.append(f"- Elapsed time from human instruction to last agent event: median {summary['autonomy']['medianElapsedToLastAgentEventSeconds']} seconds across {summary['autonomy']['timedHumanSpans']} timestamped spans. This excludes the gap until the next human message but includes pauses between agent events.")
    lines.append("- Provider medians describe this selected cohort; missing tool events and result-link gaps make provider rankings invalid.")
    lines.append("- A check after an edit may target different files; chronology and a successful exit alone do not prove the changed code was tested.")
    lines.append("- Edit counts are tool-call observations; the analyzer does not independently verify the resulting filesystem diff.")
    lines.append("- Formatter --write calls are possible writes, not confirmed code edits. A formatter after the last identified code edit needs a later linked check or proof that it made no changes.")
    lines.append("- A report marker is an assistant claim; session success is not inferred from it.")
    lines.extend(["", "### By provider", ""])
    for provider, bucket in summary["byProvider"].items():
        states = ", ".join(f"{name} {count}" for name, count in bucket["verificationCounts"].items())
        duration = f"{bucket['medianElapsedToLastAgentEventSeconds']} seconds across {bucket['timedHumanSpans']} spans" if bucket["timedHumanSpans"] else "unavailable"
        lines.append(f"- **{provider}**: {bucket['sessions']} sessions; {bucket['humanTurns']} visible human turns; {bucket['toolCalls']} tool calls ({bucket['unattributedToolCalls']} before a visible instruction); {bucket['codeEditSessions']} with identified code edits. Median tools per human turn: {bucket['medianToolsPerHumanTurn']}; median longest span: {bucket['medianLongestUnattendedStreak']} tools; elapsed time to last agent event: {duration}.")
        evidence = f"{bucket['failedToolCalls']} linked failed tool results, {bucket['unlinkedFailedResults']} unlinked failed results, {bucket['unparsedOrchestrationCalls']} opaque exec calls"
        if provider == "cursor":
            evidence += f", {bucket['ambiguousTerminalResults']} ambiguous terminal results, {bucket['extraneousTerminalResults']} unrelated terminal files, {bucket['sessionsMissingTerminalDirectory']} sessions without terminal directories"
        lines.append(f"  - Evidence coverage: {evidence}. Final check states: {states}.")
    lines.extend(["", "## Aggregate recommendations", ""])
    for audience, heading in (("coding_workflow", "Coding workflow"), ("analyzer", "Analyzer coverage")):
        relevant = [item for item in summary["recommendations"] if item["audience"] == audience]
        if relevant:
            lines.extend([f"### {heading}", ""])
        for recommendation in relevant:
            count = recommendation["sessions"]
            lines.append(f"- **{recommendation['kind']}** ({count} {'session' if count == 1 else 'sessions'}, {recommendation['recurrence']}): {recommendation['suggestion']} Examples: {', '.join(recommendation['sampleSessionIds'])}.")
        if relevant:
            lines.append("")
    lines.extend(["", "## Individual sessions", ""])
    for session in sessions:
        metrics = session["metrics"]
        lines.append(f"### {session['sessionId']} ({session['provider']})")
        lines.append(f"- Request: {session['request'] or 'Unavailable'}")
        if session["lastHumanRequest"] and session["lastHumanRequest"] != session["request"]:
            lines.append(f"- Last visible request: {session['lastHumanRequest']}")
        duration = f"{metrics['medianElapsedToLastAgentEventSeconds']} seconds across {metrics['timedHumanSpans']} timed turns" if metrics['timedHumanSpans'] else "unavailable"
        ratio = metrics["toolsPerHumanTurn"] if metrics["toolsPerHumanTurn"] is not None else "unavailable"
        lines.append(f"- Metrics: {metrics['humanTurns']} human turns, {metrics['toolCalls']} tools, {ratio} tools per human turn, {metrics['maxToolsBetweenHumanTurns']} max tools between human turns, median elapsed time to last agent event {duration}, {metrics['editCalls']} edits ({metrics['codeEditCalls']} identified code), {metrics['validationCalls']} checks, {metrics['failedToolCalls']} failed tools")
        lines.append(f"- Post-edit check evidence: **{session['verification']}**; report marker seen: {session['reportMarkerSeen']}")
        if session["checksAfterLastEdit"]:
            checks = "; ".join(f"{check['command']} ({check['kind']}, {check['status']})" for check in session["checksAfterLastEdit"])
            lines.append(f"- Last post-edit checks: {checks}")
        edit_spans = [span for span in session["spans"] if span["codeEditCalls"]]
        for span in edit_spans[-3:]:
            lines.append(f"  - Turn {span['turn']}: {span['codeEditCalls']} identified code edits, {span['validationCalls']} checks, {span['verification']} in the same turn; last code edit {span['lastCodeEditTarget']}; {span['request'][:100]}")
        for insight in session["insights"]:
            lines.append(f"- {insight['kind']}: {insight['evidence']}. {insight['suggestion']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
