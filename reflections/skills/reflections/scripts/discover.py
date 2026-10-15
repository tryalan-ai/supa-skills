#!/usr/bin/env python3
"""Discover local CLI session transcripts on this machine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

HOME = Path.home()


@dataclass(frozen=True)
class SessionSource:
    provider: str
    root: Path
    glob: str


DEFAULT_SOURCES: tuple[SessionSource, ...] = (
    SessionSource("codex", HOME / ".codex" / "sessions", "**/*.jsonl"),
    SessionSource("claude", HOME / ".claude" / "projects", "**/*.jsonl"),
    SessionSource("cursor", HOME / ".cursor" / "projects", "**/agent-transcripts/**/*.jsonl"),
    SessionSource("grok", HOME / ".grok" / "sessions", "**/updates.jsonl"),
    SessionSource("kimi", HOME / ".kimi-code" / "sessions", "**/wire.jsonl"),
    SessionSource("opencode", HOME / ".local" / "share" / "opencode", "**/*.jsonl"),
)

SKIP_NAMES = {
    "prompt_history.jsonl",
    "session_index.jsonl",
    "chat_history.jsonl",
    "events.jsonl",
}


def discover_session_files(
    sources: tuple[SessionSource, ...] = DEFAULT_SOURCES,
    repo: str | None = None,
    max_sessions: int | None = None,
    max_per_provider: int | None = None,
    providers: set[str] | None = None,
    skip_over_mb: float | None = None,
    min_age_seconds: float = 0,
    include_subagents: bool = True,
) -> list[Path]:
    files: list[Path] = []
    for source in sources:
        if providers and source.provider not in providers:
            continue
        if not source.root.exists():
            continue
        found = [path for path in source.root.glob(source.glob) if path.is_file() and path.name not in SKIP_NAMES]
        if not include_subagents:
            found = [path for path in found if "subagents" not in path.parts]
        found.sort(key=lambda path: path.stat().st_mtime)
        if min_age_seconds:
            cutoff = time.time() - min_age_seconds
            found = [path for path in found if path.stat().st_mtime <= cutoff]
        if skip_over_mb is not None:
            limit = skip_over_mb * 1024 * 1024
            found = [path for path in found if path.stat().st_size <= limit]
        if max_per_provider is not None:
            found = found[-max_per_provider:]
        files.extend(found)
    if repo:
        files = [path for path in files if session_mentions_repo(path, repo)]
    files.sort(key=lambda path: path.stat().st_mtime)
    if max_sessions is not None:
        files = files[-max_sessions:]
    return files


def session_mentions_repo(path: Path, repo: str) -> bool:
    needle = repo
    short = repo.split("/Documents/")[-1] if "/Documents/" in repo else repo
    encoded = repo.replace("/", "%2F")
    if needle in str(path) or short in str(path) or encoded in str(path):
        return True
    try:
        with path.open(errors="ignore") as handle:
            for index, line in enumerate(handle):
                if index > 80:
                    break
                if needle in line or short in line:
                    return True
    except OSError:
        return False
    return False


def provider_for_path(path: Path) -> str:
    normalized = str(path).replace("\\", "/")
    if "/.grok/" in normalized:
        return "grok"
    if "/.codex/" in normalized:
        return "codex"
    if "/.claude/" in normalized:
        return "claude"
    if "/.cursor/" in normalized or "/agent-transcripts/" in normalized:
        return "cursor"
    if "/.kimi" in normalized:
        return "kimi"
    if "/opencode" in normalized:
        return "opencode"
    return "unknown"
