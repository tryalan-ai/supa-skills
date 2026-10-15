#!/usr/bin/env python3
"""Run the standalone Reflections core on bounded local CLI sessions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from discover import discover_session_files, provider_for_path
from reflections_core import analyze_session
from reflections_report import aggregate, markdown_report
from session_events import cursor_terminal_sources


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_signature(path: Path) -> dict[str, int | str]:
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtimeNs": stat.st_mtime_ns}


def validate_jsonl(path: Path) -> None:
    try:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at line {number}: {path}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"Non-object JSONL at line {number}: {path}")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Invalid UTF-8 in JSONL: {path}") from exc


def cohort_files(manifest_path: Path | None, discovered: list[Path], policy: dict | None = None) -> tuple[list[Path], dict[str, dict[str, int | str]]]:
    if manifest_path is None:
        return discovered, {}
    if manifest_path.exists():
        manifest_path.chmod(0o600)
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("version") not in {1, 2} or not isinstance(manifest.get("files"), list):
            raise ValueError(f"Invalid cohort manifest: {manifest_path}")
        entries = manifest["files"]
    else:
        entries = [source_signature(path) for path in discovered]
        private_write(manifest_path, json.dumps({"version": 1, "files": entries, "scanPolicy": policy or {}}, indent=2))
    return [Path(entry["path"]) for entry in entries], {entry["path"]: entry for entry in entries}


def auxiliary_signatures(path: Path) -> list[dict[str, int | str]]:
    if provider_for_path(path) != "cursor":
        return []
    return [source_signature(terminal) for terminal in cursor_terminal_sources(path)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-per-provider", type=int, default=8)
    parser.add_argument("--providers", help="Comma-separated provider names")
    parser.add_argument("--skip-over-mb", type=float, default=25)
    parser.add_argument("--min-age-minutes", type=float, default=10, help="Exclude files modified recently so the scan is stable")
    parser.add_argument("--include-subagents", action="store_true", help="Treat child-agent transcripts as separate records")
    parser.add_argument("--manifest", type=Path, help="Freeze and reuse an exact input cohort; creates the file on first run")
    parser.add_argument("--output", type=Path, default=Path("/tmp/alan-reflections/core.md"))
    parser.add_argument("--json-output", type=Path, default=Path("/tmp/alan-reflections/core.json"))
    args = parser.parse_args()
    providers = set(args.providers.split(",")) if args.providers else None
    discovery_policy = {"maxPerProvider": args.max_per_provider, "skipOverMb": args.skip_over_mb, "minAgeMinutes": args.min_age_minutes, "providers": sorted(providers) if providers else None, "includeSubagents": args.include_subagents}
    discovered = [] if args.manifest and args.manifest.exists() else discover_session_files(max_per_provider=args.max_per_provider, providers=providers, skip_over_mb=args.skip_over_mb, min_age_seconds=args.min_age_minutes * 60, include_subagents=args.include_subagents)
    files, expected = cohort_files(args.manifest, discovered, discovery_policy)
    manifest = json.loads(args.manifest.read_text()) if args.manifest else None
    expected_sidecars = manifest.get("sidecars") if manifest and manifest.get("version") == 2 else None
    sessions = []
    errors = []
    unstable = []
    sidecars: dict[str, list[dict[str, int | str]]] = {}
    for path in files:
        try:
            before = source_signature(path)
            if expected and before != expected[str(path)]:
                unstable.append(str(path))
                continue
            validate_jsonl(path)
            before_sidecars = auxiliary_signatures(path)
            if expected_sidecars is not None and before_sidecars != expected_sidecars.get(str(path), []):
                unstable.append(f"{path}: Cursor terminal sidecars changed")
                continue
            session = analyze_session(path)
            after = source_signature(path)
            after_sidecars = auxiliary_signatures(path)
            if before != after or before_sidecars != after_sidecars:
                unstable.append(str(path))
                continue
            sidecars[str(path)] = after_sidecars
            sessions.append(session)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    summary = aggregate(sessions)
    summary["skippedFiles"] = errors
    summary["unstableFiles"] = unstable
    if manifest and manifest.get("version") == 1 and not errors and not unstable:
        manifest["version"] = 2
        manifest["sidecars"] = sidecars
        private_write(args.manifest, json.dumps(manifest, indent=2))
    summary["scanPolicy"] = {**(manifest.get("scanPolicy", {}) if manifest else discovery_policy), "manifest": str(args.manifest) if args.manifest else None, "frozenSidecars": sum(len(items) for items in sidecars.values()) if args.manifest else 0}
    payload = {"aggregate": summary, "sessions": sessions}
    private_write(args.output, markdown_report(summary, sessions))
    private_write(args.json_output, json.dumps(payload, indent=2))
    print(f"{len(sessions)} sessions; {summary['explicitOutcomeSessions']}/{summary['implementationEditSessions']} code-edit sessions have explicit final check outcomes ({summary['passedCheckAfterLastEdit']} passed); {summary['observedAfterLastEdit']} observed without exit status; {len(unstable)} unstable files; {len(errors)} errors")
    print(args.output)
    print(args.json_output)
    return 0 if sessions and not errors and not unstable else 1


if __name__ == "__main__":
    raise SystemExit(main())
