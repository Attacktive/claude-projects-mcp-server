import json
from pathlib import Path

import pytest

from claude_projects_mcp.client import ClaudeProjectsClient
from claude_projects_mcp.config import Settings

from .fake_transport import FakeClaudeProjectsApi

ORGANIZATION = "organization-1"
PROJECT = "project-1"


@pytest.fixture
def anyio_backend():
	return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
	"""Settings pointing at a backup directory this test alone owns."""
	return Settings.from_env(
		{
			"CLAUDE_PROJECTS_SESSION_KEY": "sk-ant-sid01-test",
			"CLAUDE_PROJECTS_BACKUP_DIRECTORY": str(tmp_path / "trash"),
		}
	)


async def call(server, tool, **arguments):
	"""Invoke a tool the way an MCP client does, and hand back its parsed answer."""
	# `tool` rather than `name`, so a tool taking a `name` argument can still be called.
	result = await server.call_tool(tool, arguments)
	return json.loads(result.content[0].text)


@pytest.fixture
def api() -> FakeClaudeProjectsApi:
	"""A fake API preloaded with one chat-capable organization holding one project."""
	fake = FakeClaudeProjectsApi()
	fake.add_organization(ORGANIZATION, name="Acme", capabilities=["chat", "claude_pro"])
	fake.add_project(ORGANIZATION, PROJECT, name="팀 지식 베이스")
	return fake


@pytest.fixture
def client(api: FakeClaudeProjectsApi) -> ClaudeProjectsClient:
	return ClaudeProjectsClient(api)


@pytest.fixture
def stub_api() -> FakeClaudeProjectsApi:
	"""A fake whose listings omit content, so a separate fetch is needed per document.

	The real API includes content today.
	This covers the fallback, which matters because nothing obliges an undocumented API to keep doing that.
	"""
	fake = FakeClaudeProjectsApi(list_includes_content=False)
	fake.add_organization(ORGANIZATION, name="Acme", capabilities=["chat", "claude_pro"])
	fake.add_project(ORGANIZATION, PROJECT, name="팀 지식 베이스")
	return fake


@pytest.fixture
def stub_client(stub_api: FakeClaudeProjectsApi) -> ClaudeProjectsClient:
	return ClaudeProjectsClient(stub_api)
