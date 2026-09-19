"""Private atomic artifacts, with an exclusive lock for each compaction job."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from .rpc import CompactionError


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def write_text(path: Path, text: str, *, exclusive: bool = False) -> None:
    private_dir(path.parent)
    if exclusive:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise CompactionError(f"File already exists; not overwriting it: {path}") from exc
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".compacto-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value, *, exclusive: bool = False) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", exclusive=exclusive)


def read_json(path: Path, *, limit: int = 128 * 1024 * 1024):
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise CompactionError(f"Artifact exceeds the read limit: {path.name}")
        return json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise CompactionError(f"Cannot read JSON artifact: {path}") from exc


@contextmanager
def job_lock(job: Path):
    path = job / ".apply.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CompactionError(f"This job is locked: {path}. If its process crashed, confirm no helper is running before removing that one lock file.") from exc
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)
