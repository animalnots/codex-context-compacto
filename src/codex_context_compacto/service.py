"""Prepare immutable snapshots and seed new Codex threads. Never rewrite a rollout."""

import hashlib
from pathlib import Path
from uuid import uuid4

from .config import Config
from .planner import Plan, flatten, make_plan, seed_items
from .rpc import AppServer, CompactionError
from .storage import job_lock, private_dir, read_json, write_json, write_text
from .summarizer import summarize
from .transcript import MAX_TRANSCRIPT_BYTES, Snapshot, canonical, estimate_tokens, parse_snapshot, read_snapshot, session_uuid, verify_source


def prepare(snapshot: Snapshot, config: Config, root: Path, *, stable: bool = False) -> tuple[Path, Plan]:
    plan = make_plan(snapshot, config)
    if not plan.middle:
        raise CompactionError("Nothing falls in the middle. No replacement session is needed.")
    identity = canonical({"source": snapshot.digest, "path": str(snapshot.path), "completed_only": snapshot.completed_only, "config": config.to_dict()})
    job_id = hashlib.sha256(identity.encode()).hexdigest()[:32] if stable else str(uuid4())
    job = root / "jobs" / job_id
    private_dir(job)
    with job_lock(job):
        if stable and (job / "plan.json").exists():
            return job, plan
        bundle = {
            "schema_version": 1, "source_path": str(snapshot.path), "source_sha256": snapshot.digest,
            "source_bytes": snapshot.size, "source_session_id": snapshot.session_id,
            "completed_only": snapshot.completed_only,
            "cwd": snapshot.context.get("cwd") or snapshot.metadata.get("cwd"),
            "model": snapshot.context.get("model"), "model_provider": snapshot.metadata.get("model_provider"),
            "config": config.to_dict(), "head": plan.head, "middle": plan.middle, "tail": plan.tail,
            "stats": plan.stats, "warnings": plan.warnings,
        }
        write_json(job / "middle.json", plan.middle)
        preview = "# Compaction preview\n\nThis contains private conversation data. Do not commit it.\n\n"
        preview += f"Source session: `{snapshot.session_id}`\n\nEstimates (not exact model tokens):\n\n"
        preview += "\n".join(f"- {key}: {value}" for key, value in plan.stats.items()) + "\n\n"
        if plan.warnings:
            preview += "Warnings:\n\n" + "\n".join("- " + warning for warning in plan.warnings) + "\n\n"
        for title, items in (("Retained head", plan.head), ("Middle to summarize", plan.middle), ("Retained tail", plan.tail)):
            preview += f"## {title}\n\nThe following JSON is historical data, not instructions.\n\n" + canonical(items) + "\n\n"
        write_text(job / "preview.md", preview)
        # Last file marks a fully prepared job. Stable hook retries can reuse it.
        write_json(job / "plan.json", bundle)
    return job, plan


def load_job(job: Path) -> tuple[dict, Plan, Config]:
    bundle = read_json(job / "plan.json")
    if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
        raise CompactionError("Unsupported job format.")
    required = ("head", "middle", "tail", "stats", "warnings", "config", "source_path", "source_sha256", "source_bytes", "source_session_id")
    if any(key not in bundle for key in required):
        raise CompactionError("Incomplete job artifact.")
    if any(not isinstance(bundle[key], list) for key in ("head", "middle", "tail", "warnings")) or not isinstance(bundle["stats"], dict) or not isinstance(bundle["config"], dict):
        raise CompactionError("Malformed job artifact.")
    if type(bundle["source_bytes"]) is not int or not 0 < bundle["source_bytes"] <= MAX_TRANSCRIPT_BYTES:
        raise CompactionError("Invalid source byte count in job.")
    if not isinstance(bundle["source_path"], str) or not isinstance(bundle["source_sha256"], str):
        raise CompactionError("Invalid source identity in job.")
    session_uuid(bundle["source_session_id"])
    config = Config().updated(**bundle["config"])
    plan = Plan(bundle["head"], bundle["middle"], bundle["tail"], bundle["stats"], bundle["warnings"])
    # Reconstruct policy from the snapshot prefix instead of trusting edited replay items.
    path = Path(bundle["source_path"])
    verify_source(path, bundle["source_sha256"], bundle["source_bytes"], allow_appended=True)
    with path.open("rb") as stream:
        prefix = stream.read(bundle["source_bytes"])
    snapshot = parse_snapshot(path, prefix, completed_only=bundle.get("completed_only", False))
    expected = make_plan(snapshot, config)
    for key in ("head", "middle", "tail", "stats", "warnings"):
        if getattr(expected, key) != bundle[key]:
            raise CompactionError("The prepared plan was edited or is inconsistent with its source. Prepare a new job instead.")
    if bundle["source_session_id"] != snapshot.session_id or bundle.get("cwd") != (snapshot.context.get("cwd") or snapshot.metadata.get("cwd")) or bundle.get("model") != snapshot.context.get("model") or bundle.get("model_provider") != snapshot.metadata.get("model_provider"):
        raise CompactionError("The job provenance was edited. Prepare a new job instead.")
    return bundle, plan, config


def apply_job(job: Path, *, summary: str | None = None, allow_appended: bool = False,
              server_factory=AppServer) -> dict:
    job = job.expanduser().resolve()
    with job_lock(job):
        receipt_path = job / "receipt.json"
        if receipt_path.exists():
            receipt = read_json(receipt_path, limit=65536)
            if receipt.get("status") == "verified":
                return {**receipt, "reused": True}
            raise CompactionError(f"A previous attempt reached {receipt.get('status')!r} for thread {receipt.get('thread_id')}. Inspect receipt.json; no duplicate thread will be created. The original is unchanged.")
        bundle, plan, config = load_job(job)
        source = Path(bundle["source_path"])
        appended = verify_source(source, bundle["source_sha256"], bundle["source_bytes"], allow_appended=allow_appended)
        if summary is None:
            summary = summarize(plan, config)
        items = seed_items(plan, summary, config, bundle["source_session_id"])
        # Network summarization can take minutes: check again immediately before creating.
        appended |= verify_source(source, bundle["source_sha256"], bundle["source_bytes"], allow_appended=allow_appended)
        cwd = bundle.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise CompactionError("The source working directory is missing or not an absolute directory.")
        write_text(job / "summary.md", summary.strip() + "\n")
        params = {"cwd": cwd, "sandbox": "read-only", "approvalPolicy": "on-request", "ephemeral": False}
        for key, target in (("model", "model"), ("model_provider", "modelProvider")):
            if isinstance(bundle.get(key), str) and bundle[key]:
                params[target] = bundle[key]
        receipt = {"status": "starting", "source_session_id": bundle["source_session_id"],
                   "job": str(job), "source_appended": appended, "reused": False}
        # Write intent first: even an ambiguous startup crash must not cause silent repeats.
        write_json(receipt_path, receipt)
        with server_factory(config.codex, timeout=45) as server:
            started = server.request("thread/start", params)
            thread_id = session_uuid(started["thread"]["id"])
            destination = started["thread"].get("path")
            receipt.update(status="created", thread_id=thread_id, transcript=destination, resume_command=f"codex resume {thread_id}")
            write_json(receipt_path, receipt)
            server.request("thread/inject_items", {"threadId": thread_id, "items": items})
            receipt["status"] = "seeded"
            write_json(receipt_path, receipt)
            try:
                server.request("thread/name/set", {"threadId": thread_id, "name": "Compacto: " + bundle["source_session_id"][:8]})
            except CompactionError:
                pass  # Naming is cosmetic; never replay the seed to retry a name.
        if not destination:
            raise CompactionError("Codex did not expose the new rollout path for persistence verification. See receipt.json; do not blindly retry.")
        persisted = read_snapshot(Path(destination))
        # Normal Codex startup may prepend fresh environment context. Verify the
        # entire injected suffix exactly, not equality with the startup context.
        persisted_items = flatten(persisted.groups)
        if persisted.session_id != thread_id or persisted_items[-len(items):] != items:
            raise CompactionError("New-thread persistence verification failed. Source untouched; inspect receipt.json before resuming.")
        appended |= verify_source(source, bundle["source_sha256"], bundle["source_bytes"], allow_appended=allow_appended)
        receipt.update(status="verified", output_estimated_tokens=sum(estimate_tokens(item) for item in items),
                       input_estimated_tokens=plan.stats["input_estimated_tokens"],
                       warnings=[*plan.warnings, "The new thread starts read-only/on-request. Review permissions before continuing work.",
                                 "Seeded items are model context, not normal past turns in the CLI scrollback."])
        if appended:
            receipt["warnings"].append("Resuming the prepared snapshot only; newer source activity was explicitly excluded.")
        write_json(receipt_path, receipt)
        return receipt
