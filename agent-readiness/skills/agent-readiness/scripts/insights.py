#!/usr/bin/env python3
"""Turn repository criteria and session behavior into ranked, evidence-linked findings.

Each finding pairs a behavioral symptom (what agents did) with the repository
root cause (what the scan found), the cost, and the remediation playbook.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

from behavior import SessionMetrics, segment_category, shell_segments

def n(value: float) -> str:
    """Whole numbers in prose: 7.0 -> 7, 55.5 -> 56."""
    return str(int(round(value)))


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

PLAYBOOKS: dict[str, dict[str, str]] = {
    "environment": {
        "name": "Reproducible agent environments",
        "alan": "Alan environment setup discovers how the repo runs, scripts setup and start, and builds a cloud sandbox image every agent session starts from, so services, seeds and env vars exist before the first prompt.",
    },
    "verification": {
        "name": "Verification gates",
        "alan": "Alan task-to-PR runs focused tests, type checks and browser checks before a PR is opened, and attaches a test report with screenshots and video to the task.",
    },
    "context": {
        "name": "Living agent context",
        "alan": "Alan generates and maintains AGENTS.md and component instructions from real sessions, and keeps a team code index so agents find the right files on the first search.",
    },
    "specs": {
        "name": "Spec-first task intake",
        "alan": "Alan turns a one-line request into a reviewed plan with acceptance criteria before code is written, and keeps decisions on the task record.",
    },
    "skills": {
        "name": "Reusable team skills",
        "alan": "Alan captures repeated procedures (deploy, migration, release, QA) as team-owned skills every agent and developer shares.",
    },
    "guardrails": {
        "name": "Enforced guardrails",
        "alan": "Alan applies permission policies and hooks across every harness, so 'never do X' rules are enforced, not suggested.",
    },
    "orchestration": {
        "name": "Orchestrated agent workflows",
        "alan": "Alan workflows run review, QA and maintenance agents on triggers and schedules, with every run logged against a task.",
    },
    "observability": {
        "name": "Agent-visible telemetry",
        "alan": "Alan connects agents to logs, traces and the issue tracker through MCP, so debugging starts from evidence.",
    },
}


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    headline: str
    metric: str
    evidence: list[dict] = field(default_factory=list)
    root_causes: list[dict] = field(default_factory=list)
    impact: str = ""
    fix: list[str] = field(default_factory=list)
    playbook: str = ""
    effort: str = "S"
    horizon: str = "30"
    source: str = "behavior"


HARNESS_FILES: dict[str, tuple[str, list[str]]] = {
    "claude_cli": ("Claude Code", ["CLAUDE.md", ".claude/CLAUDE.md"]),
    "codex_app_server": ("Codex", ["AGENTS.md"]),
    "cursor_agent_cli": ("Cursor", ["AGENTS.md", ".cursorrules", ".cursor/rules"]),
    "gemini_cli": ("Gemini CLI", ["GEMINI.md", "AGENTS.md"]),
    "copilot_cli": ("Copilot", [".github/copilot-instructions.md", "AGENTS.md"]),
    "opencode_cli": ("OpenCode", ["AGENTS.md"]),
    "droid_cli": ("Factory Droid", ["AGENTS.md"]),
}


def instruction_reach(repo: Path, sessions: list[SessionMetrics]) -> dict:
    used = sorted({s.provider for s in sessions if s.has_tool_data and s.provider in HARNESS_FILES})
    gaps = []
    for provider in used:
        name, files = HARNESS_FILES[provider]
        if not any((repo / f).exists() for f in files):
            present = [f for f in ("AGENTS.md", "CLAUDE.md", ".cursorrules", "GEMINI.md") if (repo / f).exists()]
            share = round(100 * sum(1 for s in sessions if s.provider == provider) / max(1, sum(1 for s in sessions if s.has_tool_data)))
            gaps.append({"harness": name, "expects": " or ".join(files), "present": present, "sessionShare": share})
    tool = [s for s in sessions if s.has_tool_data]
    read_rate = round(100 * sum(1 for s in tool if s.read_agent_doc) / max(1, len(tool)), 1)
    return {"harnessesUsed": [HARNESS_FILES[p][0] for p in used], "gaps": gaps, "agentDocReadRate": read_rate}


DOC_COMMAND = re.compile(r"`((?:CI=\w+\s+)?(?:npm|pnpm|yarn|bun|npx|make|pytest|go|cargo|uv|poetry|just|turbo)\b[^`]{2,120})`")
FORBIDDEN = re.compile(r"(?:do not|don't|never|avoid)\b[^.\n]{0,40}?`([^`]{3,80})`", re.I)
FORBIDDEN_PHRASE = re.compile(r"\b(never|do not|don't)\s+(mock|use classes|push|force[- ]push|stash|commit secrets|hand-edit)", re.I)


def criterion(scan: dict, cid: str) -> dict | None:
    return next((c for c in scan.get("criteria", []) if c["id"] == cid), None)


def failing(scan: dict, *ids: str) -> list[dict]:
    out = []
    for cid in ids:
        c = criterion(scan, cid)
        if c and not c["passed"] and not c.get("skipped"):
            out.append({"id": c["id"], "title": c["title"], "evidence": c["evidence"], "passed": False})
    return out


def passing(scan: dict, *ids: str) -> list[dict]:
    out = []
    for cid in ids:
        c = criterion(scan, cid)
        if c and c["passed"]:
            out.append({"id": c["id"], "title": c["title"], "evidence": c["evidence"], "passed": True})
    return out


def doc_rules(repo: Path) -> tuple[list[str], list[str], list[str]]:
    text = ""
    for name in ("AGENTS.md", "CLAUDE.md", ".cursorrules", ".github/copilot-instructions.md"):
        path = repo / name
        if path.exists():
            text += path.read_text(errors="ignore") + "\n"
    documented = sorted(set(m.strip() for m in DOC_COMMAND.findall(text)))
    forbidden = sorted(set(m.strip() for m in FORBIDDEN.findall(text) if re.match(r"(npm|pnpm|yarn|npx|git|rm|docker|make)\b", m.strip())))
    phrases = sorted(set(m.group(0).lower() for m in FORBIDDEN_PHRASE.finditer(text)))
    return documented, forbidden, phrases


WRAPPERS = re.compile(r"^\s*(\(?\s*cd [^&;]+(&&|;)\s*|timeout\s+(-\S+\s+)*\d+[smh]?\s+|time\s+|(\w+=\S*\s+)+|npx\s+(--yes\s+|-y\s+)?|env\s+)+")


def command_core(command: str) -> str:
    segments = shell_segments(command)
    core = next((seg for seg in segments if segment_category(seg) == "validate"), segments[0] if segments else "")
    core = WRAPPERS.sub("", core)
    return re.sub(r"\s+", " ", core).strip()


def head(core: str) -> str:
    """Match key for a command: `vitest run`, `tsc --noEmit`, or `npm run lint` for package-manager scripts."""
    tokens = core.split()
    if len(tokens) >= 3 and tokens[0] in ("npm", "pnpm", "yarn", "bun") and tokens[1] == "run":
        return " ".join(tokens[:3])
    if len(tokens) >= 2 and tokens[0] in ("npm", "pnpm", "yarn", "bun") and tokens[1] in ("test", "t"):
        return f"{tokens[0]} run test"
    return " ".join(tokens[:2])


def documented_heads(repo: Path, documented: list[str]) -> set[str]:
    """Two-token heads of documented commands, with `npm run x` expanded to the script it runs."""
    scripts: dict[str, str] = {}
    for manifest in [*repo.glob("*/*/package.json"), repo / "package.json"]:
        try:
            scripts.update({k: str(v) for k, v in (json.loads(manifest.read_text()).get("scripts") or {}).items()})
        except (OSError, json.JSONDecodeError):
            continue
    heads: set[str] = set()
    for doc in documented:
        core = command_core(doc)
        heads.add(head(core))
        run = re.match(r"(?:npm|pnpm|yarn|bun) (?:run )?([\w:.-]+)", core)
        if run and run.group(1) in scripts:
            body = command_core(scripts[run.group(1)].split("&&")[-1])
            heads.add(head(body))
    return {h for h in heads if h}


def adherence(sessions: list[SessionMetrics], repo: Path) -> dict:
    documented, forbidden, phrases = doc_rules(repo)
    doc_heads = documented_heads(repo, documented)
    validation_calls = []
    violations = []
    for s in sessions:
        for call in s.calls:
            if "never mock" in phrases and call["category"] == "edit" and re.search(r"\bvi\.mock\(|\bjest\.mock\(|mock\.patch\(|vi\.spyOn\(", call.get("content", "")):
                target = (call.get("paths") or ["a test file"])[0]
                violations.append({"session": s.id, "rule": "Never mock", "command": f"wrote vi.mock(...) into {target}"})
            core = command_core(call["command"])
            if not core:
                continue
            if call["category"] == "validate" or (call["category"] == "edit" and segment_category(core) == "validate"):
                matched = head(core) in doc_heads
                validation_calls.append({"session": s.id, "command": core[:140], "documented": matched})
            for rule in forbidden:
                if rule in core:
                    violations.append({"session": s.id, "rule": f"Do not run `{rule}`", "command": core[:140]})

    rule_text = " ".join(open(repo / f, errors="ignore").read() for f in ("AGENTS.md", "CLAUDE.md") if (repo / f).exists()).lower()
    written = {
        "infrastructure": r"do not start (databases|containers|services)|don't start (databases|containers)",
        "data_access": r"do not (start databases[^.]*)?query databases|do not query databases|not query databases directly",
        "history_rewrite": r"do not rewrite git history|no `git commit --amend`",
        "outside_workspace": r"work only inside this checkout|do not write to other directories",
        "hook_bypass": r"--no-verify|do not (skip|bypass) (hooks|checks)",
    }
    for s in sessions:
        for risk in s.risks:
            pattern = written.get(risk["kind"])
            if pattern and re.search(pattern, rule_text):
                violations.append({"session": s.id, "rule": f"Written rule: {risk['label'].lower()} is not allowed", "command": risk["command"][:140]})
    used = sum(1 for v in validation_calls if v["documented"])
    undocumented = Counter_top([v["command"] for v in validation_calls if not v["documented"]])
    return {
        "documentedCommands": documented[:20],
        "forbiddenRules": [f"Do not run `{r}`" for r in forbidden] + phrases,
        "validationCalls": len(validation_calls),
        "documentedShare": round(100 * used / len(validation_calls), 1) if validation_calls else None,
        "undocumentedTop": undocumented,
        "violations": violations[:20],
    }


def Counter_top(items: list[str], n: int = 6) -> list[dict]:
    counts: dict[str, int] = {}
    for item in items:
        key = " ".join(item.split()[:5])
        counts[key] = counts.get(key, 0) + 1
    return [{"command": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:n]]


def pick_excerpts(sessions: list[SessionMetrics], kind: str, n: int = 3, match: str | None = None) -> list[dict]:
    out = []
    for s in sessions:
        for e in s.excerpts:
            if e.get("kind") != kind:
                continue
            if match and match != e.get("failureKind"):
                continue
            out.append({"session": s.id, "prompt": s.first_prompt[:140], **e})
            break
        if len(out) >= n:
            break
    return out


def build_findings(scan: dict, agg: dict, sessions: list[SessionMetrics], adhere: dict, reach: dict | None = None) -> list[Finding]:
    findings: list[Finding] = []
    quality = agg.get("dataQuality", {})
    if not (reach or {}).get("gaps") and quality.get("launchedOutsideRepo") and any(s.provider == "claude_cli" for s in sessions):
        findings.append(Finding(
            "instruction-reach", "medium",
            "Instruction loading is fixed on disk but not yet observed",
            f"The repository now has an instruction file for every harness in use, but {quality['launchedOutsideRepo']} of {len(sessions)} sessions were launched from outside the repository, so none of them loaded it automatically. Agents opened AGENTS.md themselves in {n((reach or {}).get('agentDocReadRate', 0))}% of sessions.",
            f"0/{quality['launchedOutsideRepo']} sessions loaded this repo's CLAUDE.md automatically",
            root_causes=passing(scan, "ctx.multi"),
            impact="Until a session launched inside the repository shows the file in its context, the fix is a plausible improvement, not a measured one.",
            fix=["Launch agents from the repository root (or each worktree) and re-run this assessment."],
            playbook="context", effort="S", horizon="30",
        ))
    for gap in (reach or {}).get("gaps", []):
        findings.append(Finding(
            "instruction-reach", "critical",
            f"{gap['harness']} does not load this repository's agent instructions",
            f"{gap['sessionShare']}% of observed sessions ran in {gap['harness']}, which loads {gap['expects']}. This repository only has {', '.join(gap['present']) or 'no instruction file'}, so those agents start without this repository's rules. Agents opened the instruction file themselves in {n(reach['agentDocReadRate'])}% of sessions.",
            f"{gap['sessionShare']}% of sessions miss this repo's rules · {round(reach['agentDocReadRate'])}% opened AGENTS.md themselves",
            evidence=[{"text": v["rule"] + f" — broken by `{v['command']}`", "session": v["session"]} for v in adhere.get("violations", [])[:3]],
            root_causes=failing(scan, "ctx.multi") + passing(scan, "ctx.agent_doc"),
            impact="Rules the team wrote down are silently ignored by the harness most of its sessions use; the gap looks like agent disobedience but is a loading problem.",
            fix=[f"Add {gap['expects'].split(' or ')[0]} that imports the existing instructions (for Claude Code: a one-line `@AGENTS.md`).",
                 "Check instruction loading in CI so a new harness cannot silently miss them."],
            playbook="context", effort="S", horizon="30",
        ))
    tool = [s for s in sessions if s.has_tool_data]
    editing = [s for s in tool if s.category_counts.get("edit")]
    n_tool = len(tool)
    archetypes = agg.get("archetypes", {})

    blocked = [s for s in tool if s.archetype == "blocked"]
    env_kinds = {"service_unavailable", "env_config", "command_not_found", "missing_dependency", "auth"}
    env_fail = sum(v for s in tool for k, v in s.failure_kinds.items() if k in env_kinds)
    reasons = {"environment": [], "context": []}
    for s in blocked:
        text = next((e["text"] for e in s.excerpts if e["kind"] == "blocked"), "")
        env = re.search(r"database|postgres|docker|container|service|server|port \d+|install|node_modules|prisma|generated|env(ironment)? var|\.env|credential|sandbox|permission", text, re.I)
        reasons["environment" if env else "context"].append(s)
    if n_tool and reasons["environment"]:
        env_blocked = reasons["environment"]
        share = round(100 * len(env_blocked) / n_tool)
        findings.append(Finding(
            "env-blockers", "critical" if share >= 15 and len(env_blocked) >= 2 else "high" if len(env_blocked) >= 2 else "medium",
            "Agents cannot run what they change",
            f"{len(env_blocked)} of {n_tool} sessions ({share}%) stopped because the agent could not start a database, service or generated client it needed" + (f"; {len(reasons['context'])} more stopped for missing context" if reasons["context"] else "") + ".",
            f"{len(env_blocked)}/{n_tool} sessions blocked by environment · {env_fail} environment-class tool failures",
            evidence=[{"session": s.id, "prompt": s.first_prompt[:140], "text": next((e["text"] for e in s.excerpts if e["kind"] == "blocked"), "")} for s in env_blocked[:3]],
            root_causes=failing(scan, "env.codegen", "env.setup", "env.services", "env.sandbox", "env.env_example", "env.seed"),
            impact="Every blocked session is a round-trip back to a human; the work is not reduced, only deferred.",
            fix=["Make code generation and database setup run unattended in the agent's environment.",
                 "Document a no-Docker path (hosted dev database or test containers) for agents.",
                 "Commit .env.example with every variable the app needs to boot."],
            playbook="environment", effort="M", horizon="30",
        ))
    if n_tool and reasons["context"]:
        ctx_blocked = reasons["context"]
        findings.append(Finding(
            "context-blockers", "medium",
            "Agents stop and ask for what the request left out",
            f"{len(ctx_blocked)} of {n_tool} sessions ended with the agent asking for a reproduction, an expected behavior or a target, instead of a change.",
            f"{len(ctx_blocked)}/{n_tool} sessions stopped for missing context",
            evidence=[{"session": s.id, "prompt": s.first_prompt[:140], "text": next((e["text"] for e in s.excerpts if e["kind"] == "blocked"), "")} for s in ctx_blocked[:3]],
            root_causes=failing(scan, "spec.issues", "spec.contributing"),
            impact="The agent behaved correctly; the request did not contain enough to act on.",
            fix=["Route one-line bug reports through a plan step that captures repro, expected behavior and verification."],
            playbook="specs", effort="S", horizon="30",
        ))

    risks = agg.get("risks", {})
    if risks.get("sessionsWithRisk"):
        undisclosed = risks.get("undisclosed", 0)
        findings.append(Finding(
            "sandbox-escape", "critical" if undisclosed >= 3 else "high",
            "Agents act beyond their workspace, often without saying so",
            f"{risks['sessionsWithRisk']} of {n_tool} sessions ({round(risks['sessionShare'])}%) started databases or containers, changed shared dependencies, rewrote git history, bypassed hooks or queried data directly. {undisclosed} of {risks['total']} such actions were not mentioned in the agent's final report ({risks.get('lowRisk', 0)} lower-risk scratch writes not counted).",
            f"{risks['sessionsWithRisk']}/{n_tool} sessions with risky actions · {undisclosed} undisclosed",
            evidence=[{"session": r["session"], "text": f"{r['label']}: `{r['command']}`" + ("" if r["disclosed"] else " — not disclosed")} for r in risks.get("events", []) if r["severity"] == "high"][:5],
            root_causes=failing(scan, "sec.permissions", "tool.hooks", "sec.destructive", "env.sandbox"),
            impact="Unreported side effects are how agent work corrupts shared environments and data; reviewers only see the diff.",
            fix=["Run agents in isolated sandboxes with their own dependencies and databases.",
                 "Commit an agent permission policy that denies writes outside the workspace, history rewrites and direct database access.",
                 "Require agents to list side effects in their final report, and check it."],
            playbook="guardrails", effort="M", horizon="30",
        ))
    if editing:
        verified = sum(1 for s in editing if s.validated_after_last_edit)
        checked_failing = sum(1 for s in editing if s.checked_after_last_edit and not s.validated_after_last_edit)
        lint_only = sum(1 for s in editing if not s.validated_after_last_edit and not s.checked_after_last_edit and s.verification_depth == "lint")
        none = sum(1 for s in editing if not s.validated_after_last_edit and not s.checked_after_last_edit and s.verification_depth != "lint")
        rate = round(100 * verified / len(editing))
        if rate < 85:
            findings.append(Finding(
                "verification-gap", "critical" if rate < 50 else "high",
                "Changes are handed back without proof they work",
                f"Only {verified} of {len(editing)} code-changing sessions ({rate}%) had a test, type check or build pass after their final edit. {checked_failing} ran a check that failed or could not run, {lint_only} relied on lint alone and {none} ran nothing.",
                f"{verified}/{len(editing)} verified after the final edit · {checked_failing} check failed · {lint_only} lint only · {none} no check",
                evidence=[{"session": s.id, "prompt": s.first_prompt[:140], "text": f"Edited {s.distinct_files_edited} file(s); " + ("a check ran after the final edit but did not pass" if s.checked_after_last_edit else "lint only after the final edit" if s.verification_depth == "lint" else "no check after the final edit")} for s in editing if not s.validated_after_last_edit][:4],
                root_causes=failing(scan, "env.codegen", "ver.focused", "ctx.commands", "ver.command", "ver.typecheck", "ver.e2e", "guard.precommit"),
                impact="Unverified agent output moves the testing burden to reviewers, which is where agent productivity gains are lost.",
                fix=["Document one fast, focused test command per package in AGENTS.md.",
                     "Make 'ran the relevant test and type check' part of the definition of done.",
                     "Add a stop hook or CI gate that rejects agent PRs without test evidence."],
                playbook="verification", effort="S", horizon="30",
            ))

    if adhere.get("validationCalls"):
        share = adhere.get("documentedShare") or 0
        if share < 70 or adhere.get("violations"):
            sev = "high" if share < 40 or len(adhere.get("violations", [])) >= 2 else "medium"
            findings.append(Finding(
                "instruction-drift", sev,
                "Agents ignore the instructions you already wrote",
                f"Only {n(share)}% of agent verification commands match a command documented in your agent instructions, and {len(adhere.get('violations', []))} commands broke a written rule.",
                f"{n(share)}% documented-command adoption · {len(adhere.get('violations', []))} rule violations",
                evidence=[{"text": f"Ran `{v['command']}` — rule: {v['rule']}", "session": v["session"]} for v in adhere.get("violations", [])[:3]]
                + [{"text": f"Undocumented: `{u['command']}` ×{u['count']}"} for u in adhere.get("undocumentedTop", [])[:3]],
                root_causes=passing(scan, "ctx.agent_doc", "ctx.commands") + failing(scan, "sec.permissions", "tool.hooks"),
                impact="Instructions that are not enforced give a false sense of control; teams believe agents follow rules they routinely skip.",
                fix=["Put the commands agents actually need first, per package, with exact flags.",
                     "Turn the top 'never' rules into permission deny-lists or pre-tool hooks.",
                     "Re-run this report after each AGENTS.md change to measure adoption."],
                playbook="guardrails", effort="S", horizon="30",
            ))

    human = [s for s in sessions if s.user_prompts and s.has_tool_data] or [s for s in sessions if s.user_prompts]
    if human:
        rows = agg["humans"].get("specificityVsOutcome", [])
        vague = next((r for r in rows if r["bucket"].startswith("Vague")), None)
        specific = next((r for r in rows if r["bucket"].startswith("Specific")), None)
        vague_rate = agg["humans"]["vaguePromptRate"]
        if vague_rate >= 25:
            delta = ""
            if vague and specific:
                delta = f" Vague requests ended in verified work {n(vague['verified'])}% of the time versus {n(specific['verified'])}% for specific ones."
            findings.append(Finding(
                "vague-prompts", "high" if vague_rate >= 40 else "medium",
                "Developers hand agents under-specified tasks",
                f"{n(vague_rate)}% of sessions start from a request with no file, acceptance criterion, reproduction or verification step.{delta}",
                f"{n(vague_rate)}% vague first prompts · median {n(agg['humans']['medianFirstPromptWords'])} words",
                evidence=[{"session": s.id, "text": f"“{s.first_prompt[:160]}” → {s.archetype.replace('_', ' ')}"} for s in human if s.prompt_specificity <= 1][:4],
                root_causes=failing(scan, "spec.issues", "spec.contributing", "ctx.architecture"),
                impact="The agent fills the gaps with guesses; the developer pays for them in corrections and review.",
                fix=["Route one-line requests through a plan step that names files, acceptance criteria and the verification command.",
                     "Add issue templates with 'expected behavior' and 'how to verify' fields.",
                     "Share examples of high-yield prompts from this report with the team."],
                playbook="specs", effort="S", horizon="30",
            ))

    exp = agg["exploration"]
    if editing and (exp["medianCallsBeforeFirstEdit"] >= 15 or exp["indexUseRate"] < 10 or exp["editSessionsWithBlindEdits"] >= 30):
        sev = "high" if exp["editSessionsWithBlindEdits"] >= 40 or exp["medianCallsBeforeFirstEdit"] >= 25 else "medium"
        findings.append(Finding(
            "exploration-cost", sev,
            "Agents rediscover the codebase every session",
            f"Agents spend a median of {n(exp['medianCallsBeforeFirstEdit'])} tool calls orienting before their first edit, {n(exp['exploreShare'])}% of all calls are reads and searches, and {n(exp['editSessionsWithBlindEdits'])}% of code-changing sessions modified a file they never opened.",
            f"{n(exp['exploreShare'])}% of calls spent exploring · {n(exp['indexUseRate'])}% sessions used a code index",
            evidence=[{"session": s.id, "prompt": s.first_prompt[:140], "text": f"{s.calls_before_first_edit} calls before first edit, {s.files_read_before_first_edit} files read, {s.blind_edits} blind edits"} for s in sorted(editing, key=lambda x: -(x.calls_before_first_edit or 0))[:3]],
            root_causes=failing(scan, "nav.lockfile_map", "ctx.architecture", "nav.file_size", "ctx.nested", "nav.codeowners"),
            impact="Orientation is paid for in tokens and minutes on every session, and blind edits are a leading source of regressions.",
            fix=["Give agents a semantic code index instead of grep-and-guess.",
                 "Add component-level AGENTS.md files that say where each feature lives.",
                 "Split the largest source files so agents can read a whole unit."],
            playbook="context", effort="M", horizon="60",
        ))

    fails = agg["failures"]
    if fails["topLoops"] or fails["failureRate"] >= 12:
        top = fails["kinds"][:3]
        findings.append(Finding(
            "failure-loops", "high" if fails["sessionsWithLoops"] >= 20 else "medium",
            "Agents repeat the same failing command",
            f"{n(fails['failureRate'])}% of tool calls fail and {n(fails['sessionsWithLoops'])}% of sessions retried an identical failing command. Leading causes: {', '.join(k['label'].lower() for k in top) or 'n/a'}.",
            f"{fails['failedCalls']} failed calls · {fails['loopCalls']} spent in retry loops",
            evidence=[{"session": l["session"], "text": f"`{l['command']}` failed {l['failures']}× — {l['lastError']}"} for l in fails["topLoops"][:4]],
            root_causes=failing(scan, "obs.artifacts", "ctx.commands", "env.runtime", "env.setup"),
            impact="Retry loops burn context and time without new information, and often end in the agent giving up.",
            fix=["Document the failure modes behind the top loops and the fix in agent instructions.",
                 "Pin runtimes and make missing-service errors explicit with a health check.",
                 "Capture these as a skill so the next agent recovers in one step."],
            playbook="skills", effort="S", horizon="30",
        ))

    humans = agg["humans"]
    if humans["sessionsWithCorrections"] >= 15:
        findings.append(Finding(
            "corrections", "medium",
            "Developers steer agents back on course",
            f"{n(humans['sessionsWithCorrections'])}% of sessions needed a correction from the developer ({humans['correctionsPerSession']} per session).",
            f"{humans['correctionsPerSession']} corrections per session",
            evidence=pick_excerpts(sessions, "correction", 4),
            root_causes=failing(scan, "ctx.invariants", "spec.contracts", "ctx.adrs"),
            impact="Each correction is developer attention the agent was supposed to save.",
            fix=["After a correction, add the lesson to agent instructions in the same session.",
                 "Review corrections monthly; repeated ones become rules or skills."],
            playbook="context", effort="S", horizon="60",
        ))

    origins = humans.get("promptOrigins", {})
    total_prompts = sum(origins.values())
    automated = origins.get("workflow", 0) + origins.get("scheduled", 0)
    if total_prompts:
        share = round(100 * automated / total_prompts)
        if share < 20:
            findings.append(Finding(
                "orchestration", "medium",
                "Every agent run starts with a human typing",
                f"{100 - share}% of agent work is started by hand. Review, QA, triage and maintenance are not yet running as triggered or scheduled agent workflows.",
                f"{share}% of agent runs automated",
                root_causes=failing(scan, "tool.skills", "ci.pr", "ci.template"),
                impact="Agent throughput is capped by developer attention rather than by the backlog.",
                fix=["Start with PR review and test-report agents triggered on every pull request.",
                     "Add a nightly maintenance agent for flaky tests and dependency drift."],
                playbook="orchestration", effort="M", horizon="90", source="behavior",
            ))

    wf = agg["workflow"]
    if n_tool >= 5 and wf["planUseRate"] < 20 and wf["skillUseRate"] < 20:
        findings.append(Finding(
            "no-reuse", "medium",
            "Every session starts from zero",
            f"Agents used a plan in {n(wf['planUseRate'])}% of sessions and a team skill in {n(wf['skillUseRate'])}%. Procedures the team repeats (tests, migrations, QA) are re-derived each time.",
            f"{n(wf['skillUseRate'])}% skill use · {n(wf['planUseRate'])}% plan use",
            root_causes=failing(scan, "tool.skills", "tool.mcp", "tool.hooks") or passing(scan, "tool.skills"),
            impact="Knowledge stays in individual sessions instead of compounding across the team.",
            fix=["Turn the three most repeated workflows in this report into skills.",
                 "Require a written plan for multi-file changes."],
            playbook="skills", effort="S", horizon="60",
        ))

    blockers = scan.get("nextLevelBlockers", [])
    if blockers:
        lvl = scan["level"] + 1
        findings.append(Finding(
            "level-blockers", "high" if scan["level"] < 3 else "medium",
            f"{len(blockers)} repository gaps stand between you and Level {lvl}",
            f"The repository reaches Level {scan['level']} ({scan['levelName']}). These criteria at or below Level {lvl} are failing: " + "; ".join(b["title"] for b in blockers[:6]) + ("…" if len(blockers) > 6 else "."),
            f"Level {scan['level']} → {lvl}: {len(blockers)} failing criteria",
            evidence=[{"text": f"{b['title']}: {b['evidence']}"} for b in blockers[:6]],
            root_causes=[{"id": b["id"], "title": b["title"], "evidence": b["evidence"], "passed": False} for b in blockers[:6]],
            impact="Each missing criterion is a class of task agents cannot complete unattended.",
            fix=[b["fix"] for b in blockers[:5]],
            playbook="context", effort="M", horizon="60", source="repository",
        ))

    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.source != "behavior"))
    return findings


def ops_score(agg: dict) -> dict:
    """Agent Operations score (0-100): how well agents actually perform here."""
    if not agg.get("toolSessionCount"):
        return {"score": None, "components": []}
    v = agg["verification"]
    f = agg["failures"]
    h = agg["humans"]
    e = agg["exploration"]
    tool = agg["toolSessionCount"]
    blocked = agg["archetypes"].get("Blocked by environment or missing context", 0) + agg["archetypes"].get("Stuck in failure loops", 0)
    risk_share = agg.get("risks", {}).get("sessionShare", 0)
    components = [
        ("Verified delivery", v["validatedAfterLastEdit"] if v["editSessions"] else 50, 0.25),
        ("Unblocked sessions", 100 - 100 * blocked / max(1, tool), 0.15),
        ("Safe operation", 100 - risk_share, 0.15),
        ("Tool reliability", max(0, 100 - 4 * f["failureRate"]), 0.15),
        ("Prompt quality", 100 - h["vaguePromptRate"], 0.15),
        ("Grounded edits", 100 - e["editSessionsWithBlindEdits"], 0.05),
        ("Low rework", max(0, 100 - 2 * h["sessionsWithCorrections"]), 0.1),
    ]
    score = round(sum(value * weight for _, value, weight in components))
    return {"score": score, "components": [{"name": n, "value": round(v), "weight": w} for n, v, w in components]}


def practice_level(score: int | None) -> int | None:
    if score is None:
        return None
    for level, floor in ((5, 90), (4, 80), (3, 65), (2, 45), (1, 25)):
        if score >= floor:
            return level
    return 0


def cost_model(agg: dict, sessions: list[SessionMetrics], developers: int, sessions_per_dev_week: float) -> dict:
    tool = [s for s in sessions if s.has_tool_data]
    if not tool:
        return {}
    durations = [s.duration_min for s in tool if s.duration_min]
    median_min = statistics.median(durations) if durations else 8.0
    blocked = sum(1 for s in tool if s.archetype in ("blocked", "struggling"))
    unverified = sum(1 for s in tool if s.archetype == "unverified_change")
    per_session = {
        "blockedMinutes": blocked / len(tool) * (median_min + 15),
        "reviewMinutes": unverified / len(tool) * 20,
        "wasteMinutes": agg["friction"]["wastedCalls"] / len(tool) * 0.5,
        "correctionMinutes": agg["friction"]["corrections"] / len(tool) * 4,
    }
    minutes = sum(per_session.values())
    weekly_hours = minutes * sessions_per_dev_week * developers / 60
    return {
        "assumptions": {
            "developers": developers,
            "sessionsPerDevWeek": sessions_per_dev_week,
            "medianSessionMinutes": round(median_min, 1),
            "blockedHandoffMinutes": 15,
            "reviewMinutesPerUnverifiedChange": 20,
            "minutesPerWastedCall": 0.5,
            "developerMinutesPerCorrection": 4,
        },
        "minutesPerSession": round(minutes, 1),
        "breakdown": {k: round(v, 1) for k, v in per_session.items()},
        "hoursPerWeek": round(weekly_hours),
        "hoursPerYear": round(weekly_hours * 46),
        "fteEquivalent": round(weekly_hours / 40, 1),
    }


def findings_dicts(findings: list[Finding]) -> list[dict]:
    out = []
    for f in findings:
        item = asdict(f)
        item["playbookName"] = PLAYBOOKS.get(f.playbook, {}).get("name", "")
        item["alan"] = PLAYBOOKS.get(f.playbook, {}).get("alan", "")
        out.append(item)
    return out


def ops_interval(sessions: list[SessionMetrics], rounds: int = 400) -> dict:
    """80% bootstrap interval of the operations score, so small samples show their uncertainty."""
    import random

    from behavior import aggregate

    tool = [s for s in sessions if s.has_tool_data]
    if len(tool) < 3:
        return {}
    rng = random.Random(7)
    scores = []
    for _ in range(rounds):
        sample = [rng.choice(tool) for _ in tool]
        score = ops_score(aggregate(sample)).get("score")
        if score is not None:
            scores.append(score)
    scores.sort()
    low, high = scores[int(0.1 * len(scores))], scores[int(0.9 * len(scores)) - 1]
    return {"low": low, "high": high, "levelLow": practice_level(low), "levelHigh": practice_level(high), "n": len(tool)}


def auto_caveats(agg: dict, repo_roots: list[str]) -> list[str]:
    caveats = []
    n = agg.get("toolSessionCount", 0)
    if 0 < n < 30:
        caveats.append(f"Directional: {n} tool-level sessions. Percentages move by about {round(100 / n)} points per session; ranges are 80% bootstrap intervals.")
    quality = agg.get("dataQuality", {})
    if quality.get("launchedOutsideRepo"):
        dirs = ", ".join(quality.get("launchDirs", {}))
        caveats.append(f"{quality['launchedOutsideRepo']} sessions were launched from outside the repository ({dirs}), so that directory's agent instructions applied instead of this repository's.")
    return caveats
