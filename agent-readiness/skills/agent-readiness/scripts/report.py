#!/usr/bin/env python3
"""Render the agent readiness assessment as one self-contained HTML file.

No external scripts, fonts or images: the report must open offline and pass
enterprise content-security review.
"""

from __future__ import annotations

import html
import json
import math
import re

from repo_scan import LEVEL_MEANING, LEVEL_NAMES, PILLARS

SHORT_PILLAR = {"context": "Context", "navigability": "Navigability", "environment": "Environment", "verification": "Testing", "guardrails": "Guardrails", "ci": "CI/CD", "observability": "Observability", "safety": "Security", "specs": "Specs", "tooling": "Tooling"}
SEV_LABEL = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}
HORIZON = {"30": "Days 0–30", "60": "Days 31–60", "90": "Days 61–90"}
PALETTE = ["#4f46e5", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#64748b", "#14b8a6"]
ARCHETYPE_COLOR = {
    "Verified delivery": "#10b981",
    "Delivered (reported in chat)": "#34d399",
    "Unverified change": "#f59e0b",
    "Blocked by environment or missing context": "#ef4444",
    "Stuck in failure loops": "#dc2626",
    "Exploration / Q&A": "#0ea5e9",
    "Ops & environment work": "#8b5cf6",
    "Scheduled automation": "#6366f1",
    "Conversation only": "#94a3b8",
}


PATH_PREFIXES: list[re.Pattern[str]] = []


def set_path_prefixes(repo_roots: list[str], worktree_roots: list[str]) -> None:
    """Show repository-relative paths: strip repo roots, and worktree roots plus their checkout directory."""
    PATH_PREFIXES[:] = [re.compile(re.escape(r.rstrip("/")) + "/") for r in repo_roots]
    PATH_PREFIXES.extend(re.compile(re.escape(r.rstrip("/")) + r"/[^/\s`'\"]+/") for r in worktree_roots)


def clean(text: str) -> str:
    for pattern in PATH_PREFIXES:
        text = pattern.sub("", text)
    return re.sub(r"\*\*", "", text)


def e(value: object) -> str:
    return html.escape(clean(str(value if value is not None else "")))


def md_code(text: str) -> str:
    out = e(text)
    parts = out.split("`")
    return "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))


def gauge(value: int | None, label: str, sub: str, color: str) -> str:
    if value is None:
        return f'<div class="gauge"><div class="gauge-na">n/a</div><div class="gauge-label">{e(label)}</div><div class="gauge-sub">{e(sub)}</div></div>'
    frac = max(0, min(5, value)) / 5
    r, cx, cy = 54, 64, 64
    start = math.pi * 0.75
    sweep = math.pi * 1.5
    def pt(f: float) -> tuple[float, float]:
        a = start + sweep * f
        return cx + r * math.cos(a), cy + r * math.sin(a)
    x0, y0 = pt(0)
    x1, y1 = pt(1)
    xv, yv = pt(frac)
    large_bg = 1
    large_v = 1 if sweep * frac > math.pi else 0
    arc_v = f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 {large_v} 1 {xv:.1f},{yv:.1f}" stroke="{color}" stroke-width="12" fill="none" stroke-linecap="round"/>' if frac > 0 else ""
    return f'''<div class="gauge"><svg viewBox="0 0 128 112" width="150" height="132" role="img" aria-label="{e(label)} level {value}">
<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 {large_bg} 1 {x1:.1f},{y1:.1f}" stroke="var(--track)" stroke-width="12" fill="none" stroke-linecap="round"/>{arc_v}
<text x="64" y="62" text-anchor="middle" class="gauge-num">L{value}</text>
<text x="64" y="82" text-anchor="middle" class="gauge-name">{e(LEVEL_NAMES.get(value, "Not ready"))}</text></svg>
<div class="gauge-label">{e(label)}</div><div class="gauge-sub">{e(sub)}</div></div>'''


def hbars(rows: list[tuple[str, float, str]], unit: str = "", max_value: float | None = None) -> str:
    if not rows:
        return '<p class="muted">No data.</p>'
    top = max_value or max(v for _, v, _ in rows) or 1
    items = []
    for label, value, color in rows:
        width = max(1.5, 100 * value / top)
        items.append(f'<div class="hbar"><div class="hbar-label">{e(label)}</div><div class="hbar-track"><div class="hbar-fill" style="width:{width:.1f}%;background:{color}"></div></div><div class="hbar-val">{e(fmt(value))}{e(unit)}</div></div>')
    return '<div class="hbars">' + "".join(items) + "</div>"


def fmt(value: float) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.1f}"
    return f"{int(value):,}"


def donut(rows: list[tuple[str, float, str]], center: str, sub: str) -> str:
    total = sum(v for _, v, _ in rows) or 1
    r, c = 46, 2 * math.pi * 46
    offset = 0.0
    segs = []
    for _, value, color in rows:
        length = c * value / total
        segs.append(f'<circle r="{r}" cx="60" cy="60" fill="none" stroke="{color}" stroke-width="16" stroke-dasharray="{length:.2f} {c - length:.2f}" stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 60 60)"/>')
        offset += length
    legend = "".join(f'<li><span class="dot" style="background:{color}"></span>{e(label)}<b>{e(fmt(value))}</b></li>' for label, value, color in rows)
    return f'<div class="donut"><svg viewBox="0 0 120 120" width="150" height="150">{"".join(segs)}<text x="60" y="58" text-anchor="middle" class="donut-num">{e(center)}</text><text x="60" y="74" text-anchor="middle" class="donut-sub">{e(sub)}</text></svg><ul class="legend">{legend}</ul></div>'


def radar(pillars: dict) -> str:
    keys = list(pillars)
    n = len(keys)
    cx, cy, r = 170, 150, 110
    def point(i: int, value: float) -> tuple[float, float]:
        a = -math.pi / 2 + 2 * math.pi * i / n
        return cx + r * value * math.cos(a), cy + r * value * math.sin(a)
    rings = "".join(
        f'<polygon points="{" ".join(f"{point(i, f)[0]:.1f},{point(i, f)[1]:.1f}" for i in range(n))}" fill="none" stroke="var(--grid)"/>'
        for f in (0.25, 0.5, 0.75, 1)
    )
    spokes = "".join(f'<line x1="{cx}" y1="{cy}" x2="{point(i, 1)[0]:.1f}" y2="{point(i, 1)[1]:.1f}" stroke="var(--grid)"/>' for i in range(n))
    shape = " ".join(f"{point(i, pillars[k]['score'] / 100)[0]:.1f},{point(i, pillars[k]['score'] / 100)[1]:.1f}" for i, k in enumerate(keys))
    labels = []
    for i, k in enumerate(keys):
        x, y = point(i, 1.2)
        anchor = "middle" if abs(x - cx) < 20 else ("start" if x > cx else "end")
        labels.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" class="radar-label">{e(SHORT_PILLAR.get(k, k))} <tspan font-weight="700">{pillars[k]["score"]}</tspan></text>')
    return f'<svg viewBox="-30 0 400 300" class="radar" role="img" aria-label="Pillar scores">{rings}{spokes}<polygon points="{shape}" fill="rgba(79,70,229,.18)" stroke="#4f46e5" stroke-width="2"/>{"".join(labels)}</svg>'


def ladder(scan: dict) -> str:
    head = "".join(f'<th>L{l}<span>{e(LEVEL_NAMES[l])}</span></th>' for l in LEVEL_NAMES)
    rows = []
    for key, name in PILLARS.items():
        cells = []
        for level in LEVEL_NAMES:
            items = [c for c in scan["criteria"] if c["pillar"] == key and c["level"] == level and not c.get("skipped")]
            if not items:
                cells.append('<td class="cell empty">·</td>')
                continue
            passed = sum(c["passed"] for c in items)
            ratio = passed / len(items)
            cls = "full" if ratio == 1 else "part" if ratio > 0 else "none"
            tip = "; ".join(f'{"✓" if c["passed"] else "✗"} {c["title"]}' for c in items)
            cells.append(f'<td class="cell {cls}" title="{e(tip)}">{passed}/{len(items)}</td>')
        rows.append(f'<tr><th class="rowh">{e(name)}</th>{"".join(cells)}</tr>')
    rates = "".join(f'<td class="rate">{round(100 * scan["levelRates"][str(l)] if isinstance(next(iter(scan["levelRates"])), str) else 100 * scan["levelRates"][l])}%</td>' for l in LEVEL_NAMES)
    return f'<table class="ladder"><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(rows)}<tr><th class="rowh">Level pass rate (80% unlocks)</th>{rates}</tr></tbody></table>'


def practice_sub(data: dict) -> str:
    ops = data.get("ops") or {}
    if ops.get("score") is None:
        return "No tool-level sessions"
    interval = data.get("opsInterval") or {}
    if interval:
        levels = f"L{interval['levelLow']}" if interval["levelLow"] == interval["levelHigh"] else f"L{interval['levelLow']}–L{interval['levelHigh']}"
        return f"Operations {ops['score']}/100 · 80% range {interval['low']}–{interval['high']} ({levels})"
    return f"Agent operations {ops['score']}/100"


def stat(value: str, label: str, tone: str = "") -> str:
    value = re.sub(r"(\d+)\.0%", r"\1%", str(value))
    value = re.sub(r"(\d+)\.(\d)\d*%", lambda m: f"{round(float(m.group(1) + '.' + m.group(2)))}%", value)
    return f'<div class="stat {tone}"><div class="stat-v">{e(value)}</div><div class="stat-l">{e(label)}</div></div>'


def funnel(agg: dict, sessions: list[dict]) -> str:
    tool = [s for s in sessions if s["has_tool_data"]]
    if not tool:
        return ""
    steps = [
        ("Sessions with a coding request", len(tool)),
        ("Explored the codebase", sum(1 for s in tool if s["category_counts"].get("read") or s["category_counts"].get("search") or s["category_counts"].get("index"))),
        ("Changed code", sum(1 for s in tool if s["category_counts"].get("edit"))),
        ("Verified after final edit", sum(1 for s in tool if s["validated_after_last_edit"])),
        ("Committed or opened a PR", sum(1 for s in tool if s["delivered"])),
    ]
    top = steps[0][1] or 1
    rows = []
    for i, (label, value) in enumerate(steps):
        width = 100 * value / top
        drop = ""
        if i:
            prev = steps[i - 1][1] or 1
            lost = prev - value
            drop = f'<span class="drop">−{lost} ({round(100 * lost / prev)}%)</span>' if lost > 0 else ""
        rows.append(f'<div class="fstep"><div class="flabel">{e(label)}</div><div class="ftrack"><div class="ffill" style="width:{width:.1f}%"></div><span class="fval">{value}</span></div>{drop}</div>')
    return '<div class="funnel">' + "".join(rows) + "</div>"


def finding_card(f: dict, idx: int) -> str:
    evidence = "".join(
        f'<li>{md_code(ev.get("text") or ev.get("error") or "")}'
        + (f'<span class="ev-src">{e(ev.get("prompt") or "")[:120]}</span>' if ev.get("prompt") else "")
        + "</li>"
        for ev in f.get("evidence", []) if (ev.get("text") or ev.get("error"))
    )
    failing_causes = [c for c in f.get("root_causes", []) if c.get("passed") is not True]
    context = [c for c in f.get("root_causes", []) if c.get("passed") is True]
    causes = "".join(f'<li><code>{e(c["id"])}</code> {e(c["title"])} — <span class="muted">{e(c["evidence"])}</span></li>' for c in failing_causes)
    causes += "".join(f'<li class="ctx"><code>{e(c["id"])}</code> passes: {e(c["title"])} — <span class="muted">{e(c["evidence"])}</span></li>' for c in context)
    fixes = "".join(f"<li>{md_code(x)}</li>" for x in f.get("fix", []))
    return f'''<article class="finding sev-{e(f["severity"])}" id="f-{e(f["id"])}">
<header><span class="badge {e(f["severity"])}">{e(SEV_LABEL[f["severity"]])}</span><span class="fnum">Finding {idx}</span><span class="fsrc">{e("Observed in sessions" if f["source"] == "behavior" else "Repository scan")}</span></header>
<h3>{e(f["title"])}</h3>
<p class="headline">{md_code(f["headline"])}</p>
<div class="fmetric">{e(f["metric"])}</div>
<div class="fgrid">
<div><h4>What we saw</h4><ul class="evidence">{evidence or '<li class="muted">See metric above.</li>'}</ul></div>
<div><h4>Why it happens</h4><ul class="causes">{causes or '<li class="muted">Behavioral: no single repository criterion explains it.</li>'}</ul></div>
</div>
<div class="fgrid">
<div><h4>Impact</h4><p>{e(f["impact"])}</p><h4>Fix</h4><ol>{fixes}</ol></div>
<div><h4>Owner playbook</h4><p class="alan-line"><b>{e(f.get("playbookName", ""))}</b> · effort {e(f["effort"])} · {e(HORIZON.get(f["horizon"], ""))}</p></div>
</div></article>'''


def panel_section(panel: dict | None) -> str:
    if not panel:
        return '<p class="muted">Run the expert panel step (see SKILL.md) to add an adversarial review of these findings.</p>'
    experts = "".join(
        f'''<div class="expert"><div class="ex-head"><div class="avatar" style="background:{PALETTE[i % len(PALETTE)]}">{e(p.get("name", "?")[:1])}</div><div><b>{e(p.get("name"))}</b><span>{e(p.get("role"))}</span></div><span class="stance {e(p.get("stance", "agree"))}">{e(p.get("stanceLabel") or p.get("stance", ""))}</span></div>
<p class="ex-take">“{md_code(p.get("position", ""))}”</p>
<ul>{"".join(f"<li>{md_code(x)}</li>" for x in p.get("arguments", []))}</ul>
{f'<p class="ex-challenge"><b>Challenge:</b> {md_code(p["challenge"])}</p>' if p.get("challenge") else ""}</div>'''
        for i, p in enumerate(panel.get("experts", []))
    )
    disagreements = "".join(
        f'<li><b>{e(d.get("topic"))}</b> — {md_code(d.get("resolution", ""))}</li>' for d in panel.get("disagreements", [])
    )
    verdict = panel.get("verdict", {})
    adjustments = "".join(f"<li>{md_code(a)}</li>" for a in verdict.get("adjustments", []))
    return f'''<div class="experts">{experts}</div>
<div class="verdict"><h3>Panel verdict</h3><p class="headline">{md_code(verdict.get("summary", ""))}</p>
{f"<h4>Where the panel disagreed</h4><ul>{disagreements}</ul>" if disagreements else ""}
{f"<h4>Adjustments made to this report</h4><ul>{adjustments}</ul>" if adjustments else ""}
{f'<p class="confidence">Confidence: <b>{e(verdict.get("confidence"))}</b> · {md_code(verdict.get("confidenceReason", ""))}</p>' if verdict.get("confidence") else ""}</div>'''


def roadmap(findings: list[dict], projection: dict) -> str:
    lanes = []
    for key, label in HORIZON.items():
        items = [f for f in findings if f["horizon"] == key]
        cards = "".join(f'<li><a href="#f-{e(f["id"])}">{e(f["title"])}</a><span>{e(f.get("playbookName", ""))} · effort {e(f["effort"])}</span></li>' for f in items)
        proj = projection.get(key)
        tail = f'<div class="lane-proj">Repository level after this phase: <b>L{proj["level"]} {e(LEVEL_NAMES.get(proj["level"], ""))}</b> · score {proj["score"]}</div>' if proj else ""
        lanes.append(f'<div class="lane"><h4>{e(label)}</h4><ul>{cards or "<li class=muted>—</li>"}</ul>{tail}</div>')
    return '<div class="lanes">' + "".join(lanes) + "</div>"


def risk_card(agg: dict) -> str:
    risks = agg.get("risks") or {}
    if not risks.get("total"):
        return ""
    by_kind = "".join(f'<tr><td>{e(k["label"])}</td><td class=num>{k["count"]}</td><td class=num><b class="{"bad" if k["undisclosed"] else ""}">{k["undisclosed"]}</b></td></tr>' for k in risks["byKind"])
    rows = "".join(
        f'<tr><td class="prompt">{e(r.get("prompt", ""))}</td><td>{e(r["label"])}</td><td><code>{e(r["command"])}</code></td><td>{"✓ disclosed" if r["disclosed"] else "<b class=bad>✗ not disclosed</b>"}</td></tr>'
        for r in [r for r in risks.get("events", []) if r.get("severity") == "high"][:10]
    )
    return f'''<div class="card risk"><h3>What agents did beyond the diff <span class="n">n={agg.get("toolSessionCount", 0)} sessions</span></h3>
<p class="muted small">Side effects a code reviewer never sees: writes outside the workspace, shared dependency changes, databases and containers, history rewrites, direct data access. "Disclosed" means the agent's final report mentioned it.</p>
<div class="stats">{stat(f'{risks["sessionsWithRisk"]}/{agg.get("toolSessionCount", 0)}', "sessions with at least one side effect", "bad" if risks["sessionShare"] >= 30 else "warn")}{stat(str(risks["total"]), f"high-risk actions (+{risks.get('lowRisk', 0)} scratch writes)")}{stat(str(risks["undisclosed"]), "high-risk actions not mentioned in the final report", "bad" if risks["undisclosed"] else "good")}</div>
<div class="fgrid"><div><table class="mini"><thead><tr><th>Action</th><th class=num>Count</th><th class=num>Undisclosed</th></tr></thead><tbody>{by_kind}</tbody></table></div>
<div class="tablewrap flat"><table class="mini"><thead><tr><th>Request</th><th>Action</th><th>Command</th><th>Reported?</th></tr></thead><tbody>{rows}</tbody></table></div></div></div>'''


def model_table(agg: dict) -> str:
    rows = agg.get("byModel") or []
    if len(rows) < 2:
        return ""
    body = "".join(
        f'<tr><td>{e(r["model"].replace("claude-", ""))}</td><td class=num>{r["sessions"]}</td><td class=num>{"—" if r["verified"] is None else str(round(r["verified"])) + "%"}</td><td class=num>{round(r["blocked"])}%</td><td class=num>{r["medianCalls"]:.0f}</td><td class=num>{round(r["failureRate"])}%</td><td class=num>{r["riskEvents"]}</td></tr>'
        for r in rows
    )
    return f'<table class="mini"><thead><tr><th>Model</th><th class=num>Sessions</th><th class=num>Verified</th><th class=num>Blocked</th><th class=num>Median calls</th><th class=num>Failed calls</th><th class=num>Side effects</th></tr></thead><tbody>{body}</tbody></table>'


COMPARE_METRICS: list[tuple[str, str, bool, str]] = [
    ("Code changes verified after the final edit", "verification.validatedAfterLastEdit", True, "%"),
    ("Sessions blocked", "_blocked", False, "%"),
    ("Tool calls that fail", "failures.failureRate", False, "%"),
    ("Verification commands that follow AGENTS.md", "_adherence", True, "%"),
    ("Sessions that opened the agent instructions", "_docread", True, "%"),
    ("Sessions with side effects outside the diff", "risks.sessionShare", False, "%"),
    ("Undisclosed side effects", "risks.undisclosed", False, ""),
    ("Median tool calls per session", "workflow.medianToolCalls", False, ""),
    ("Agent operations score", "_ops", True, ""),
    ("Repository readiness score", "_repo", True, ""),
]


def metric_value(data: dict, key: str) -> float | None:
    agg = data["behavior"]
    if key == "_blocked":
        tool = agg.get("toolSessionCount") or 0
        blocked = agg["archetypes"].get("Blocked by environment or missing context", 0) + agg["archetypes"].get("Stuck in failure loops", 0)
        return round(100 * blocked / tool) if tool else None
    if key == "_adherence":
        return (data.get("adherence") or {}).get("documentedShare")
    if key == "_docread":
        return (data.get("instructionReach") or {}).get("agentDocReadRate")
    if key == "_ops":
        return (data.get("ops") or {}).get("score")
    if key == "_repo":
        return data["repos"][0]["score"]
    node: object = agg
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, (int, float)) else None


def comparison(data: dict) -> str:
    base = data.get("baseline")
    if not base:
        return ""
    rows = []
    for label, key, higher_better, unit in COMPARE_METRICS:
        before, after = metric_value(base, key), metric_value(data, key)
        if before is None or after is None:
            continue
        delta = after - before
        good = (delta > 0) == higher_better if delta else None
        tone = "good" if good else "bad" if good is False else "muted"
        arrow = "▲" if delta > 0 else "▼" if delta < 0 else "＝"
        rows.append(f'<tr><td>{e(label)}</td><td class=num>{round(before)}{unit}</td><td class=num><b>{round(after)}{unit}</b></td><td class="num {tone}">{arrow} {abs(round(delta))}{unit}</td></tr>')
    changes = "".join(f"<li>{md_code(c)}</li>" for c in data.get("baselineChanges", []))
    return f'''<section id="change"><div class="card change"><p class="eyebrow">Measured change</p><h3>Same {base["behavior"].get("toolSessionCount", 0)} requests, before and after the day-one fixes</h3>
<p class="muted">Baseline: {e(base.get("label", "before"))} · After: {e(data.get("label", "after"))}. Same prompts, same models, fresh isolated workspaces.</p>
{f"<h4>What changed in the repository</h4><ul>{changes}</ul>" if changes else ""}
<table class="mini compare"><thead><tr><th>Metric</th><th class=num>Before</th><th class=num>After</th><th class=num>Change</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>'''


def proof_plan(data: dict) -> str:
    agg = data["behavior"]
    scan = data["repos"][0]
    tool = agg.get("toolSessionCount", 0)
    rows: list[tuple[str, str, str, str]] = []
    proj = data.get("projection", {}).get("30") or {}
    rows.append(("Repository readiness level", f"L{scan['level']} · {scan['score']}", f"L{proj.get('level', scan['level'])} · {proj.get('score', scan['score'])}", "Criteria re-scanned on every run"))
    if tool:
        blocked = agg["archetypes"].get("Blocked by environment or missing context", 0) + agg["archetypes"].get("Stuck in failure loops", 0)
        rows.append(("Sessions blocked by environment", f"{round(100 * blocked / tool)}%", "under 10%", "Session outcome classification"))
        if agg["verification"]["editSessions"]:
            rows.append(("Code changes verified after the final edit", f"{round(agg['verification']['validatedAfterLastEdit'])}%", "90%+", "Test, type check or build passing after the last edit"))
        reach = data.get("instructionReach") or {}
        if reach.get("gaps"):
            rows.append(("Sessions that load your agent instructions", f"{100 - max(g['sessionShare'] for g in reach['gaps'])}%", "100%", "Instruction files per harness in use"))
        adhere = data.get("adherence") or {}
        if adhere.get("documentedShare") is not None:
            rows.append(("Verification commands that follow AGENTS.md", f"{round(adhere['documentedShare'])}%", "70%+", "Commands run vs commands documented"))
        rows.append(("Requests with files, criteria or a check", f"{round(100 - agg['humans']['vaguePromptRate'])}%", "70%+", "First-prompt specificity signals"))
        rows.append(("Code-changing sessions with blind edits", f"{round(agg['exploration']['editSessionsWithBlindEdits'])}%", "under 15%", "Files modified without being opened first"))
        rows.append(("Tool calls that fail", f"{round(agg['failures']['failureRate'])}%", "under 5%", "Tool results with errors or non-zero exits"))
    body = "".join(f"<tr><td>{e(a)}</td><td class=num><b>{e(b)}</b></td><td class=num><b class=target>{e(c)}</b></td><td class=muted>{e(d)}</td></tr>" for a, b, c, d in rows)
    weeks = [
        ("Week 1", "Instructions reach every harness; agents get a reproducible environment with services, seeds and env vars; the top failing commands are documented."),
        ("Weeks 2–3", "Verification gates on every agent change: focused tests and type checks before handoff, and a test report with evidence on each PR."),
        ("Week 4", "Spec-first intake for one-line requests, the three most repeated procedures captured as team skills, and this assessment re-run to show the change."),
    ]
    steps = "".join(f'<div class="wk"><b>{e(w)}</b><p>{e(t)}</p></div>' for w, t in weeks)
    return (
        '<div class="proof"><p class="eyebrow">How Alan fixes this</p><h3>Thirty days, measured by the same report</h3>'
        '<p class="muted">Every number below comes from the instrument you just read. We re-run it weekly on your sessions, so progress is observed, not claimed.</p>'
        f'<div class="weeks">{steps}</div>'
        '<table class="mini proof-table"><thead><tr><th>Metric</th><th class=num>Today</th><th class=num>30-day target</th><th>How it is measured</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def scorecard(scan: dict) -> str:
    blocks = []
    for key, name in PILLARS.items():
        p = scan["pillars"][key]
        rows = "".join(
            f'<tr class="{"pass" if c["passed"] else "skip" if c.get("skipped") else "fail"}"><td>{"✓" if c["passed"] else "—" if c.get("skipped") else "✗"}</td><td>L{c["level"]}</td><td><b>{e(c["title"])}</b><div class="muted">{e(c["why"])}</div></td><td>{e(c["evidence"])}{"" if c["passed"] or c.get("skipped") else f"<div class=fixline>Fix: {e(c['fix'])}</div>"}</td></tr>'
            for c in sorted((c for c in scan["criteria"] if c["pillar"] == key), key=lambda c: c["level"])
        )
        blocks.append(f'<details><summary><span class="pname">{e(name)}</span><span class="pbar"><span style="width:{p["score"]}%"></span></span><span class="pscore">{p["passed"]}/{p["total"]} · {p["score"]}</span></summary><table class="crit"><tbody>{rows}</tbody></table></details>')
    return "".join(blocks)


def session_table(sessions: list[dict]) -> str:
    rows = []
    for s in sorted(sessions, key=lambda x: (not x["has_tool_data"], x["archetype"])):
        if not s["has_tool_data"] and not s["user_prompts"]:
            continue
        color = ARCHETYPE_COLOR.get(s.get("archetypeLabel", ""), "#94a3b8")
        rows.append(
            f'<tr><td class="prompt">{e(s["first_prompt"][:150] or s["title"][:150])}</td><td>{e((s["model"] or s["provider"]).replace("claude-", ""))}</td>'
            f'<td><span class="pill" style="border-color:{color};color:{color}">{e(s.get("archetypeLabel", s["archetype"]))}</span></td>'
            f'<td class="num">{s["tool_calls"] if s["has_tool_data"] else "—"}</td><td>{e(s["verification_depth"] if s["has_tool_data"] else "—")}</td>'
            f'<td class="num">{s["failed_calls"] if s["has_tool_data"] else "—"}</td><td class="num">{s["corrections"]}</td><td class="num">{e(s["duration_min"] if s["duration_min"] is not None else "—")}</td></tr>'
        )
    return f'<div class="tablewrap"><table class="sessions"><thead><tr><th>Developer request</th><th>Model</th><th>Outcome</th><th>Tool calls</th><th>Verification</th><th>Failures</th><th>Corrections</th><th>Minutes</th></tr></thead><tbody>{"".join(rows[:80])}</tbody></table></div>'


def render(data: dict) -> str:
    set_path_prefixes([r["path"] for r in data["repos"]], data.get("worktreeRoots", []))
    org = data["org"]
    scan = data["repos"][0]
    agg = data["behavior"]
    sessions = data["sessions"]
    findings = data["findings"]
    ops = data["ops"]
    cost = data.get("cost") or {}
    adhere = data.get("adherence") or {}
    panel = data.get("panel")
    paper = scan["level"]
    practice = data.get("practiceLevel")
    tool_n = agg.get("toolSessionCount", 0)

    verdict = data.get("verdict") or (
        f"On paper {e(org)} is Level {paper}. In practice its agents operate at Level {practice}."
        if practice is not None else f"{e(org)} reaches Level {paper} ({LEVEL_NAMES.get(paper, '')})."
    )
    top3 = findings[:3]
    top_cards = "".join(
        f'<a class="topf sev-{e(f["severity"])}" href="#f-{e(f["id"])}"><span class="badge {e(f["severity"])}">{e(SEV_LABEL[f["severity"]])}</span><h4>{e(f["title"])}</h4><p>{e(f["metric"])}</p></a>'
        for f in top3
    )
    arche = agg.get("archetypes", {})
    arche_rows = [(k, v, ARCHETYPE_COLOR.get(k, "#94a3b8")) for k, v in arche.items()]
    tool_mix = agg.get("toolMix", {})
    mix_labels = {"read": "Read files", "search": "Search", "index": "Code index", "edit": "Edit", "validate": "Test / lint / build", "shell": "Other shell", "mcp": "MCP tools", "delegate": "Sub-agents", "plan": "Planning", "web": "Web", "skill": "Skills", "deliver": "Commit / PR", "other": "Other"}
    mix_rows = [(mix_labels.get(k, k), v, PALETTE[i % len(PALETTE)]) for i, (k, v) in enumerate(tool_mix.items())]
    fail_rows = [(k["label"], k["count"], "#ef4444") for k in agg.get("failures", {}).get("kinds", [])][:8]
    depth = {}
    for s in sessions:
        if s["has_tool_data"] and s["category_counts"].get("edit"):
            depth[s["verification_depth"]] = depth.get(s["verification_depth"], 0) + 1
    depth_names = {"e2e": "End-to-end tests", "test": "Unit/integration tests", "typecheck": "Type check", "build": "Build", "lint": "Lint only", "none": "Nothing"}
    depth_colors = {"e2e": "#059669", "test": "#10b981", "typecheck": "#34d399", "build": "#6ee7b7", "lint": "#f59e0b", "none": "#ef4444"}
    depth_rows = [(depth_names[k], depth[k], depth_colors[k]) for k in ("e2e", "test", "typecheck", "build", "lint", "none") if depth.get(k)]
    origins = agg.get("humans", {}).get("promptOrigins", {})
    origin_names = {"direct": "Typed by a developer", "chat_relay": "Relayed from Slack/Teams", "workflow": "Workflow / orchestrator", "scheduled": "Scheduled automation", "continuation": "Continuation / resume", "interrupt": "Interrupt"}
    origin_rows = [(origin_names.get(k, k), v, PALETTE[i % len(PALETTE)]) for i, (k, v) in enumerate(sorted(origins.items(), key=lambda kv: -kv[1]))]
    spec_rows = agg.get("humans", {}).get("specificityVsOutcome", [])
    spec_table = "".join(f'<tr><td>{e(r["bucket"])}</td><td class="num">{r["sessions"]}</td><td class="num">{r["verified"]}%</td><td class="num">{r["corrections"]}</td><td class="num">{r["medianCalls"]}</td></tr>' for r in spec_rows)
    loops = agg.get("failures", {}).get("topLoops", [])
    loop_rows = "".join(f'<tr><td><code>{e(l["command"])}</code></td><td class="num">{l["failures"]}×</td><td>{e(l["lastError"])}</td></tr>' for l in loops[:6])
    exp = agg.get("exploration", {})
    ver = agg.get("verification", {})
    hum = agg.get("humans", {})
    wf = agg.get("workflow", {})
    tok = agg.get("tokens", {})
    models = agg.get("models", {})
    model_rows = [(k.replace("claude-", ""), v, PALETTE[i % len(PALETTE)]) for i, (k, v) in enumerate(models.items())]

    adherence_html = ""
    if adhere.get("validationCalls"):
        viol = "".join(f'<li><b>{md_code(v["rule"])}</b> — agent ran <code>{e(v["command"])}</code></li>' for v in adhere.get("violations", [])[:5])
        undoc = "".join(f'<li><code>{e(u["command"])}</code> ×{u["count"]}</li>' for u in adhere.get("undocumentedTop", [])[:5])
        adherence_html = f'''<div class="card"><h3>Do agents follow your instructions?</h3>
<div class="stats">{stat(f'{adhere.get("documentedShare")}%', "verification commands that match AGENTS.md", "warn" if (adhere.get("documentedShare") or 0) < 60 else "good")}{stat(str(len(adhere.get("violations", []))), "commands that broke a written rule", "bad" if adhere.get("violations") else "good")}{stat(str(len(adhere.get("documentedCommands", []))), "commands documented for agents")}</div>
<div class="fgrid"><div><h4>Rules broken</h4><ul class="evidence">{viol or "<li class=muted>None observed.</li>"}</ul></div><div><h4>What agents ran instead</h4><ul class="evidence">{undoc or "<li class=muted>—</li>"}</ul></div></div></div>'''

    cost_html = ""
    if cost:
        a = cost["assumptions"]
        cost_html = f'''<div class="card cost"><h3>What agent friction costs <span class="n">illustrative</span></h3>
<div class="stats">{stat(f'{cost["minutesPerSession"]} min', "lost per agent session", "bad")}{stat(f'{cost["hoursPerWeek"]:,} h', f"per week across {a['developers']} developers", "bad")}{stat(f'{cost["fteEquivalent"]}', "engineer-equivalents of capacity", "bad")}</div>
<p class="muted small">Estimate from observed sessions: blocked sessions × (median session {a["medianSessionMinutes"]} min + {a["blockedHandoffMinutes"]} min handoff) + unverified changes × {a["reviewMinutesPerUnverifiedChange"]} min extra review + wasted calls × {a["minutesPerWastedCall"]} min + corrections × {a["developerMinutesPerCorrection"]} min, at {a["sessionsPerDevWeek"]} agent sessions per developer per week. Change the inputs with <code>--developers</code> and <code>--sessions-per-week</code>.</p></div>'''

    findings_html = "".join(finding_card(f, i + 1) for i, f in enumerate(findings))
    proj = data.get("projection", {})
    proj_final = proj.get("90") or proj.get("60") or proj.get("30")

    sources = agg.get("sources", {})
    provider_list = ", ".join(f"{k} ({v})" for k, v in agg.get("providers", {}).items())
    method_sources = "".join(f"<li>{e(k)}: {v} sessions</li>" for k, v in sources.items())
    caveats = "".join(f"<li>{md_code(c)}</li>" for c in data.get("caveats", []))

    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Readiness Assessment — {e(org)}</title><style>{CSS}</style></head><body>
<header class="top"><div class="brand"><span class="logo">A</span><div><b>Agent Readiness Assessment</b><span>{e(org)} · {e(data["generatedAt"])}</span></div></div><span class="conf">Confidential · generated locally, no code left this machine</span></header>
<nav class="toc"><a href="#summary">Summary</a>{'<a href="#change">Measured change</a>' if data.get("baseline") else ''}<a href="#ladder">Readiness ladder</a><a href="#behavior">How agents work</a><a href="#findings">Findings</a><a href="#panel">Expert panel</a><a href="#roadmap">Roadmap</a><a href="#proof">30-day plan</a><a href="#scorecard">Scorecard</a><a href="#sessions">Sessions</a><a href="#method">Method</a></nav>
<main>
<section id="summary" class="hero">
<div class="hero-left"><p class="eyebrow">Executive summary</p><h1>{verdict}</h1><p class="lede">{e(data.get("lede") or LEVEL_MEANING.get(practice if practice is not None else paper, ""))}</p>
{f'<p class="confidence-line">{md_code(data["caveats"][0])}</p>' if data.get("caveats") else ""}<div class="stats">{stat(str(len(data["repos"])), "repositories scanned")}{stat(f"{agg.get('sessionCount', 0):,}", "agent sessions analyzed")}{stat(f"{agg.get('toolCalls', 0):,}", "tool calls inspected")}{stat(str(len(models) or len(agg.get('providers', {}))), "models / harnesses")}</div></div>
<div class="hero-right">{gauge(paper, "On paper", f"Repository score {scan['score']}/100", "#4f46e5")}{gauge(practice, "In practice", practice_sub(data), "#f59e0b")}{gauge(proj_final["level"] if proj_final else None, "After the 90-day plan", f"Repository score {proj_final['score'] if proj_final else 'n/a'}/100", "#10b981")}</div>
</section>
<section class="topfindings"><h2>The three things to fix first</h2><div class="topgrid">{top_cards}</div></section>
{cost_html}
{comparison(data)}
<section id="ladder"><h2>Readiness ladder</h2><p class="sub">{len(scan["criteria"])} evidence-backed criteria across 10 pillars. A level unlocks when 80% of its criteria and every level below it pass. Hover a cell for its criteria.</p>
<div class="ladder-wrap"><div>{ladder(scan)}</div><div class="card radar-card"><h3>Pillar scores</h3>{radar(scan["pillars"])}</div></div></section>

<section id="behavior"><h2>How your agents actually work</h2><p class="sub">Measured from {tool_n} tool-level transcripts ({agg.get("toolCalls", 0):,} tool calls){f" and {agg.get('dialogueOnlyCount')} dialogue-only sessions" if agg.get("dialogueOnlyCount") else ""}. This is the layer a file scan cannot see.</p>
<div class="grid2"><div class="card"><h3>From request to verified change</h3>{funnel(agg, sessions)}</div><div class="card"><h3>How sessions end <span class="n">n={agg.get("sessionCount", 0)}</span></h3>{donut(arche_rows, str(agg.get("sessionCount", 0)), "sessions")}</div></div>
<div class="grid3">
<div class="card"><h3>Studying the codebase <span class="n">n={tool_n}</span></h3><div class="stats col">{stat(f'{exp.get("medianCallsBeforeFirstEdit", 0)}', "median tool calls before first edit")}{stat(f'{exp.get("exploreShare", 0)}%', "of all calls spent reading and searching")}{stat(f'{exp.get("editSessionsWithBlindEdits", 0)}%', "of code-changing sessions edited a file they never opened", "warn" if exp.get("editSessionsWithBlindEdits", 0) >= 30 else "")}{stat(f'{exp.get("indexUseRate", 0)}%', "of sessions used a code index", "warn" if exp.get("indexUseRate", 0) < 10 else "good")}</div></div>
<div class="card"><h3>How changes are verified <span class="n">n={ver.get("editSessions", 0)}</span></h3>{donut(depth_rows, f'{ver.get("validatedAfterLastEdit", 0)}%', "verified") if depth_rows else '<p class="muted">No code-changing sessions.</p>'}</div>
<div class="card"><h3>Where tool time goes</h3>{hbars(mix_rows)}</div></div>
<div class="grid2"><div class="card"><h3>Why tool calls fail</h3>{hbars(fail_rows) if fail_rows else '<p class="muted">No failed tool calls.</p>'}{f'<h4>Repeated failing commands</h4><table class="mini"><tbody>{loop_rows}</tbody></table>' if loop_rows else ""}</div>
<div class="card"><h3>How developers direct agents</h3><div class="stats">{stat(f'{hum.get("medianFirstPromptWords", 0):.0f}', "median words in the first request")}{stat(f'{hum.get("vaguePromptRate", 0)}%', "requests with no file, criterion or check", "warn" if hum.get("vaguePromptRate", 0) >= 25 else "")}{stat(f'{hum.get("sessionsWithCorrections", 0)}%', "sessions needing a correction")}</div>
{f'<table class="mini"><thead><tr><th>Request quality</th><th>Sessions</th><th>Verified</th><th>Corrections</th><th>Median calls</th></tr></thead><tbody>{spec_table}</tbody></table>' if spec_table else ""}</div></div>
<div class="grid3">
<div class="card"><h3>Who starts agent work</h3>{donut(origin_rows, f'{sum(origins.values())}', "prompts") if origin_rows else '<p class="muted">—</p>'}</div>
<div class="card"><h3>Models in use</h3>{hbars(model_rows) if model_rows else '<p class="muted">Model not recorded.</p>'}{model_table(agg)}<p class="muted small">Harnesses: {e(provider_list)}</p></div>
<div class="card"><h3>Working style</h3><div class="stats col">{stat(f'{wf.get("planUseRate", 0)}%', "sessions that wrote a plan")}{stat(f'{wf.get("delegationRate", 0)}%', "sessions that delegated to sub-agents")}{stat(f'{wf.get("skillUseRate", 0)}%', "sessions that used a team skill")}{stat(f'{tok.get("cacheHitRate", 0)}%', "prompt-cache hit rate")}</div></div></div>
{adherence_html}
{risk_card(agg)}
</section>

<section id="findings"><h2>Findings</h2><p class="sub">Each finding links what agents did to the repository gap that causes it, what it costs, and the fix.</p>{findings_html}</section>
<section id="panel"><h2>Expert panel review</h2>{'<p class="sub">Four independent reviewers recomputed the numbers from raw transcripts and challenged every finding before this report was finalized.</p>' if panel else ''}{panel_section(panel)}</section>
<section id="roadmap"><h2>90-day roadmap</h2><p class="sub">Sequenced so each phase unblocks the next. Repository levels are recomputed from the criteria each phase fixes.</p>{roadmap(findings, proj)}</section>
<section id="proof">{proof_plan(data)}</section>
<section id="scorecard"><h2>Repository scorecard · {e(scan["name"])}</h2><p class="sub">{scan["stats"]["sourceFiles"]:,} source files · {scan["stats"]["testFiles"]:,} test files · {", ".join(f"{k} {v:,}" for k, v in scan["stats"]["languages"].items())}</p>{scorecard(scan)}</section>
<section id="sessions"><h2>Session explorer</h2>{session_table(sessions)}</section>
<section id="method"><h2>Methodology</h2><div class="grid2"><div class="card"><h3>Data sources</h3><ul>{method_sources}<li>Harnesses: {e(provider_list)}</li><li>Repository: {e(scan["path"])}</li></ul>
<h3>Privacy</h3><p>Everything runs locally. Transcripts are parsed on this machine; excerpts are redacted for keys, tokens and passwords, and capped at 220 characters.</p></div>
<div class="card"><h3>How scores work</h3><ul><li><b>On paper</b>: repository level from {len(scan["criteria"])} pass/fail criteria with file-level evidence; 80% gating per level.</li><li><b>In practice</b>: agent operations score from verified delivery (30%), unblocked sessions (20%), tool reliability (15%), request quality (15%), grounded edits (10%) and low rework (10%), mapped to the same five levels.</li><li><b>Verified</b> means a test, type check or build passed after the session's final edit. Lint alone does not count.</li></ul>
{f"<h3>Caveats</h3><ul>{caveats}</ul>" if caveats else ""}</div></div></section>
</main><footer>Generated by the agent-readiness skill · {e(data["generatedAt"])}</footer>
<script type="application/json" id="report-data">{html.escape(json.dumps({"findings": [f["id"] for f in findings]}))}</script></body></html>'''


CSS = """
:root{--bg:#f7f7fb;--card:#fff;--ink:#0f172a;--mute:#64748b;--line:#e5e7eb;--track:#e8e8f0;--grid:#e2e8f0;--accent:#4f46e5}
:root{color-scheme:light dark}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0b0d14;--card:#131722;--ink:#e5e7eb;--mute:#94a3b8;--line:#242a38;--track:#232838;--grid:#2a3142}}
:root[data-theme="dark"]{--bg:#0b0d14;--card:#131722;--ink:#e5e7eb;--mute:#94a3b8;--line:#242a38;--track:#232838;--grid:#2a3142}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:rgba(99,102,241,.09);padding:1px 5px;border-radius:4px;word-break:break-word}
.top{display:flex;justify-content:space-between;align-items:center;padding:14px 32px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.brand{display:flex;gap:12px;align-items:center}.brand span{display:block;color:var(--mute);font-size:13px}.logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#4f46e5,#0ea5e9);color:#fff!important;display:grid!important;place-items:center;font-weight:800;font-size:17px!important}
.conf{font-size:12px;color:var(--mute);border:1px solid var(--line);padding:4px 10px;border-radius:999px}
.toc{display:flex;gap:4px;padding:8px 32px;overflow:auto;background:var(--card);border-bottom:1px solid var(--line)}.toc a{color:var(--mute);text-decoration:none;font-size:13px;padding:4px 10px;border-radius:6px;white-space:nowrap}.toc a:hover{background:var(--bg);color:var(--ink)}
main{max-width:1180px;margin:0 auto;padding:28px 32px 60px}section{margin:0 0 44px}h2{font-size:24px;margin:0 0 6px;letter-spacing:-.01em}.sub{color:var(--mute);margin:0 0 18px}
h3{font-size:16px;margin:0 0 12px}h4{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:14px 0 6px}
.hero{display:grid;grid-template-columns:1.25fr 1fr;gap:28px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:30px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 8px}.hero h1{font-size:30px;line-height:1.2;margin:0 0 10px;letter-spacing:-.02em}.lede{color:var(--mute);margin:0 0 18px;font-size:16px}
.hero-right{display:flex;justify-content:space-around;gap:6px}.gauge{text-align:center}.gauge-num{font-size:30px;font-weight:800;fill:var(--ink)}.gauge-name{font-size:10px;fill:var(--mute);text-transform:uppercase;letter-spacing:.08em}.gauge-label{font-weight:700;margin-top:-6px}.gauge-sub{font-size:12px;color:var(--mute)}.gauge-na{height:132px;display:grid;place-items:center;color:var(--mute);font-size:28px}
.stats{display:flex;flex-wrap:wrap;gap:10px}.stats.col{flex-direction:column}.stat{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:10px 14px;min-width:120px;flex:1}.stat-v{font-size:22px;font-weight:800;letter-spacing:-.02em}.stat-l{font-size:12px;color:var(--mute)}
.stat.bad .stat-v{color:#dc2626}.stat.warn .stat-v{color:#d97706}.stat.good .stat-v{color:#059669}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.grid2>.card,.grid3>.card{margin:0}.grid2+.grid3,.grid3+.grid2,.grid2+.grid2,.grid3+.grid3{margin-top:16px}
.topfindings{margin-top:22px}.topgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.topf{display:block;text-decoration:none;color:inherit;background:var(--card);border:1px solid var(--line);border-left:4px solid #f59e0b;border-radius:12px;padding:16px}.topf.sev-critical{border-left-color:#dc2626}.topf h4{font-size:16px;text-transform:none;letter-spacing:0;color:var(--ink);margin:8px 0 6px}.topf p{margin:0;color:var(--mute);font-size:13px}
.badge{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:2px 8px;border-radius:999px}.badge.critical{background:#fee2e2;color:#b91c1c}.badge.high{background:#ffedd5;color:#c2410c}.badge.medium{background:#fef9c3;color:#a16207}.badge.low{background:#e0f2fe;color:#0369a1}
.n{font-size:11px;font-weight:600;color:var(--mute);background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:1px 8px;margin-left:6px;vertical-align:middle}
.confidence-line{font-size:12.5px;color:#92400e;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:6px 10px;margin:0 0 14px}
.alan-line{font-size:13px;margin:0}li.ctx{color:var(--mute)}b.bad,.bad{color:#dc2626}td.good{color:#059669;font-weight:700}td.bad{color:#dc2626;font-weight:700}
.risk{border-color:#fecaca}.tablewrap.flat{border:0;border-radius:0}
.change{border:2px solid #10b981}.change h3{font-size:22px}.compare td{font-size:14px;padding:9px 6px}
.cost{margin-top:16px;border-color:#fecaca}.small{font-size:12px}.muted{color:var(--mute)}
.ladder-wrap{display:grid;grid-template-columns:1.5fr 1fr;gap:16px;align-items:start}.ladder{width:100%;border-collapse:separate;border-spacing:4px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px}
.ladder th{font-size:12px;font-weight:700;text-align:center}.ladder th span{display:block;font-weight:500;color:var(--mute);font-size:10.5px}.ladder .rowh{text-align:left;font-size:13px;font-weight:600;white-space:nowrap;padding-right:6px}
.cell{text-align:center;border-radius:7px;font-size:12.5px;font-weight:700;padding:8px 0;cursor:help}.cell.full{background:#10b981;color:#fff}.cell.part{background:#fde68a;color:#78350f}.cell.none{background:#fecaca;color:#7f1d1d}.cell.empty{color:var(--mute)}
.rate{text-align:center;font-weight:800;font-size:13px}.radar{width:100%}.radar-label{font-size:10.5px;fill:var(--mute)}
.funnel{display:flex;flex-direction:column;gap:10px}.fstep{display:grid;grid-template-columns:190px 1fr 90px;gap:10px;align-items:center}.flabel{font-size:13px}.ftrack{position:relative;height:26px;background:var(--track);border-radius:7px;overflow:hidden}.ffill{height:100%;background:linear-gradient(90deg,#4f46e5,#0ea5e9);border-radius:7px}.fval{position:absolute;left:10px;top:3px;color:#fff;font-weight:800;font-size:13px;text-shadow:0 1px 2px rgba(0,0,0,.3)}.drop{font-size:12px;color:#dc2626;font-weight:600}
.donut{display:flex;gap:14px;align-items:center}.donut-num{font-size:20px;font-weight:800;fill:var(--ink)}.donut-sub{font-size:9px;fill:var(--mute);text-transform:uppercase;letter-spacing:.06em}.legend{list-style:none;margin:0;padding:0;font-size:12.5px;flex:1}.legend li{display:flex;align-items:center;gap:6px;padding:2px 0}.legend b{margin-left:auto}.dot{width:9px;height:9px;border-radius:3px;flex:none}
.hbars{display:flex;flex-direction:column;gap:6px}.hbar{display:grid;grid-template-columns:140px 1fr 44px;gap:8px;align-items:center;font-size:12.5px}.hbar-track{height:12px;background:var(--track);border-radius:4px;overflow:hidden}.hbar-fill{height:100%;border-radius:4px}.hbar-val{text-align:right;font-weight:700}
table.mini{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}table.mini td,table.mini th{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left}.num{text-align:right!important;font-variant-numeric:tabular-nums}
.finding{background:var(--card);border:1px solid var(--line);border-left:5px solid #f59e0b;border-radius:14px;padding:22px;margin-bottom:18px}.finding.sev-critical{border-left-color:#dc2626}.finding.sev-medium{border-left-color:#eab308}.finding header{display:flex;gap:10px;align-items:center;font-size:12px;color:var(--mute)}.finding h3{font-size:20px;margin:8px 0 4px}.headline{font-size:15.5px;margin:0 0 10px}
.fmetric{display:inline-block;font-weight:700;font-size:13px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:5px 10px}.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.fgrid ul,.fgrid ol{margin:0;padding-left:18px;font-size:13.5px}.fgrid li{margin:4px 0}
.ev-src{display:block;color:var(--mute);font-size:12px;font-style:italic}.alan{background:linear-gradient(135deg,rgba(79,70,229,.07),rgba(14,165,233,.07));border:1px solid rgba(79,70,229,.25);border-radius:12px;padding:4px 16px 14px;margin-top:10px}.alan h4{color:var(--accent)}.alan p{margin:0 0 10px;font-size:13.5px}.chips{display:flex;gap:6px}.chips span{font-size:11.5px;border:1px solid var(--line);background:var(--card);border-radius:999px;padding:2px 9px}
.experts{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.expert{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}.ex-head{display:flex;gap:10px;align-items:center}.ex-head span{display:block;color:var(--mute);font-size:12px}.avatar{width:36px;height:36px;border-radius:50%;color:#fff;display:grid;place-items:center;font-weight:800}.stance{margin-left:auto;font-size:11px;font-weight:700;border-radius:999px;padding:2px 9px;background:var(--bg);border:1px solid var(--line)}.stance.challenge{color:#c2410c}.stance.agree{color:#059669}.stance.escalate{color:#b91c1c}
.ex-take{font-size:15px;font-weight:600;margin:12px 0 6px}.expert ul{padding-left:18px;font-size:13.5px;margin:6px 0}.ex-challenge{font-size:13px;background:var(--bg);border-radius:8px;padding:8px 10px;margin:8px 0 0}
.verdict{background:var(--card);border:2px solid var(--accent);border-radius:14px;padding:20px;margin-top:16px}.confidence{font-size:13px;color:var(--mute)}
.lanes{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.lane{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column}.lane h4{margin-top:0;color:var(--accent)}.lane ul{list-style:none;padding:0;margin:0;flex:1}.lane li{padding:8px 0;border-bottom:1px solid var(--line)}.lane li a{color:var(--ink);font-weight:600;text-decoration:none}.lane li span{display:block;font-size:12px;color:var(--mute)}.lane-proj{margin-top:12px;font-size:13px;background:rgba(16,185,129,.1);border-radius:8px;padding:8px 10px}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:8px}summary{display:grid;grid-template-columns:260px 1fr 90px;gap:14px;align-items:center;padding:12px 16px;cursor:pointer;font-weight:600}.pbar{height:8px;background:var(--track);border-radius:4px;overflow:hidden}.pbar span{display:block;height:100%;background:linear-gradient(90deg,#4f46e5,#10b981)}.pscore{text-align:right;font-size:13px;color:var(--mute)}
table.crit{width:100%;border-collapse:collapse;font-size:13px}table.crit td{border-top:1px solid var(--line);padding:9px 12px;vertical-align:top}table.crit tr.pass td:first-child{color:#059669;font-weight:800}table.crit tr.fail td:first-child{color:#dc2626;font-weight:800}table.crit tr.skip{opacity:.6}.fixline{color:#c2410c;font-size:12px;margin-top:3px}
.tablewrap{overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:14px}table.sessions{width:100%;border-collapse:collapse;font-size:12.5px}table.sessions th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);padding:10px;border-bottom:1px solid var(--line);background:var(--bg)}table.sessions td{padding:8px 10px;border-bottom:1px solid var(--line)}td.prompt{max-width:420px}.pill{border:1px solid;border-radius:999px;padding:1px 8px;font-size:11.5px;white-space:nowrap;font-weight:600}
.proof{background:linear-gradient(135deg,#1e1b4b,#0c4a6e);color:#e0e7ff;border-radius:18px;padding:28px}.proof h3{font-size:24px;color:#fff;margin:0 0 6px}.proof .muted{color:#c7d2fe}.proof .eyebrow{color:#7dd3fc}
.weeks{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}.wk{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);border-radius:12px;padding:14px}.wk b{color:#7dd3fc}.wk p{margin:6px 0 0;font-size:13.5px}
.proof-table{background:rgba(255,255,255,.06);border-radius:12px;overflow:hidden}.proof-table td,.proof-table th{border-bottom:1px solid rgba(255,255,255,.12)!important;padding:9px 12px!important;color:#e0e7ff}.proof-table th{color:#a5b4fc;font-size:11px;text-transform:uppercase;letter-spacing:.05em}.target{color:#6ee7b7}
footer{text-align:center;color:var(--mute);font-size:12px;padding:20px}
@media (max-width:900px){.hero,.grid2,.grid3,.topgrid,.lanes,.weeks,.experts,.fgrid,.ladder-wrap{grid-template-columns:1fr}.hero-right{flex-wrap:wrap}main{padding:18px}}
@media print{.top,.toc{position:static}details{break-inside:avoid}details>table{display:table}.finding,.card{break-inside:avoid}body{background:#fff}}
"""
