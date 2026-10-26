"""Configuration is read locally; no credential is exposed as an MCP argument."""

import json
import os
import re
import tempfile
import tomllib
from contextlib import suppress
from pathlib import Path
from typing import Literal

from platformdirs import user_config_path, user_data_path
from pydantic import Field, SecretStr, ValidationError, field_validator

from telegram_mcp.domain.models import Model, ToolError
from telegram_mcp.domain.periods import load_zone
from telegram_mcp.domain.policy import ReadPolicy


class Settings(Model):
    api_id: int | None = Field(default=None, gt=0)
    api_hash: SecretStr | None = Field(default=None, min_length=1)
    session_path: Path = Field(
        default_factory=lambda: user_data_path("telegram-mcp") / "account.session"
    )
    allowed_chat_ids: list[str] = Field(default_factory=list)
    allowed_folder_ids: list[int] = Field(default_factory=list)
    allow_all_chats: bool = False
    allow_public_channel_reads: bool = False
    allow_public_channel_discovery: bool = False
    allow_attachment_downloads: bool = False
    archive_directory: Path | None = None
    mcp_schema_mode: Literal["compact", "full"] = "compact"
    timezone: str = "UTC"
    rpc_timeout_seconds: float = Field(default=20, gt=0, le=60)
    max_dialogs: int = Field(default=10000, ge=1, le=100000)
    catalog_cache_ttl_seconds: float = Field(default=300, ge=0, le=300)
    catalog_cache_stale_seconds: float = Field(default=300, ge=0, le=300)
    rpc_pages_per_call: int = Field(default=12, ge=1, le=100)
    cursor_ttl_seconds: int = Field(default=900, ge=1)
    max_cursors: int = Field(default=128, ge=1)
    http_port: int = Field(default=8765, ge=1, le=65535, strict=True)
    http_token_path: Path = Field(
        default_factory=lambda: user_data_path("telegram-mcp") / "mcp.token"
    )

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            load_zone(value)
        except ToolError:
            raise ValueError("must be an IANA time zone name such as Europe/Moscow") from None
        return value

    def credentials(self) -> tuple[int, str]:
        if self.api_id is None or self.api_hash is None:
            raise ToolError("AUTH_REQUIRED", "Run telegram-mcp login in a local terminal first.")
        return self.api_id, self.api_hash.get_secret_value()


def config_path() -> Path:
    return Path(
        os.environ.get("TELEGRAM_MCP_CONFIG", user_config_path("telegram-mcp") / "config.toml")
    ).expanduser()


def validation_error(
    error: ValidationError, source: str, overrides: dict[str, str] | None = None
) -> ToolError:
    """Describe fields and expectations without including submitted values."""
    details = []
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "configuration"
        origin = (overrides or {}).get(str(item["loc"][0]), source) if item["loc"] else source
        details.append(f"{origin}: {location}: {item['msg']}")
    return ToolError("INVALID_CONFIGURATION", "; ".join(details))


def validate_settings(settings: Settings, source: str) -> Settings:
    try:
        return Settings.model_validate(settings.model_dump())
    except ValidationError as exc:
        raise validation_error(exc, source) from None


def local_io_error(action: str, path: Path, error: OSError) -> ToolError:
    if isinstance(error, PermissionError):
        reason = "permission denied; check file and directory permissions"
    elif isinstance(error, FileNotFoundError):
        reason = "file or parent directory does not exist"
    elif isinstance(error, IsADirectoryError):
        reason = "expected a file, found a directory"
    elif isinstance(error, NotADirectoryError):
        reason = "a parent path is not a directory"
    else:
        reason = "local filesystem operation failed; check the path and available disk space"
    return ToolError("LOCAL_IO_ERROR", f"Cannot {action} {path}: {reason}.")


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    except UnicodeError:
        raise ToolError("INVALID_CONFIGURATION", f"{path}: configuration must use UTF-8.") from None
    except OSError as exc:
        raise local_io_error("read configuration", path, exc) from None
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        # TOML diagnostics identify syntax/position; never print the input line.
        line = getattr(exc, "lineno", None)
        column = getattr(exc, "colno", None)
        if line is None:
            match = re.search(r"\(at line (\d+), column (\d+)\)$", str(exc))
            if match:
                line, column = match.groups()
        position = f" at line {line}, column {column}" if line is not None else ""
        raise ToolError(
            "INVALID_CONFIGURATION", f"{path}: invalid TOML syntax{position}."
        ) from None
    overrides = {}
    for key, environment in (
        ("api_id", "TELEGRAM_API_ID"),
        ("api_hash", "TELEGRAM_API_HASH"),
        ("session_path", "TELEGRAM_SESSION_PATH"),
    ):
        if value := os.environ.get(environment):
            data[key] = value
            overrides[key] = environment
    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        raise validation_error(exc, str(path), overrides) from None
    for key in ("session_path", "http_token_path", "archive_directory"):
        if getattr(settings, key) is None:
            continue
        try:
            setattr(settings, key, getattr(settings, key).expanduser().absolute())
        except (OSError, RuntimeError, ValueError):
            raise ToolError(
                "INVALID_CONFIGURATION", f"{overrides.get(key, str(path))}: {key}: invalid path."
            ) from None
    if not str(settings.session_path).endswith(".session"):
        settings.session_path = Path(str(settings.session_path) + ".session")
    return settings


def save_settings(settings: Settings, path: Path) -> None:
    settings = validate_settings(settings, str(path))
    data = settings.model_dump(mode="json", exclude_none=True)
    if settings.api_hash:
        data["api_hash"] = settings.api_hash.get_secret_value()
    text = (
        "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in data.items())
        + "\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
    except OSError as exc:
        raise local_io_error("write configuration", path, exc) from None


class ConfiguredPolicyProvider:
    """Snapshot current in-memory settings on each call, without reading credentials."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def current(self) -> ReadPolicy:
        return ReadPolicy(
            allowed_chat_ids=tuple(self.settings.allowed_chat_ids),
            allowed_folder_ids=tuple(self.settings.allowed_folder_ids),
            allow_all_chats=self.settings.allow_all_chats,
            allow_public_channel_reads=self.settings.allow_public_channel_reads,
            allow_public_channel_discovery=self.settings.allow_public_channel_discovery,
            allow_attachment_downloads=self.settings.allow_attachment_downloads,
        )
