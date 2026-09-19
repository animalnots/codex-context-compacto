"""Machine-readable CLI; all successful output is a single JSON value."""

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .config import load_config, state_root
from .planner import make_plan
from .rpc import CompactionError, codex_command
from .service import apply_job, prepare
from .storage import write_json
from .transcript import locate_session, read_snapshot


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Preserve selected Codex history around a summarized middle, in a NEW session.")
    result.add_argument("--version", action="version", version=__version__)
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("preview", "prepare", "compact"):
        command = sub.add_parser(name)
        source = command.add_mutually_exclusive_group()
        source.add_argument("--transcript", type=Path, help="Source Codex rollout JSONL")
        source.add_argument("--session", help="Source session UUID (defaults to CODEX_THREAD_ID, never latest)")
        command.add_argument("--state-dir", help="Private job/config directory; defaults to ~/.codex/context-compacto")
        command.add_argument("--head-tokens", type=int)
        command.add_argument("--tail-tokens", type=int)
        command.add_argument("--completed-only", action="store_true", help="Explicitly omit an unfinished final tool group")
        if name == "compact":
            command.add_argument("--summary-file", type=Path, help="Reviewed UTF-8 checkpoint; otherwise use configured API backend")
    command = sub.add_parser("apply", help="Seed a new session from a prepared job")
    command.add_argument("--job", type=Path, required=True)
    command.add_argument("--summary-file", type=Path)
    command.add_argument("--allow-appended", action="store_true", help="Explicitly exclude activity appended after preparation")
    config = sub.add_parser("config")
    config.add_argument("action", choices=("show", "init", "set"))
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?", help="JSON value, e.g. true, 25000, or a quoted JSON string")
    config.add_argument("--state-dir")
    doctor = sub.add_parser("doctor", help="Check local prerequisites without model requests or state writes")
    doctor.add_argument("--state-dir")
    return result


def read_summary(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise CompactionError("Summary file exceeds 1 MiB.")
        return raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise CompactionError("Cannot read the UTF-8 summary file.") from exc


def run(args) -> dict:
    if args.command == "apply":
        return apply_job(args.job, summary=read_summary(args.summary_file), allow_appended=args.allow_appended)
    root = state_root(args.state_dir)
    config = load_config(root)
    if args.command == "doctor":
        return {"version": __version__, "python": sys.version.split()[0], "codex_executable": codex_command(config.codex)[0],
                "state_dir": str(root), "backend": config.backend, "hook_enabled": config.enabled,
                "summary_api_key_present": bool(os.environ.get(config.api_key_env)),
                "tested_codex_version": "0.154.0", "note": "No model request, installation, or live-session change was performed."}
    if args.command == "config":
        if args.action == "init":
            write_json(root / "config.json", config.to_dict(), exclusive=True)
        elif args.action == "set":
            if not args.key or args.value is None:
                raise CompactionError("config set requires a key and value.")
            try:
                value = json.loads(args.value)
            except ValueError:
                value = args.value  # Convenient bare string for model/backend names.
            config = config.updated(**{args.key: value})
            write_json(root / "config.json", config.to_dict())
        return {"path": str(root / "config.json"), "config": config.to_dict()}
    overrides = {key: getattr(args, key) for key in ("head_tokens", "tail_tokens") if getattr(args, key) is not None}
    config = config.updated(**overrides)
    identifier = args.session or os.environ.get("CODEX_THREAD_ID")
    if args.transcript is None and not identifier:
        raise CompactionError("Supply --session UUID or --transcript PATH. There is no unsafe 'latest session' fallback.")
    path = args.transcript or locate_session(identifier)
    snapshot = read_snapshot(path, completed_only=args.completed_only)
    if args.command == "preview":
        plan = make_plan(snapshot, config)
        return {"source_session_id": snapshot.session_id, "stats": plan.stats, "warnings": plan.warnings,
                "would_compact": bool(plan.middle), "note": "Preview is read-only; use prepare for readable artifacts."}
    job, plan = prepare(snapshot, config, root)
    if args.command == "prepare":
        return {"job": str(job), "preview": str(job / "preview.md"), "middle": str(job / "middle.json"),
                "source_session_id": snapshot.session_id, "stats": plan.stats, "warnings": plan.warnings}
    return apply_job(job, summary=read_summary(args.summary_file))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        value = run(parser().parse_args(argv))
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (CompactionError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('{"error":"Interrupted. Source unchanged; check any job receipt before retrying."}', file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
