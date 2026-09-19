"""Read-only adapter for Codex JSONL rollouts, deliberately strict on ambiguity."""

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from uuid import UUID

from .config import codex_home
from .rpc import CompactionError

MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
CALL_TYPES = {"function_call": "function_call_output", "custom_tool_call": "custom_tool_call_output", "tool_search_call": "tool_search_output"}
OUTPUT_TYPES = set(CALL_TYPES.values())
SUPPORTED = {"message", "web_search_call", "image_generation_call", *CALL_TYPES, *OUTPUT_TYPES}
DISCARD = {"reasoning", "configuration_update", "compaction_trigger"}


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_tokens(value) -> int:
    """An explicit UTF-8 bytes / 3 estimate, NOT a model-tokenizer guarantee."""
    text = value if isinstance(value, str) else canonical(value)
    return (len(text.encode("utf-8")) + 2) // 3


def session_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CompactionError("A session ID must be a UUID, not a filename pattern.") from exc


def locate_session(session_id: str) -> Path:
    identifier = session_uuid(session_id)
    matches = list((codex_home() / "sessions").rglob(f"*{identifier}.jsonl"))
    if len(matches) != 1:
        raise CompactionError(f"Expected one rollout for {identifier}; found {len(matches)}. Supply --transcript explicitly.")
    return matches[0]


def clean_item(item: dict, warnings: list[str]) -> dict | None:
    if not isinstance(item, dict):
        raise CompactionError("A response item is not an object.")
    kind = item.get("type")
    if kind in DISCARD:
        if kind == "reasoning" and "Hidden reasoning is omitted; messages and tool data are retained." not in warnings:
            warnings.append("Hidden reasoning is omitted; messages and tool data are retained.")
        return None
    if kind not in SUPPORTED:
        raise CompactionError(f"Unsupported history item {kind!r}; refusing to guess how to replay it.")
    if kind == "message":
        if item.get("role") in ("system", "developer"):
            return None  # Fresh startup regenerates instructions; never promote source text.
        if item.get("role") not in ("user", "assistant") or not isinstance(item.get("content"), list):
            raise CompactionError("Unsupported message role or content shape.")
        for content in item["content"]:
            if not isinstance(content, dict) or content.get("type") not in ("input_text", "output_text", "input_image"):
                raise CompactionError("Unsupported message content; this adapter handles text and input images only.")
            if content["type"] in ("input_text", "output_text") and not isinstance(content.get("text"), str):
                raise CompactionError("Message text must be a string.")
    # Item IDs and per-turn metadata belong to the source thread. Tool call IDs do not.
    return {key: value for key, value in item.items() if key not in ("id", "internal_chat_message_metadata_passthrough")}


def group_items(items: list[dict], *, completed_only: bool = False) -> tuple[list[list[dict]], int]:
    """Keep user turns and interleaved tool-call/result transactions together."""
    groups, current, pending, seen = [], [], {}, set()
    for item in items:
        kind = item["type"]
        if kind == "message" and item["role"] == "user" and current:
            if pending:
                raise CompactionError("A user message splits an unfinished tool transaction.")
            groups.append(current)
            current = []
        if kind in CALL_TYPES:
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                raise CompactionError("A tool call has a missing or duplicate call_id.")
            seen.add(call_id)
            pending[call_id] = CALL_TYPES[kind]
        elif kind in OUTPUT_TYPES:
            if not isinstance(item.get("call_id"), str) or pending.get(item.get("call_id")) != kind:
                raise CompactionError("A tool result is missing its matching call.")
            del pending[item["call_id"]]
        current.append(item)
    omitted = 0
    if pending:
        if not completed_only:
            raise CompactionError("The transcript ends with an unfinished tool call. Wait for the turn to finish, or use --completed-only to explicitly omit its entire final group.")
        omitted = len(current)
    elif current:
        groups.append(current)
    return groups, omitted


@dataclass
class Snapshot:
    path: Path
    digest: str
    size: int
    metadata: dict
    context: dict
    groups: list[list[dict]]
    warnings: list[str] = field(default_factory=list)
    completed_only: bool = False

    @property
    def session_id(self) -> str:
        return session_uuid(self.metadata.get("id") or self.metadata.get("session_id"))


def read_snapshot(path: Path, *, completed_only: bool = False) -> Snapshot:
    path = path.expanduser().resolve()
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_TRANSCRIPT_BYTES + 1)
    except OSError as exc:
        raise CompactionError(f"Cannot read transcript: {path}") from exc
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise CompactionError("Transcript exceeds the 64 MiB safety limit.")
    return parse_snapshot(path, raw, completed_only=completed_only)


def parse_snapshot(path: Path, raw: bytes, *, completed_only: bool = False) -> Snapshot:
    metadata, context, items, warnings = {}, {}, [], []
    try:
        lines = raw.decode("utf-8-sig").splitlines()
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                raise CompactionError(f"Malformed transcript record at line {line_number}.")
            kind, payload = record.get("type"), record["payload"]
            if kind == "session_meta":
                if metadata:
                    raise CompactionError("Multiple session metadata records are not supported.")
                metadata = payload
            elif kind == "turn_context":
                context = payload
            elif kind == "response_item":
                item = clean_item(payload, warnings)
                if item is not None:
                    items.append(item)
            elif kind == "compacted":
                replacement = payload.get("replacement_history")
                if not isinstance(replacement, list):
                    raise CompactionError("A previous compaction has no explicit replacement_history. Cannot reconstruct its live context safely.")
                items = [item for original in replacement if (item := clean_item(original, warnings)) is not None]
                warnings.append("Used the last explicit native compaction replacement_history.")
            elif kind == "event_msg":
                if payload.get("type") == "thread_rolled_back":
                    raise CompactionError("Rollbacks are not supported by this rollout adapter.")
            elif kind not in ("world_state",):
                raise CompactionError(f"Unknown rollout record {kind!r}; update the adapter before using this format.")
    except (ValueError, UnicodeError) as exc:
        raise CompactionError("Invalid or partially written JSONL transcript. Retry when the session is idle.") from exc
    if not metadata:
        raise CompactionError("Missing session_meta record; this is not a supported Codex rollout.")
    groups, omitted = group_items(items, completed_only=completed_only)
    if omitted:
        warnings.append(f"Explicitly omitted the final incomplete group ({omitted} items). The original session still contains it.")
    if not groups:
        raise CompactionError("No complete conversation groups to compact.")
    result = Snapshot(path, hashlib.sha256(raw).hexdigest(), len(raw), metadata, context, groups, warnings, completed_only)
    result.session_id  # Validate provenance before any writes.
    return result


def verify_source(path: Path, digest: str, size: int, *, allow_appended: bool = False) -> bool:
    try:
        with path.open("rb") as stream:
            prefix = stream.read(size)
            appended = bool(stream.read(1))
    except OSError as exc:
        raise CompactionError("The source transcript is no longer readable.") from exc
    if len(prefix) != size or hashlib.sha256(prefix).hexdigest() != digest:
        raise CompactionError("The source transcript was rewritten after the snapshot. Prepare a new job.")
    if appended and not allow_appended:
        raise CompactionError("The source transcript grew after the snapshot. Prepare a new job, or explicitly use --allow-appended to resume the older snapshot.")
    return appended
