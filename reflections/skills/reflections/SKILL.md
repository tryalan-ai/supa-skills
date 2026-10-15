---
name: reflections
description: "This skill should be used when the user asks to \"run reflections\", \"analyze local sessions\", \"why do agents stop\", or \"session loop quality\". Do not use for repo-wide agent-readiness scoring, planning a product, or implementing an unrelated feature."
---

# Reflections

Classify local CLI agent sessions and turn the patterns into suggestions that raise autonomous loop rate. This skill only does session analysis. It does not score repository readiness.

Assumptions derived from the requesting task: project skill, local JSONL first (Alan DB later), all local agents, suggestions not auto-applied.

## When Not to Use

- The user wants the 11-dimension repo audit → `/agent-readiness` in supa-skills.
- The user wants to plan or implement a product feature.
- The user asks how session parsing works, without running it.

## Preconditions

- Python 3 on PATH.
- At least one local session root exists: `~/.codex/sessions`, `~/.claude/projects`, `~/.cursor/projects/**/agent-transcripts` (results in `terminals/{shell_id}.txt`), `~/.grok/sessions/**/updates.jsonl`, `~/.kimi-code/sessions/**/wire.jsonl` plus sibling `logs/kimi-code.log`.

## Procedure

For the standalone, evidence-first core (aggregate and every individual session), freeze the input cohort and run:

```bash
python3 scripts/run_core.py \
  --max-per-provider 30 \
  --manifest /tmp/alan-reflections/cohort.json
```

It writes `/tmp/alan-reflections/core.md` and `core.json`. The manifest records transcript paths, sizes, and modification times, plus selected Cursor terminal sidecars. Later runs fail if a selected input changes or the sidecar set changes. Delete the manifest deliberately to select a new cohort. The core excludes child-agent transcripts, files modified within ten minutes, and files over 25 MB by default; record these coverage limits with the report.

`passed` requires explicit successful outcomes for substantive checks after the final **identified code edit**. A successful `git diff --check` alone is `hygiene_only`; an unrelated pass does not clear an unknown or failed check. `observed` means check output exists without an explicit exit status; `unresolved` means JavaScript orchestration could not be paired safely with its shell result. Documentation, reports, and temporary probes remain edit activity but do not trigger code-verification advice. Human turns exclude injected host instructions and Codex replay bursts. The explicit-outcome figure measures log visibility, **not** task success or efficiency. No model calls are made.

Formatter `--write` calls are tracked separately from confirmed code edits. A `No fixes applied` result preserves an earlier check; a reported file change requires a later check to establish order; missing formatter output leaves the state unresolved. Keep these outcomes separate when summarizing check coverage.

Review the cited last code target and last check before repeating a suggestion. Keep workflow suggestions separate from parser and logging gaps. The local JSON contains request snippets and paths; handle it as session data.

The broad exploratory scanner below is useful for classifications and visual summaries. Its labels and counts need the same provider-level audit before they become product metrics.

1. From the skill directory, run the combined scanner:

```bash
python3 scripts/run_reflections.py \
  --output /tmp/alan-reflections/report.md \
  --output-html /tmp/alan-reflections/report.html \
  --report-json /tmp/alan-reflections/summary.json
```

2. If the user named a repo, add `--repo /path/to/repo`. If they named agents, add `--providers grok,claude,codex,cursor`. For a cheap first pass, add `--max-per-provider 30`. After a parser change, add `--no-cache`. Do not skip large files unless the user asks; stream them.
3. If no files are found, stop and report which roots were missing. Do not invent sessions.
4. If the report shows `0` tool calls for a provider that should have tools, treat it as a parser miss: inspect one compact sample path, patch `scripts/session_events.py`, add a fixture to `scripts/test_session_events.py`, re-run tests, then re-run the scanner.
5. Read the markdown report. Lead the user reply with:
   - session count by agent
   - classification mix (task-finisher vs research-only vs asked-human vs blocked vs code-generator vs debug-loop)
   - autonomy overlay: tools per human turn, max unattended streak, turn duration, stop reasons (clarify / interrupt / finish), style mix
   - top failure patterns
   - the suggestions list, grouped as prompt / tooling / setup / environment
6. Persist a visual HTML artifact on the Alan task when artifact tools are available:
   - title: `Local session reflections`
   - `content`: short markdown of session counts, autonomy mix, and the suggestion list
   - `html`: the file at `--output-html` (default `/tmp/alan-reflections/report.html`)
   - `presentation`: `full`
   If MCP is down, leave the HTML path and say that. Do not invent charts that are not in the scan.

Do not score AGENTS.md, tests, or browser automation. Do not edit skills, prompts, or env files unless the user asks to apply a specific suggestion.

## Validation

```bash
python3 scripts/test_session_events.py
python3 scripts/test_reflections_core.py
python3 -m py_compile scripts/*.py
```

For the standalone core, success means focused tests pass, the manifest reports no changed inputs or file errors, every workflow suggestion cites a specific event sequence, and unknown results remain unknown. For the exploratory scanner, retain classification shares as context rather than treating them as product metrics.

## Failure Handling

- **No session files:** print the missing roots and stop.
- **One provider has sessions but 0 tools:** parser gap. Patch the adapter for that format, add a test, re-run. Do not classify those sessions as research-only.
- **A single file is huge and stalls the run:** keep streaming; compact skips files over 40MB. If the process is killed, rerun with `--providers` excluding the stuck agent.
- **Cache looks stale after a parser change:** rerun with `--no-cache`.
- **Codex looks babysat despite long autonomous runs:** resume JSONL replays prior user turns a few milliseconds apart and duplicates `event_msg.user_message`. Collapse those bursts; do not treat them as new human turns.
- **Do not match system-prompt prose.** Autonomy is user-role vs agent-role after adapters. Acknowledgements and course corrections are the same follow-up bucket. Skip only structural noise: tool results labeled as user, duplicate event types, and resume bursts. Unwrap `<user_query>` when present. Size caps apply only when scanning failure text, not when counting turns.

## Artifacts

- Markdown report: `/tmp/alan-reflections/report.md` (override with `--output`)
- HTML report: `/tmp/alan-reflections/report.html` (override with `--output-html`)
- JSON summary: `--report-json`
- Parse cache: `/tmp/alan-reflections/.reflections-cache.json`

When the standalone core runs on an Alan task, attach a concise task artifact with cohort limits, aggregate evidence states, representative individual findings, and links or paths to owner-only local reports. Do not attach raw request snippets or transcript JSON. For the exploratory HTML scanner, create a task artifact titled `Local session reflections` with markdown `content` plus the HTML body (`presentation: full`). The HTML leads with action cards tagged prompt / tooling / setup / environment. Environment means missing packages, missing variables, and similar host gaps (sandbox is one subset). It is not kickoff skills and not sandbox-only.

## Self-update

If a live run shows a missed CLI format, wrong classification, or a bad suggestion rule, patch this skill's scripts and tests in the same turn before reporting.
