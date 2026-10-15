#!/usr/bin/env python3
"""Render a visual, actionable HTML report from a reflections summary."""

from __future__ import annotations

from html import escape
from typing import Any

KIND_META = {
    "prompt": {"label": "Prompt", "where": "Skills or AGENTS.md"},
    "tooling": {"label": "Tooling", "where": "Harness, scripts, or CLI"},
    "setup": {"label": "Setup", "where": "Auth or credentials"},
    "environment": {"label": "Environment", "where": "Missing packages, env vars, or host"},
}

STYLE_ORDER = ("delegated", "mixed", "pair", "babysat", "stuck-asking", "unattended-fail", "empty")
STYLE_COLOR = {
    "delegated": "#209752",
    "mixed": "#0B6FEA",
    "pair": "#5750E2",
    "babysat": "#9B6508",
    "stuck-asking": "#8250DF",
    "unattended-fail": "#D31212",
    "empty": "#8C8C8C",
}
CLASS_COLOR = {
    "task-finisher": "#209752",
    "assisted-implementor": "#0B6FEA",
    "code-generator": "#9B6508",
    "research-only": "#8C8C8C",
    "asked-human": "#8250DF",
    "blocked": "#D31212",
    "debug-loop": "#D31212",
    "validation-only": "#0B6FEA",
    "setup-failed": "#D31212",
    "empty": "#8C8C8C",
}


def _esc(value: Any) -> str:
    text = str(value if value is not None else "").replace("\u2014", "-").replace("\u2013", "-")
    return escape(text, quote=True)


def _pct(count: int, total: int) -> int:
    if total <= 0:
        return 0
    return round(100 * count / total)


def _dist(value: Any, key: str = "p50") -> str:
    if isinstance(value, dict):
        number = value.get(key, 0)
    else:
        number = value or 0
    if isinstance(number, float) and number >= 10:
        return str(int(round(number)))
    if isinstance(number, float):
        return f"{number:.1f}"
    return str(number)


def _bar(counts: dict[str, int], colors: dict[str, str], total: int) -> str:
    if total <= 0:
        return '<div class="bar empty"></div>'
    parts: list[str] = []
    for name, count in counts.items():
        if count <= 0:
            continue
        width = max(1, _pct(count, total))
        color = colors.get(name, "#8C8C8C")
        parts.append(
            f'<span class="seg" style="width:{width}%;background:{color}" title="{_esc(name)} {count}"></span>'
        )
    return f'<div class="bar">{"".join(parts)}</div>'


def _legend(counts: dict[str, int], colors: dict[str, str], total: int) -> str:
    items: list[str] = []
    for name, count in sorted(counts.items(), key=lambda item: item[1], reverse=True):
        if count <= 0:
            continue
        items.append(
            f'<li><i style="background:{colors.get(name, "#8C8C8C")}"></i>'
            f"{_esc(name)} <b>{count}</b> ({_pct(count, total)}%)</li>"
        )
    return "<ul class='legend'>" + "".join(items) + "</ul>"


def _ordered(counts: dict[str, int], order: tuple[str, ...]) -> dict[str, int]:
    ordered: dict[str, int] = {}
    for name in order:
        if name in counts:
            ordered[name] = int(counts[name])
    for name, count in counts.items():
        if name not in ordered:
            ordered[name] = int(count)
    return ordered


def html_report(summary: dict[str, Any], digests: list[dict] | None = None) -> str:
    total = int(summary.get("sessionCount") or 0)
    skipped = int(summary.get("skippedCount") or 0)
    classes = {str(k): int(v) for k, v in (summary.get("classificationCounts") or {}).items()}
    autonomy = summary.get("autonomy") or {}
    styles = {str(k): int(v) for k, v in (autonomy.get("styleCounts") or {}).items()}
    styles = _ordered(styles, STYLE_ORDER)
    suggestions = list(summary.get("suggestions") or [])
    providers = summary.get("byProvider") or {}
    patterns = list(summary.get("topFailurePatterns") or [])
    failed_tools = list(summary.get("topFailedTools") or [])
    rates = summary.get("rates") or {}
    samples = digests if digests is not None else list(summary.get("recommendedSamples") or [])

    delegated = styles.get("delegated", 0)
    research = classes.get("research-only", 0)
    finishers = classes.get("task-finisher", 0)
    follow = int(autonomy.get("stopInterrupt") or 0)
    finish_stops = int(autonomy.get("stopFinish") or 0)

    kind_choices = (("all", "All"), ("prompt", "Prompt"), ("tooling", "Tooling"), ("setup", "Setup"), ("environment", "Environment"))
    action_inputs = "".join(
        f'<input type="radio" name="akind" id="k-{kind}" {"checked" if kind == "all" else ""}>'
        for kind, _label in kind_choices
    )
    action_filters = "".join(f'<label for="k-{kind}">{label}</label>' for kind, label in kind_choices)
    action_cards: list[str] = []
    for index, item in enumerate(suggestions, start=1):
        kind = str(item.get("kind") or "environment")
        if kind == "moment":
            kind = "environment"
        meta = KIND_META.get(kind, KIND_META["environment"])
        action_cards.append(
            f"""<article class="action" data-kind="{_esc(kind)}">
  <header>
    <span class="kind kind-{_esc(kind)}">{_esc(meta["label"])}</span>
    <span class="idx">{index:02d}</span>
  </header>
  <h3>{_esc(item.get("title"))}</h3>
  <p class="why">{_esc(item.get("why"))}</p>
  <p class="change"><strong>Change</strong> {_esc(item.get("change"))}</p>
  <p class="where">Where: {_esc(meta["where"])}</p>
</article>"""
        )

    provider_rows: list[str] = []
    for name, bucket in providers.items():
        count = int(bucket.get("sessionCount") or 0)
        p_styles = _ordered({str(k): int(v) for k, v in (bucket.get("autonomyStyles") or {}).items()}, STYLE_ORDER)
        tph = _dist(bucket.get("toolsPerHumanTurn"))
        streak = _dist(bucket.get("maxUnattendedStreak"))
        dur = _dist(bucket.get("turnDurationP50s"))
        if dur in {"0", "0.0"}:
            dur = "n/a"
        provider_rows.append(
            f"""<tr>
  <th>{_esc(name)}</th>
  <td>{count}</td>
  <td>{_bar(p_styles, STYLE_COLOR, count)}</td>
  <td class="num">{tph}</td>
  <td class="num">{streak}</td>
  <td class="num">{dur}</td>
  <td class="num">{int(bucket.get("stopInterrupt") or 0)}</td>
</tr>"""
        )

    pattern_rows: list[str] = []
    for item in patterns[:8]:
        pattern_rows.append(
            f"<tr><th>{_esc(item.get('category'))}</th>"
            f"<td class='num'>{int(item.get('sessions') or 0)}</td>"
            f"<td class='num'>{int(item.get('mentions') or 0)}</td></tr>"
        )

    tool_rows: list[str] = []
    for entry in failed_tools[:8]:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            name, count = entry
        else:
            name, count = entry, 0
        tool_rows.append(f"<tr><th>{_esc(name)}</th><td class='num'>{int(count)}</td></tr>")

    sample_cards: list[str] = []
    for digest in samples[:8]:
        metrics = digest.get("metrics") or digest
        users = digest.get("userRequests") or []
        preview = ""
        if users:
            preview = str(users[0])[:160]
        elif digest.get("reasons"):
            reason = str((digest.get("reasons") or [""])[0])
            preview = reason if " " in reason else ""
        sample_cards.append(
            f"""<article class="sample">
  <header>
    <span class="agent">{_esc(digest.get("provider") or "?")}</span>
    <span class="pill">{_esc(digest.get("autonomyStyle") or "")}</span>
    <span class="pill">{_esc(digest.get("classification") or "")}</span>
  </header>
  <p class="id">{_esc(digest.get("sessionId"))}</p>
  <p class="preview">{_esc(preview) or "No user preview"}</p>
  <p class="meta">{int(metrics.get("toolCalls") or digest.get("toolCalls") or 0)} tools · {int(metrics.get("editCalls") or digest.get("editCalls") or 0)} edits · {int(metrics.get("failedToolCalls") or digest.get("failedToolCalls") or 0)} failed</p>
</article>"""
        )

    class_bar = _bar(_ordered(classes, tuple(CLASS_COLOR)), CLASS_COLOR, total)
    style_bar = _bar(styles, STYLE_COLOR, total)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local session reflections</title>
<style>
:root {{
  --surface:#FAFAFA; --elevated:#F2F2F2; --text:#0A0A0A; --muted:#666666;
  --border:#D9D9D9; --primary:#2B22D3; --ok:#209752; --warn:#9B6508; --bad:#D31212;
  --radius:10px;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--surface); color:var(--text);
  font:14px/20px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}}
main {{ max-width:1080px; margin:0 auto; padding:28px 20px 64px; }}
h1 {{ font-size:24px; line-height:32px; font-weight:600; margin:0 0 6px; letter-spacing:0; }}
h2 {{ font-size:16px; line-height:24px; font-weight:600; margin:36px 0 12px; }}
h3 {{ font-size:16px; line-height:24px; font-weight:600; margin:0 0 8px; }}
p {{ margin:0; }}
.lede {{ color:var(--muted); max-width:62ch; margin-bottom:24px; }}
.kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }}
.kpi {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; }}
.label {{ color:var(--muted); font-size:12px; line-height:16px; font-weight:500; }}
.kpi .label {{ color:var(--muted); font-size:12px; line-height:16px; font-weight:500; }}
.kpi .value {{ font-size:28px; line-height:36px; font-weight:600; font-variant-numeric:tabular-nums; }}
.kpi .hint {{ color:var(--muted); font-size:12px; margin-top:4px; }}
.change-board > input {{ position:absolute; opacity:0; width:1px; height:1px; }}
.filters {{ display:flex; flex-wrap:wrap; gap:8px; margin:8px 0 16px; }}
.filters label {{
  border:1px solid var(--border); border-radius:999px; padding:4px 12px; cursor:pointer;
  font-size:12px; font-weight:500; background:#fff;
}}
.change-board > input:focus-visible + .filters label {{ outline:2px solid var(--primary); outline-offset:2px; }}
.change-board:has(#k-all:checked) label[for="k-all"],
.change-board:has(#k-prompt:checked) label[for="k-prompt"],
.change-board:has(#k-tooling:checked) label[for="k-tooling"],
.change-board:has(#k-setup:checked) label[for="k-setup"],
.change-board:has(#k-environment:checked) label[for="k-environment"] {{
  background:var(--primary); color:#FAFAFA; border-color:var(--primary);
}}
.actions {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.action {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:16px; }}
.action header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
.kind {{ font-size:11px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; padding:2px 8px; border-radius:999px; }}
.kind-prompt {{ background:#EDECFB; color:var(--primary); }}
.kind-tooling {{ background:#E8F4FF; color:#0B6FEA; }}
.kind-setup {{ background:#F6EFD8; color:var(--warn); }}
.kind-environment {{ background:#E7F6EE; color:var(--ok); }}
.idx {{ color:var(--muted); font-variant-numeric:tabular-nums; font-size:12px; }}
.why, .where {{ color:var(--muted); margin-top:8px; }}
.change {{ margin-top:10px; }}
.where {{ font-size:12px; }}
.change-board:has(#k-prompt:checked) .action:not([data-kind="prompt"]),
.change-board:has(#k-tooling:checked) .action:not([data-kind="tooling"]),
.change-board:has(#k-setup:checked) .action:not([data-kind="setup"]),
.change-board:has(#k-environment:checked) .action:not([data-kind="environment"]) {{ display:none; }}
.panel {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:16px; }}
.bar {{ display:flex; height:12px; border-radius:999px; overflow:hidden; background:var(--elevated); }}
.bar .seg {{ height:100%; }}
.legend {{ list-style:none; padding:12px 0 0; margin:0; display:flex; flex-wrap:wrap; gap:8px 16px; color:var(--muted); font-size:12px; }}
.legend i {{ display:inline-block; width:8px; height:8px; border-radius:99px; margin-right:6px; }}
table {{ width:100%; border-collapse:collapse; }}
th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--border); vertical-align:middle; }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.split {{ display:grid; grid-template-columns:1.4fr .8fr; gap:12px; }}
.samples {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.sample {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:14px; }}
.sample header {{ display:flex; gap:8px; align-items:center; margin-bottom:8px; }}
.agent {{ font-weight:600; }}
.pill {{ font-size:11px; color:var(--muted); border:1px solid var(--border); border-radius:999px; padding:1px 8px; }}
.id {{ font-family:"JetBrains Mono",ui-monospace,Menlo,monospace; font-size:11px; color:var(--muted); }}
.preview {{ margin:8px 0; }}
.meta {{ color:var(--muted); font-size:12px; }}
@media (max-width:800px) {{
  .kpis, .actions, .split, .samples {{ grid-template-columns:1fr; }}
}}
@media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important; }} }}
</style>
</head>
<body>
<main>
  <h1>Local session reflections</h1>
  <p class="lede">{total} sessions. {delegated} delegated ({_pct(delegated, total)}%). {research} never became an implementation loop. Follow-ups stay one paid bucket: {follow} in-flight, {finish_stops} finished.</p>
  <section class="kpis" aria-label="Headline numbers">
    <article class="kpi"><div class="label">Sessions</div><div class="value">{total}</div><div class="hint">{skipped} parse errors skipped</div></article>
    <article class="kpi"><div class="label">Delegated</div><div class="value">{_pct(delegated, total)}%</div><div class="hint">{delegated} of {total}</div></article>
    <article class="kpi"><div class="label">Task finishers</div><div class="value">{_pct(finishers, total)}%</div><div class="hint">{finishers} edited, validated, reported</div></article>
    <article class="kpi"><div class="label">Tools / human</div><div class="value">{_dist(autonomy.get("toolsPerHumanTurn"))}</div><div class="hint">p50 unattended streak {_dist(autonomy.get("maxUnattendedStreak"))}</div></article>
  </section>

  <h2>What to change</h2>
  <p class="lede">Each card is one lever. Prompt changes skills. Tooling changes harness. Setup changes auth. Environment changes missing packages, env vars, and similar host gaps, not only sandbox.</p>
  <section class="change-board">
    {action_inputs}
    <div class="filters">{action_filters}</div>
    <div class="actions">
      {"".join(action_cards) or "<p>No suggestions for this scan.</p>"}
    </div>
  </section>

  <h2>Autonomy by agent</h2>
  <div class="panel">
    {style_bar}
    {_legend(styles, STYLE_COLOR, total)}
    <table>
      <thead><tr><th>Agent</th><th>n</th><th>Style</th><th class="num">Tools/human</th><th class="num">Streak</th><th class="num">Turn s</th><th class="num">Follow-ups</th></tr></thead>
      <tbody>{"".join(provider_rows)}</tbody>
    </table>
  </div>

  <h2>How sessions ended</h2>
  <div class="split">
    <div class="panel">
      <p class="label">Classification</p>
      {class_bar}
      {_legend(classes, CLASS_COLOR, total)}
      <p class="meta" style="margin-top:12px">Validation after edit: {round(float(rates.get("editSessionsWithValidationAfterEdit") or 0) * 100)}% of edit sessions. Manual handoff markers: {round(float(rates.get("sessionsWithManualHandoffMarkers") or 0) * 100)}%.</p>
    </div>
    <div class="panel">
      <p class="label">Stops</p>
      <table>
        <tr><th>Clarify</th><td class="num">{int(autonomy.get("stopClarify") or 0)}</td></tr>
        <tr><th>Follow-up</th><td class="num">{follow}</td></tr>
        <tr><th>Finish</th><td class="num">{finish_stops}</td></tr>
      </table>
    </div>
  </div>

  <h2>Recurring friction</h2>
  <div class="split">
    <div class="panel">
      <table>
        <thead><tr><th>Pattern</th><th class="num">Sessions</th><th class="num">Mentions</th></tr></thead>
        <tbody>{"".join(pattern_rows) or "<tr><td colspan='3'>None</td></tr>"}</tbody>
      </table>
    </div>
    <div class="panel">
      <table>
        <thead><tr><th>Failed tool</th><th class="num">Calls</th></tr></thead>
        <tbody>{"".join(tool_rows) or "<tr><td colspan='2'>None</td></tr>"}</tbody>
      </table>
    </div>
  </div>

  <h2>Sample sessions</h2>
  <div class="samples">{"".join(sample_cards) or "<p>No samples.</p>"}</div>
</main>
</body>
</html>
"""
