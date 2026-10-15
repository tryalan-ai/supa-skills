#!/usr/bin/env python3
"""Focused tests for agent-readiness multi-CLI session normalization."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analyze_sessions import parse_session
from render_html import html_report
from run_reflections import build_suggestions, suggestion_kind_for_pattern
from session_events import cursor_terminal_sources, parse_session_events


def assistant_texts(path: Path) -> list[str]:
    return [event.text for event in parse_session_events(path, "auto") if event.kind == "assistant_message"]


class SessionEventNormalizationTest(unittest.TestCase):
    def write_jsonl(self, relative_path: str, lines: list[str]) -> Path:
        path = self.tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_auto_detects_claude_tool_events(self) -> None:
        path = self.write_jsonl(
            "claude/session.jsonl",
            [
                '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Fix parser"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"shell_command","input":{"command":"pnpm test"}}]}}',
                '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"Exit code: 0"}]}}',
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Changed files\\nValidation"}]}}',
            ],
        )

        events = parse_session_events(path, "auto")
        stats = parse_session(path, session_format="auto")

        self.assertEqual([event.kind for event in events].count("tool_call"), 1)
        self.assertEqual(stats.tool_calls, 1)
        self.assertEqual(stats.validation_calls, 1)
        self.assertEqual(stats.final_report_markers, 2)

    def test_auto_detects_provider_and_generic_tool_events(self) -> None:
        cases = {
            "gemini/session.jsonl": [
                '{"type":"message","role":"user","content":"Implement adapter"}',
                '{"type":"tool_use","tool_name":"shell_command","tool_id":"g1","parameters":{"command":"pytest"}}',
                '{"type":"tool_result","tool_id":"g1","output":"Exit code: 0"}',
                '{"type":"message","role":"assistant","content":"Changed files\\nValidation"}',
            ],
            "kimi/session.jsonl": [
                '{"role":"user","content":[{"type":"text","text":"Wire Kimi"}]}',
                '{"role":"assistant","tool_calls":[{"id":"k1","function":{"name":"shell_command","arguments":"{\\"command\\":\\"go test ./...\\"}"}}]}',
                '{"role":"tool","tool_call_id":"k1","content":[{"type":"text","text":"Exit code: 0"}]}',
                '{"role":"assistant","content":[{"type":"text","text":"Changed files\\nValidation"}]}',
            ],
            "opencode/session.jsonl": [
                '{"role":"user","content":"Support OpenCode"}',
                '{"type":"tool","id":"o1","tool":"shell_command","state":{"status":"completed","input":{"command":"cargo test"},"output":"Exit code: 0"}}',
                '{"type":"text","part":{"text":"Changed files\\nValidation"}}',
            ],
            "generic/session.jsonl": [
                '{"role":"user","content":"Support generic logs"}',
                '{"type":"tool_call","name":"shell_command","id":"x1","parameters":{"command":"npm test"}}',
                '{"type":"tool_result","id":"x1","output":"Exit code: 0"}',
                '{"role":"assistant","content":"Changed files\\nValidation"}',
            ],
        }

        for relative_path, lines in cases.items():
            with self.subTest(relative_path=relative_path):
                path = self.write_jsonl(relative_path, lines)
                stats = parse_session(path, session_format="auto")

                self.assertEqual(stats.tool_calls, 1)
                self.assertEqual(stats.validation_calls, 1)
                self.assertGreaterEqual(stats.final_report_markers, 2)

    def test_codex_visible_agent_messages_are_preserved(self) -> None:
        path = self.write_jsonl(
            ".codex/sessions/session.jsonl",
            [
                '{"type":"response_item","payload":{"type":"agent_message","message":"Working on parser"}}',
                '{"type":"response_item","payload":{"type":"assistant_message","content":[{"type":"output_text","text":"Streaming answer"}]}}',
                '{"type":"response_item","payload":{"type":"agent_update","message":{"content":[{"type":"text","text":"Update note"}]}}}',
                '{"type":"response_item","payload":{"type":"task_complete","last_agent_message":"Changed files\\nValidation\\nResidual risk"}}',
            ],
        )

        self.assertEqual(
            assistant_texts(path),
            [
                "Working on parser",
                "Streaming answer",
                "Update note",
                "Changed files\nValidation\nResidual risk",
            ],
        )

    def test_multipart_agent_messages_preserve_all_text_blocks(self) -> None:
        cases = {
            "copilot/session-state/abc/events.jsonl": [
                '{"type":"assistant.message","data":{"content":"visible","reasoningText":"because"}}',
            ],
            "cursor/agent-transcripts/session.jsonl": [
                '{"type":"assistant","message":{"content":[{"type":"thinking","text":"plan"},{"type":"text","text":"answer"}]}}',
            ],
            "kimi/session.jsonl": [
                '{"role":"assistant","content":[{"type":"think","think":"reason"},{"type":"text","text":"visible"}]}',
            ],
        }

        for relative_path, lines in cases.items():
            with self.subTest(relative_path=relative_path):
                path = self.write_jsonl(relative_path, lines)
                self.assertEqual(len(assistant_texts(path)), 1)
                text = assistant_texts(path)[0]
                self.assertIn("\n", text)
                self.assertGreaterEqual(len(text.splitlines()), 2)

    def test_provider_visible_agent_messages_are_preserved(self) -> None:
        cases = {
            "copilot/session-state/abc/events.jsonl": [
                '{"type":"assistant.message_delta","data":{"deltaContent":"Hello "}}',
                '{"type":"assistant.message","data":{"content":"done","reasoningText":"because"}}',
            ],
            "cursor/agent-transcripts/session.jsonl": [
                '{"type":"assistant","message":{"content":[{"type":"thinking","text":"plan"},{"type":"text","text":"answer"}]}}',
                '{"type":"result","result":"final answer"}',
            ],
            "droid/session.jsonl": [
                '{"type":"message","role":"assistant","text":"partial"}',
                '{"type":"completion","finalText":"complete"}',
            ],
            "kimi/session.jsonl": [
                '{"role":"assistant","content":[{"type":"think","think":"reason"},{"type":"text","text":"visible"}]}',
            ],
            "opencode/session.jsonl": [
                '{"type":"text","part":{"text":"open answer"}}',
            ],
            "gemini/session.jsonl": [
                '{"type":"message","role":"assistant","content":"gemini answer"}',
            ],
        }

        for relative_path, lines in cases.items():
            with self.subTest(relative_path=relative_path):
                path = self.write_jsonl(relative_path, lines)
                texts = assistant_texts(path)
                self.assertTrue(texts, f"expected assistant text for {relative_path}")
                self.assertTrue(all(text.strip() for text in texts))

    def test_grok_updates_coalesce_chunks_and_tools(self) -> None:
        path = self.write_jsonl(
            "updates.jsonl",
            [
                '{"timestamp":"2026-08-23T00:00:00Z","method":"session/update","params":{"sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","update":{"sessionUpdate":"user_message_chunk","content":{"type":"text","text":"Fix "}}}}',
                '{"timestamp":"2026-08-23T00:00:01Z","method":"session/update","params":{"sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","update":{"sessionUpdate":"user_message_chunk","content":{"type":"text","text":"the parser"}}}}',
                '{"timestamp":"2026-08-23T00:00:02Z","method":"session/update","params":{"sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","update":{"sessionUpdate":"tool_call","toolCallId":"c1","title":"run_terminal_command","rawInput":{"command":"pnpm test"}}}}',
                '{"timestamp":"2026-08-23T00:00:03Z","method":"session/update","params":{"sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","update":{"sessionUpdate":"tool_call_update","toolCallId":"c1","status":"completed","content":[{"type":"content","content":{"type":"text","text":"Exit code: 0"}}]}}}',
                '{"timestamp":"2026-08-23T00:00:04Z","method":"session/update","params":{"sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Changed files\\nValidation"}}}}',
            ],
        )
        events = parse_session_events(path, "auto")
        kinds = [event.kind for event in events]
        self.assertIn("user_message", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("assistant_message", kinds)
        user = next(event for event in events if event.kind == "user_message")
        self.assertEqual(user.text, "Fix the parser")
        self.assertEqual(user.timestamp, "2026-08-23T00:00:00Z")
        stats = parse_session(path, session_format="auto")
        self.assertEqual(stats.tool_calls, 1)
        self.assertEqual(stats.validation_calls, 1)

    def test_grok_assistant_chunk_uses_its_own_time_before_next_user(self) -> None:
        path = self.write_jsonl(
            "updates.jsonl",
            [
                '{"timestamp":"2026-08-23T00:00:00Z","method":"session/update","params":{"update":{"sessionUpdate":"user_message_chunk","content":{"type":"text","text":"Inspect"}}}}',
                '{"timestamp":"2026-08-23T00:00:20Z","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Done"}}}}',
                '{"timestamp":"2026-08-23T00:05:00Z","method":"session/update","params":{"update":{"sessionUpdate":"user_message_chunk","content":{"type":"text","text":"Continue"}}}}',
            ],
        )
        assistant = next(event for event in parse_session_events(path, "auto") if event.kind == "assistant_message")
        self.assertEqual(assistant.timestamp, "2026-08-23T00:00:20Z")

    def test_codex_exec_apply_patch_counts_as_edit_and_validation(self) -> None:
        path = self.write_jsonl(
            ".codex/sessions/session.jsonl",
            [
                '{"type":"response_item","payload":{"type":"custom_tool_call","name":"exec","call_id":"e1","arguments":"const r = await tools.exec_command({cmd:\\"python3 scripts/test_session_events.py\\",workdir:\\"/repo\\"}); tools.apply_patch(\\"*** Begin Patch\\");"}}',
                '{"type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"e1","output":"Exit code: 0"}}',
                '{"type":"response_item","payload":{"type":"agent_message","message":"Changed files\\nValidation"}}',
            ],
        )
        stats = parse_session(path, session_format="auto")
        self.assertGreaterEqual(stats.tool_calls, 1)
        self.assertGreaterEqual(stats.edit_calls, 1)
        self.assertGreaterEqual(stats.validation_calls, 1)

    def test_codex_counts_user_role_not_prompt_wording(self) -> None:
        first = "You are a totally different factory agent.\n\nFix the stuck resume UI."
        follow = "You are still a different agent.\n\nIs it E2B or Daytona?"
        path = self.write_jsonl(
            ".codex/sessions/session.jsonl",
            [
                json.dumps({"type": "response_item", "timestamp": "2026-08-16T15:46:50.800Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<recommended_plugins>none</recommended_plugins>"}]}}),
                json.dumps({"type": "response_item", "timestamp": "2026-08-16T15:46:50.813Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": first}]}}),
                json.dumps({"type": "event_msg", "timestamp": "2026-08-16T15:46:50.813Z", "payload": {"type": "user_message", "message": first}}),
                json.dumps({"type": "response_item", "timestamp": "2026-08-16T15:57:59.047Z", "payload": {"type": "function_call", "name": "exec", "call_id": "e1", "arguments": "tools.exec_command({cmd:\"pnpm test\"})"}}),
                json.dumps({"type": "response_item", "timestamp": "2026-08-16T16:02:28.451Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": follow}]}}),
                json.dumps({"type": "event_msg", "timestamp": "2026-08-16T16:02:28.452Z", "payload": {"type": "user_message", "message": follow}}),
            ],
        )
        stats = parse_session(path, session_format="auto")
        self.assertEqual(stats.human_turns, 2)
        self.assertGreater(stats.turn_duration_p50_s, 1)

    def test_user_query_envelope_is_display_text_not_a_prompt_match(self) -> None:
        wrapped = "<user_query>Ship the cutover</user_query>"
        path = self.write_jsonl(
            ".codex/sessions/session.jsonl",
            [
                json.dumps({"type": "response_item", "timestamp": "2026-08-16T15:46:50.813Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": wrapped}]}}),
                json.dumps({"type": "response_item", "timestamp": "2026-08-16T15:47:10.000Z", "payload": {"type": "function_call", "name": "exec", "call_id": "e1", "arguments": "tools.exec_command({cmd:\"pnpm test\"})"}}),
            ],
        )
        stats = parse_session(path, session_format="auto")
        self.assertEqual(stats.human_turns, 1)
        self.assertEqual(stats.user_messages, 1)

    def test_inflight_followups_are_one_bucket_not_ack_vs_correction(self) -> None:
        path = self.write_jsonl(
            "claude/session.jsonl",
            [
                '{"type":"user","timestamp":"2026-08-01T10:00:00Z","message":{"role":"user","content":[{"type":"text","text":"Fix the parser"}]}}',
                '{"type":"assistant","timestamp":"2026-08-01T10:00:01Z","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"pnpm test"}}]}}',
                '{"type":"user","timestamp":"2026-08-01T10:00:20Z","message":{"role":"user","content":[{"type":"text","text":"yes"}]}}',
                '{"type":"assistant","timestamp":"2026-08-01T10:00:21Z","message":{"content":[{"type":"tool_use","id":"t2","name":"Bash","input":{"command":"pnpm test"}}]}}',
                '{"type":"user","timestamp":"2026-08-01T10:00:40Z","message":{"role":"user","content":[{"type":"text","text":"stop, do the other file"}]}}',
            ],
        )
        stats = parse_session(path, session_format="auto")
        self.assertEqual(stats.human_turns, 3)
        self.assertEqual(stats.stop_interrupt, 2)

    def test_codex_resume_history_burst_counts_as_one_human_turn(self) -> None:
        path = self.write_jsonl(
            ".codex/sessions/session.jsonl",
            [
                json.dumps({"type": "response_item", "timestamp": "2026-08-10T21:27:33.224Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Look at task 3068"}]}}),
                json.dumps({"type": "event_msg", "timestamp": "2026-08-10T21:27:33.224Z", "payload": {"type": "user_message", "message": "Look at task 3068"}}),
                json.dumps({"type": "response_item", "timestamp": "2026-08-10T21:27:33.233Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Try the MCP again once more"}]}}),
                json.dumps({"type": "response_item", "timestamp": "2026-08-10T21:27:33.240Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "yes"}]}}),
                json.dumps({"type": "response_item", "timestamp": "2026-08-10T21:28:10.000Z", "payload": {"type": "function_call", "name": "exec", "call_id": "e1", "arguments": "tools.exec_command({cmd:\"pnpm test\"})"}}),
                json.dumps({"type": "response_item", "timestamp": "2026-08-10T21:45:00.000Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}}),
            ],
        )
        stats = parse_session(path, session_format="auto")
        self.assertEqual(stats.human_turns, 2)
        self.assertGreater(stats.turn_duration_p50_s, 10)

    def test_long_unattended_codex_session_is_delegated_not_babysat(self) -> None:
        from analyze_sessions import SessionStats, classify_autonomy

        stats = SessionStats(id="x", path="x", provider="codex")
        stats.human_turns = 20
        stats.max_unattended_streak = 60
        stats.stop_interrupt = 10
        stats.turn_duration_p50_s = 80
        stats.turn_duration_max_s = 900
        stats.tool_calls = 400
        stats.failed_tool_calls = 1
        self.assertEqual(classify_autonomy(stats), "delegated")
        stats.turn_duration_max_s = 0
        self.assertEqual(classify_autonomy(stats), "babysat")

    def test_span_metrics_count_human_turns_streak_and_duration(self) -> None:
        path = self.write_jsonl(
            "claude/session.jsonl",
            [
                '{"type":"user","timestamp":"2026-08-01T10:00:00Z","message":{"role":"user","content":[{"type":"text","text":"Fix the parser"}]}}',
                '{"type":"assistant","timestamp":"2026-08-01T10:00:01Z","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"pnpm test"}}]}}',
                '{"type":"user","timestamp":"2026-08-01T10:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"Exit code: 0"}]}}',
                '{"type":"assistant","timestamp":"2026-08-01T10:00:05Z","message":{"content":[{"type":"tool_use","id":"t2","name":"Bash","input":{"command":"pnpm test"}}]}}',
                '{"type":"assistant","timestamp":"2026-08-01T10:00:20Z","message":{"role":"assistant","content":[{"type":"text","text":"Changed files\\nValidation\\nResidual risk"}]}}',
                '{"type":"user","timestamp":"2026-08-01T10:00:25Z","message":{"role":"user","content":[{"type":"text","text":"stop, do the other file"}]}}',
            ],
        )
        stats = parse_session(path, session_format="auto")
        self.assertEqual(stats.human_turns, 2)
        self.assertGreaterEqual(stats.max_unattended_streak, 2)
        self.assertGreater(stats.tools_per_human_turn, 0)
        self.assertGreater(stats.turn_duration_p50_s, 0)
        self.assertGreaterEqual(stats.turn_duration_max_s, stats.turn_duration_p50_s)

    def test_cursor_await_shell_reads_terminal_exit_code(self) -> None:
        transcript = self.write_jsonl(
            "proj/agent-transcripts/session.jsonl",
            [
                '{"role":"user","message":{"content":[{"type":"text","text":"Run tests"}]}}',
                '{"role":"assistant","message":{"content":[{"type":"tool_use","name":"Shell","input":{"command":"pnpm test"}}]}}',
                '{"role":"assistant","message":{"content":[{"type":"tool_use","name":"AwaitShell","input":{"shell_id":"4242","pattern":"EXIT:\\\\d+"}}]}}',
            ],
        )
        terminal = transcript.parents[1] / "terminals" / "4242.txt"
        terminal.parent.mkdir(parents=True, exist_ok=True)
        terminal.write_text("---\ncommand: pnpm test\nstatus: failed\n---\nFAIL src/foo.test.ts\n---\nexit_code: 1\nelapsed_ms: 12\n---\n")
        stats = parse_session(transcript, session_format="auto")
        self.assertGreaterEqual(stats.tool_calls, 2)
        self.assertGreaterEqual(stats.failed_tool_calls, 1)

    def test_cursor_shell_id_cannot_select_file_outside_terminals(self) -> None:
        transcript = self.write_jsonl(
            "proj/agent-transcripts/session.jsonl",
            ['{"role":"assistant","message":{"content":[{"type":"tool_use","name":"AwaitShell","input":{"shell_id":"../escape"}}]}}'],
        )
        escape = transcript.parents[1] / "escape.txt"
        escape.write_text("command: pnpm test\nexit_code: 0\n")
        terminals = transcript.parents[1] / "terminals"
        terminals.mkdir()
        self.assertEqual(cursor_terminal_sources(transcript), [])
        (terminals / "4242.txt").symlink_to(escape)
        transcript = self.write_jsonl(
            "proj/agent-transcripts/session.jsonl",
            ['{"role":"assistant","message":{"content":[{"type":"tool_use","name":"AwaitShell","input":{"shell_id":"4242"}}]}}'],
        )
        self.assertEqual(cursor_terminal_sources(transcript), [])

    def test_kimi_login_failure_is_setup_failed_not_research(self) -> None:
        path = self.write_jsonl(
            ".kimi-code/sessions/wd_x/session_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/agents/main/wire.jsonl",
            [
                '{"type":"metadata","protocol_version":"1.4","created_at":1}',
                '{"type":"config.update","profileName":"agent","systemPrompt":"You are Kimi"}',
                '{"type":"tools.set_active_tools","names":["Read","Bash"]}',
            ],
        )
        log_path = path.parents[2] / "logs" / "kimi-code.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("2026-07-19T03:52:12.107Z WARN  login failed  providerName=managed:kimi-code\n")
        stats = parse_session(path, session_format="auto")
        self.assertGreaterEqual(stats.failed_tool_calls, 1)
        self.assertEqual(stats.classification, "setup-failed")

    def test_kimi_session_does_not_borrow_global_user_history(self) -> None:
        path = self.write_jsonl(
            ".kimi-code/sessions/wd_x/session_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/agents/main/wire.jsonl",
            ['{"type":"metadata","protocol_version":"1.4","created_at":1}'],
        )
        history = self.tmp_path / ".kimi-code" / "user-history" / "history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text('{"content":"Unrelated user prompt"}\n')
        with patch("session_events.Path.home", return_value=self.tmp_path):
            events = parse_session_events(path, "auto")
        self.assertFalse(any(event.kind == "user_message" for event in events))

    def test_cursor_claude_style_tool_use_counts(self) -> None:
        path = self.write_jsonl(
            "cursor/agent-transcripts/session.jsonl",
            [
                '{"role":"user","message":{"content":[{"type":"text","text":"Implement adapter"}]}}',
                '{"role":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"pnpm test"}}]}}',
                '{"role":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"Exit code: 0"}]}}',
                '{"role":"assistant","message":{"content":[{"type":"text","text":"Changed files\\nValidation"}]}}',
            ],
        )
        stats = parse_session(path, session_format="auto")
        self.assertEqual(stats.tool_calls, 1)
        self.assertEqual(stats.validation_calls, 1)
        self.assertGreaterEqual(stats.final_report_markers, 2)

    def test_html_report_is_actionable_and_escapes_user_text(self) -> None:
        html = html_report(
            {
                "sessionCount": 10,
                "skippedCount": 0,
                "classificationCounts": {"task-finisher": 6, "research-only": 4},
                "autonomy": {
                    "styleCounts": {"delegated": 7, "babysat": 3},
                    "toolsPerHumanTurn": {"p50": 12},
                    "maxUnattendedStreak": {"p50": 20},
                    "stopClarify": 1,
                    "stopInterrupt": 9,
                    "stopFinish": 4,
                },
                "rates": {"editSessionsWithValidationAfterEdit": 0.9, "sessionsWithManualHandoffMarkers": 0},
                "byProvider": {
                    "codex": {
                        "sessionCount": 10,
                        "autonomyStyles": {"delegated": 7, "babysat": 3},
                        "toolsPerHumanTurn": {"p50": 24},
                        "maxUnattendedStreak": {"p50": 60},
                        "turnDurationP50s": {"p50": 140},
                        "stopInterrupt": 9,
                    }
                },
                "topFailurePatterns": [{"category": "Browser login/auth", "sessions": 5, "mentions": 8}],
                "topFailedTools": [["exec", 12]],
                "suggestions": [
                    {
                        "kind": "environment",
                        "title": "Sandbox cannot start",
                        "why": "3/10 sessions blocked on provision",
                        "change": "Fix sandbox env vars on the host",
                    }
                ],
            },
            [{"sessionId": "abc", "provider": "codex", "classification": "task-finisher", "autonomyStyle": "delegated", "userRequests": ["<script>alert(1)</script>"], "metrics": {"toolCalls": 3, "editCalls": 1, "failedToolCalls": 0}}],
        )
        self.assertIn("What to change", html)
        self.assertIn("Where: Missing packages, env vars, or host", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("data-kind=\"environment\"", html)

    def test_environment_kind_covers_missing_deps_and_vars_not_only_sandbox(self) -> None:
        self.assertEqual(
            suggestion_kind_for_pattern({"pattern": "dependency_missing", "category": "Missing dependency"}),
            "environment",
        )
        self.assertEqual(
            suggestion_kind_for_pattern({"pattern": "missing_env_or_secret", "category": "Missing env/secret"}),
            "environment",
        )
        self.assertEqual(
            suggestion_kind_for_pattern({"pattern": "sandbox_provisioning_failure", "category": "Sandbox provisioning"}),
            "environment",
        )
        self.assertEqual(
            suggestion_kind_for_pattern({"pattern": "browser_login_auth_failure", "category": "Browser login/auth"}),
            "setup",
        )
        self.assertEqual(
            suggestion_kind_for_pattern({"pattern": "test_failure", "category": "Test failure"}),
            "tooling",
        )
        suggestions = build_suggestions(
            {
                "sessionCount": 20,
                "classificationCounts": {"task-finisher": 10, "research-only": 4, "setup-failed": 1},
                "autonomy": {"styleCounts": {"delegated": 12}},
                "rates": {},
                "topFailurePatterns": [
                    {"pattern": "test_failure", "category": "Test failure", "sessions": 8, "mentions": 12},
                    {"pattern": "sandbox_provisioning_failure", "category": "Sandbox provisioning", "sessions": 6, "mentions": 8},
                    {"pattern": "browser_login_auth_failure", "category": "Browser login/auth", "sessions": 5, "mentions": 7},
                    {"pattern": "dependency_missing", "category": "Missing dependency", "sessions": 4, "mentions": 5},
                    {"pattern": "missing_env_or_secret", "category": "Missing env/secret", "sessions": 3, "mentions": 4},
                ],
                "topFailedTools": [],
            }
        )
        by_title = {item["title"]: item["kind"] for item in suggestions}
        self.assertEqual(by_title["Recurring: Missing dependency"], "environment")
        self.assertEqual(by_title["Recurring: Missing env/secret"], "environment")
        self.assertEqual(by_title["Recurring: Sandbox provisioning"], "environment")
        self.assertEqual(by_title["Recurring: Browser login/auth"], "setup")
        self.assertEqual(by_title["Recurring: Test failure"], "tooling")
        self.assertEqual(by_title["CLI failed before the first turn"], "environment")
        self.assertEqual(by_title["Keep long-horizon work on the agents that already run unattended"], "prompt")
        self.assertNotIn("loopify", " ".join(item.get("change", "") for item in suggestions if item["kind"] == "environment").lower())


if __name__ == "__main__":
    unittest.main()
