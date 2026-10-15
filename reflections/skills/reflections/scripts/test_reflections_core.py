"""Behavioral checks for conservative session verification."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reflections_core import GROK_CONTEXT_END, STANDALONE_CONTEXT_END, TASK_CONTEXT_END, analyze_session, check_exit_reliable, check_kind, check_label, codex_exec_result_status, event_result_status, is_check_command, nested_result_status, result_status, terminal_result_command, visible_user_query
from reflections_report import aggregate, markdown_report
from session_events import NormalizedSessionEvent
from run_core import cohort_files, main as run_main, private_write
from discover import SessionSource, discover_session_files
from reflections_core import local_session_id


class ReflectionsCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "claude" / "session.jsonl"
        self.path.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def scan(self, events: list[dict]) -> dict:
        self.path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return analyze_session(self.path)

    def user(self) -> dict:
        return {"type": "user", "message": {"role": "user", "content": "Fix parser"}}

    def call(self, name: str, command: str, call_id: str) -> dict:
        args = {"file_path": "src/parser.ts"} if name == "Edit" else {"command": command}
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "id": call_id, "input": args}]}}

    def result(self, call_id: str, output: str) -> dict:
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": output}]}}

    def test_later_edit_invalidates_earlier_pass(self) -> None:
        session = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 0"), self.call("Edit", "", "e2")])
        self.assertEqual(session["verification"], "stale")
        self.assertEqual(session["insights"][0]["kind"], "verification_gap")

    def test_formatter_write_after_check_is_unresolved_until_diff_is_known(self) -> None:
        session = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 0"), self.call("Bash", "npx biome check --write src/parser.ts", "f1")])
        self.assertEqual(session["metrics"]["codeEditCalls"], 1)
        self.assertEqual(session["metrics"]["possibleFormatterWriteCalls"], 1)
        self.assertEqual(session["verification"], "unresolved_write")
        self.assertEqual(session["insights"][0]["kind"], "formatter_result_unknown")
        self.assertEqual(aggregate([session])["recommendations"][0]["audience"], "analyzer")
        self.assertEqual(session["spans"][0]["lastCodeEditTarget"], "src/parser.ts")
        no_change = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 0"), self.call("Bash", "npx biome check --write src/parser.ts", "f1"), self.result("f1", "Checked 1 file. No fixes applied.")])
        self.assertEqual(no_change["verification"], "passed")
        self.assertEqual(no_change["metrics"]["confirmedFormatterNoChanges"], 1)
        failed_formatter = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 0"), self.call("Bash", "npx biome check --write src/parser.ts", "f1"), self.result("f1", "Exit code: 1\nNo fixes applied.")])
        self.assertEqual(failed_formatter["verification"], "unresolved_write")
        self.assertEqual(failed_formatter["insights"][0]["kind"], "formatter_failed_after_check")
        aborted_formatter = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 0"), self.call("Bash", "npx biome check --write src/parser.ts 2>&1 | tail -40", "f1"), self.result("f1", "[warn] Linter process terminated abnormally (possibly out of memory)")])
        self.assertEqual(aborted_formatter["verification"], "unresolved_write")
        self.assertEqual(aborted_formatter["insights"][0]["kind"], "formatter_failed_after_check")
        changed = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 0"), self.call("Bash", "npx biome check --write src/parser.ts", "f1"), self.result("f1", "Checked 1 file. Fixed 1 file.")])
        self.assertEqual(changed["verification"], "unresolved_write")
        self.assertEqual(changed["metrics"]["confirmedFormatterChanges"], 1)
        self.assertEqual(changed["insights"][0]["kind"], "formatter_changed_after_check")
        formatter_only = self.scan([self.user(), self.call("Bash", "npx biome check --write src/parser.ts", "f1")])
        self.assertEqual(formatter_only["verification"], "not_applicable")
        search = self.scan([self.user(), self.call("Bash", "rg 'biome check --write' src", "r1")])
        self.assertEqual(search["metrics"]["editCalls"], 0)
        same_call = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "npx biome check --write src/parser.ts && pnpm test", "f1")])
        self.assertEqual(same_call["verification"], "unresolved_write")
        self.assertNotIn("after the last substantive check", same_call["insights"][0]["evidence"])
        captured = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "out=$(npx biome check --write src/parser.ts 2>&1); echo \"$out\"", "f1"), self.result("f1", "Checked 1 file. Fixed 1 file.\nExit code: 0")])
        self.assertEqual(captured["metrics"]["confirmedFormatterChanges"], 1)
        self.assertEqual(captured["verification"], "unresolved_write")

    def test_commit_request_with_code_edits_gets_contextual_advice(self) -> None:
        session = self.scan([{"type": "user", "message": {"role": "user", "content": "Commit the changes"}}, self.call("Edit", "", "e1")])
        self.assertIn("commit request", session["insights"][0]["suggestion"])

    def test_later_goal_turn_qualifies_prior_edit_gap(self) -> None:
        later_user = {"type": "user", "message": {"role": "user", "content": "Is this on current main?"}}
        session = self.scan([self.user(), self.call("Edit", "", "e1"), later_user, self.call("Bash", "git status", "s1")])
        self.assertEqual(session["verification"], "unchecked")
        self.assertEqual(session["lastHumanRequest"], "Is this on current main?")
        self.assertIn("earlier goal", session["insights"][0]["suggestion"])
        self.assertIn("turn 1", session["insights"][0]["evidence"])
        self.assertIn("Last visible request: Is this on current main?", markdown_report(aggregate([session]), [session]))

    def test_timestamp_and_host_completion_prompt_are_not_human_turns(self) -> None:
        timestamp = {"type": "user", "message": {"role": "user", "content": "<timestamp>Saturday, Sep 19, 2026, 9:59 PM (UTC+5:30)</timestamp>"}}
        host = {"type": "user", "message": {"role": "user", "content": "<timestamp>Saturday, Sep 19, 2026, 9:59 PM (UTC+5:30)</timestamp>\n<user_query>Briefly inform the user about the task result and perform any follow-up actions (if needed).</user_query>"}}
        session = self.scan([timestamp, host, self.user()])
        self.assertEqual(session["metrics"]["humanTurns"], 1)

    def test_failed_final_check_is_not_passed(self) -> None:
        session = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 1")])
        self.assertEqual(session["verification"], "failed")
        self.assertIn("caused by this change", session["insights"][0]["suggestion"])
        self.assertEqual(aggregate([session])["passedCheckAfterLastEdit"], 0)
        self.assertEqual(aggregate([session])["recommendations"][0]["recurrence"], "isolated")

    def test_zero_failure_summary_is_observed_without_exit_status(self) -> None:
        self.assertEqual(result_status("PASS (127) FAIL (0)", False), "observed")
        self.assertEqual(result_status("Tests: 24 passed, 0 failed", False), "observed")
        self.assertEqual(result_status("PASS (126) FAIL (1)", False), "unknown")
        self.assertEqual(result_status("Found source text mentioning Exit code: 0", False), "observed")

    def test_captured_zsh_pipeline_status_preserves_check_failure(self) -> None:
        command = 'rtk proxy npx biome check src/parser.ts 2>&1 | tail -5; echo "exit=${pipestatus[1]}"'
        self.assertTrue(check_exit_reliable(command))
        self.assertEqual(result_status("Found 1 error.\nexit=1", False), "failed")
        session = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", command, "b1"), self.result("b1", "Found 1 error.\nexit=1")])
        self.assertEqual(session["verification"], "failed")
        self.assertFalse(check_exit_reliable("rtk proxy npx biome check src/parser.ts 2>&1 | head -40"))
        piped = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "npx biome check src/parser.ts 2>&1 | head -40", "b1"), self.result("b1", "Checked 1 file. No fixes applied.\nFound 1 error.\n\n  × Some errors were emitted while running checks.")])
        self.assertEqual(piped["verification"], "failed")

    def test_release_workflow_watch_is_goal_check(self) -> None:
        command = "GH_HOST=github.com gh run watch 123 --exit-status"
        self.assertTrue(is_check_command(command))
        self.assertEqual(check_kind(command), "release_check")
        session = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", command, "g1"), self.result("g1", "status: success")])
        self.assertEqual(session["verification"], "observed")
        self.assertFalse(any(insight["kind"] == "verification_gap" for insight in session["insights"]))

    def test_composite_check_does_not_hide_test_behind_diff_hygiene(self) -> None:
        command = "pnpm test && git diff --check"
        self.assertEqual(check_kind(command), "test")
        self.assertTrue(check_exit_reliable(command))
        passed_wrapper = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", command, "c1"), self.result("c1", "Exit code: 0")])
        self.assertEqual(passed_wrapper["verification"], "passed")
        failed_wrapper = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", command, "c1"), self.result("c1", "Exit code: 1")])
        self.assertEqual(failed_wrapper["verification"], "unknown")
        self.assertFalse(check_exit_reliable("pnpm test && git diff --check || true"))

    def test_passing_unrelated_check_does_not_clear_failed_test(self) -> None:
        session = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 1"), self.call("Bash", "pnpm lint", "l1"), self.result("l1", "Exit code: 0")])
        self.assertEqual(session["verification"], "unresolved_failure")
        self.assertIn("pnpm test failed", session["insights"][0]["evidence"])
        retried = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 1"), self.call("Bash", "pnpm test", "t2"), self.result("t2", "Exit code: 0")])
        self.assertEqual(retried["verification"], "passed")

    def test_diff_hygiene_does_not_stand_in_for_code_verification(self) -> None:
        hygiene = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "git diff --check", "h1"), self.result("h1", "Exit code: 0")])
        self.assertEqual(hygiene["verification"], "hygiene_only")
        self.assertEqual(aggregate([hygiene])["explicitOutcomeSessions"], 0)
        pending_test = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.call("Bash", "git diff --check", "h1"), self.result("h1", "Exit code: 0")])
        self.assertEqual(pending_test["verification"], "unknown")

    def test_explicit_pass_and_unreported_result(self) -> None:
        passed = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 0")])
        self.assertEqual(passed["verification"], "passed")
        observed = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "all checks complete")])
        self.assertEqual(observed["verification"], "observed")
        self.assertEqual(aggregate([passed, observed])["explicitOutcomeCoverage"], 0.5)

    def test_passing_stdout_without_exit_code_is_only_observed(self) -> None:
        session = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "12 tests passed")])
        self.assertEqual(session["verification"], "observed")

    def test_shell_wrapper_exit_does_not_prove_piped_check_passed(self) -> None:
        piped = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test 2>&1 | tail -20", "t1"), self.result("t1", "Exit code: 0")])
        self.assertEqual(piped["verification"], "observed")
        trailing_echo = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test; echo done", "t1"), self.result("t1", "Exit code: 0")])
        self.assertEqual(trailing_echo["verification"], "observed")
        self.assertTrue(check_exit_reliable("cd apps/web && pnpm test"))
        self.assertTrue(check_exit_reliable('pnpm test 2>&1; echo "EXIT CODE: $?"'))
        self.assertFalse(check_exit_reliable('pnpm test | tail -20; echo "EXIT CODE: $?"'))
        self.assertFalse(check_exit_reliable('pnpm test; pwd; echo "EXIT CODE: $?"'))

    def test_later_report_edit_does_not_invalidate_code_check(self) -> None:
        report = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "id": "r1", "input": {"file_path": "test-output/reports/validation.html"}}]}}
        session = self.scan([self.user(), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 0"), report])
        self.assertEqual(session["metrics"]["editCalls"], 2)
        self.assertEqual(session["metrics"]["codeEditCalls"], 1)
        self.assertEqual(session["verification"], "passed")

    def test_document_only_session_gets_no_code_test_advice(self) -> None:
        document = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "id": "d1", "input": {"file_path": "notes/research.md"}}]}}
        session = self.scan([self.user(), document])
        self.assertEqual(session["verification"], "not_applicable")
        self.assertFalse(any(insight["kind"] == "verification_gap" for insight in session["insights"]))
        report = markdown_report(aggregate([session]), [session])
        self.assertIn("elapsed time to last agent event: unavailable", report)
        self.assertIn("median elapsed time to last agent event unavailable", report)
        self.assertIn("Post-edit check evidence: **not_applicable**", report)

    def test_temporary_probe_and_mdx_document_are_not_code_edits(self) -> None:
        script = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "id": "p1", "input": {"file_path": "/tmp/sz.sh"}}]}}
        document = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "id": "d1", "input": {"file_path": "apps/docs/content/guide.mdx"}}]}}
        session = self.scan([self.user(), script, document])
        self.assertEqual(session["metrics"]["codeEditCalls"], 0)
        self.assertEqual(session["verification"], "not_applicable")

    def test_background_watch_script_is_not_a_product_code_edit(self) -> None:
        watch = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "id": "w1", "input": {"file_path": "~/.grok/long-running-background-tasks/watch_fix.sh"}}]}}
        session = self.scan([{"type": "user", "message": {"role": "user", "content": "READ-ONLY observe the running task"}}, watch])
        self.assertEqual(session["metrics"]["codeEditCalls"], 0)
        self.assertFalse(any(insight["kind"] == "read_only_code_edit" for insight in session["insights"]))

    def test_read_only_session_gets_no_implementation_advice(self) -> None:
        session = self.scan([self.user(), self.call("Bash", "rg parser src", "r1"), self.result("r1", "src/parser.ts")])
        self.assertEqual(session["verification"], "not_applicable")
        self.assertEqual(session["insights"], [])

    def test_explicit_read_only_goal_ignores_temporary_script_edit(self) -> None:
        session = self.scan([
            {"type": "user", "message": {"role": "user", "content": "READ-ONLY research task; do not edit files"}},
            self.call("Bash", "cat > /tmp/probe.py <<'EOF'\nprint(1)\nEOF", "e1"),
        ])
        self.assertEqual(session["verification"], "not_applicable")
        self.assertEqual(session["insights"], [])

    def test_later_implementation_turn_supersedes_initial_read_only_goal(self) -> None:
        session = self.scan([
            {"type": "user", "message": {"role": "user", "content": "READ-ONLY inspect parser"}},
            {"type": "user", "message": {"role": "user", "content": "Fix parser now"}},
            self.call("Edit", "", "e1"),
        ])
        self.assertEqual(session["verification"], "unchecked")
        self.assertEqual(session["goalMode"], "unspecified")

    def test_code_edit_during_read_only_instruction_is_flagged(self) -> None:
        session = self.scan([{"type": "user", "message": {"role": "user", "content": "READ-ONLY inspect parser"}}, self.call("Edit", "", "e1")])
        self.assertEqual(session["verification"], "not_applicable")
        self.assertEqual(session["insights"][0]["kind"], "read_only_code_edit")

    def test_read_only_edit_remains_visible_after_later_authorized_edit(self) -> None:
        session = self.scan([
            {"type": "user", "message": {"role": "user", "content": "READ-ONLY inspect parser"}},
            self.call("Edit", "", "e1"),
            {"type": "user", "message": {"role": "user", "content": "Fix parser now"}},
            self.call("Edit", "", "e2"),
        ])
        self.assertEqual(session["metrics"]["readOnlyCodeEditCalls"], 1)
        self.assertTrue(any(insight["kind"] == "read_only_code_edit" for insight in session["insights"]))

    def test_paths_and_git_add_do_not_count_as_checks(self) -> None:
        self.assertFalse(is_check_command("git add src/parser.test.ts && git commit -m fix"))
        self.assertFalse(is_check_command("ln -snf /repo/node_modules/.bin/vitest /tmp/vitest"))
        self.assertFalse(is_check_command("npx biome check --write src/parser.ts"))
        self.assertTrue(is_check_command("cd apps/web && pnpm exec vitest run src/parser.test.ts"))
        self.assertTrue(is_check_command("pnpm exec biome check src/parser.ts"))
        self.assertTrue(is_check_command("python3 scripts/test_reflections_core.py"))
        self.assertTrue(is_check_command("cd apps/api && TEST_DATABASE_URL=postgresql://local npx vitest run test/parser.test.ts"))
        self.assertTrue(is_check_command("npx @biomejs/biome check packages/alan-tui"))
        self.assertTrue(is_check_command("rtk test npx vitest run test/unit.test.ts"))
        self.assertTrue(is_check_command("rtk tsc --noEmit"))
        self.assertTrue(is_check_command("../../node_modules/.bin/vitest run src/parser.test.ts"))
        self.assertTrue(is_check_command("../../node_modules/.bin/tsc --noEmit"))
        self.assertTrue(is_check_command("TEST_DATABASE_URL='postgres://local' node node_modules/vitest/vitest.mjs run test/parser.test.ts"))
        self.assertTrue(is_check_command("bash scripts/services/pc-stack-guard.test.sh; echo exit=$?"))
        self.assertTrue(is_check_command("bash -n scripts/ci-build-workspace-packages.sh && sh -n scripts/bootstrap.sh"))
        self.assertTrue(is_check_command("npx biome check --write src/parser.ts && pnpm exec vitest run test/parser.test.ts"))
        self.assertTrue(is_check_command("cd /repo && ALAN_WV_RESET=1 bash scripts/infisical-run-dev.sh -- bun packages/harness/scripts/eval-webvoyager-jev-browser.ts | tee report.log"))
        self.assertTrue(is_check_command("ruby -ryaml -e 'abort unless YAML.load_file(\"workflow.yml\")'"))
        self.assertFalse(is_check_command("ruby -ryaml -e 'puts YAML.load_file(\"workflow.yml\")'"))
        self.assertTrue(is_check_command("bash ../scripts/infisical-run-dev.sh -- pnpm exec tsx verify-vm.ts vm-id"))
        self.assertTrue(is_check_command("bash scripts/infisical-run-dev.sh -- bun packages/harness/scripts/smoke-browser-suite.ts"))
        self.assertTrue(is_check_command("PGPASSWORD=local psql -h 127.0.0.1 -v ON_ERROR_STOP=1 -c 'SELECT 1'"))
        self.assertTrue(is_check_command("DATABASE_URL=local rtk proxy pnpm db:migrate 2>&1 | tail -40"))
        self.assertTrue(is_check_command("test ! -e apps/web/src/pages/session-first"))
        self.assertEqual(check_kind("npx biome check --write src/parser.ts && pnpm exec vitest run test/parser.test.ts"), "test")
        self.assertFalse(is_check_command("python3 - <<'PY'\nprint('npx vitest run tests')\nPY"))
        self.assertFalse(is_check_command('python3 -c "print(\'pnpm test; npx vitest run\')"'))
        self.assertFalse(is_check_command('rg "test|lint|pnpm test" src'))
        self.assertEqual(check_kind("python3 - <<'PY'\nprint('pnpm test')\nPY\npnpm exec vitest run src/parser.test.ts"), "test")

    def test_check_kind_distinguishes_behavior_tests_from_hygiene(self) -> None:
        self.assertEqual(check_kind("pnpm exec vitest run src/parser.test.ts"), "test")
        self.assertEqual(check_kind("pnpm exec tsc --noEmit"), "type_check")
        self.assertEqual(check_kind("pnpm exec biome check src/parser.ts"), "lint")
        self.assertEqual(check_kind("git diff --check"), "diff_hygiene")
        self.assertEqual(check_kind("pnpm exec tsx verify-vm.ts vm-id"), "runtime_smoke")
        self.assertEqual(check_label("ruby -ryaml -e 'abort unless true'"), "ruby YAML assertion")
        self.assertEqual(check_kind("ruby -ryaml -e 'abort unless true'"), "runtime_smoke")
        self.assertEqual(check_kind("pnpm db:migrate"), "migration_run")
        self.assertEqual(check_kind("test ! -e path"), "file_assertion")

    def test_single_codex_nested_command_uses_its_exit_code(self) -> None:
        self.path = self.path.parent / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        call = {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "x1", "arguments": 'const r=await tools.exec_command({cmd:"pnpm test"});text(r)'}}
        output = json.dumps([{"type": "input_text", "text": "Script completed"}, {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": "Tests passed"})}])
        result = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "x1", "output": output}}
        edit = {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "call_id": "e1", "arguments": "*** Begin Patch\n*** Update File: src/parser.ts\n*** End Patch"}}
        session = self.scan([edit, call, result])
        self.assertEqual(session["verification"], "passed")

    def test_codex_output_prose_does_not_fake_a_failed_tool(self) -> None:
        block = json.dumps([{"type": "input_text", "text": "Script completed"}, {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": "source contains Error and failed"})}])
        self.assertEqual(nested_result_status(block), "passed")
        prose = json.dumps([{"type": "input_text", "text": "Script completed"}, {"type": "input_text", "text": "source contains Error and failed"}])
        self.assertEqual(codex_exec_result_status(prose, 1), "observed")
        script_error = json.dumps([{"type": "input_text", "text": "Script failed with an exception"}])
        self.assertEqual(nested_result_status(script_error), "failed")
        self.assertEqual(codex_exec_result_status(script_error, 1), "failed")

    def test_codex_parallel_shell_results_require_all_exit_codes(self) -> None:
        passing = json.dumps([{"type": "input_text", "text": json.dumps({"i": i, "result": {"status": "fulfilled", "value": {"exit_code": 0}}})} for i in range(2)])
        failing = json.dumps([{"type": "input_text", "text": json.dumps({"i": i, "result": {"status": "fulfilled", "value": {"exit_code": i}}})} for i in range(2)])
        partial = json.dumps([{"type": "input_text", "text": json.dumps({"i": 0, "result": {"status": "fulfilled", "value": {"exit_code": 0}}})}])
        self.assertEqual(codex_exec_result_status(passing, 2), "passed")
        self.assertEqual(codex_exec_result_status(partial, 2), "observed")
        self.assertEqual(codex_exec_result_status(failing, 2), "failed")

    def test_empty_search_result_is_not_tool_friction(self) -> None:
        empty = json.dumps([{"type": "input_text", "text": json.dumps({"exit_code": 1, "output": ""})}])
        broken = json.dumps([{"type": "input_text", "text": json.dumps({"exit_code": 2, "output": "rg: missing path"})}])
        self.assertEqual(nested_result_status(empty, command="rtk proxy rg pattern src"), "observed")
        self.assertEqual(nested_result_status(broken, command="rtk proxy rg pattern missing"), "failed")
        parallel = json.dumps([{"type": "input_text", "text": json.dumps({"i": 0, "status": "fulfilled", "value": {"exit_code": 1, "output": ""}})}])
        self.assertEqual(codex_exec_result_status(parallel, 1, ["rg missing src"]), "observed")

    def test_parallel_codex_shell_calls_without_results_stay_unknown(self) -> None:
        self.path = self.path.parent / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        edit = {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "call_id": "e1", "arguments": "*** Begin Patch\n*** Update File: src/parser.ts\n*** End Patch"}}
        call = {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "x1", "arguments": 'const a=tools.exec_command({cmd:"pnpm test"});const b=tools.exec_command({cmd:"pnpm lint"});text(a,b)'}}
        session = self.scan([edit, call])
        self.assertEqual(session["verification"], "unknown")
        self.assertEqual(session["metrics"]["validationCalls"], 2)

    def test_parallel_codex_shell_results_are_attributed_per_command(self) -> None:
        self.path = self.path.parent / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        edit = {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "call_id": "e1", "arguments": "*** Begin Patch\n*** Update File: src/parser.ts\n*** End Patch"}}
        call = {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "x1", "arguments": 'const r=await Promise.allSettled([tools.exec_command({cmd:"pnpm test"}),tools.exec_command({cmd:"pnpm lint"})]);r.forEach(text)'}}
        output = json.dumps([{"type": "input_text", "text": json.dumps({"i": i, "result": {"status": "fulfilled", "value": {"exit_code": 0, "output": "ok"}}})} for i in range(2)])
        result = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "x1", "output": output}}
        session = self.scan([edit, call, result])
        self.assertEqual(session["metrics"]["validationCalls"], 2)
        self.assertEqual(session["verification"], "passed")
        self.assertEqual([check["status"] for check in session["checksAfterLastEdit"]], ["passed", "passed"])

    def test_parallel_codex_exit_markers_require_matching_source_and_each_result(self) -> None:
        self.path = self.path.parent / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        edit = {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "call_id": "e1", "arguments": "*** Begin Patch\n*** Update File: src/parser.ts\n*** End Patch"}}
        source = 'const r=await Promise.all([tools.exec_command({cmd:"pnpm test"}),tools.exec_command({cmd:"pnpm lint"})]);r.forEach((r, i) => text(`--- result ${i + 1} exit=${r.exit_code} ---`))'
        call = {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "x1", "arguments": source}}
        output = json.dumps([{"type": "input_text", "text": "--- result 1 exit=0 ---"}, {"type": "input_text", "text": "--- result 2 exit=undefined ---"}])
        result = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "x1", "output": output}}
        partial = self.scan([edit, call, result])
        self.assertEqual([check["status"] for check in partial["checksAfterLastEdit"]], ["passed", "unknown"])
        self.assertEqual(partial["verification"], "unknown")
        output = json.dumps([{"type": "input_text", "text": "--- result 1 exit=0 ---"}, {"type": "input_text", "text": "--- result 2 exit=0 ---"}])
        result["payload"]["output"] = output
        self.assertEqual(self.scan([edit, call, result])["verification"], "passed")
        call["payload"]["arguments"] = source.replace("${r.exit_code}", "${r.output}")
        self.assertEqual(self.scan([edit, call, result])["verification"], "unknown")

    def test_earlier_codex_orchestration_does_not_hide_later_unchecked_code_edit(self) -> None:
        self.path = self.path.parent / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        call = {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "x1", "arguments": 'const a=tools.exec_command({cmd:"pnpm test"});const b=tools.exec_command({cmd:"pnpm lint"});text(a,b)'}}
        patch = {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "call_id": "e1", "arguments": "*** Begin Patch\n*** Update File: src/parser.ts\n*** End Patch"}}
        session = self.scan([call, patch])
        self.assertEqual(session["verification"], "stale")

    def test_codex_single_command_with_inline_exit_is_attributed(self) -> None:
        self.path = self.path.parent / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        edit = {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "call_id": "e1", "arguments": "*** Begin Patch\n*** Update File: src/parser.ts\n*** End Patch"}}
        call = {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "x1", "arguments": 'const r=await tools.exec_command({"cmd":"pnpm test"});text(r.output);text(`EXIT ${r.exit_code}`)'}}
        output = json.dumps([{"type": "input_text", "text": "Script completed"}, {"type": "input_text", "text": "Tests 3 passed"}, {"type": "input_text", "text": "EXIT 0"}])
        result = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "x1", "output": output}}
        session = self.scan([edit, call, result])
        self.assertEqual(session["verification"], "passed")
        call["payload"]["arguments"] = 'const r=await tools.exec_command({"cmd":"pnpm test"});text(r.output)'
        untrusted = self.scan([edit, call, result])
        self.assertEqual(untrusted["verification"], "unknown")

    def test_codex_js_patch_finds_code_target_after_document_target(self) -> None:
        self.path = self.path.parent / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        patch = "*** Begin Patch\n*** Add File: notes/plan.md\n+Plan\n*** Update File: src/parser.ts\n+code\n*** End Patch"
        call = {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "e1", "arguments": f"const patch={json.dumps(patch)};await tools.apply_patch(patch)"}}
        session = self.scan([call])
        self.assertEqual(session["metrics"]["codeEditCalls"], 1)
        self.assertEqual(session["spans"], [])

    def test_grok_context_wrapper_preserves_real_user_request(self) -> None:
        wrapped = f"You are Alan, the software factory agent.\n\n{GROK_CONTEXT_END}\n\nFix the deploy\n\n<alan-selected-skill>skill metadata</alan-selected-skill>"
        self.assertEqual(visible_user_query(wrapped), "Fix the deploy")
        self.assertEqual(visible_user_query(f"You are Alan, the software factory agent.\n\n{GROK_CONTEXT_END}\n\n<alan-selected-skill>only metadata</alan-selected-skill>"), "")
        self.assertEqual(visible_user_query(f"## Alan platform tools\n\n{GROK_CONTEXT_END}\n\nContinue the fix"), "Continue the fix")
        self.assertEqual(visible_user_query(f"## Memory (non-code context)\n\n{GROK_CONTEXT_END}\n\nFix the white screen"), "Fix the white screen")
        self.assertEqual(visible_user_query(f"You are Alan, the software factory agent.\n\n{GROK_CONTEXT_END}\n\n# Current User\n- Name: Prasad\n\n{TASK_CONTEXT_END}\n\nFix the UI"), "Fix the UI")
        self.assertEqual(visible_user_query(f"# Current User\n- Name: Prasad\n\n{STANDALONE_CONTEXT_END}\n\nFind the missing chat"), "Find the missing chat")
        self.assertEqual(visible_user_query("<resume_context>Old task history</resume_context>\n\nCheck the logs"), "Check the logs")
        self.assertEqual(visible_user_query("# AGENTS.md instructions for /repo\n<INSTRUCTIONS>rules</INSTRUCTIONS>\n<environment_context>host</environment_context>"), "")
        self.assertEqual(visible_user_query("# AGENTS.md instructions for /repo\n<INSTRUCTIONS>rules</INSTRUCTIONS>\n<environment_context>host</environment_context>\nFix the bug"), "Fix the bug")
        image_and_context = f'<image name=[Image #1] path="/tmp/screenshot.png">\n</image>\n## Alan platform tools\n{GROK_CONTEXT_END}\nWhat does Cursor do?\n<relevantContext>injected evidence</relevantContext>'
        self.assertEqual(visible_user_query(image_and_context), "What does Cursor do?")
        missing_image = f'<image name=[Image #1] path="/tmp/screenshot.png">\n</image>\nCodex could not read the local image at `-`: No such file or directory (os error 2)\nYou are Alan, the software factory agent.\n{GROK_CONTEXT_END}\nFind the request loop'
        self.assertEqual(visible_user_query(missing_image), "Find the request loop")
        self.assertEqual(visible_user_query('<image name=[Image #1] path="/tmp/screenshot.png">\n</image>'), "[attached image]")

    def test_completed_grok_read_containing_traceback_is_not_failed_tool(self) -> None:
        event = NormalizedSessionEvent(kind="tool_result", text="80→Traceback from an example file", failed=True, raw={"params": {"update": {"status": "completed"}}})
        self.assertNotEqual(event_result_status(event, "grok", "read_file"), "failed")
        event.raw["params"]["update"]["status"] = "failed"
        self.assertEqual(event_result_status(event, "grok", "read_file"), "failed")

    def test_source_text_exit_code_is_not_read_tool_status(self) -> None:
        grok = NormalizedSessionEvent(kind="tool_result", text="source says Exit code: 1", failed=True, raw={"params": {"update": {"status": "completed"}}})
        self.assertEqual(event_result_status(grok, "grok", "read_file"), "observed")
        claude = NormalizedSessionEvent(kind="tool_result", text="source says Exit code: 1", failed=True, tool_call_id="r1", raw={"message": {"content": [{"type": "tool_result", "tool_use_id": "r1", "is_error": False}]}})
        self.assertEqual(event_result_status(claude, "claude", "Read"), "observed")

    def test_grok_shell_uses_structured_exit_code(self) -> None:
        event = NormalizedSessionEvent(kind="tool_result", text="12 tests passed", raw={"params": {"update": {"status": "completed", "rawOutput": {"type": "Bash", "command": "pnpm test", "exit_code": 0, "output": [111, 107]}}}})
        self.assertEqual(event_result_status(event, "grok", "run_terminal_command"), "passed")
        event.raw["params"]["update"]["rawOutput"]["exit_code"] = 1
        self.assertEqual(event_result_status(event, "grok", "run_terminal_command"), "failed")
        event.raw["params"]["update"]["rawOutput"].update({"command": "rg missing src", "output": []})
        self.assertEqual(event_result_status(event, "grok", "run_terminal_command"), "observed")
        event.raw["params"]["update"]["rawOutput"].update({"command": "pnpm test", "exit_code": 0, "timed_out": True})
        self.assertEqual(event_result_status(event, "grok", "run_terminal_command"), "failed")

    def test_codex_wait_timeout_is_expected_control_flow(self) -> None:
        event = NormalizedSessionEvent(kind="tool_result", text='{"message":"Wait timed out.","timed_out":true}', failed=True)
        self.assertEqual(event_result_status(event, "codex", "wait_agent"), "observed")

    def test_repeated_failure_requires_no_progress_between_attempts(self) -> None:
        repeated = self.scan([self.user(), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 1"), self.call("Bash", "pnpm test", "t2"), self.result("t2", "Exit code: 1")])
        self.assertEqual(repeated["metrics"]["repeatIdenticalFailuresWithinTurn"], 1)
        self.assertIn("repeat_failure", [item["kind"] for item in repeated["insights"]])
        fixed = self.scan([self.user(), self.call("Bash", "pnpm test", "t1"), self.result("t1", "Exit code: 1"), self.call("Edit", "", "e1"), self.call("Bash", "pnpm test", "t2"), self.result("t2", "Exit code: 1")])
        self.assertEqual(fixed["metrics"]["repeatIdenticalFailuresWithinTurn"], 0)

    def test_distinct_codex_batches_with_quoted_searches_do_not_repeat(self) -> None:
        self.path = Path(self.tmp.name) / ".codex" / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        self.path.parent.mkdir(parents=True)
        calls = []
        for number, pattern in enumerate(("first", "second"), 1):
            script = f'const r=await Promise.allSettled([tools.exec_command({{cmd:"rg -n \'{pattern}\' src"}}),tools.exec_command({{cmd:"pwd"}})]);r.forEach(text)'
            calls.append({"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": f"x{number}", "arguments": script}})
            output = json.dumps([{"type": "input_text", "text": json.dumps({"i": 0, "result": {"status": "fulfilled", "value": {"exit_code": 2, "output": "rg error"}}})}])
            calls.append({"type": "response_item", "payload": {"type": "function_call_output", "call_id": f"x{number}", "output": output}})
        session = self.scan([self.user(), *calls])
        self.assertEqual(session["metrics"]["failedToolCalls"], 2)
        self.assertEqual(session["metrics"]["repeatIdenticalFailuresWithinTurn"], 0)

    def test_codex_replayed_user_events_are_one_turn(self) -> None:
        self.path = self.path.parent / "rollout-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        first = {"type": "response_item", "timestamp": "2026-08-10T21:27:33.224Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix parser"}]}}
        replay = {"type": "event_msg", "timestamp": "2026-08-10T21:27:33.225Z", "payload": {"type": "user_message", "message": "Fix parser"}}
        tool = {"type": "response_item", "timestamp": "2026-08-10T21:27:34.000Z", "payload": {"type": "function_call", "name": "shell_command", "call_id": "r1", "arguments": json.dumps({"command": "rg parser src"})}}
        followup = {"type": "response_item", "timestamp": "2026-08-10T21:30:00.000Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Continue"}]}}
        session = self.scan([first, replay, tool, followup])
        self.assertEqual(session["metrics"]["humanTurns"], 2)
        self.assertEqual(session["metrics"]["maxToolsBetweenHumanTurns"], 1)

    def test_tools_without_visible_user_instruction_remain_unattributed(self) -> None:
        session = self.scan([self.call("Bash", "rg parser src", "r1"), self.user(), self.call("Bash", "pnpm test", "t1")])
        self.assertEqual(session["metrics"]["toolCalls"], 2)
        self.assertEqual(session["metrics"]["unattributedToolCalls"], 1)
        self.assertEqual(session["metrics"]["toolsPerHumanTurn"], 1.0)

    def test_orphan_failed_result_is_not_a_failed_tool_call(self) -> None:
        session = self.scan([self.user(), self.result("missing-call", "Exit code: 1")])
        self.assertEqual(session["metrics"]["toolCalls"], 0)
        self.assertEqual(session["metrics"]["failedToolCalls"], 0)
        self.assertEqual(session["metrics"]["unlinkedFailedResults"], 1)

    def test_cohort_manifest_reuses_exact_paths(self) -> None:
        manifest = self.path.parent / "cohort.json"
        self.path.write_text("{}\n")
        files, expected = cohort_files(manifest, [self.path])
        self.assertEqual(files, [self.path])
        self.assertIn(str(self.path), expected)
        other = self.path.parent / "other.jsonl"
        other.write_text("{}\n")
        frozen, _ = cohort_files(manifest, [other])
        self.assertEqual(frozen, [self.path])
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)

    def test_report_write_replaces_existing_file_with_private_permissions(self) -> None:
        report = self.path.parent / "report.md"
        report.write_text("old")
        report.chmod(0o644)
        private_write(report, "new")
        self.assertEqual(report.read_text(), "new")
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)

    def test_changed_manifest_input_fails_instead_of_silent_partial_report(self) -> None:
        manifest = self.path.parent / "cohort.json"
        self.path.write_text(json.dumps(self.user()) + "\n")
        cohort_files(manifest, [self.path])
        self.path.write_text(json.dumps(self.user()) + "\n" + json.dumps(self.user()) + "\n")
        args = ["run_core.py", "--manifest", str(manifest), "--output", str(self.path.parent / "report.md"), "--json-output", str(self.path.parent / "report.json")]
        with patch("sys.argv", args):
            self.assertEqual(run_main(), 1)
        self.assertEqual(json.loads((self.path.parent / "report.json").read_text())["aggregate"]["unstableFiles"], [str(self.path)])

    def test_malformed_jsonl_is_reported_as_incomplete(self) -> None:
        self.path.write_text(json.dumps(self.user()) + "\n{bad json\n")
        manifest = self.path.parent / "cohort.json"
        cohort_files(manifest, [self.path])
        args = ["run_core.py", "--manifest", str(manifest), "--output", str(self.path.parent / "report.md"), "--json-output", str(self.path.parent / "report.json")]
        with patch("sys.argv", args):
            self.assertEqual(run_main(), 1)
        report = json.loads((self.path.parent / "report.json").read_text())
        self.assertEqual(report["aggregate"]["sessionCount"], 0)
        self.assertIn("Invalid JSONL at line 2", report["aggregate"]["skippedFiles"][0]["error"])

    def test_invalid_utf8_is_reported_as_incomplete(self) -> None:
        self.path.write_bytes(b'{"type":"user","content":"\xff"}\n')
        manifest = self.path.parent / "cohort.json"
        cohort_files(manifest, [self.path])
        args = ["run_core.py", "--manifest", str(manifest), "--output", str(self.path.parent / "report.md"), "--json-output", str(self.path.parent / "report.json")]
        with patch("sys.argv", args):
            self.assertEqual(run_main(), 1)
        report = json.loads((self.path.parent / "report.json").read_text())
        self.assertEqual(report["aggregate"]["sessionCount"], 0)
        self.assertIn("Invalid UTF-8", report["aggregate"]["skippedFiles"][0]["error"])

    def test_cursor_terminal_sidecar_is_frozen_with_manifest(self) -> None:
        self.path = Path(self.tmp.name) / "project" / "agent-transcripts" / "session.jsonl"
        self.path.parent.mkdir(parents=True)
        terminal = self.path.parents[1] / "terminals" / "4242.txt"
        terminal.parent.mkdir(parents=True)
        terminal.write_text('---\ncommand: "pnpm test"\nstatus: succeeded\n---\nexit_code: 0\n')
        self.path.write_text("\n".join(json.dumps(event) for event in [
            {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {"command": "pnpm test"}}]}},
            {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "AwaitShell", "input": {"shell_id": "4242"}}]}},
        ]) + "\n")
        manifest = self.path.parent / "cohort.json"
        cohort_files(manifest, [self.path])
        args = ["run_core.py", "--manifest", str(manifest), "--output", str(self.path.parent / "report.md"), "--json-output", str(self.path.parent / "report.json")]
        with patch("sys.argv", args):
            self.assertEqual(run_main(), 0)
        frozen = json.loads(manifest.read_text())
        self.assertEqual(frozen["version"], 2)
        self.assertEqual(len(frozen["sidecars"][str(self.path)]), 1)
        terminal.write_text('---\ncommand: "pnpm test"\nstatus: failed\n---\nexit_code: 1\n')
        with patch("sys.argv", args):
            self.assertEqual(run_main(), 1)
        self.assertEqual(len(json.loads((self.path.parent / "report.json").read_text())["aggregate"]["unstableFiles"]), 1)

    def test_primary_cohort_excludes_claude_subagents(self) -> None:
        root = self.path.parent
        parent = root / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        child = root / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" / "subagents" / "agent-child.jsonl"
        parent.write_text("{}\n")
        child.parent.mkdir(parents=True)
        child.write_text("{}\n")
        source = (SessionSource("claude", root, "**/*.jsonl"),)
        self.assertEqual(discover_session_files(sources=source, include_subagents=False), [parent])
        self.assertNotEqual(local_session_id(parent), local_session_id(child))

    def test_turn_spans_keep_later_validation_separate(self) -> None:
        session = self.scan([
            self.user(),
            self.call("Edit", "", "e1"),
            {"type": "user", "message": {"role": "user", "content": "Continue and test"}},
            self.call("Bash", "pnpm test", "t1"),
            self.result("t1", "Exit code: 0"),
        ])
        self.assertEqual(session["verification"], "passed")
        self.assertEqual(session["spans"][0]["verification"], "unchecked")
        self.assertEqual(session["spans"][1]["verification"], "not_applicable")
        self.assertEqual(aggregate([session])["editSpanVerificationCounts"], {"unchecked": 1})

    def test_late_result_updates_span_where_check_was_called(self) -> None:
        session = self.scan([
            self.user(),
            self.call("Edit", "", "e1"),
            self.call("Bash", "pnpm test", "t1"),
            {"type": "user", "message": {"role": "user", "content": "Is it done?"}},
            self.result("t1", "Exit code: 0"),
        ])
        self.assertEqual(session["spans"][0]["verification"], "passed")

    def test_span_duration_stops_at_last_agent_activity_not_next_human(self) -> None:
        first = {"type": "user", "timestamp": "2026-09-01T00:00:00Z", "message": {"role": "user", "content": "Inspect parser"}}
        tool = self.call("Bash", "rg parser src", "r1")
        tool["timestamp"] = "2026-09-01T00:00:30Z"
        next_user = {"type": "user", "timestamp": "2026-09-01T00:05:00Z", "message": {"role": "user", "content": "Continue"}}
        session = self.scan([first, tool, next_user])
        self.assertEqual(session["spans"][0]["elapsedToLastAgentEventSeconds"], 30.0)
        self.assertIsNone(session["spans"][1]["elapsedToLastAgentEventSeconds"])

    def test_cursor_terminal_result_matches_unique_command(self) -> None:
        self.path = Path(self.tmp.name) / "project" / "agent-transcripts" / "session.jsonl"
        self.path.parent.mkdir(parents=True)
        terminal = self.path.parents[1] / "terminals" / "4242.txt"
        terminal.parent.mkdir(parents=True)
        terminal.write_text('---\ncommand: "pnpm test"\nstatus: succeeded\n---\nTests passed\n---\nexit_code: 0\n---\n')
        session = self.scan([
            {"role": "user", "message": {"content": [{"type": "text", "text": "Fix parser"}]}},
            {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/parser.ts"}}]}},
            {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {"command": "pnpm test"}}]}},
            {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "AwaitShell", "input": {"shell_id": "4242"}}]}},
        ])
        self.assertEqual(terminal_result_command(terminal.read_text()), "pnpm test")
        self.assertEqual(result_status(terminal.read_text(), failed=True), "passed")
        self.assertEqual(session["verification"], "passed")
        terminal.write_text('---\ncommand: "pnpm lint"\nstatus: failed\n---\n---\nexit_code: 1\n---\n')
        mismatch = analyze_session(self.path)
        self.assertEqual(mismatch["verification"], "unknown")
        self.assertEqual(mismatch["metrics"]["unmatchedTerminalResults"], 1)
        self.assertEqual(mismatch["metrics"]["extraneousTerminalResults"], 1)
        self.assertEqual(mismatch["metrics"]["failedToolCalls"], 0)
        self.assertIn("unknown_check_result", [insight["kind"] for insight in mismatch["insights"]])

    def test_missing_cursor_terminal_directory_is_analyzer_gap(self) -> None:
        self.path = Path(self.tmp.name) / "project" / "agent-transcripts" / "session.jsonl"
        self.path.parent.mkdir(parents=True)
        session = self.scan([
            {"role": "user", "message": {"content": [{"type": "text", "text": "Fix parser"}]}},
            {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/parser.ts"}}]}},
            {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {"command": "pnpm test"}}]}},
        ])
        self.assertEqual(session["verification"], "unknown")
        self.assertIs(session["metrics"]["cursorTerminalDirectoryAvailable"], False)
        self.assertIn("missing_terminal_sidecar", [insight["kind"] for insight in session["insights"]])
        self.assertEqual(aggregate([session])["recommendations"][0]["audience"], "analyzer")


if __name__ == "__main__":
    unittest.main()
