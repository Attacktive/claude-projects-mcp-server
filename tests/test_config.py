from pathlib import Path

import pytest

from claude_projects_mcp.config import DEFAULT_BASE_URL, Settings
from claude_projects_mcp.errors import ConfigError

MINIMAL = {"CLAUDE_PROJECTS_SESSION_KEY": "sk-ant-sid01-abc"}


def test_reads_session_key():
	settings = Settings.from_env(MINIMAL)

	assert settings.session_key == "sk-ant-sid01-abc"


def test_missing_session_key_is_an_actionable_error():
	with pytest.raises(ConfigError) as exception_info:
		Settings.from_env({})

	message = str(exception_info.value)
	assert "CLAUDE_PROJECTS_SESSION_KEY" in message
	assert "sessionKey" in message, "should say where to get the value"
	assert ".env" in message, "should say where to put it"


def test_blank_session_key_counts_as_missing():
	with pytest.raises(ConfigError):
		Settings.from_env({"CLAUDE_PROJECTS_SESSION_KEY": "   "})


def test_values_are_stripped():
	settings = Settings.from_env({"CLAUDE_PROJECTS_SESSION_KEY": "  sk-ant-sid01-abc  "})

	assert settings.session_key == "sk-ant-sid01-abc"


def test_base_url_defaults_and_can_be_overridden():
	assert Settings.from_env(MINIMAL).base_url == DEFAULT_BASE_URL

	env = MINIMAL | {"CLAUDE_PROJECTS_BASE_URL": "http://localhost:8899/api/"}
	assert Settings.from_env(env).base_url == "http://localhost:8899/api", "trailing slash trimmed"


def test_impersonate_defaults_to_latest_chrome():
	"""Pinning an old fingerprint rots: Cloudflare scores fingerprint age."""
	assert Settings.from_env(MINIMAL).impersonate == "chrome"


def test_backup_dir_honours_xdg_data_home():
	env = MINIMAL | {"XDG_DATA_HOME": "/xdg/data"}
	settings = Settings.from_env(env)

	assert settings.backup_directory == Path("/xdg/data/claude-projects-mcp/trash")


def test_backup_dir_falls_back_to_local_share(monkeypatch):
	monkeypatch.setenv("HOME", "/home/someone")
	settings = Settings.from_env(MINIMAL)

	assert settings.backup_directory == Path("/home/someone/.local/share/claude-projects-mcp/trash")


def test_explicit_backup_dir_wins_and_is_expanded(monkeypatch):
	monkeypatch.setenv("HOME", "/home/someone")
	env = MINIMAL | {"XDG_DATA_HOME": "/xdg/data", "CLAUDE_PROJECTS_BACKUP_DIRECTORY": "~/my-backups"}
	settings = Settings.from_env(env)

	assert settings.backup_directory == Path("/home/someone/my-backups")


def test_repr_does_not_leak_the_session_key():
	"""The key is a live account credential; it must not reach logs or tracebacks."""
	settings = Settings.from_env(MINIMAL)

	assert "sk-ant-sid01-abc" not in repr(settings)
