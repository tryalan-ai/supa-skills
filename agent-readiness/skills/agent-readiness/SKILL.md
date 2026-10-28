---
name: agent-readiness
description: Assess how ready a codebase and its team are for autonomous coding agents. It scores the repository on 66 criteria and analyzes real agent session transcripts from Claude Code, Codex, Cursor, Droid, Gemini, Copilot and Alan to measure how agents study the codebase, verify their work, fail and follow instructions, and how developers direct them. An expert panel reviews the findings before the report is delivered. Use when asked for an agent readiness report, assessment, audit or scorecard, or to analyze how a team uses agents. Do not use for a single code review or for fixing one bug.
---

# Agent Readiness Assessment

The report answers two questions on one five-level scale:

- **On paper:** is the repository built so an agent can succeed in it?
- **In practice:** do the agents the team actually runs succeed in it?

The gap between the two, with the transcript evidence behind it, is the point of the report. Read `references/framework.md` for the full framework: levels, pillars, behavioral dimensions and formulas.

Everything runs locally with Python 3 and the standard library. No code or transcripts leave the machine.

## 1. Run the assessment

```bash
python3 scripts/run_readiness_check.py \
  --repo /path/to/repo \
  --org "Customer name" \
  --developers 40 --sessions-per-week 15 \
  --out /tmp/agent-readiness
```

By default it discovers session stores in `~/.claude/projects`, `~/.codex/sessions`, `~/.factory/sessions`, `~/.gemini/tmp` and `~/.copilot/session-state`, and keeps only the sessions that touched the repository.

| Option | Use |
|---|---|
| `--sessions <dir or file>` | Add explicit stores (repeatable), including exported team transcripts or a directory of Alan session exports (`{sessionId, title, messages[]}` JSON). |
| `--repo-match <path>` | Count sessions run in worktrees or other clones of the repository. |
| `--prompt-extract '<regex>'` | Pull the human request out of orchestrator-wrapped prompts, such as ticket bots or task runners. The first capture group is the request. |
| `--repo <path>` (repeatable) | Scan more repositories. The first is the primary. |
| `--skip-sessions` | Run the repository scan only, when no transcripts are available. |
| `--caveat "<text>"` | Print a caveat in the methodology section. |

The run writes two files:

- `report.html`: a self-contained report with no external assets, safe to email or print.
- `assessment.json`: every number in the report.

Before going further, check the run's JSON summary on stdout. If it shows `sessions: 0`, the stores did not match the repository; fix that with `--sessions` or `--repo-match` before reading anything else into the report.

## 2. Convene the expert panel (required before delivery)

Follow `references/panel.md`. In short:

1. Spawn four read-only reviewers in parallel: the Buyer, the Auditor, the Security Lead and the Platform Lead. Give each the paths to `assessment.json`, `report.html`, the transcripts, the repository and `scripts/`.
2. Fix every confirmed analyzer bug in code and re-run. Never hand-edit a number in the report.
3. Write `panel.json`, which carries the experts' positions, the disagreements and their resolutions, the verdict, severity overrides and caveats.
4. Re-render with the panel:

   ```bash
   python3 scripts/run_readiness_check.py <same flags> --panel /tmp/agent-readiness/panel.json
   ```

5. Repeat until no reviewer reports a "wrong" verdict or an open analyzer bug.

If sub-agents are not available, do the four reviews yourself in sequence, one role at a time, and say so in the panel verdict.

## 3. Measure a fix (before and after)

To prove a remediation worked, run the same requests before and after it, then compare:

```bash
python3 scripts/run_readiness_check.py --repo <after checkout> --sessions <after transcripts> \
  --baseline /tmp/agent-readiness-before/assessment.json \
  --change 'Added `CLAUDE.md` containing `@AGENTS.md`' --label "after day-one fixes" --out /tmp/agent-readiness-after
```

The report adds a "Measured change" section. Count-based rows are tested with a Fisher exact test and marked as noise when p > 0.1. Keep the runs comparable:

- same requests and models;
- fresh, isolated checkouts with their own dependencies and databases;
- runs that do not overlap on one machine;
- agents launched from inside each checkout. The report warns when sessions were launched elsewhere, because the launch directory's instruction files are what the agent loads.

## 4. Deliver

- **In Alan:** attach `report.html` as a document artifact (`create_document_artifact`). Post a short summary to the conversation: the on-paper and in-practice levels, the top three findings, and the 30-day targets.
- **Otherwise:** share the path to `report.html` and paste that same summary.
- **For an executive audience,** lead with:
  - the gap between on paper and in practice;
  - the three findings to fix first;
  - the cost estimate, with its stated assumptions;
  - the 30-day plan measured by the same report.

## Example

`examples/` holds a complete demonstration on the open-source Documenso repository:

- `documenso-baseline.html`: the diagnosis, from 12 sessions on `main`.
- `documenso-after-fixes.html`: the re-assessment after four day-one fixes, with the measured change.
- `panel-*.json`: the expert panel files used to render both reports.

## Interpreting the results

- **In practice below on paper:** the repository looks ready, but agents are blocked, unverified or not reading the instructions. Fix agent operations first: environments, instruction reach and verification gates.
- **On paper below in practice:** strong developers are compensating for gaps in the repository. Fix the repository criteria so agents stop depending on expert prompting.
- **An instruction-reach finding:** usually the cheapest fix with the biggest effect. For example, Claude Code loads `CLAUDE.md`, not `AGENTS.md`, and a one-line `@AGENTS.md` import fixes it.
- **A small sample:** fewer than 30 tool-level sessions is directional. Say so in the summary.

## Tests

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```
