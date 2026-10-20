"""Configuration is read locally; no credential is exposed as an MCP argument."""

import os
import tomllib
from pathlib import Path

from platformdirs import user_config_path, user_data_path
from pydantic import Field, SecretStr

from telegram_mcp.models import Model, ToolError


class Settings(Model):
    api_id: int | None = Field(default=None, gt=0)
    api_hash: SecretStr | None = None
    session_path: Path = Field(
        default_factory=lambda: user_data_path("telegram-mcp") / "account.session"
    )
    allowed_chat_ids: list[str] = Field(default_factory=list)
    allowed_folder_ids: list[int] = Field(default_factory=list)
    allow_all_chats: bool = False
    allow_public_channel_reads: bool = False
    allow_public_channel_discovery: bool = False
    rpc_timeout_seconds: float = Field(default=20, gt=0, le=60)
    max_dialogs: int = Field(default=10000, ge=1, le=100000)
    rpc_pages_per_call: int = Field(default=12, ge=1, le=100)
    cursor_ttl_seconds: int = Field(default=900, ge=1)
    max_cursors: int = Field(default=128, ge=1)

    def credentials(self) -> tuple[int, str]:
        if self.api_id is None or self.api_hash is None:
            raise ToolError("AUTH_REQUIRED", "Run telegram-mcp login in a local terminal first.")
        return self.api_id, self.api_hash.get_secret_value()


def config_path() -> Path:
    return Path(
        os.environ.get("TELEGRAM_MCP_CONFIG", user_config_path("telegram-mcp") / "config.toml")
    ).expanduser()


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    if value := os.environ.get("TELEGRAM_API_ID"):
        data["api_id"] = int(value)
    if value := os.environ.get("TELEGRAM_API_HASH"):
        data["api_hash"] = value
    if value := os.environ.get("TELEGRAM_SESSION_PATH"):
        data["session_path"] = value
    settings = Settings.model_validate(data)
    settings.session_path = settings.session_path.expanduser().absolute()
    if not str(settings.session_path).endswith(".session"):
        settings.session_path = Path(str(settings.session_path) + ".session")
    return settings
