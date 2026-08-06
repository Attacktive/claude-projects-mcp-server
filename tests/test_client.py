import pytest

from claude_projects_mcp.client import ClaudeProjectsClient
from claude_projects_mcp.errors import ApiError, NotFoundError, RateLimitedError

from .conftest import ORGANIZATION, PROJECT


class TestOrgs:
	def test_lists_only_chat_capable_orgs(self, api):
		api.add_organization("organization-api-only", capabilities=["api"])
		client = ClaudeProjectsClient(api)

		assert [organization.uuid for organization in client.list_organizations()] == [ORGANIZATION]

	def test_fetches_the_org_list_once_across_calls(self, api, client):
		client.list_organizations()
		client.list_organizations()

		assert api.methods_logged().count("GET") == 1, "organization membership does not change mid-session"


class TestProjects:
	def test_lists_projects_across_every_chat_capable_org(self, api):
		api.add_organization("organization-2", capabilities=["chat"])
		api.add_project("organization-2", "project-2", name="Second")
		client = ClaudeProjectsClient(api)

		projects = client.list_projects()

		assert {project.uuid for project in projects} == {PROJECT, "project-2"}

	def test_each_project_carries_its_owning_org(self, api, client):
		"""A project uuid alone is not actionable on a multi-organization account."""
		projects = client.list_projects()

		assert projects[0].organization_uuid == ORGANIZATION

	def test_can_be_scoped_to_one_org(self, api):
		api.add_organization("organization-2", capabilities=["chat"])
		api.add_project("organization-2", "project-2")
		client = ClaudeProjectsClient(api)

		assert [project.uuid for project in client.list_projects(organization_id="organization-2")] == ["project-2"]

	def test_resolves_which_org_owns_a_project(self, api):
		api.add_organization("organization-2", capabilities=["chat"])
		api.add_project("organization-2", "project-2")
		client = ClaudeProjectsClient(api)

		assert client.resolve_organization_for_project("project-2") == "organization-2"

	def test_an_unknown_project_is_a_not_found_error(self, client):
		with pytest.raises(NotFoundError) as exception_info:
			client.resolve_organization_for_project("nope")

		assert "nope" in str(exception_info.value)

	def test_the_search_stops_at_the_org_that_has_it(self, api):
		api.add_organization("organization-2", capabilities=["chat"])
		api.add_project("organization-2", "project-2")
		client = ClaudeProjectsClient(api)

		client.resolve_organization_for_project(PROJECT)

		assert not any("organization-2" in path for _, path in api.log), "stopped once it found the owner"

	def test_resolution_is_cached(self, api, client):
		client.resolve_organization_for_project(PROJECT)
		before = len(api.log)
		client.resolve_organization_for_project(PROJECT)

		assert len(api.log) == before


class TestReadingDocs:
	def test_lists_documents(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")

		documents = client.list_documents(PROJECT)

		assert [document.file_name for document in documents] == ["notes.md"]

	def test_listing_yields_stubs_when_the_api_omits_content(self, stub_api, stub_client):
		stub_api.add_document(PROJECT, "notes.md", "hello")

		assert stub_client.list_documents(PROJECT)[0].is_stub is True

	def test_listing_yields_full_docs_when_the_api_includes_content(self, api, client):
		"""What the real API was observed doing on 2026-08-06."""
		api.add_document(PROJECT, "notes.md", "hello")

		assert client.list_documents(PROJECT)[0].content == "hello"

	def test_read_by_name_needs_no_extra_fetch_when_the_listing_has_content(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		api.log.clear()

		assert client.read_document(PROJECT, "notes.md").content == "hello"
		assert not [path for _, path in api.log if "/docs/" in path], "the listing already had it"

	def test_gets_one_document_with_its_content(self, api, client):
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		assert client.get_document(PROJECT, uuid).content == "hello"

	def test_read_by_name_fetches_content_for_a_stub(self, stub_api, stub_client):
		stub_api.add_document(PROJECT, "notes.md", "hello")

		assert stub_client.read_document(PROJECT, "notes.md").content == "hello"

	def test_read_accepts_a_uuid(self, api, client):
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		assert client.read_document(PROJECT, uuid).content == "hello"

	def test_read_of_a_missing_name_is_a_not_found_error(self, client):
		with pytest.raises(NotFoundError):
			client.read_document(PROJECT, "absent.md")

	def test_read_of_a_duplicated_name_returns_the_newest(self, api, client):
		api.add_document(PROJECT, "notes.md", "older")
		api.add_document(PROJECT, "notes.md", "newer")

		assert client.read_document(PROJECT, "notes.md").content == "newer"

	def test_find_by_name_returns_every_match_newest_first(self, api, client):
		api.add_document(PROJECT, "notes.md", "older")
		api.add_document(PROJECT, "notes.md", "newer")
		api.add_document(PROJECT, "other.md", "x")

		matches = client.find_documents_by_name(PROJECT, "notes.md")

		assert len(matches) == 2
		assert matches[0].uuid != matches[1].uuid


class TestWritingDocs:
	def test_creates_a_document(self, api, client):
		document = client.create_document(PROJECT, "notes.md", "hello")

		assert document.file_name == "notes.md"
		assert api.content_of(PROJECT, "notes.md") == ["hello"]

	def test_deletes_a_document(self, api, client):
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		client.delete_document(PROJECT, uuid)

		assert api.document_names(PROJECT) == []

	def test_deleting_something_already_gone_is_not_an_error(self, client):
		"""A teammate deleting it first achieved the same end state."""
		client.delete_document(PROJECT, "never-existed")


class TestRateLimiting:
	def test_retries_after_a_429_and_succeeds(self, api):
		slept = []
		client = ClaudeProjectsClient(api, sleep=slept.append)
		api.fail_once("GET", "/docs$", RateLimitedError("slow down", retry_after=3))
		api.add_document(PROJECT, "notes.md", "hello")

		documents = client.list_documents(PROJECT)

		assert [document.file_name for document in documents] == ["notes.md"]
		assert slept == [3]

	def test_falls_back_to_a_default_delay_when_retry_after_is_absent(self, api):
		slept = []
		client = ClaudeProjectsClient(api, sleep=slept.append)
		api.fail_once("GET", "/docs$", RateLimitedError("slow down"))

		client.list_documents(PROJECT)

		assert slept and slept[0] > 0

	def test_a_long_retry_after_is_capped(self, api):
		"""An MCP call that blocks for an hour is worse than one that fails."""
		slept = []
		client = ClaudeProjectsClient(api, sleep=slept.append)
		api.fail_once("GET", "/docs$", RateLimitedError("slow down", retry_after=9999))

		client.list_documents(PROJECT)

		assert slept[0] <= 30

	def test_gives_up_after_repeated_rate_limits(self, api):
		client = ClaudeProjectsClient(api, sleep=lambda _: None)
		for _ in range(5):
			api.fail_once("GET", "/docs$", RateLimitedError("slow down"))

		with pytest.raises(RateLimitedError):
			client.list_documents(PROJECT)

	def test_other_errors_are_not_retried(self, api):
		client = ClaudeProjectsClient(api, sleep=lambda _: None)
		api.fail_once("GET", "/docs$", ApiError("boom", status=500))

		with pytest.raises(ApiError):
			client.list_documents(PROJECT)

		docs_requests = [path for _, path in api.log if path.endswith("/docs")]
		assert len(docs_requests) == 1, "a 500 is an answer, not a hiccup; it must not be retried"
