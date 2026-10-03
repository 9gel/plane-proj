"""Where the API key comes from, and the wrapper that keeps it out of tracebacks.

**A credential in a plain `str` is printed by any traceback that renders
locals.** Python does not do that by default, but `rich`, `better-exceptions`,
Sentry, and several test runners do, and a library frame deep inside `requests`
holding the header dict is enough. The key is therefore carried in `Secret` all
the way to the one call that needs its value, and `Secret.__repr__` and
`__str__` redact.

`reveal()` is deliberately ugly to read at a call site. There should be exactly
one of them per program, and a review should be able to find it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values, set_key

from plane_proj.guards import ConfigError

HOST_ENV_VAR = "PLANE_API_HOST_URL"
KEY_ENV_VAR = "PLANE_API_KEY"
SLUG_ENV_VAR = "PLANE_WORKSPACE_SLUG"

REDACTED = "<redacted>"
CLOUD_UI_HOSTS = frozenset({"app.plane.so", "plane.so", "www.plane.so"})


class Secret:
    """A string that does not render itself.

    Not a dataclass and not a `str` subclass: both would print the value
    through some path — a dataclass through its generated `__repr__`, a `str`
    subclass through every format call that never asked.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The plaintext. One call site, in `board.discover`."""
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class Credentials:
    """Everything needed to reach one Plane workspace."""

    host: str
    api_key: Secret
    workspace_slug: str


def load_credentials(
    config_slug: str | None = None, *, config_path: Path | str | None = None,
    env_file: Path | None = None,
) -> Credentials:
    """Read .env_plane, or .env if absent; explicit credentials override it.

    Default files are beside the selected config, or in cwd when none is selected.
    defaults.workspace scopes saved estimate UUIDs and wins over the env slug.
    """
    return _credentials_from_values(
        _environment_values(config_path, env_file), config_slug=config_slug
    )


def create_credentials_file(
    path: Path, config_slug: str | None = None, *, env_file: Path | None = None,
) -> Credentials:
    """Persist resolved Plane credentials for a newly initialized project."""
    values = _environment_values(path, env_file)
    credentials = _credentials_from_values(values, config_slug=config_slug)
    created = False
    try:
        path.touch(mode=0o600, exist_ok=False)
        created = True
        saved = {
            HOST_ENV_VAR: credentials.host,
            KEY_ENV_VAR: values[KEY_ENV_VAR].strip(),
            SLUG_ENV_VAR: credentials.workspace_slug,
        }
        for name, value in saved.items():
            set_key(path, name, value, quote_mode="always")
        path.chmod(0o600)
    except Exception:
        if created and path.exists():
            path.unlink()
        raise
    return credentials


def _credentials_from_values(
    values: dict[str, str], *, config_slug: str | None,
) -> Credentials:
    host = values.get(HOST_ENV_VAR, "").strip()
    key = values.get(KEY_ENV_VAR, "").strip()
    slug = (config_slug or "").strip() or values.get(SLUG_ENV_VAR, "").strip()
    missing = [name for name, value in ((HOST_ENV_VAR, host), (KEY_ENV_VAR, key)) if not value]
    if missing:
        raise ConfigError(
            f"{' and '.join(missing)} not set. Set them in .env_plane, .env, "
            "the process environment, or --env-file."
        )
    if not slug:
        raise ConfigError(
            f"Set defaults.workspace or {SLUG_ENV_VAR}. Plane can never recover "
            "the workspace slug without it being supplied."
        )
    if urlparse(host).hostname in CLOUD_UI_HOSTS:
        raise ConfigError(
            f"{HOST_ENV_VAR} points to Plane's web app. "
            "Plane Cloud API requests must use https://api.plane.so."
        )
    return Credentials(host=host.rstrip("/"), api_key=Secret(key), workspace_slug=slug)


def _environment_values(config_path: Path | str | None, env_file: Path | None) -> dict[str, str]:
    # Credentials live in the invocation directory, beside plane/.
    directory = Path.cwd()
    values: dict[str, str] = {}
    path = directory / ".env_plane"
    if not path.is_file():
        path = directory / ".env"
    if path.is_file():
        values.update(load_env_file(path))
    for name in (HOST_ENV_VAR, KEY_ENV_VAR, SLUG_ENV_VAR):
        if name in os.environ:
            values[name] = os.environ[name]
    if env_file is not None:
        values.update(load_env_file(env_file))
    return values


def load_connection_target(
    config_slug: str | None, *, config_path: Path | str | None, env_file: Path | None
) -> tuple[str, str]:
    """Resolve workspace and optional endpoint locally, without requiring an API key."""
    values = _environment_values(config_path, env_file)
    host = values.get(HOST_ENV_VAR, "").strip().rstrip("/")
    slug = (config_slug or "").strip() or values.get(SLUG_ENV_VAR, "").strip()
    if not slug:
        raise ConfigError(
            "Sprint register binding requires defaults.workspace "
            "or PLANE_WORKSPACE_SLUG in the selected config/environment."
        )
    return host, slug


def load_env_file(path: Path) -> dict[str, str]:
    """Parse dotenv syntax without executing shell code or importing unrelated keys."""
    wanted = {HOST_ENV_VAR, KEY_ENV_VAR, SLUG_ENV_VAR}
    return {
        name: value for name, value in dotenv_values(path, interpolate=False).items()
        if name in wanted and value is not None
    }
