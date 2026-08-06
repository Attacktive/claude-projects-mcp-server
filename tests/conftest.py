import pytest

from claude_projects_mcp.client import ClaudeProjectsClient

from .fake_transport import FakeClaudeProjectsApi

ORGANIZATION = "organization-1"
PROJECT = "project-1"


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
