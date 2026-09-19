"""A bounded stdio App Server client. Never auto-approve server requests."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Any


class CompactionError(Exception):
    """An actionable error safe to show without a traceback."""


def codex_command(executable: str = "codex") -> list[str]:
    resolved = shutil.which(executable)
    if resolved is None:
        raise CompactionError(f"Codex executable not found: {executable}")
    path = Path(resolved)
    if os.name == "nt" and path.suffix.lower() in (".cmd", ".bat"):
        # Invoke the native executable, avoiding cmd.exe's argument expansion.
        package = path.parent / "node_modules" / "@openai" / "codex"
        candidates = []
        for prefix in ("node_modules/@openai/codex-win32-*/vendor", "vendor"):
            for directory in ("bin", "codex"):
                candidates += sorted(package.glob(f"{prefix}/*/{directory}/codex.exe"))
        architecture = "aarch64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" else "x86_64"
        compatible = [candidate for candidate in candidates if architecture in str(candidate)]
        if not compatible:
            raise CompactionError("Cannot resolve the Windows Codex shim. Pass --codex with the full path to codex.exe.")
        path = compatible[0]
    return [str(path)]


class AppServer:
    """Owns only the child server it starts; does not attach to the live daemon."""

    def __init__(self, executable: str = "codex", *, timeout: float = 45,
                 config: dict[str, Any] | None = None, env: dict[str, str] | None = None):
        self.executable = executable
        self.timeout = timeout
        self.config = config or {}
        self.env = env
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[Any] = queue.Queue()
        self.notifications: deque[dict[str, Any]] = deque(maxlen=1000)
        self.stderr: deque[str] = deque(maxlen=40)
        self.counter = 0
        self.readers: list[threading.Thread] = []

    def __enter__(self) -> AppServer:
        command = codex_command(self.executable) + ["app-server", "--stdio", "--disable", "hooks"]
        for key, value in self.config.items():
            command += ["-c", f"{key}={toml_value(value)}"]
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=self.env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise CompactionError(f"Could not start Codex App Server: {exc}") from exc
        for target in (self._read_stdout, self._read_stderr):
            reader = threading.Thread(target=target, daemon=True)
            reader.start()
            self.readers.append(reader)
        try:
            self.request("initialize", {
                "clientInfo": {"name": "codex_context_compacto", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            })
            self._write({"method": "initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                if line.strip():
                    try:
                        self.messages.put(json.loads(line))
                    except ValueError:
                        self.messages.put(CompactionError("App Server emitted invalid JSON."))
        finally:
            self.messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            self.stderr.append(line.rstrip())

    def _write(self, value: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise CompactionError("App Server is not running.")
        try:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CompactionError("App Server input closed unexpectedly.") from exc

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.counter += 1
        request_id = self.counter
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise CompactionError(f"App Server timed out during {method}.") from exc
            if message is None:
                detail = "\n".join(list(self.stderr)[-4:])[-2000:]
                raise CompactionError(f"App Server stopped during {method}. {detail}".strip())
            if isinstance(message, Exception):
                raise message
            if not isinstance(message, dict):
                raise CompactionError("Invalid App Server message.")
            if "method" in message:
                if "id" in message:
                    self._write({"id": message["id"], "error": {
                        "code": -32601, "message": "This compaction client does not approve or execute server requests."
                    }})
                else:
                    self.notifications.append(message)
            elif message.get("id") == request_id:
                if "error" in message:
                    error = message["error"]
                    detail = error.get("message", "request failed") if isinstance(error, dict) else "invalid error response"
                    raise CompactionError(f"{method}: {detail}")
                return message.get("result")
            if time.monotonic() >= deadline:
                raise CompactionError(f"App Server timed out during {method}.")

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()
        self.process = None

    def wait_notification(self, method: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Wait for one notification, rejecting requests instead of approving them."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            for notification in list(self.notifications):
                if notification.get("method") == method:
                    self.notifications.remove(notification)
                    return notification.get("params", {})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CompactionError(f"App Server timed out waiting for {method}.")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise CompactionError(f"App Server timed out waiting for {method}.") from exc
            if message is None:
                raise CompactionError(f"App Server closed while waiting for {method}.")
            if isinstance(message, Exception):
                raise message
            if isinstance(message, dict) and "method" in message:
                if "id" in message:
                    self._write({"id": message["id"], "error": {
                        "code": -32601, "message": "Compaction never approves server requests."
                    }})
                else:
                    self.notifications.append(message)

    def __exit__(self, *_: object) -> None:
        self.close()


def toml_value(value: Any) -> str:
    """Encode the small TOML subset needed for per-process overrides."""
    if value is None:
        raise CompactionError("A TOML override cannot be null.")
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(str(k)) + " = " + toml_value(v) for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)
