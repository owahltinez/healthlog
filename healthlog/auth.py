"""OAuth 2.0 authentication manager for Google Health API."""

import base64
import json
import os
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from healthlog.datatypes import READ_SCOPES, WRITE_SCOPES
from healthlog.storage import (
    delete_tokens,
    load_tokens,
    save_tokens,
)

SCOPES = [*WRITE_SCOPES, *READ_SCOPES]

TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

# Default bundled desktop client credentials for zero-friction user OAuth
_DEFAULT_CLIENT_ID_B64 = (
    "MjgxODQyMDk3MjUxLXBucGNpczlhOTN0cGVjNjQyMjFiY2o2MjNrdXZpYjNj"
    "LmFwcHMuZ29vZ2xldXNlcmNvbnRlbnQuY29t"
)
_DEFAULT_CLIENT_SECRET_B64 = "R09DU1BYLWNyZ0RDbHBoQWEwdWxKOUEyOTBwOTRxSEphNHk="

DEFAULT_CLIENT_ID = base64.b64decode(_DEFAULT_CLIENT_ID_B64).decode("utf-8")
DEFAULT_CLIENT_SECRET = base64.b64decode(_DEFAULT_CLIENT_SECRET_B64).decode(
    "utf-8"
)


def is_headless_or_ssh() -> bool:
    """Detect an SSH session, headless environment, or missing display."""
    if (
        os.getenv("SSH_CLIENT")
        or os.getenv("SSH_TTY")
        or os.getenv("SSH_CONNECTION")
    ):
        return True
    return os.name == "posix" and not (
        os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")
    )


def extract_auth_code(input_str: str) -> str:
    """Extract the authorization code from a raw code or full redirect URL."""
    cleaned = input_str.strip()
    if "code=" in cleaned:
        if cleaned.startswith("code="):
            code_part = cleaned.split("code=", 1)[1]
            return urllib.parse.unquote(code_part.split("&", 1)[0])
        if not cleaned.startswith(("http://", "https://")):
            cleaned = "http://" + cleaned
        parsed = urllib.parse.urlparse(cleaned)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            return qs["code"][0]
        for part in cleaned.split("&"):
            if part.startswith("code="):
                return urllib.parse.unquote(part.split("=", 1)[1])
    return cleaned


def get_client_config(
    client_config_path: Path | None = None,
) -> dict[str, Any] | None:
    """Retrieve client credentials from a path, env vars, or defaults.

    The packaged defaults are used when neither of the others is present.
    """
    if client_config_path and client_config_path.exists():
        try:
            return json.loads(client_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    env_id = os.getenv("HEALTHLOG_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID")
    env_secret = os.getenv("HEALTHLOG_CLIENT_SECRET") or os.getenv(
        "GOOGLE_CLIENT_SECRET"
    )
    if env_id and env_secret:
        return {
            "installed": {
                "client_id": env_id,
                "client_secret": env_secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "redirect_uris": ["http://localhost"],
            }
        }

    if DEFAULT_CLIENT_ID and DEFAULT_CLIENT_SECRET:
        return {
            "installed": {
                "client_id": DEFAULT_CLIENT_ID,
                "client_secret": DEFAULT_CLIENT_SECRET,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "redirect_uris": ["http://localhost"],
            }
        }

    return None


def _token_dict_from_creds(creds: Credentials) -> dict[str, Any]:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def get_credentials() -> Credentials | None:
    """Load valid credentials from storage, refreshing them if expired."""
    token_data = load_tokens()
    if not token_data:
        return None

    try:
        # The token's own scopes, not this version's. Google refuses to refresh
        # a token for a scope it never granted, so asking for a scope added
        # since the login would strand food logging over an unrelated type.
        creds = Credentials.from_authorized_user_info(
            token_data, token_data.get("scopes") or SCOPES
        )
    except (KeyError, TypeError, ValueError):
        return None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_tokens(_token_dict_from_creds(creds))
        except (GoogleAuthError, OSError):
            # If refresh fails, creds might be invalid
            return None

    return creds if (creds and (creds.valid or creds.token)) else None


def _create_flow(
    client_config_path: Path | None = None,
) -> InstalledAppFlow:
    if client_config_path and client_config_path.exists():
        return InstalledAppFlow.from_client_secrets_file(
            str(client_config_path),
            scopes=SCOPES,
        )
    client_config = get_client_config()
    if not client_config:
        raise ValueError(
            "No OAuth client credentials found. Please provide a "
            "client_secrets.json file, set HEALTHLOG_CLIENT_ID and "
            "HEALTHLOG_CLIENT_SECRET, or run 'healthlog auth setup'."
        )
    return InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)


def login(
    client_config_path: Path | None = None,
    port: int = 0,
    open_browser: bool | None = None,
) -> Credentials:
    """Run local server OAuth 2.0 flow to obtain user credentials."""
    flow = _create_flow(client_config_path)

    should_open = (
        open_browser
        if open_browser is not None
        else (not is_headless_or_ssh())
    )

    creds = flow.run_local_server(
        port=port,
        open_browser=should_open,
        prompt="consent",
        access_type="offline",
    )

    save_tokens(_token_dict_from_creds(creds))
    return creds


def login_remote(
    client_config_path: Path | None = None,
    input_callback: Callable[[str], str] | None = None,
) -> Credentials:
    """Run the copy-paste OAuth 2.0 flow.

    For remote SSH or otherwise browserless environments.
    """
    flow = _create_flow(client_config_path)
    flow.redirect_uri = "http://localhost"

    auth_url, _ = flow.authorization_url(
        prompt="consent", access_type="offline"
    )

    if input_callback:
        raw_input = input_callback(auth_url)
    else:
        click.echo(
            "\nGoogle OAuth Remote Authorization\n\n"
            f"1. Open this URL in your local browser:\n\n{auth_url}\n\n"
            "2. Grant consent. Your browser will redirect to a URL starting "
            "with http://localhost/?code=... . The browser may say the site "
            "cannot be reached; this is expected.\n\n"
            "3. Copy the entire URL from the address bar and paste it below."
        )
        raw_input = input(
            "Paste the redirected URL (or code) from address bar: "
        )

    code = extract_auth_code(raw_input)
    flow.fetch_token(code=code)
    creds = flow.credentials
    save_tokens(_token_dict_from_creds(creds))
    return creds


def get_auth_status() -> dict[str, Any]:
    """Get the current authentication status and metadata."""
    creds = get_credentials()
    has_custom = bool(
        os.getenv("HEALTHLOG_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID")
    )
    if not creds:
        return {
            "authenticated": False,
            "has_saved_tokens": load_tokens() is not None,
            "has_credentials_configured": get_client_config() is not None,
            "using_default_credentials": not has_custom,
        }

    return {
        "authenticated": True,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
        "scopes": creds.scopes,
        # A token predating a new data type is valid but reads less, and a 403
        # deep in a read is a worse way to find that out than asking.
        "missing_scopes": sorted(set(SCOPES) - set(creds.scopes or [])),
        "has_credentials_configured": True,
        "using_default_credentials": not has_custom,
    }


def logout() -> bool:
    """Log out by clearing stored user tokens."""
    return delete_tokens()
