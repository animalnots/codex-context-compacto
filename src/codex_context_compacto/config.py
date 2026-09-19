"""Strict, opt-in configuration. Secrets never belong in configuration files."""

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .rpc import CompactionError


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    head_tokens: int = 0
    tail_tokens: int = 25000
    summary_tokens: int = 4000
    max_input_tokens: int = 180000
    max_output_tokens: int = 40000
    backend: str = "manual"
    summary_model: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    api_base_url: str = "https://api.openai.com/v1"
    timeout_seconds: int = 180
    codex: str = "codex"

    def validate(self) -> "Config":
        if type(self.enabled) is not bool:
            raise CompactionError("enabled must be true or false.")
        for name in ("head_tokens", "tail_tokens", "summary_tokens", "max_input_tokens", "max_output_tokens", "timeout_seconds"):
            value = getattr(self, name)
            minimum = 0 if name in ("head_tokens", "tail_tokens") else 1
            if type(value) is not int or not minimum <= value <= 10000000:
                raise CompactionError(f"{name} must be an integer between {minimum} and 10000000.")
        if self.timeout_seconds > 240:
            raise CompactionError("timeout_seconds must not exceed 240 (the hook has a bounded deadline).")
        if self.head_tokens + self.tail_tokens + self.summary_tokens + 256 > self.max_output_tokens:
            raise CompactionError("max_output_tokens must cover head_tokens + tail_tokens + summary_tokens + 256 summary-envelope tokens.")
        for name in ("backend", "summary_model", "api_key_env", "api_base_url", "codex"):
            if not isinstance(getattr(self, name), str):
                raise CompactionError(f"{name} must be a string.")
        if self.backend not in ("manual", "responses"):
            raise CompactionError("backend must be manual or responses.")
        if not self.api_key_env or not self.codex:
            raise CompactionError("api_key_env and codex must not be empty.")
        try:
            endpoint = urlsplit(self.api_base_url)
            endpoint.port
        except ValueError as exc:
            raise CompactionError("api_base_url is not a valid URL.") from exc
        if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise CompactionError("api_base_url must be an endpoint URL without credentials, query, or fragment.")
        if endpoint.scheme != "https" and not (endpoint.scheme == "http" and endpoint.hostname in ("127.0.0.1", "localhost", "::1")):
            raise CompactionError("api_base_url requires HTTPS, except for a loopback test server.")
        if self.enabled and (self.backend != "responses" or not self.summary_model.strip()):
            raise CompactionError("The unattended hook requires backend=responses and an explicit summary_model. Leave enabled=false for skill/manual use.")
        return self

    def updated(self, **values) -> "Config":
        try:
            return replace(self, **values).validate()
        except TypeError as exc:
            raise CompactionError("Unknown configuration key.") from exc

    def to_dict(self) -> dict:
        return asdict(self)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def state_root(explicit: str | None = None) -> Path:
    # Identical in the standalone helper and plugin hook: PLUGIN_DATA is usually
    # provided only to hooks and would otherwise select a different config file.
    selected = explicit or os.environ.get("COMPACTO_STATE_DIR")
    return Path(selected).expanduser().resolve() if selected else (codex_home() / "context-compacto").resolve()


def load_config(root: Path) -> Config:
    path = root / "config.json"
    if not path.exists():
        return Config().validate()
    try:
        if path.stat().st_size > 65536:
            raise CompactionError("Configuration exceeds 64 KiB.")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise CompactionError("Configuration must be a JSON object.")
        return Config().updated(**data)
    except (OSError, ValueError) as exc:
        raise CompactionError(f"Cannot read configuration: {path}") from exc
