"""Project CRUD at the client level.

Documents live inside projects, so the same account can now create and destroy the containers as well as their contents.
Deleting a project takes every document with it, which is why the destructive half of this is deliberately awkward to reach — see the `delete_project` tool in server.py for the confirmation gate.
"""

import pytest

from claude_projects_mcp.client import ClaudeProjectsClient
from claude_projects_mcp.errors import ApiError, ConfigError, NotFoundError

from .conftest import ORGANIZATION, PROJECT


class TestCreate:
	def test_creates_a_project_and_returns_it(self, client):
		project = client.create_project("Nový projekt", description="shared notes")

		assert project.uuid
		assert project.name == "Nový projekt"
		assert project.description == "shared notes"

	def test_the_new_project_is_really_there(self, api, client):
		created = client.create_project("Second")

		assert created.uuid in api.projects

	def test_description_is_sent_even_when_empty(self, api, client):
		"""The real API answers 400 'description: Field required' without it."""
		client.create_project("No description")

		assert api.projects, "the fake rejects a create with no description, as the API does"

	def test_created_project_carries_its_owning_org(self, client):
		assert client.create_project("Owned").organization_uuid == ORGANIZATION

	def test_instructions_are_applied_when_given(self, client):
		"""The create endpoint has no field for them, so this costs a follow-up update."""
		project = client.create_project("Guided", instructions="Answer in Korean.")

		assert project.instructions == "Answer in Korean."

	def test_no_pointless_update_when_no_instructions_are_given(self, api, client):
		client.create_project("Plain")

		assert "PUT" not in api.methods_logged()

	def test_privacy_is_honoured(self, client):
		assert client.create_project("Mine", is_private=True).is_private is True
		assert client.create_project("Ours").is_private is False

	def test_targets_the_named_org(self, api):
		api.add_organization("organization-2", capabilities=["chat"])
		client = ClaudeProjectsClient(api)

		created = client.create_project("Elsewhere", organization_id="organization-2")

		assert created.organization_uuid == "organization-2"
		assert api.projects[created.uuid]["_organization"] == "organization-2"

	def test_the_only_org_needs_no_saying(self, client):
		assert client.create_project("Obvious").organization_uuid == ORGANIZATION

	def test_several_orgs_and_no_hint_is_a_refusal_that_names_them(self, api):
		api.add_organization("organization-2", name="Other Team", capabilities=["chat"])
		client = ClaudeProjectsClient(api)

		with pytest.raises(ConfigError) as exception_info:
			client.create_project("Ambiguous")

		message = str(exception_info.value)
		assert "organization-2" in message and ORGANIZATION in message, "should name the candidates"
		assert "organization_id" in message, "should say how to settle it"

	def test_a_new_project_needs_no_search_to_be_used(self, api, client):
		"""Creating it already told us the organization, so the next call must not rescan."""
		created = client.create_project("Fresh")
		before = len(api.log)

		assert client.resolve_organization_for_project(created.uuid) == ORGANIZATION
		assert len(api.log) == before


class TestRead:
	def test_fetches_one_project_with_its_instructions(self, api, client):
		api.add_project(ORGANIZATION, "project-2", name="Guided", instructions="Be brief.")

		assert client.get_project("project-2").instructions == "Be brief."

	def test_a_listing_carries_no_instructions(self, api, client):
		"""Observed 2026-08-06: the listing omits prompt_template, the single fetch has it."""
		api.add_project(ORGANIZATION, "project-2", name="Guided", instructions="Be brief.")

		listed = next(project for project in client.list_projects() if project.uuid == "project-2")

		assert listed.instructions == ""

	def test_an_unknown_project_is_not_found(self, client):
		with pytest.raises(NotFoundError):
			client.get_project("project-nope")


class TestUpdate:
	def test_renames_a_project(self, client):
		assert client.update_project(PROJECT, name="Renamed").name == "Renamed"

	def test_sets_instructions_under_the_name_the_api_uses(self, api, client):
		client.update_project(PROJECT, instructions="Answer in Korean.")

		assert api.projects[PROJECT]["prompt_template"] == "Answer in Korean."

	def test_leaves_untouched_fields_alone(self, api, client):
		"""A partial body is the whole point: this must not blank the other fields."""
		api.projects[PROJECT]["description"] = "keep me"

		client.update_project(PROJECT, name="Renamed")

		assert api.projects[PROJECT]["description"] == "keep me"

	def test_an_empty_string_still_clears_a_field(self, api, client):
		"""None means 'leave alone'; '' means 'make it empty'. They must not be confused."""
		api.projects[PROJECT]["description"] = "clear me"

		client.update_project(PROJECT, description="")

		assert api.projects[PROJECT]["description"] == ""

	def test_updating_nothing_is_refused_rather_than_sent(self, api, client):
		with pytest.raises(ValueError):
			client.update_project(PROJECT)

		assert "PUT" not in api.methods_logged(), "a no-op must not cost a request"

	def test_an_unknown_project_is_not_found(self, client):
		with pytest.raises(NotFoundError):
			client.update_project("project-nope", name="Ghost")


class TestDelete:
	def test_deletes_the_project(self, api, client):
		assert client.delete_project(PROJECT) is True
		assert PROJECT not in api.projects

	def test_deleting_twice_is_not_an_error(self, client):
		"""A teammate deleting it first reached the same end state."""
		client.delete_project(PROJECT)

		assert client.delete_project(PROJECT) is False

	def test_the_documents_go_with_it(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")

		client.delete_project(PROJECT)

		assert PROJECT not in api.documents

	def test_the_stale_org_mapping_is_forgotten(self, api, client):
		"""Otherwise a re-created uuid would resolve against a cached, possibly wrong organization."""
		client.resolve_organization_for_project(PROJECT)
		client.delete_project(PROJECT)

		assert PROJECT not in client._project_organizations


class TestPagination:
	"""The listing is paginated, and a prefix must never pass for the whole thing.

	The older endpoint returned a bare array, which could not say whether more remained.
	This one does, so "no such project in this organization" is now a real answer rather than a guess about how many the server felt like sending.
	"""

	def test_every_page_is_walked(self, api):
		for index in range(250):
			api.add_project(ORGANIZATION, f"project-{index:03d}")

		client = ClaudeProjectsClient(api)

		assert len(client.list_projects()) == 251, "one from the fixture plus 250"

	def test_a_project_on_a_later_page_still_resolves(self, api):
		for index in range(250):
			api.add_project(ORGANIZATION, f"project-{index:03d}")

		client = ClaudeProjectsClient(api)

		assert client.resolve_organization_for_project("project-249") == ORGANIZATION

	def test_a_small_account_costs_a_single_page(self, api, client):
		client.list_projects()

		pages = [path for _, path in api.log if "projects_v2" in path]
		assert len(pages) == 1

	def test_a_listing_that_never_ends_is_refused_rather_than_truncated(self):
		class NeverEnding:
			"""Always claims there is more, so the guard has something to catch."""

			def request(self, method, path, *, json_body=None):
				if path == "/organizations":
					return [{"uuid": "organization-1", "name": "Organization", "capabilities": ["chat"]}]

				return {"data": [{"uuid": "p"}], "pagination": {"has_more": True}}

			def close(self):
				pass

		with pytest.raises(ApiError) as exception_info:
			ClaudeProjectsClient(NeverEnding()).list_projects()

		assert "pages" in str(exception_info.value)


class TestEveryOrgIsReachable:
	"""There is no configured-organization setting: a project is found wherever it lives.

	An earlier design pinned one organization from the environment, which quietly made every project outside it unreachable.
	Searching is cheap — one listing per organization per session, cached — and it cannot be wrong.
	"""

	def test_a_project_in_any_org_resolves(self, api):
		api.add_organization("organization-2", capabilities=["chat"])
		api.add_project("organization-2", "project-2")
		client = ClaudeProjectsClient(api)

		assert client.resolve_organization_for_project("project-2") == "organization-2"

	def test_an_org_is_listed_once_however_many_projects_are_resolved(self, api):
		api.add_project(ORGANIZATION, "project-2")
		client = ClaudeProjectsClient(api)

		client.resolve_organization_for_project(PROJECT)
		before = len(api.log)
		client.resolve_organization_for_project("project-2")

		assert len(api.log) == before, "the first search cached every project it saw"

	def test_a_project_in_no_org_is_an_error(self, client):
		with pytest.raises(NotFoundError):
			client.resolve_organization_for_project("project-unlisted")
