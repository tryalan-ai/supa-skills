#!/usr/bin/env python3
"""Focused tests for agent-readiness multi-CLI session normalization."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from behavior import analyze_events
from session_events import parse_session_events


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
        stats = analyze_events(path.stem, "local", events)

        self.assertEqual([event.kind for event in events].count("tool_call"), 1)
        self.assertEqual(stats.tool_calls, 1)
        self.assertEqual(stats.validations, 1)

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
                stats = analyze_events(path.stem, "local", parse_session_events(path, "auto"))

                self.assertEqual(stats.tool_calls, 1)
                self.assertEqual(stats.validations, 1)

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

    def test_reasoning_blocks_are_excluded_from_visible_replies(self) -> None:
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
                self.assertEqual(assistant_texts(path), ["answer" if "cursor" in relative_path else "visible"])

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


if __name__ == "__main__":
    unittest.main()
