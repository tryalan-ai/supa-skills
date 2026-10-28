#!/usr/bin/env python3
"""Deterministic, evidence-backed repository readiness scan.

Every criterion returns pass/fail plus the file or command that proves it, so a
reader can audit the score instead of trusting it. Criteria are grouped into
pillars and maturity levels; a level is reached only when 80% of its criteria
and every lower level pass (the same gating model Factory's report uses).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

LEVEL_NAMES: dict[int, str] = {
    1: "Functional",
    2: "Documented",
    3: "Standardized",
    4: "Optimized",
    5: "Autonomous",
}

LEVEL_MEANING: dict[int, str] = {
    0: "Agents can read the code but cannot reliably build, run, or verify it.",
    1: "Agents can make small edits; a human must build, run and verify every change.",
    2: "Agents can follow written conventions on scoped changes with close review.",
    3: "Agents can take well-scoped tasks to locally verified changes.",
    4: "Agents can deliver multi-file features with light review and fast feedback.",
    5: "Agents can take a spec to a tested, reviewed pull request with strategic oversight only.",
}

PASS_THRESHOLD = 0.8

PILLARS: dict[str, str] = {
    "context": "Agent Context & Instructions",
    "navigability": "Codebase Navigability",
    "environment": "Environment Reproducibility",
    "verification": "Testing & Verification",
    "guardrails": "Static Guardrails",
    "ci": "CI/CD & Feedback Loops",
    "observability": "Observability & Debuggability",
    "safety": "Security & Governance",
    "specs": "Task & Spec Discipline",
    "tooling": "Agent Tooling & Integrations",
}

SOURCE_EXTENSIONS: dict[str, str] = {
    ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".py": "Python", ".go": "Go", ".rs": "Rust",
    ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".scala": "Scala", ".c": "C", ".cc": "C++", ".cpp": "C++", ".h": "C/C++",
}

EXCLUDED_DIRS = {"node_modules", ".git", "dist", "build", "vendor", ".venv", "venv", "target", ".next", ".turbo", "__pycache__"}
TEST_PATTERN = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)|[._-](test|spec)\.[a-z]+$|_test\.(go|py)$|(^|/)test_[^/]+\.py$|Tests?\.(java|cs|kt)$", re.I)
E2E_PATTERN = re.compile(r"(^|/)(e2e|cypress|playwright|integration-tests?)(/|$)|playwright\.config|cypress\.config", re.I)
GENERATED_PATTERN = re.compile(r"(\.gen\.|\.generated\.|_pb2\.py$|\.pb\.go$|(^|/)generated/|\.min\.js$)")


@dataclass
class Criterion:
    id: str
    pillar: str
    level: int
    title: str
    why: str
    fix: str
    passed: bool = False
    skipped: bool = False
    evidence: str = ""


@dataclass
class RepoFacts:
    root: Path
    files: list[str]
    file_set: set[str]
    languages: Counter
    source_files: list[str]
    test_files: list[str]
    line_counts: dict[str, int]
    package_json: dict
    scripts: dict[str, str]
    agent_docs: dict[str, str]
    git_log_days: int | None
    shallow: bool
    commit_count_90d: int
    contributors_90d: int
    extras: dict = field(default_factory=dict)


def list_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        files = [line for line in out.splitlines() if line]
        if files:
            return files
    except (subprocess.SubprocessError, OSError):
        pass
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            found.append(str(Path(dirpath, name).relative_to(root)))
    return found


def read_text(root: Path, rel: str, limit: int = 400_000) -> str:
    try:
        with (root / rel).open(errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def count_lines(root: Path, rel: str) -> int:
    try:
        with (root / rel).open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def git_stats(root: Path) -> tuple[int | None, int, int]:
    def run(args: list[str]) -> str:
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60).stdout
        except (subprocess.SubprocessError, OSError):
            return ""

    first = run(["log", "--reverse", "--format=%ct", "--max-count=1"]).strip()
    last = run(["log", "--format=%ct", "--max-count=1"]).strip()
    age = None
    if first and last:
        age = max(0, (int(last) - int(first)) // 86400)
    recent = run(["log", "--since=90.days", "--format=%ae"]).splitlines()
    return age, len(recent), len(set(recent))


def collect_facts(root: Path) -> RepoFacts:
    files = [f for f in list_files(root) if not any(part in EXCLUDED_DIRS for part in Path(f).parts)]
    file_set = set(files)
    languages: Counter = Counter()
    source_files: list[str] = []
    for rel in files:
        lang = SOURCE_EXTENSIONS.get(Path(rel).suffix.lower())
        if lang and not GENERATED_PATTERN.search(rel):
            languages[lang] += 1
            source_files.append(rel)
    test_files = [f for f in source_files if TEST_PATTERN.search(f)]
    sample = source_files if len(source_files) <= 6000 else source_files[:: max(1, len(source_files) // 6000)]
    line_counts = {rel: count_lines(root, rel) for rel in sample}

    package_json: dict = {}
    if "package.json" in file_set:
        try:
            package_json = json.loads(read_text(root, "package.json"))
        except json.JSONDecodeError:
            package_json = {}
    scripts = package_json.get("scripts", {}) if isinstance(package_json.get("scripts"), dict) else {}
    workspace_scripts: dict[str, str] = {}
    for rel in files:
        if rel.endswith("package.json") and rel != "package.json" and 1 <= rel.count("/") <= 3 and "node_modules" not in rel:
            try:
                manifest = json.loads(read_text(root, rel, 200_000))
            except json.JSONDecodeError:
                continue
            for name, command in (manifest.get("scripts") or {}).items():
                workspace_scripts[f"{rel.rsplit('/', 1)[0]}:{name}"] = str(command)

    agent_doc_names = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "GEMINI.md", ".github/copilot-instructions.md", ".windsurfrules")
    agent_docs = {name: read_text(root, name) for name in agent_doc_names if name in file_set}
    for rel in files:
        if rel.startswith(".cursor/rules/") or rel.startswith(".clinerules"):
            agent_docs[rel] = read_text(root, rel)

    age, commits, contributors = git_stats(root)
    shallow = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-shallow-repository"], capture_output=True, text=True).stdout.strip() == "true"
    facts = RepoFacts(root, files, file_set, languages, source_files, test_files, line_counts,
                      package_json, scripts, agent_docs, age, shallow, commits, contributors)
    facts.extras["workspace_scripts"] = workspace_scripts
    return facts


def any_file(facts: RepoFacts, pattern: str) -> str:
    regex = re.compile(pattern, re.I)
    for rel in facts.files:
        if regex.search(rel):
            return rel
    return ""


def all_files(facts: RepoFacts, pattern: str) -> list[str]:
    regex = re.compile(pattern, re.I)
    return [rel for rel in facts.files if regex.search(rel)]


def grep_files(facts: RepoFacts, candidates: list[str], pattern: str) -> str:
    regex = re.compile(pattern, re.I | re.M)
    for rel in candidates:
        if regex.search(read_text(facts.root, rel)):
            return rel
    return ""


def script_matching(facts: RepoFacts, pattern: str, workspace: bool = True) -> str:
    regex = re.compile(pattern, re.I)
    for name, command in facts.scripts.items():
        if regex.search(name) or regex.search(str(command)):
            return f"package.json script `{name}`"
    if workspace:
        for key, command in facts.extras.get("workspace_scripts", {}).items():
            pkg, name = key.split(":", 1)
            if regex.search(name) or regex.search(command):
                return f"{pkg}/package.json script `{name}`"
    for rel in ("Makefile", "justfile", "Taskfile.yml", "pyproject.toml", "tox.ini", "noxfile.py"):
        if rel in facts.file_set and regex.search(read_text(facts.root, rel)):
            return rel
    return ""


def agent_doc_text(facts: RepoFacts) -> str:
    return "\n".join(facts.agent_docs.values())


def primary_agent_doc(facts: RepoFacts) -> str:
    for name in ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md", ".cursorrules", "GEMINI.md"):
        if name in facts.agent_docs:
            return name
    return next(iter(facts.agent_docs), "")


def workflow_files(facts: RepoFacts) -> list[str]:
    return all_files(facts, r"^\.github/workflows/.+\.ya?ml$|^\.gitlab-ci\.yml$|^\.circleci/config\.yml$|^azure-pipelines\.yml$|^Jenkinsfile$|^bitbucket-pipelines\.yml$|^\.buildkite/")


def ci_text(facts: RepoFacts) -> str:
    return "\n".join(read_text(facts.root, rel, 200_000) for rel in workflow_files(facts))


class Skip(Exception):
    """Raised by a check that cannot be measured in this checkout."""


CheckResult = tuple[bool, str]
Check = Callable[[RepoFacts], CheckResult]


def found(evidence: str, missing: str) -> CheckResult:
    return (bool(evidence), evidence or missing)


def check_agent_doc(facts: RepoFacts) -> CheckResult:
    doc = primary_agent_doc(facts)
    return found(doc and f"{doc} ({len(facts.agent_docs[doc].splitlines())} lines)", "No AGENTS.md, CLAUDE.md, Copilot or Cursor instructions found")


def check_agent_doc_commands(facts: RepoFacts) -> CheckResult:
    text = agent_doc_text(facts)
    hits = sorted(set(re.findall(r"\b(pnpm|npm|yarn|bun|make|pytest|go test|cargo|uv|poetry|gradle|mvn|dotnet)\b[^\n`]{0,40}(test|lint|build|check|dev|run)", text)))
    return (len(hits) >= 2, f"{len(hits)} runnable build/test/lint commands documented for agents" if hits else "Agent instructions do not list runnable test, lint or build commands")


def check_agent_doc_size(facts: RepoFacts) -> CheckResult:
    doc = primary_agent_doc(facts)
    if not doc:
        return (False, "No agent instructions file")
    lines = len(facts.agent_docs[doc].splitlines())
    return (20 <= lines <= 600, f"{doc} is {lines} lines (useful range is 20-600; larger files dilute context every turn)")


def check_agent_doc_invariants(facts: RepoFacts) -> CheckResult:
    text = agent_doc_text(facts)
    hits = [w for w in ("never", "do not", "must", "don't", "always") if re.search(rf"\b{w}\b", text, re.I)]
    risky = [w for w in ("migration", "secret", "credential", "delete", "production", "deploy", "force") if re.search(w, text, re.I)]
    return (len(hits) >= 2 and len(risky) >= 2, f"Guardrail language: {', '.join(hits) or 'none'}; high-risk topics covered: {', '.join(risky) or 'none'}")


def check_agent_config_not_ignored(facts: RepoFacts) -> CheckResult:
    ignored = []
    for rel in [f for f in facts.files if f.endswith(".gitignore") and f.count("/") <= 2]:
        for line in read_text(facts.root, rel).splitlines():
            entry = line.strip().strip("/")
            if entry in ("CLAUDE.md", ".claude", "AGENTS.md", ".cursor", ".cursorrules", ".codex", "GEMINI.md", ".github/copilot-instructions.md", ".mcp.json"):
                ignored.append(f"{rel}: {entry}")
    return (not ignored, f"Agent configuration is gitignored, so it cannot be shared: {', '.join(ignored)}" if ignored else "No agent configuration is gitignored")


def check_nested_agent_docs(facts: RepoFacts) -> CheckResult:
    nested = [f for f in facts.files if re.search(r".+/(AGENTS|CLAUDE)\.md$", f) and "node_modules" not in f]
    return (len(nested) >= 2, f"{len(nested)} component-level instruction files (e.g. {', '.join(nested[:3])})" if nested else "No component-level AGENTS.md/CLAUDE.md for sub-packages")


def check_multi_agent_docs(facts: RepoFacts) -> CheckResult:
    claude = "CLAUDE.md" in facts.file_set or ".claude/CLAUDE.md" in facts.file_set
    agents = "AGENTS.md" in facts.file_set
    missing = [name for name, ok in (("CLAUDE.md (Claude Code)", claude), ("AGENTS.md (Codex, Cursor, Droid, OpenCode)", agents)) if not ok]
    if missing:
        return (False, f"Missing {' and '.join(missing)}; present: {', '.join(facts.agent_docs) or 'none'}")
    return (True, f"CLAUDE.md and AGENTS.md both present ({', '.join(list(facts.agent_docs)[:5])})")


def check_agent_doc_fresh(facts: RepoFacts) -> CheckResult:
    doc = primary_agent_doc(facts)
    if not doc:
        return (False, "No agent instructions file")
    if facts.shallow:
        raise Skip("Shallow clone: git history unavailable")
    try:
        out = subprocess.run(["git", "-C", str(facts.root), "log", "-1", "--format=%cr", "--", doc], capture_output=True, text=True, timeout=30).stdout.strip()
        count = subprocess.run(["git", "-C", str(facts.root), "log", "--since=90.days", "--format=%h", "--", doc], capture_output=True, text=True, timeout=30).stdout.split()
    except (subprocess.SubprocessError, OSError):
        return (False, "Git history unavailable")
    return (len(count) >= 2, f"{doc} last changed {out or 'never'}; {len(count)} edits in 90 days")


def check_readme(facts: RepoFacts) -> CheckResult:
    readme = any_file(facts, r"^readme(\.md|\.rst|\.txt)?$")
    if not readme:
        return (False, "No root README")
    lines = len(read_text(facts.root, readme).splitlines())
    return (lines >= 30, f"{readme} ({lines} lines)")


def check_architecture_doc(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)(architecture|ARCHITECTURE|CONTEXT|DESIGN)(\.md|/)|(^|/)docs/.*architecture"), "No architecture or system-context document")


def check_adrs(facts: RepoFacts) -> CheckResult:
    adrs = all_files(facts, r"(^|/)(adr|adrs|decisions|rfcs?)/.+\.md$|(^|/)\d{4}-.+-adr\.md$")
    return (len(adrs) >= 3, f"{len(adrs)} decision records" if adrs else "No ADR/RFC/decision records")


def check_types(facts: RepoFacts) -> CheckResult:
    langs = facts.languages
    typed = sum(langs[l] for l in ("TypeScript", "Go", "Rust", "Java", "Kotlin", "C#", "Swift", "Scala", "C++", "C"))
    py = langs["Python"]
    if py and any_file(facts, r"(^|/)(mypy\.ini|pyrightconfig\.json|\.mypy\.ini)$|pyproject\.toml"):
        if grep_files(facts, [f for f in ("pyproject.toml", "mypy.ini", "setup.cfg") if f in facts.file_set], r"\[(tool\.)?(mypy|pyright)"):
            typed += py
    total = sum(langs.values()) or 1
    share = typed / total
    return (share >= 0.7, f"{share:.0%} of source files are statically typed ({', '.join(f'{k} {v}' for k, v in langs.most_common(3))})")


def check_ts_strict(facts: RepoFacts) -> CheckResult:
    configs = all_files(facts, r"(^|/)tsconfig[^/]*\.json$")
    if not configs:
        return (facts.languages["TypeScript"] == 0, "No TypeScript in repo" if not facts.languages["TypeScript"] else "TypeScript without tsconfig")
    strict = [c for c in configs if re.search(r'"strict"\s*:\s*true', read_text(facts.root, c))]
    loose = [c for c in configs if re.search(r'"strict"\s*:\s*false', read_text(facts.root, c))]
    if not strict:
        return (False, f"{len(configs)} tsconfig files, none enable strict")
    return (len(loose) <= len(configs) // 4, f"strict enabled in {', '.join(strict[:2])}{' (shared base config)' if len(strict) < len(configs) else ''}; {len(loose)} configs opt out")


def check_file_size(facts: RepoFacts) -> CheckResult:
    if not facts.line_counts:
        return (False, "No source files")
    big = sorted(((n, f) for f, n in facts.line_counts.items() if n > 1500 and not TEST_PATTERN.search(f)), reverse=True)
    share = len(big) / len(facts.line_counts)
    facts.extras["largest_files"] = [{"path": f, "lines": n} for n, f in big[:8]]
    return (share <= 0.01, f"{len(big)} source files over 1,500 lines ({share:.1%}); largest: {big[0][1]} ({big[0][0]} lines)" if big else "No source files over 1,500 lines")


def check_generated_marked(facts: RepoFacts) -> CheckResult:
    attrs = read_text(facts.root, ".gitattributes")
    marked = "linguist-generated" in attrs or "-diff" in attrs
    generated = [f for f in facts.files if GENERATED_PATTERN.search(f)]
    if not generated:
        return (True, "No generated sources tracked")
    return (marked, f"{len(generated)} generated files; .gitattributes {'marks' if marked else 'does not mark'} them")


def check_monorepo_map(facts: RepoFacts) -> CheckResult:
    workspace = any_file(facts, r"^(pnpm-workspace\.yaml|lerna\.json|nx\.json|turbo\.json|go\.work|Cargo\.toml|rush\.json)$")
    top_dirs = {Path(f).parts[0] for f in facts.files if len(Path(f).parts) > 1}
    if len(top_dirs) < 8 and not workspace:
        return (True, "Single-package layout; no workspace map needed")
    text = agent_doc_text(facts) + read_text(facts.root, "README.md")
    mentioned = [d for d in ("apps", "packages", "services", "libs", "modules", "src", "cmd", "internal") if d in top_dirs and re.search(rf"\b{d}/", text)]
    return (bool(mentioned), f"Workspace ({workspace or 'multi-dir'}) mapped in docs via {', '.join(mentioned)}" if mentioned else "Multi-package repo with no directory map in README or agent docs")


def check_codeowners(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)CODEOWNERS$"), "No CODEOWNERS; agents cannot tell who owns a surface")


def check_lockfile(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"^(pnpm-lock\.yaml|package-lock\.json|yarn\.lock|bun\.lockb?|poetry\.lock|uv\.lock|Pipfile\.lock|go\.sum|Cargo\.lock|Gemfile\.lock|composer\.lock|packages\.lock\.json|gradle\.lockfile)$"), "No dependency lockfile committed")


def check_runtime_pinned(facts: RepoFacts) -> CheckResult:
    pinned = any_file(facts, r"^(\.nvmrc|\.node-version|\.tool-versions|\.python-version|\.ruby-version|rust-toolchain(\.toml)?|\.go-version|mise\.toml|\.mise\.toml|global\.json)$")
    if not pinned and facts.package_json.get("engines"):
        pinned = "package.json engines"
    if not pinned and facts.package_json.get("packageManager"):
        pinned = "package.json packageManager"
    return found(pinned, "Runtime version is not pinned (.nvmrc/.tool-versions/etc.)")


def check_env_example(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)\.env[^/]*\.(example|sample|template)$|(^|/)\.env\.example$|(^|/)example\.env$"), "No .env.example; agents guess required variables")


def check_setup_script(facts: RepoFacts) -> CheckResult:
    evidence = script_matching(facts, r"^(setup|bootstrap|install:all|services:setup|init)$|scripts/setup|bootstrap\.sh")
    if not evidence:
        evidence = any_file(facts, r"^(scripts/)?(setup|bootstrap)(\.sh|\.ps1|\.py)?$|^\.alan/setup\.sh$|^\.devcontainer/")
    return found(evidence, "No one-command setup/bootstrap script")


def check_codegen_on_install(facts: RepoFacts) -> CheckResult:
    generators = {"prisma generate": r"schema\.prisma$", "graphql-codegen": r"codegen\.(ya?ml|ts|js)$", "buf generate": r"buf\.gen\.ya?ml$", "openapi-generator": r"openapitools\.json$"}
    needed = [tool for tool, pattern in generators.items() if any_file(facts, pattern)]
    if not needed:
        return (True, "No code generators that must run before type checking")
    hooks: list[str] = []
    typos: list[str] = []
    for rel in [f for f in facts.files if f.endswith("package.json") and f.count("/") <= 3 and "node_modules" not in f]:
        try:
            scripts = json.loads(read_text(facts.root, rel, 200_000)).get("scripts") or {}
        except json.JSONDecodeError:
            continue
        for name, command in scripts.items():
            if any(tool.split()[0] in str(command) for tool in needed):
                if name in ("postinstall", "prepare", "preinstall", "install"):
                    hooks.append(f"{rel} `{name}`")
                elif re.fullmatch(r"post[-_ ]install|post_install|postInstall", name):
                    typos.append(f"{rel} defines `{name}` (npm never runs it; the hook is `postinstall`)")
    if typos and not hooks:
        return (False, "; ".join(typos))
    setup = script_matching(facts, r"^(setup|bootstrap|dx|init)$")
    return found(", ".join(hooks) or (setup and f"{setup} (manual step)"), f"{', '.join(needed)} is required but nothing runs it on install")


def check_containerized_services(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)(docker-compose[^/]*\.ya?ml|compose\.ya?ml|process-compose\.ya?ml|Tiltfile|skaffold\.yaml)$"), "No docker-compose or process manager for backing services")


def check_devcontainer(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"^\.devcontainer/|^\.alan/environment\.json$|^flake\.nix$|^devbox\.json$|^\.gitpod\.yml$|^\.codesandbox/"), "No devcontainer/Nix/cloud dev environment for sandboxed agents")


def check_seed_data(facts: RepoFacts) -> CheckResult:
    evidence = script_matching(facts, r"seed|fixtures?:load|db:reset")
    return found(evidence or any_file(facts, r"(^|/)(seeds?|fixtures)/"), "No seed data or fixtures for a working local dataset")


def check_tests_exist(facts: RepoFacts) -> CheckResult:
    return (len(facts.test_files) >= 10, f"{len(facts.test_files)} test files")


def check_test_ratio(facts: RepoFacts) -> CheckResult:
    src = len(facts.source_files) - len(facts.test_files)
    ratio = len(facts.test_files) / max(1, src)
    return (ratio >= 0.2, f"{ratio:.2f} test files per source file ({len(facts.test_files)} tests / {src} sources)")


def check_test_command(facts: RepoFacts) -> CheckResult:
    root = script_matching(facts, r"^test$", workspace=False)
    if root:
        return (True, root)
    pkg = script_matching(facts, r"^test$|vitest|jest|pytest|go test|cargo test|mocha|rspec|phpunit|dotnet test|gradle.*test")
    return found(pkg and f"{pkg} (no root `test` script; agents must know which package to run)", "No discoverable test command")


def check_focused_tests(facts: RepoFacts) -> CheckResult:
    names = [n for n in facts.scripts if re.search(r"test[:\-]", n)]
    pkg_tests = len({k.split(":", 1)[0] for k in facts.extras.get("workspace_scripts", {}) if re.search(r":test(:|$)", k)})
    documented = len(re.findall(r"`[^`]*(vitest|jest|pytest|test:dev|test\b)[^`]*(--|\s)[\w/.-]+\.(spec|test)\.[a-z]+[^`]*`|`[^`]*-- [\w/.-]*e2e/path", agent_doc_text(facts)))
    total = len(names) + pkg_tests + documented
    return (total >= 3, f"{len(names)} scoped root test scripts, {pkg_tests} packages with their own test script, {documented} single-file test commands in agent docs")


def check_e2e(facts: RepoFacts) -> CheckResult:
    e2e = [f for f in facts.files if E2E_PATTERN.search(f)]
    return (len(e2e) >= 3, f"{len(e2e)} end-to-end/integration test files" if e2e else "No end-to-end or integration tests")


def check_coverage(facts: RepoFacts) -> CheckResult:
    evidence = script_matching(facts, r"coverage|--cov")
    if not evidence:
        evidence = any_file(facts, r"(^|/)(\.coveragerc|codecov\.ya?ml|\.nycrc[^/]*|jest\.config\.[cm]?[jt]s)$")
        if evidence and evidence.startswith("jest") and "coverage" not in read_text(facts.root, evidence):
            evidence = ""
    return found(evidence, "No coverage measurement configured")


def check_typecheck_command(facts: RepoFacts) -> CheckResult:
    if facts.languages["TypeScript"] == 0 and facts.languages["Python"] == 0:
        return (True, "Compiled language: the build is the type check")
    return found(script_matching(facts, r"^type-?check$") or script_matching(facts, r"type-?check|tsc|mypy|pyright"), "No explicit type-check command")


def check_linter(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)(biome\.jsonc?|\.eslintrc[^/]*|eslint\.config\.[cm]?[jt]s|\.golangci\.ya?ml|ruff\.toml|\.ruff\.toml|\.flake8|\.pylintrc|\.rubocop\.yml|clippy\.toml|checkstyle\.xml|\.swiftlint\.yml)$") or grep_files(facts, [f for f in ("pyproject.toml",) if f in facts.file_set], r"\[tool\.(ruff|pylint|flake8)"), "No linter configuration")


def check_formatter(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)(biome\.jsonc?|\.prettierrc[^/]*|prettier\.config\.[cm]?js|rustfmt\.toml|\.clang-format)$") or grep_files(facts, [f for f in ("pyproject.toml",) if f in facts.file_set], r"\[tool\.(black|ruff\.format|ruff)") or ("gofmt" if facts.languages["Go"] else ""), "No formatter configuration")


def check_precommit(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"^(\.husky/|lefthook\.ya?ml|\.pre-commit-config\.yaml|\.githooks/|\.lintstagedrc)") or ("lint-staged" if "lint-staged" in facts.package_json else ""), "No pre-commit hooks; agents push unformatted code")


def check_custom_lint_rules(facts: RepoFacts) -> CheckResult:
    checks = [n for n in facts.scripts if re.search(r"^check[:\-]|:check$|guard|boundar", n)]
    return (len(checks) >= 2, f"{len(checks)} project-specific guard scripts ({', '.join(checks[:4])})" if checks else "No architecture/boundary checks beyond generic lint")


def check_lint_fix(facts: RepoFacts) -> CheckResult:
    return found(script_matching(facts, r"lint:fix|format|--fix|--write"), "No auto-fix command; agents hand-fix formatting")


def check_ci(facts: RepoFacts) -> CheckResult:
    wf = workflow_files(facts)
    return (bool(wf), f"{len(wf)} CI pipeline files" if wf else "No CI pipeline")


def check_ci_on_pr(facts: RepoFacts) -> CheckResult:
    text = ci_text(facts)
    return (bool(re.search(r"pull_request|merge_request|pr:", text)), "CI runs on pull requests" if re.search(r"pull_request|merge_request|pr:", text) else "CI does not run on pull requests")


def check_ci_tests(facts: RepoFacts) -> CheckResult:
    text = ci_text(facts)
    test = re.search(r"\b(test|vitest|jest|pytest|go test|cargo test)\b", text)
    lint = re.search(r"\b(lint|biome|eslint|ruff|golangci)\b", text)
    return (bool(test and lint), f"CI runs {'tests' if test else 'no tests'} and {'lint' if lint else 'no lint'}")


def check_ci_cache(facts: RepoFacts) -> CheckResult:
    text = ci_text(facts)
    return (bool(re.search(r"actions/cache|cache:\s*['\"]?(pnpm|npm|yarn|pip)|setup-.*cache|turbo|nx-cloud", text)), "CI caches dependencies/builds" if re.search(r"actions/cache|cache:|turbo", text) else "CI has no dependency or build cache")


def check_pr_template(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)pull_request_template\.md$|(^|/)PULL_REQUEST_TEMPLATE/|merge_request_templates/"), "No pull request template")


def check_deploy_automation(facts: RepoFacts) -> CheckResult:
    text = ci_text(facts)
    return (bool(re.search(r"deploy|release|publish", text, re.I)), "Deploys are automated in CI" if re.search(r"deploy|release", text, re.I) else "No automated deploy/release pipeline")


def check_dependency_bot(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"^\.github/dependabot\.ya?ml$|^renovate\.json5?$|^\.renovaterc|^\.github/renovate\.json"), "No Dependabot/Renovate")


def check_structured_logging(facts: RepoFacts) -> CheckResult:
    deps = json.dumps(facts.package_json.get("dependencies", {})) + json.dumps(facts.package_json.get("devDependencies", {}))
    manifests = [f for f in facts.files if re.search(r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|pom\.xml|build\.gradle)$", f) and f.count("/") <= 3]
    hit = re.search(r"pino|winston|bunyan|structlog|loguru|zap|zerolog|slog|tracing|serilog|logback", deps)
    if not hit:
        hit_file = grep_files(facts, manifests[:80], r"pino|winston|bunyan|structlog|loguru|go\.uber\.org/zap|zerolog|log/slog|\btracing\b|serilog|logback")
        return found(hit_file and f"Structured logger declared in {hit_file}", "No structured logging library")
    return (True, f"Structured logger: {hit.group(0)}")


def check_error_tracking(facts: RepoFacts) -> CheckResult:
    manifests = [f for f in facts.files if re.search(r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|pom\.xml|build\.gradle)$", f) and f.count("/") <= 3]
    hit = grep_files(facts, manifests[:80], r"sentry|opentelemetry|@opentelemetry|datadog|dd-trace|newrelic|honeycomb|rollbar|bugsnag|signoz")
    return found(hit and f"Error tracking/telemetry declared in {hit}", "No error tracking or OpenTelemetry")


def check_health_endpoint(facts: RepoFacts) -> CheckResult:
    by_name = any_file(facts, r"(^|/)(health|healthz|healthcheck|readiness|liveness)(\.[a-z]+)+$|(^|/)api\+?/health")
    if by_name and not TEST_PATTERN.search(by_name):
        return (True, f"{by_name}")
    candidates = sorted((f for f in facts.source_files if re.search(r"(route|server|app|main|health|handler)", f, re.I) and not TEST_PATTERN.search(f) and "/scripts/" not in f), key=lambda f: ("health" not in f, len(f)))[:600]
    return found(grep_files(facts, candidates, r"['\"/]health(z|check)?['\"]|/ready['\"]|livez"), "No health check endpoint")


def check_runbooks(facts: RepoFacts) -> CheckResult:
    books = all_files(facts, r"(runbook|playbook|incident|troubleshoot|postmortem|debugging)[^/]*\.md$")
    return (bool(books), f"{len(books)} runbooks/incident docs" if books else "No runbooks or troubleshooting docs")


def check_test_artifacts(facts: RepoFacts) -> CheckResult:
    configs = all_files(facts, r"(^|/)(playwright|cypress)\.config\.[cm]?[jt]s$")
    hit = grep_files(facts, configs, r"trace|screenshot|video")
    return found(hit and f"{hit} captures traces/screenshots", "Browser tests do not capture traces or screenshots")


def check_gitignore_secrets(facts: RepoFacts) -> CheckResult:
    text = read_text(facts.root, ".gitignore")
    ok = bool(re.search(r"^\.env", text, re.M) or re.search(r"\*\.env|\.env\*", text))
    leaked = [f for f in facts.files if re.search(r"(^|/)\.env$|(^|/)\.env\.(local|production|prod)$|\.pem$|id_rsa$|credentials\.json$", f)]
    if leaked:
        return (False, f"Possible secrets committed: {', '.join(leaked[:3])}")
    return (ok, ".gitignore excludes .env files" if ok else ".gitignore does not exclude .env files")


def check_secret_scanning(facts: RepoFacts) -> CheckResult:
    text = ci_text(facts) + read_text(facts.root, ".pre-commit-config.yaml") + read_text(facts.root, "lefthook.yml")
    hit = re.search(r"gitleaks|trufflehog|detect-secrets|secretlint|ggshield|codeql", text, re.I)
    return (bool(hit), f"Secret/code scanning via {hit.group(0)}" if hit else "No secret scanning in CI or hooks")


def check_security_policy(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)SECURITY\.md$"), "No SECURITY.md")


def check_migration_guardrails(facts: RepoFacts) -> CheckResult:
    migrations = any_file(facts, r"(^|/)(migrations?|drizzle|alembic|flyway|liquibase|prisma/migrations|db/migrate)/")
    if not migrations:
        return (True, "No database migrations in repo")
    text = agent_doc_text(facts)
    documented = re.search(r"migration", text, re.I)
    guarded = re.search(r"migration", ci_text(facts) + " ".join(facts.scripts), re.I)
    return (bool(documented and guarded), f"Migrations {'documented for agents' if documented else 'not mentioned in agent docs'}; {'CI/script guard present' if guarded else 'no CI guard'}")


def check_agent_permissions(facts: RepoFacts) -> CheckResult:
    configs = [f for f in facts.files if re.search(r"^\.claude/settings(\.local)?\.json$|^\.codex/config\.toml$|^\.cursor/cli\.json$|^opencode\.json$|^\.factory/", f)]
    hit = grep_files(facts, configs, r"deny|allow|permissions|sandbox|approval")
    return found(hit and f"Agent permission policy in {hit}", "No committed agent permission policy (allow/deny lists)")


def check_destructive_rules(facts: RepoFacts) -> CheckResult:
    text = agent_doc_text(facts)
    hit = re.search(r"(reset --hard|force[- ]push|drop table|rm -rf|hard-delete|destructive)", text, re.I)
    return (bool(hit), f"Agent docs name destructive operations ({hit.group(0)})" if hit else "Agent docs do not forbid destructive operations")


def check_issue_templates(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"^\.github/ISSUE_TEMPLATE/|^\.gitlab/issue_templates/"), "No issue templates for structured task intake")


def check_contributing(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)CONTRIBUTING\.md$") or (primary_agent_doc(facts) if re.search(r"(definition of done|before (saying|claiming).*complete|delivery policy|pr description)", agent_doc_text(facts), re.I) else ""), "No CONTRIBUTING guide or definition of done")


def check_specs_dir(facts: RepoFacts) -> CheckResult:
    specs = all_files(facts, r"(^|/)(specs?|prds?|rfcs?|notes|docs/design|design-docs)/.+\.md$")
    return (len(specs) >= 5, f"{len(specs)} spec/plan/notes documents in repo" if specs else "No specs, plans or design notes in the repo")


def check_changelog(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"^CHANGELOG\.md$|^HISTORY\.md$|^\.changeset/"), "No changelog")


def check_api_contracts(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(openapi|swagger)[^/]*\.(ya?ml|json)$|\.proto$|\.graphql$|schema\.graphql|(^|/)validators?/|zod|@fastify/swagger") or grep_files(facts, [f for f in facts.files if f.endswith("package.json") and f.count('/') <= 2][:40], r"zod|openapi|swagger|@trpc|graphql"), "No machine-readable API contracts")


def check_skills(facts: RepoFacts) -> CheckResult:
    skills = all_files(facts, r"(^|/)\.(agents|claude|codex|cursor|opencode|factory)/(skills|commands|droids|agents)/.+\.md$")
    return (len(skills) >= 3, f"{len(skills)} committed agent skills/commands" if skills else "No reusable agent skills or slash commands")


def check_mcp_config(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)(\.mcp\.json|mcp\.json|\.cursor/mcp\.json|\.vscode/mcp\.json)$") or grep_files(facts, [f for f in ("opencode.json", ".codex/config.toml") if f in facts.file_set], r"mcp"), "No project MCP server configuration")


def check_agent_hooks(facts: RepoFacts) -> CheckResult:
    configs = [f for f in facts.files if re.search(r"^\.claude/settings\.json$|^\.codex/|^\.cursor/hooks", f)]
    return found(grep_files(facts, configs, r"\"hooks\"|PostToolUse|PreToolUse|hooks\s*="), "No agent lifecycle hooks (format-on-edit, guard rails)")


def check_browser_automation(facts: RepoFacts) -> CheckResult:
    return found(any_file(facts, r"(^|/)(playwright|cypress)\.config\.[cm]?[jt]s$|agent-browser|(^|/)\.agents/skills/.*(browser|test-feature)"), "No browser automation for agents to verify UI")


def check_auth_bypass(facts: RepoFacts) -> CheckResult:
    text = agent_doc_text(facts) + "\n".join(read_text(facts.root, f, 50_000) for f in all_files(facts, r"(^|/)(playwright|cypress)\.config\.|global-setup|auth\.setup")[:10])
    hit = re.search(r"storageState|seed(ed)? (PAT|token|user)|test (user|account)|auth bypass|personal access token|globalSetup", text, re.I)
    return (bool(hit), f"Agents can authenticate without the login page ({hit.group(0)})" if hit else "No documented way for agents to authenticate to the running app")


def check_agent_infra_dirs(facts: RepoFacts) -> CheckResult:
    dirs = sorted({Path(f).parts[0] for f in facts.files if re.match(r"^\.(agents|claude|codex|cursor|opencode|factory|alan|supatest|windsurf|gemini)/", f)})
    return (len(dirs) >= 2, f"Agent config for {len(dirs)} harnesses: {', '.join(dirs)}" if dirs else "No agent harness configuration committed")


CRITERIA: list[tuple[str, str, int, str, str, str, Check]] = [
    ("ctx.agent_doc", "context", 1, "Agent instruction file exists", "Without it every session rediscovers the stack from scratch.", "Generate AGENTS.md from the repo's real commands and invariants.", check_agent_doc),
    ("ctx.readme", "context", 1, "README explains the project", "Agents and new hires start from the README.", "Expand README with purpose, layout and quickstart.", check_readme),
    ("ctx.shared", "context", 2, "Agent configuration can be shared", "A gitignored CLAUDE.md or .claude/ means every developer's agent runs with different, private rules.", "Remove agent instruction and settings paths from .gitignore; keep only personal *.local files ignored.", check_agent_config_not_ignored),
    ("ctx.commands", "context", 2, "Agent docs list runnable commands", "Agents otherwise guess test/lint invocations and burn turns on failures.", "Document exact build, test, lint and type-check commands per package.", check_agent_doc_commands),
    ("ctx.size", "context", 2, "Agent docs are a usable size", "Very large instruction files are loaded every turn and dilute attention.", "Keep root instructions short; move detail to component docs and skills.", check_agent_doc_size),
    ("ctx.invariants", "context", 3, "High-risk invariants written down", "Agents cannot infer which mistakes are unrecoverable.", "Add a short 'never do' section covering migrations, secrets, prod and data deletion.", check_agent_doc_invariants),
    ("ctx.architecture", "context", 3, "Architecture/system context doc", "Cross-cutting changes need a map of services and data flow.", "Write an architecture doc with a component diagram.", check_architecture_doc),
    ("ctx.nested", "context", 4, "Component-level agent instructions", "Path-specific rules beat one global file in large repos.", "Add AGENTS.md per high-change package.", check_nested_agent_docs),
    ("ctx.fresh", "context", 4, "Agent docs are actively maintained", "Stale instructions are worse than none; agents follow them confidently.", "Update agent docs in the same PR that changes behavior.", check_agent_doc_fresh),
    ("ctx.multi", "context", 5, "Instructions for every agent your devs use", "Developers mix Claude, Codex, Cursor and Copilot; each reads a different file.", "Single-source AGENTS.md and shim the others.", check_multi_agent_docs),
    ("ctx.adrs", "context", 5, "Decision records capture why", "Agents undo deliberate decisions they cannot see.", "Record ADRs for non-obvious architecture choices.", check_adrs),

    ("nav.types", "navigability", 1, "Most code is statically typed", "Types are the cheapest spec an agent can verify against.", "Adopt TypeScript/mypy/pyright incrementally on hot paths.", check_types),
    ("nav.lockfile_map", "navigability", 2, "Workspace layout is documented", "Agents waste exploration turns finding where a feature lives.", "Add a directory map to README/AGENTS.md.", check_monorepo_map),
    ("nav.strict", "navigability", 3, "Strict type checking enabled", "Loose types let agents ship code that compiles but is wrong.", "Enable strict mode and ratchet existing errors.", check_ts_strict),
    ("nav.file_size", "navigability", 3, "Few oversized source files", "Files over 1,500 lines exceed what an agent reads at once and invite blind edits.", "Split the largest files; add a file-size budget check.", check_file_size),
    ("nav.generated", "navigability", 4, "Generated code is marked", "Agents edit generated files and lose the change on regeneration.", "Mark generated paths linguist-generated in .gitattributes.", check_generated_marked),
    ("nav.codeowners", "navigability", 4, "Ownership is declared", "Agents and reviewers need to know who approves a surface.", "Add CODEOWNERS.", check_codeowners),

    ("env.lockfile", "environment", 1, "Dependency lockfile committed", "Unpinned installs make agent runs non-reproducible.", "Commit the lockfile and install with --frozen-lockfile.", check_lockfile),
    ("env.runtime", "environment", 2, "Runtime version pinned", "Version drift is a top cause of 'works on my machine' agent failures.", "Add .nvmrc/.tool-versions and engines.", check_runtime_pinned),
    ("env.codegen", "environment", 2, "Generated code is produced on install", "Without generated clients, type checks fail with hundreds of unrelated errors and agents cannot verify anything.", "Run code generation in a real postinstall hook and document it.", check_codegen_on_install),
    ("env.env_example", "environment", 2, "Environment variables documented", "Agents cannot start the app without knowing required config.", "Commit .env.example with every required key.", check_env_example),
    ("env.setup", "environment", 3, "One-command setup", "Multi-step manual setup blocks agents in fresh sandboxes.", "Script setup end to end; verify in CI.", check_setup_script),
    ("env.services", "environment", 3, "Backing services are containerized", "Agents need a database and queues they can start themselves.", "Add docker-compose/process-compose for dependencies.", check_containerized_services),
    ("env.seed", "environment", 4, "Seed data for a working app", "An empty database hides most bugs from agent verification.", "Add idempotent seed scripts.", check_seed_data),
    ("env.sandbox", "environment", 5, "Reproducible cloud/sandbox environment", "Parallel agents need disposable, identical environments.", "Define a devcontainer or Alan cloud environment.", check_devcontainer),

    ("ver.tests", "verification", 1, "Automated tests exist", "Without tests an agent cannot tell whether it broke something.", "Add tests around the most-changed modules first.", check_tests_exist),
    ("ver.command", "verification", 1, "Test command is discoverable", "Agents look for a standard test entry point.", "Expose `test` in package.json/Makefile.", check_test_command),
    ("ver.typecheck", "verification", 2, "Type-check command exists", "The fastest correctness signal after an edit.", "Add a `type-check` script.", check_typecheck_command),
    ("ver.focused", "verification", 3, "Focused per-package test commands", "Whole-suite runs are slow; agents skip them or time out.", "Add package-scoped test scripts and document them.", check_focused_tests),
    ("ver.ratio", "verification", 3, "Healthy test-to-source ratio", "Sparse tests leave most agent edits unverified.", "Target one test file per five sources on changed areas.", check_test_ratio),
    ("ver.e2e", "verification", 4, "End-to-end tests", "UI and integration regressions escape unit tests.", "Add Playwright/Cypress smoke flows for core journeys.", check_e2e),
    ("ver.coverage", "verification", 5, "Coverage is measured", "Teams cannot see which agent changes landed untested.", "Report coverage in CI on changed lines.", check_coverage),

    ("guard.linter", "guardrails", 1, "Linter configured", "Lint is the cheapest automated review an agent gets.", "Adopt Biome/ESLint/Ruff/golangci-lint.", check_linter),
    ("guard.formatter", "guardrails", 2, "Formatter configured", "Formatting noise hides real changes in agent diffs.", "Adopt a formatter and format the repo once.", check_formatter),
    ("guard.fix", "guardrails", 2, "Auto-fix command", "Agents should fix lint mechanically, not by hand.", "Add `lint:fix`.", check_lint_fix),
    ("guard.precommit", "guardrails", 3, "Pre-commit hooks enforce checks", "Hooks catch agent mistakes before they reach CI.", "Add lefthook/husky/pre-commit for lint and format.", check_precommit),
    ("guard.custom", "guardrails", 4, "Architecture rules are enforced in code", "Written rules drift; executable rules hold.", "Turn repeated review comments into check scripts.", check_custom_lint_rules),

    ("ci.exists", "ci", 1, "CI pipeline exists", "Agent PRs need an independent verifier.", "Add CI that installs, builds and tests.", check_ci),
    ("ci.pr", "ci", 2, "CI runs on every pull request", "Agent output must be checked before merge.", "Trigger CI on pull_request.", check_ci_on_pr),
    ("ci.gates", "ci", 3, "CI runs tests and lint", "A green build should mean something.", "Run lint, type-check and tests in CI.", check_ci_tests),
    ("ci.template", "ci", 3, "Pull request template", "Agent PRs should state problem, changes and testing.", "Add a PR template with testing evidence.", check_pr_template),
    ("ci.cache", "ci", 4, "CI is cached for fast feedback", "Slow CI stalls agent iteration loops.", "Cache dependencies and build outputs.", check_ci_cache),
    ("ci.deploy", "ci", 4, "Deploys are automated", "Manual deploys keep humans in the loop for every change.", "Automate staging deploys from main.", check_deploy_automation),
    ("ci.deps", "ci", 5, "Dependency updates are automated", "Bots plus agents can keep dependencies current.", "Enable Renovate or Dependabot.", check_dependency_bot),

    ("obs.logging", "observability", 2, "Structured logging", "Agents debug from logs; unstructured logs are hard to query.", "Adopt a structured logger.", check_structured_logging),
    ("obs.tracking", "observability", 3, "Error tracking or tracing", "Agents need production evidence, not theories.", "Add Sentry or OpenTelemetry.", check_error_tracking),
    ("obs.health", "observability", 3, "Health check endpoint", "Agents verify a service is up before testing.", "Expose /health on every service.", check_health_endpoint),
    ("obs.artifacts", "observability", 4, "Tests capture diagnostic artifacts", "Traces and screenshots let agents fix failures without guessing.", "Enable Playwright trace/screenshot on failure.", check_test_artifacts),
    ("obs.runbooks", "observability", 5, "Runbooks for incidents", "Agents can follow a runbook; they cannot follow tribal knowledge.", "Write runbooks for the top incident types.", check_runbooks),

    ("sec.gitignore", "safety", 1, "Secrets are not committed", "Agents read everything in the repo, including leaked keys.", "Ignore .env files and rotate anything committed.", check_gitignore_secrets),
    ("sec.migrations", "safety", 2, "Database migrations are guarded", "Hand-edited or destructive migrations are unrecoverable.", "Document the migration workflow and check it in CI.", check_migration_guardrails),
    ("sec.destructive", "safety", 3, "Destructive operations are forbidden in writing", "Agents will run reset --hard or force-push if nothing says not to.", "List forbidden commands in agent docs.", check_destructive_rules),
    ("sec.permissions", "safety", 4, "Agent permission policy committed", "Allow/deny lists make autonomy safe to grant.", "Commit .claude/settings.json or equivalent with deny rules.", check_agent_permissions),
    ("sec.scanning", "safety", 4, "Secret and code scanning", "Agent-generated code needs the same security gate as human code.", "Add gitleaks/CodeQL to CI.", check_secret_scanning),
    ("sec.policy", "safety", 5, "Security policy published", "Defines disclosure and handling for agent-found issues.", "Add SECURITY.md.", check_security_policy),

    ("spec.contributing", "specs", 2, "Definition of done is written", "Agents stop early or late without a done criterion.", "Write what 'done' means: tests, evidence, PR fields.", check_contributing),
    ("spec.contracts", "specs", 3, "Machine-readable API contracts", "Schemas let agents change both sides of an API safely.", "Publish OpenAPI/Zod/protobuf contracts.", check_api_contracts),
    ("spec.issues", "specs", 3, "Structured issue templates", "Good tasks have acceptance criteria; templates enforce them.", "Add issue templates with acceptance criteria and non-goals.", check_issue_templates),
    ("spec.notes", "specs", 4, "Specs and plans live with the code", "Agents can read plans in the repo; they cannot read Slack.", "Keep PRDs, plans and investigations in a notes/ or docs/ tree.", check_specs_dir),
    ("spec.changelog", "specs", 5, "Changelog maintained", "Release notes show agents what changed recently.", "Maintain CHANGELOG.md or changesets.", check_changelog),

    ("tool.harness", "tooling", 2, "Agent harness config committed", "Shared config means every developer's agent behaves the same.", "Commit .claude/.codex/.cursor config.", check_agent_infra_dirs),
    ("tool.browser", "tooling", 3, "Browser automation available", "UI changes need a real browser to verify.", "Add Playwright or agent-browser.", check_browser_automation),
    ("tool.skills", "tooling", 3, "Reusable skills/commands", "Skills turn tribal workflows into repeatable agent procedures.", "Capture deploy, test and review workflows as skills.", check_skills),
    ("tool.auth", "tooling", 4, "Agents can authenticate to the app", "Login pages are the top blocker for agent UI verification.", "Provide a seeded test user or token path.", check_auth_bypass),
    ("tool.mcp", "tooling", 4, "MCP integrations configured", "Agents need tickets, docs and telemetry, not just code.", "Commit .mcp.json for issue tracker, docs and observability.", check_mcp_config),
    ("tool.hooks", "tooling", 5, "Agent lifecycle hooks", "Hooks enforce format-on-edit and block dangerous commands automatically.", "Add PostToolUse format and PreToolUse guard hooks.", check_agent_hooks),
]


def evaluate(facts: RepoFacts) -> list[Criterion]:
    results: list[Criterion] = []
    for cid, pillar, level, title, why, fix, check in CRITERIA:
        criterion = Criterion(cid, pillar, level, title, why, fix)
        try:
            criterion.passed, criterion.evidence = check(facts)
        except Skip as skip:
            criterion.skipped, criterion.evidence = True, str(skip)
        except Exception as exc:  # a broken check must not hide the other 60
            criterion.passed, criterion.evidence = False, f"Check failed: {exc}"
        results.append(criterion)
    return results


def level_for(criteria: list[Criterion]) -> tuple[int, dict[int, float]]:
    rates: dict[int, float] = {}
    for level in LEVEL_NAMES:
        items = [c for c in criteria if c.level == level and not c.skipped]
        rates[level] = sum(c.passed for c in items) / len(items) if items else 1.0
    achieved = 0
    for level in sorted(LEVEL_NAMES):
        if rates[level] >= PASS_THRESHOLD:
            achieved = level
        else:
            break
    return achieved, rates


def pillar_scores(criteria: list[Criterion]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, name in PILLARS.items():
        items = [c for c in criteria if c.pillar == key and not c.skipped]
        weight = sum(c.level for c in items) or 1
        earned = sum(c.level for c in items if c.passed)
        out[key] = {
            "name": name,
            "score": round(100 * earned / weight),
            "passed": sum(c.passed for c in items),
            "total": len(items),
        }
    return out


def scan_repo(root: Path) -> dict:
    facts = collect_facts(root.resolve())
    criteria = evaluate(facts)
    level, rates = level_for(criteria)
    pillars = pillar_scores(criteria)
    weight = sum(c.level for c in criteria if not c.skipped)
    score = round(100 * sum(c.level for c in criteria if c.passed) / weight)
    next_level = level + 1 if level < 5 else None
    blockers = [asdict(c) for c in criteria if next_level and c.level <= next_level and not c.passed and not c.skipped]
    return {
        "name": root.resolve().name,
        "path": str(root.resolve()),
        "level": level,
        "levelName": LEVEL_NAMES.get(level, "Not ready"),
        "levelMeaning": LEVEL_MEANING[level],
        "levelRates": rates,
        "score": score,
        "pillars": pillars,
        "criteria": [asdict(c) for c in criteria],
        "nextLevelBlockers": blockers,
        "stats": {
            "files": len(facts.files),
            "sourceFiles": len(facts.source_files),
            "testFiles": len(facts.test_files),
            "languages": dict(facts.languages.most_common(6)),
            "repoAgeDays": facts.git_log_days,
            "shallowClone": facts.shallow,
            "commits90d": facts.commit_count_90d,
            "contributors90d": facts.contributors_90d,
            "largestFiles": facts.extras.get("largest_files", []),
            "agentDocs": {k: len(v.splitlines()) for k, v in facts.agent_docs.items()},
        },
    }


if __name__ == "__main__":
    import sys

    print(json.dumps(scan_repo(Path(sys.argv[1] if len(sys.argv) > 1 else ".")), indent=2, default=str))
