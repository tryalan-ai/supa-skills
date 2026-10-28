#!/usr/bin/env python3
"""Behavioral tests for the readiness scan, session metrics, findings and report."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from behavior import aggregate, analyze_events, prompt_origin, set_prompt_extractors
from insights import adherence, build_findings, instruction_reach
from report import render
from repo_scan import scan_repo
from session_events import parse_session_events


def claude_line(role: str, content: object, ts: str = "2026-10-28T10:00:00Z") -> str:
    return json.dumps({"type": role, "timestamp": ts, "cwd": "/work/repo", "message": {"role": role, "content": content}})


def tool_use(tid: str, name: str, args: dict) -> str:
    return claude_line("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": args}])


def tool_result(tid: str, text: str, error: bool = False) -> str:
    return claude_line("user", [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": error}])


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def session(self, name: str, lines: list[str]):
        path = self.root / "claude" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        return analyze_events(name, "local", parse_session_events(path))


class SessionMetricsTest(Fixture):
    def test_read_edit_then_passing_test_is_verified_delivery(self) -> None:
        m = self.session("verified", [
            claude_line("user", "In src/url.ts reject credential URLs and run the url test until it passes"),
            tool_use("1", "Read", {"file_path": "/work/repo/src/url.ts"}),
            tool_result("1", "export const isHttpUrl = ..."),
            tool_use("2", "Edit", {"file_path": "/work/repo/src/url.ts", "old_string": "a", "new_string": "b"}),
            tool_result("2", "ok"),
            tool_use("3", "Bash", {"command": "cd /work/repo && npx vitest run src/url.test.ts"}),
            tool_result("3", "Test Files  1 passed (1)"),
            claude_line("assistant", [{"type": "text", "text": "Done, tests pass."}]),
        ])
        self.assertEqual(m.archetype, "verified_delivery")
        self.assertEqual(m.verification_depth, "test")
        self.assertEqual(m.blind_edits, 0)
        self.assertGreaterEqual(m.prompt_specificity, 3)

    def test_lint_only_after_edit_is_not_verified(self) -> None:
        m = self.session("lint", [
            claude_line("user", "fix the dates"),
            tool_use("1", "Edit", {"file_path": "/work/repo/src/table.tsx", "old_string": "a", "new_string": "b"}),
            tool_result("1", "ok"),
            tool_use("2", "Bash", {"command": "npx biome check src/table.tsx"}),
            tool_result("2", "Checked 1 file"),
        ])
        self.assertEqual(m.verification_depth, "lint")
        self.assertFalse(m.validated_after_last_edit)
        self.assertEqual(m.archetype, "unverified_change")
        self.assertEqual(m.blind_edits, 1, "modified a file never read in the session")

    def test_shell_heredoc_edit_with_test_in_same_command_counts_both(self) -> None:
        m = self.session("heredoc", [
            claude_line("user", "update the parser"),
            tool_use("1", "Bash", {"command": "cat src/p.ts"}),
            tool_result("1", "x"),
            tool_use("2", "Bash", {"command": "python3 - <<'EOF'\np='src/p.ts'\ns=open(p).read()\nopen(p,'w').write(s.replace('a','b'))\nEOF\nnpx vitest run src/p.test.ts"}),
            tool_result("2", "Tests  3 passed"),
        ])
        self.assertEqual(m.category_counts.get("edit"), 1)
        self.assertTrue(m.validated_after_last_edit)
        self.assertEqual(m.blind_edits, 0)

    def test_blocked_report_and_repeated_failures(self) -> None:
        m = self.session("blocked", [
            claude_line("user", "the e2e test is flaky, fix it"),
            tool_use("1", "Bash", {"command": "npm run dx:up"}),
            tool_result("1", "Cannot connect to the Docker daemon. Is the docker daemon running?", True),
            tool_use("2", "Bash", {"command": "npm run dx:up"}),
            tool_result("2", "Cannot connect to the Docker daemon. Is the docker daemon running?", True),
            claude_line("assistant", [{"type": "text", "text": "I couldn't run the test because the database cannot start here."}]),
        ])
        self.assertEqual(m.archetype, "blocked")
        self.assertEqual(m.failure_kinds.get("service_unavailable"), 2)
        self.assertEqual(m.loops[0]["failures"], 2)

    def test_harness_blocks_and_wrappers_are_not_human_prompts(self) -> None:
        self.assertEqual(prompt_origin("<heartbeat><automation_id>x</automation_id></heartbeat>"), "scheduled")
        self.assertEqual(prompt_origin("You are the pull-request review agent for acme/app."), "workflow")
        self.assertEqual(prompt_origin("[Alan] Dana (@dana) is asking you. fix the build"), "chat_relay")
        set_prompt_extractors([r'Developer.s request:\s*"(.+)"'])
        try:
            m = self.session("wrapped", [claude_line("user", '<system-reminder>ctx</system-reminder>You are the coding agent.\nDeveloper\'s request:\n"no that\'s wrong, fix the timezone"')])
        finally:
            set_prompt_extractors([])
        self.assertEqual(m.prompt_origins, {"direct": 1})
        self.assertTrue(m.first_prompt.startswith("no that"))


class PanelRegressionTest(Fixture):
    """Cases the expert panel caught in the first round of review."""

    def test_reasoning_is_not_read_as_the_final_reply(self) -> None:
        m = self.session("thinking", [
            claude_line("user", "fix the parser"),
            claude_line("assistant", [{"type": "thinking", "thinking": "I couldn't run this, I am blocked"}, {"type": "text", "text": "Fixed and tested."}]),
        ])
        self.assertFalse(m.reported_blocked)

    def test_reverted_edits_are_not_a_code_change(self) -> None:
        m = self.session("revert", [
            claude_line("user", "dates look weird"),
            tool_use("1", "Edit", {"file_path": "/work/repo/src/t.tsx", "old_string": "a", "new_string": "b"}),
            tool_result("1", "ok"),
            tool_use("2", "Bash", {"command": "git checkout -- src/t.tsx"}),
            tool_result("2", ""),
            claude_line("assistant", [{"type": "text", "text": "I reverted my edit; nothing is changed."}]),
        ])
        self.assertTrue(m.reverted)
        self.assertEqual(aggregate([m])["verification"]["editSessions"], 0)

    def test_probe_misses_are_not_failures_and_filtered_output_is_not_a_pass(self) -> None:
        m = self.session("probe", [
            claude_line("user", "update a.ts"),
            tool_use("1", "Bash", {"command": "which psql"}),
            tool_result("1", "Exit code: 1", True),
            tool_use("2", "Edit", {"file_path": "/work/repo/a.ts", "old_string": "a", "new_string": "b"}),
            tool_result("2", "ok"),
            tool_use("3", "Bash", {"command": "npx tsc --noEmit | grep a.ts"}),
            tool_result("3", ""),
        ])
        self.assertEqual(m.failed_calls, 0)
        self.assertFalse(m.validated_after_last_edit, "grepped tsc output proves nothing")

    def test_compound_command_registers_reads_and_validation(self) -> None:
        m = self.session("compound", [
            claude_line("user", "change b.ts"),
            tool_use("1", "Bash", {"command": "cd /work/repo && cat src/b.ts && npx vitest run src/b.test.ts"}),
            tool_result("1", "Tests 2 passed"),
        ])
        self.assertEqual(m.category_counts.get("validate"), 1)
        self.assertEqual(m.distinct_files_read, 1)

    def test_side_effects_are_detected_with_disclosure(self) -> None:
        m = self.session("risky", [
            claude_line("user", "make it work against the db"),
            tool_use("1", "Bash", {"command": "docker compose -f docker/compose.yml up -d database"}),
            tool_result("1", "started"),
            tool_use("2", "Bash", {"command": "git commit --amend --no-edit"}),
            tool_result("2", "ok"),
            claude_line("assistant", [{"type": "text", "text": "Done. I started the database with docker."}]),
        ])
        by_kind = {r["kind"]: r for r in m.risks}
        self.assertTrue(by_kind["infrastructure"]["disclosed"])
        self.assertFalse(by_kind["history_rewrite"]["disclosed"])


class RepoAndFindingsTest(Fixture):
    def make_repo(self) -> Path:
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "AGENTS.md").write_text("# Agents\n" + "\n".join(f"- rule {i}" for i in range(25)) + "\n- Run `npm run test -w core` to test.\n- Never mock.\n- Do not run `npm run build`.\n")
        (repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "lint": "biome check ."}}))
        (repo / "src" / "a.ts").write_text("export const a = 1;\n")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        return repo

    def test_scan_reports_evidence_and_gates_levels(self) -> None:
        scan = scan_repo(self.make_repo())
        by_id = {c["id"]: c for c in scan["criteria"]}
        self.assertTrue(by_id["ctx.agent_doc"]["passed"])
        self.assertIn("AGENTS.md", by_id["ctx.agent_doc"]["evidence"])
        self.assertFalse(by_id["env.lockfile"]["passed"])
        self.assertLessEqual(scan["level"], 1, "a repo without lockfile or CI cannot pass L2 gating")

    def test_misspelled_install_hook_and_ignored_agent_config_fail(self) -> None:
        repo = self.make_repo()
        (repo / "packages" / "db").mkdir(parents=True)
        (repo / "packages" / "db" / "schema.prisma").write_text("model A { id Int @id }\n")
        (repo / "packages" / "db" / "package.json").write_text(json.dumps({"scripts": {"post-install": "prisma generate"}}))
        (repo / ".gitignore").write_text("node_modules\nCLAUDE.md\n.claude\n")
        by_id = {c["id"]: c for c in scan_repo(repo)["criteria"]}
        self.assertFalse(by_id["env.codegen"]["passed"])
        self.assertIn("post-install", by_id["env.codegen"]["evidence"])
        self.assertFalse(by_id["ctx.shared"]["passed"])

    def test_claude_sessions_without_claude_md_raise_instruction_reach(self) -> None:
        repo = self.make_repo()
        m = self.session("s", [
            claude_line("user", "add tests for a.ts"),
            tool_use("1", "Write", {"file_path": f"{repo}/src/a.test.ts", "content": "vi.mock('db')\ntest('a', () => {})"}),
            tool_result("1", "ok"),
            tool_use("2", "Bash", {"command": "npx vitest run src/a.test.ts"}),
            tool_result("2", "1 passed"),
        ])
        sessions = [m]
        reach = instruction_reach(repo, sessions)
        self.assertEqual(reach["gaps"][0]["harness"], "Claude Code")
        adhere = adherence(sessions, repo)
        self.assertEqual(adhere["violations"][0]["rule"], "Never mock")
        agg = aggregate(sessions)
        findings = build_findings(scan_repo(repo), agg, sessions, adhere, reach)
        self.assertEqual(findings[0].id, "instruction-reach")
        self.assertEqual(findings[0].severity, "critical")

    def test_report_renders_self_contained_html(self) -> None:
        repo = self.make_repo()
        scan = scan_repo(repo)
        data = {"org": "Acme", "generatedAt": "now", "repos": [scan], "behavior": aggregate([]), "sessions": [], "findings": [],
                "ops": {"score": None}, "practiceLevel": None, "projection": {}}
        page = render(data)
        self.assertIn("Agent Readiness Assessment", page)
        self.assertNotIn("<script src", page)
        self.assertNotIn("<link", page)


if __name__ == "__main__":
    unittest.main()
