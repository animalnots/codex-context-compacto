"""Codex PreCompact adapter. Opt-in; a successful seed requests manual handoff."""

import json
from pathlib import Path
import sys

from .config import load_config, state_root
from .rpc import CompactionError
from .service import apply_job, prepare
from .transcript import read_snapshot, session_uuid


def blocked(reason: str) -> dict:
    return {"continue": False, "stopReason": reason, "systemMessage": reason}


def handle(payload: dict, *, root: Path | None = None) -> dict:
    root = root or state_root()
    try:
        config = load_config(root)
        if not config.enabled:
            return {}  # Installation alone never spends money or changes native compaction.
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreCompact":
            raise CompactionError("Expected a Codex PreCompact payload.")
        if not isinstance(payload.get("transcript_path"), str):
            raise CompactionError("Codex did not supply a transcript_path. Native compaction is paused; disable Compacto to use the built-in path.")
        snapshot = read_snapshot(Path(payload["transcript_path"]))
        if snapshot.session_id != session_uuid(payload.get("session_id")):
            raise CompactionError("Hook session_id does not match the source transcript.")
        job, _ = prepare(snapshot, config, root, stable=True)
        result = apply_job(job)
        return blocked("Compacto prepared a NEW session; the original is unchanged. Exit this session and run: "
                       + result["resume_command"] + ". The new session starts read-only. Preview and receipt: " + str(job))
    except (CompactionError, OSError) as exc:
        return blocked("Compacto stopped safely: " + str(exc) + " Original session unchanged. Set enabled=false in Compacto config to allow native compaction.")


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise CompactionError("Hook payload exceeds 1 MiB.")
        payload = json.loads(raw)
        result = handle(payload)
    except (ValueError, CompactionError) as exc:
        result = blocked("Compacto received an invalid hook payload: " + str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0  # Codex decisions are JSON, never Claude's decision:block convention.


if __name__ == "__main__":
    raise SystemExit(main())
