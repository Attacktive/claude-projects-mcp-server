"""Environment into a frozen Settings object.

`from_env` takes a mapping rather than reading `os.environ` itself, so tests never have to mutate global state and the future HTTP entrypoint can build settings from elsewhere.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

DEFAULT_BASE_URL = "https://claude.ai/api"

# curl_cffi's alias for the newest fingerprint it knows.
# Pinning a specific version (the reference client pins chrome110) rots, because Cloudflare scores fingerprint age.
DEFAULT_IMPERSONATE = "chrome"

_MISSING_KEY_HELP = "CLAUDE_PROJECTS_SESSION_KEY is not set. Put it in .env next to the project, or export it. To get one: open claude.ai in a browser, then DevTools -> Application -> Cookies -> sessionKey, and copy the value (it starts with sk-ant-sid02-, or sk-ant-sid01- on older accounts)."


def load_env_file(start: Path | None = None) -> None:
	"""Load a `.env` sitting next to the project, without overriding real environment.

	Kept separate from `Settings.from_env` so that parsing stays a pure function of a mapping.
	Existing variables win, so an MCP client's configured environment is never silently overridden by a stale file.
	"""
	from dotenv import load_dotenv

	if start is None:
		start = Path(__file__).resolve().parent.parent.parent

	candidate = Path(start) / ".env"
	if candidate.is_file():
		load_dotenv(candidate, override=False)


def _clean(env: Mapping[str, str], name: str) -> str | None:
	"""A blank or whitespace-only variable means unset, not empty."""
	value = env.get(name, "").strip()
	if not value:
		return None

	return value


@dataclass(frozen=True)
class Settings:
	session_key: str = field(repr=False)
	backup_directory: Path = Path(".")
	base_url: str = DEFAULT_BASE_URL
	impersonate: str = DEFAULT_IMPERSONATE

	@classmethod
	def from_env(cls, env: Mapping[str, str]) -> Settings:
		session_key = _clean(env, "CLAUDE_PROJECTS_SESSION_KEY")
		if session_key is None:
			raise ConfigError(_MISSING_KEY_HELP)

		base_url = _clean(env, "CLAUDE_PROJECTS_BASE_URL") or DEFAULT_BASE_URL

		return cls(
			session_key=session_key,
			backup_directory=_resolve_backup_directory(env),
			base_url=base_url.rstrip("/"),
			impersonate=_clean(env, "CLAUDE_PROJECTS_IMPERSONATE") or DEFAULT_IMPERSONATE,
		)


def _resolve_backup_directory(env: Mapping[str, str]) -> Path:
	"""Explicit setting, else XDG data home, else ~/.local/share."""
	explicit = _clean(env, "CLAUDE_PROJECTS_BACKUP_DIRECTORY")
	if explicit is not None:
		return Path(explicit).expanduser()

	xdg = _clean(env, "XDG_DATA_HOME")
	if xdg is not None:
		return Path(xdg).expanduser() / "claude-projects-mcp" / "trash"

	# os.path.expanduser reads HOME at call time, which keeps this monkeypatchable.
	return Path(os.path.expanduser("~")) / ".local" / "share" / "claude-projects-mcp" / "trash"
