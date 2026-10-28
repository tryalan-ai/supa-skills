# supa-skills

> Claude Code plugin marketplace for the Supatest AI team.

This repo is a [Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces). Install skills with `/plugin install`, get updates with `/plugin update`.

## Setup (one-time per machine)

```
/plugin marketplace add supatest-ai/supa-skills
```

Then install the skills you want:

```
/plugin install commit
/plugin install work-summary
/plugin install review-pr
/plugin install test-feature
/plugin install task-to-pr
/plugin install setup-task-to-pr
/plugin install agent-browser
/plugin install automate-e2e-tests
/plugin install agent-readiness
/plugin install reflections
```

Skills are installed globally and available across all your projects.

## Updating

```
/plugin update
```

This updates all installed plugins from all marketplaces at once.

Claude Code checks for updates automatically at every startup — no action needed on your end.

## Available Skills

| Skill | Command | Description |
|-------|---------|-------------|
| **commit** | `/commit` | Commit already-staged files with conventional commit messages. Includes full merge conflict protocol. |
| **work-summary** | `/work-summary <author> "<start>" "<end>" <repos...>` | Generate work summaries from git commits with task categorization. |
| **review-pr** | `/review-pr [PR number or branch]` | Review a PR. Filters to issues introduced by the PR, scores confidence, requires approval before posting. |
| **setup-task-to-pr** | `/setup-task-to-pr [project/context]` | Set up project-local task-to-PR skills by inspecting the repo, interviewing the team, and writing enriched local workflows. |
| **task-to-pr** | `/task-to-pr [requirement-or-ticket]` | Drive a requirement from task or prompt through implementation, validation, commit, self-review, branch push, and PR. |
| **test-feature** | `/test-feature [description]` | Interactively test a feature across web, mobile, desktop, or API surfaces and produce an evidence-backed HTML validation report. |
| **agent-browser** | `/agent-browser [browser-task]` | Use the Agent Browser CLI for browser automation, screenshots, recordings, scraping, auth, and UI verification. |
| **automate-e2e-tests** | `/automate-e2e-tests [feature-or-report]` | Create or update durable E2E regression tests after feature implementation and validation. |
| **agent-readiness** | `/agent-readiness` | Assess codebase and team readiness for coding agents: 64 repository criteria, behavioral analysis of real session transcripts, an expert-panel review and a self-contained HTML report. |
| **reflections** | `/reflections` | Analyze local CLI agent session logs and turn recurring failure and stop patterns into prompt, tooling, setup, and environment suggestions. |

## Contributing

1. Create a new directory at the repo root for your skill
2. Add `.claude-plugin/plugin.json` (name, description, version)
3. Add `skills/<skill-name>/SKILL.md`
4. Add the plugin entry to `.claude-plugin/marketplace.json`
5. Submit a PR

### Plugin structure

```
my-skill/
├── .claude-plugin/
│   └── plugin.json
└── skills/
    └── my-skill/
        └── SKILL.md
```

### plugin.json template

```json
{
  "name": "my-skill",
  "description": "One-line description of what this skill does.",
  "version": "1.0.0",
  "author": { "name": "Supatest AI" },
  "license": "MIT"
}
```

### Releasing updates

**Always bump `version` in `plugin.json` when you change a skill.** Claude Code compares version strings to decide whether to auto-update. If the version stays the same, installed clients will not receive the new content even after the marketplace refreshes.

Use [semantic versioning](https://semver.org): `1.0.0` → `1.0.1` for patches, `1.1.0` for new behaviour, `2.0.0` for breaking changes.

## Resources

- [Claude Code Skills docs](https://code.claude.com/docs/en/skills)
- [Plugin Marketplaces docs](https://code.claude.com/docs/en/plugin-marketplaces)
