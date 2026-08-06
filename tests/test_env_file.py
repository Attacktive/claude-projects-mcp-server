import os

from claude_projects_mcp.config import load_env_file


def test_loads_values_from_a_dot_env(tmp_path, monkeypatch):
	(tmp_path / ".env").write_text("CLAUDE_PROJECTS_SESSION_KEY=sk-ant-sid01-from-file\n", encoding="utf-8")
	monkeypatch.delenv("CLAUDE_PROJECTS_SESSION_KEY", raising=False)

	load_env_file(tmp_path)

	assert os.environ["CLAUDE_PROJECTS_SESSION_KEY"] == "sk-ant-sid01-from-file"


def test_a_real_environment_variable_wins(tmp_path, monkeypatch):
	"""An MCP client's configured environment must not be overridden by a stale file."""
	(tmp_path / ".env").write_text("CLAUDE_PROJECTS_SESSION_KEY=from-file\n", encoding="utf-8")
	monkeypatch.setenv("CLAUDE_PROJECTS_SESSION_KEY", "from-environment")

	load_env_file(tmp_path)

	assert os.environ["CLAUDE_PROJECTS_SESSION_KEY"] == "from-environment"


def test_a_missing_file_is_not_an_error(tmp_path):
	load_env_file(tmp_path)
