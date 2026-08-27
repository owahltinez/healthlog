"""Private OAuth token storage."""

import json
import os
from pathlib import Path
from typing import Any

ENV_CONFIG_DIR = "HEALTHLOG_CONFIG_DIR"


def get_config_dir() -> Path:
    configured = os.getenv(ENV_CONFIG_DIR)
    path = (
        Path(configured) if configured else Path.home() / ".config/healthlog"
    )
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def get_tokens_path() -> Path:
    return get_config_dir() / "tokens.json"


def save_tokens(tokens: dict[str, Any]) -> None:
    path = get_tokens_path()
    path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    path.chmod(0o600)


def load_tokens() -> dict[str, Any] | None:
    path = get_tokens_path()
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def delete_tokens() -> bool:
    path = get_tokens_path()
    if not path.is_file():
        return False
    path.unlink()
    return True
