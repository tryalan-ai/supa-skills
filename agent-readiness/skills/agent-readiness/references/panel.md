# Expert panel protocol

The panel stops a report from reaching a customer with a wrong number. Four reviewers work independently from the same `assessment.json` and raw transcripts. A moderator then reconciles them. Run the panel after the first render and before delivery.

## 1. Spawn four reviewers in parallel

Give each reviewer:

- the path to `assessment.json`;
- the path to `report.html`;
- the transcript directory, or files;
- the repository root;
- the analyzer source in `scripts/`.

Each reviewer is read-only and writes one JSON file to `<out>/panel/<role>.json`.

| Reviewer | Mandate | Must do |
|---|---|---|
| **The Buyer** (VP Engineering) | Would this make an executive trust the vendor to fix the problem? What would make them dismiss it? | Verify three or more headline numbers against the transcripts. Name what an executive needs that is missing. |
| **The Auditor** (methodology) | Find every number that is wrong, overstated or produced by an analyzer bug. | Recompute tool calls, edits, verification after the final edit, failures, blind edits and outcome for at least six sessions from the raw JSONL. Check at least eight repository criteria by hand. Judge sample size and the cost model. |
| **The Security Lead** | What did agents do that a CISO must know about? Does the report surface it? | List risk events with exact commands: writes outside the workspace, shared-state mutation, containers and databases, git operations, network calls, secrets, broken written rules. Note whether each was disclosed. |
| **The Platform Lead** (DevEx) | Are the root causes right, and are the fixes the first things to do? | Trace how agents studied the codebase. Name the three highest-leverage concrete changes, with exact files. Check the roadmap sequence. |

Every reviewer file uses this shape:

```json
{
  "name": "The Auditor",
  "role": "Measurement & methodology",
  "stance": "agree | challenge | escalate",
  "stanceLabel": "2-4 words",
  "position": "one sentence",
  "arguments": ["3-5 points"],
  "challenge": "the hardest question",
  "findingReviews": [{"id": "verification-gap", "verdict": "confirmed | overstated | understated | wrong", "suggestedSeverity": "high", "note": "evidence"}],
  "analyzerBugs": [{"description": "", "evidence": "", "file": "", "suggestedFix": ""}],
  "missingInsights": [""],
  "reportChanges": [""]
}
```

## 2. Moderate

1. **Fix confirmed analyzer bugs in code first**, then re-run `run_readiness_check.py`. A number is never patched by hand in the report.
2. For each finding, take the verdict the evidence supports. Where two reviewers disagree, quote both and record the resolution.
3. Apply severity overrides only where the evidence supports them. Drop a finding only when the evidence shows it is wrong.
4. Add the caveats the Auditor requires, such as sample size or simulated prompts.

Then write `<out>/panel.json` and re-render with `--panel <out>/panel.json`:

```json
{
  "experts": [
    {
      "name": "The Buyer",
      "role": "...",
      "stance": "challenge",
      "stanceLabel": "...",
      "position": "...",
      "arguments": ["..."],
      "challenge": "..."
    }
  ],
  "disagreements": [
    {"topic": "Is 'blocked' the agent's fault or the environment's?", "resolution": "..."}
  ],
  "verdict": {
    "summary": "one paragraph the executive reads",
    "adjustments": ["what changed in this report because of the panel"],
    "confidence": "High | Medium | Low",
    "confidenceReason": "..."
  },
  "report": {
    "verdict": "optional replacement headline",
    "lede": "optional replacement sub-headline",
    "severity": {"finding-id": "high"},
    "dropFindings": ["finding-id"],
    "caveats": ["..."]
  }
}
```

## 3. Hill-climb

Repeat review, fix and re-render until no reviewer reports an analyzer bug or a "wrong" verdict. Two rounds are usually enough. Print the panel's adjustments in the report so the customer can see what the review changed.
