#!/usr/bin/env python3
"""Agent readiness assessment: repository scan + session behavior + findings + HTML report.

Usage:
  python3 scripts/run_readiness_check.py --repo . --org "Acme"
  python3 scripts/run_readiness_check.py --repo . --sessions ~/.claude/projects --sessions ~/.codex/sessions
  python3 scripts/run_readiness_check.py --repo . --panel /tmp/agent-readiness/panel.json   # re-render with expert panel

Outputs (in --out, default /tmp/agent-readiness):
  report.html      the self-contained assessment
  assessment.json  every number in the report, for the expert panel and for re-rendering
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from behavior import aggregate, analyze_paths, session_rows, set_prompt_extractors  # noqa: E402
from insights import adherence, auto_caveats, build_findings, cost_model, findings_dicts, instruction_reach, ops_interval, ops_score, practice_level  # noqa: E402
from repo_scan import level_for, scan_repo  # noqa: E402
from repo_scan import Criterion  # noqa: E402
from report import render  # noqa: E402

DEFAULT_STORES = [
    "~/.claude/projects",
    "~/.codex/sessions",
    "~/.codex/archived_sessions",
    "~/.factory/sessions",
    "~/.gemini/tmp",
    "~/.copilot/session-state",
]


def discover(stores: list[str]) -> list[Path]:
    files: list[Path] = []
    for store in stores:
        root = Path(store).expanduser()
        if root.is_file():
            files.append(root)
            continue
        if not root.is_dir():
            continue
        files.extend(p for p in root.rglob("*.jsonl") if p.stat().st_size > 512)
        files.extend(p for p in root.rglob("*.json") if p.name not in ("index.json",) and not p.name.startswith(".") and p.stat().st_size > 256)
    return files


def project(scan: dict, findings: list[dict]) -> dict:
    """Recompute the repository level as each roadmap phase lands its criteria."""
    out: dict[str, dict] = {}
    fixed: set[str] = set()
    for horizon in ("30", "60", "90"):
        for f in findings:
            if f["horizon"] == horizon:
                fixed.update(c["id"] for c in f.get("root_causes", []) if c.get("passed") is False)
        criteria = [Criterion(**{k: v for k, v in c.items()}) for c in copy.deepcopy(scan["criteria"])]
        for c in criteria:
            if c.id in fixed and not c.skipped:
                c.passed = True
        level, _ = level_for(criteria)
        weight = sum(c.level for c in criteria if not c.skipped)
        score = round(100 * sum(c.level for c in criteria if c.passed and not c.skipped) / weight)
        out[horizon] = {"level": level, "score": score, "fixed": sorted(fixed)}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", action="append", help="Repository root (repeatable; the first is the primary)")
    parser.add_argument("--sessions", action="append", help="Session store dir or file (repeatable). Default: auto-discover Claude Code, Codex, Droid, Gemini, Copilot stores")
    parser.add_argument("--all-sessions", action="store_true", help="Do not filter sessions to the repository path")
    parser.add_argument("--repo-match", action="append", default=[], help="Extra path substring that marks a session as belonging to this repo (e.g. worktree roots)")
    parser.add_argument("--max-sessions", type=int, help="Analyze at most N most recent sessions")
    parser.add_argument("--prompt-extract", action="append", default=[], help="Regex with one group that pulls the human request out of an orchestrator-wrapped prompt")
    parser.add_argument("--org", help="Organization name shown on the report")
    parser.add_argument("--developers", type=int, default=25, help="Developers using agents, for the cost estimate")
    parser.add_argument("--sessions-per-week", type=float, default=15, help="Agent sessions per developer per week, for the cost estimate")
    parser.add_argument("--panel", help="Expert panel JSON to merge (see SKILL.md)")
    parser.add_argument("--baseline", help="An earlier assessment.json to compare against (before/after)")
    parser.add_argument("--label", help="Label for this run in a before/after comparison")
    parser.add_argument("--change", action="append", default=[], help="A repository change made since the baseline (repeatable)")
    parser.add_argument("--caveat", action="append", default=[], help="Caveat to print in the methodology section")
    parser.add_argument("--out", default="/tmp/agent-readiness", help="Output directory")
    parser.add_argument("--skip-sessions", action="store_true", help="Repository scan only")
    args = parser.parse_args()

    repos = [Path(r).expanduser().resolve() for r in (args.repo or ["."])]
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    scans = [scan_repo(r) for r in repos]
    log(f"Scanned {len(scans)} repo(s): " + ", ".join(f"{s['name']} L{s['level']} ({s['score']})" for s in scans))

    sessions = []
    skipped: dict = {}
    if not args.skip_sessions:
        set_prompt_extractors(args.prompt_extract)
        files = discover(args.sessions or DEFAULT_STORES)
        match = [] if args.all_sessions else [str(r) for r in repos] + args.repo_match
        sessions, skipped = analyze_paths(files, match, args.max_sessions, repo_roots=[str(r) for r in repos])
        log(f"Analyzed {len(sessions)} sessions from {len(files)} files (skipped: {skipped})")

    agg = aggregate(sessions) if sessions else aggregate([])
    adhere = adherence(sessions, repos[0]) if sessions else {}
    reach = instruction_reach(repos[0], sessions) if sessions else {}
    findings = findings_dicts(build_findings(scans[0], agg, sessions, adhere, reach))
    ops = ops_score(agg)
    data = {
        "org": args.org or scans[0]["name"],
        "generatedAt": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "repos": scans,
        "behavior": agg,
        "sessions": session_rows(sessions),
        "skippedSessions": skipped,
        "adherence": adhere,
        "instructionReach": reach,
        "findings": findings,
        "ops": ops,
        "practiceLevel": practice_level(ops.get("score")),
        "opsInterval": ops_interval(sessions),
        "cost": cost_model(agg, sessions, args.developers, args.sessions_per_week),
        "caveats": auto_caveats(agg, [str(r) for r in repos]) + args.caveat,
        "worktreeRoots": args.repo_match,
    }
    data["projection"] = project(scans[0], findings)
    data["label"] = args.label or data["generatedAt"]
    if args.baseline:
        data["baseline"] = json.loads(Path(args.baseline).expanduser().read_text())
        data["baseline"].pop("baseline", None)
        data["baselineChanges"] = args.change

    if args.panel:
        panel = json.loads(Path(args.panel).expanduser().read_text())
        data["panel"] = panel
        for key in ("verdict", "lede", "compareBanner", "compareWarning"):
            if panel.get("report", {}).get(key):
                data[key] = panel["report"][key]
        drop = set(panel.get("report", {}).get("dropFindings", []))
        overrides = panel.get("report", {}).get("severity", {})
        data["findings"] = [dict(f, severity=overrides.get(f["id"], f["severity"])) for f in data["findings"] if f["id"] not in drop]
        data["findings"].sort(key=lambda f: ({"critical": 0, "high": 1, "medium": 2, "low": 3}[f["severity"]], f["source"] != "behavior"))
        data["caveats"] = data["caveats"] + panel.get("report", {}).get("caveats", [])

    (out / "assessment.json").write_text(json.dumps(data, indent=2, default=str))
    (out / "report.html").write_text(render(data))
    log(f"Wrote {out / 'report.html'} and {out / 'assessment.json'} in {time.time() - t0:.1f}s")
    print(json.dumps({
        "report": str(out / "report.html"),
        "assessment": str(out / "assessment.json"),
        "paperLevel": scans[0]["level"],
        "practiceLevel": data["practiceLevel"],
        "repoScore": scans[0]["score"],
        "opsScore": ops.get("score"),
        "sessions": agg.get("sessionCount"),
        "findings": [f"{f['severity']}: {f['title']}" for f in data["findings"]],
    }, indent=2))
    return 0


def log(message: str) -> None:
    print(f"[agent-readiness] {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
