"""Guards against the fake drifting away from the real API.

Every other test above the transport runs against `FakeClaudeProjectsApi`, so if the fake and claude.ai disagree, the suite stays green while the tool breaks.
These tests pin both ends to the response shapes the spike actually observed.

The fixtures hold shapes only: every leaf string was replaced before they were saved, so no team content or account identifier is committed.

They were captured on 2026-08-06 by two one-off probes, neither of which survives — the scripts were deleted and the history that briefly held one of them was squashed away.
Refreshing the shapes means writing a small probe again: `client.py` is the complete map of the endpoints and payloads it would need, and every leaf string must be replaced before saving, as above.

Day to day, `tests/live/test_contract.py` is the better canary anyway: it exercises the real endpoints end to end and needs no fixtures.
"""

import json
from pathlib import Path

import pytest

from claude_projects_mcp.models import Document, Organization, Project

from .fake_transport import FakeClaudeProjectsApi

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
	not FIXTURES.is_dir() or not any(FIXTURES.glob("*.json")),
	reason="no fixtures captured yet; recover a probe from git history (see the module docstring)",
)


def load(name: str):
	return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def keys_of(payload) -> set[str]:
	if isinstance(payload, list):
		if not payload:
			return set()

		return set(payload[0])

	return set(payload)


class TestRealResponsesParse:
	"""The parsers must accept what claude.ai actually sends, extra fields and all."""

	def test_organizations(self):
		organizations = Organization.parse_list(load("organizations"))

		assert organizations and all(organization.uuid for organization in organizations)

	def test_projects_page(self):
		projects, has_more = Project.parse_page(load("projects_v2"), organization_uuid="organization-1")

		assert projects and all(project.uuid for project in projects)
		assert has_more is False, "the captured page was the last one"

	def test_project_detail(self):
		assert Project.parse(load("project_detail")).uuid

	def test_project_created(self):
		assert Project.parse(load("project_created")).uuid

	def test_docs_list(self):
		documents = Document.parse_list(load("documents_list"))

		assert documents and all(document.uuid and document.file_name for document in documents)

	def test_doc_detail(self):
		assert Document.parse(load("document_detail")).uuid


class TestFakeMatchesReality:
	"""The fake may return fewer fields than the real API, but never invented ones."""

	@pytest.fixture
	def api(self):
		fake = FakeClaudeProjectsApi()
		fake.add_organization("organization-1", capabilities=["chat"])
		fake.add_project("organization-1", "project-1")
		fake.add_document("project-1", "notes.md", "hello")
		return fake

	def test_organization_fields_exist_upstream(self, api):
		fake_keys = keys_of(api.request("GET", "/organizations"))

		assert fake_keys <= keys_of(load("organizations"))

	def test_project_fields_exist_upstream(self, api):
		page = api.request("GET", "/organizations/organization-1/projects_v2?limit=100&offset=0")

		assert keys_of(page) <= keys_of(load("projects_v2")), "envelope"
		assert keys_of(page["data"]) <= keys_of(load("projects_v2")["data"]), "rows"

	def test_the_pagination_fields_the_client_reads_are_present(self, api):
		fake = api.request("GET", "/organizations/organization-1/projects_v2?limit=100&offset=0")["pagination"]

		assert set(fake) <= set(load("projects_v2")["pagination"])
		assert "has_more" in load("projects_v2")["pagination"], "paging stops on this field"

	def test_document_fields_exist_upstream(self, api):
		fake_keys = keys_of(api.request("GET", "/organizations/organization-1/projects/project-1/docs"))

		assert fake_keys <= keys_of(load("documents_list"))

	def test_created_project_fields_exist_upstream(self, api):
		created = api.request("POST", "/organizations/organization-1/projects", json_body={"name": "x", "description": ""})

		assert keys_of(created) <= keys_of(load("project_created"))

	def test_updated_project_fields_exist_upstream(self, api):
		"""The update response was observed matching the single-project fetch."""
		updated = api.request("PUT", "/organizations/organization-1/projects/project-1", json_body={"name": "x"})

		assert keys_of(updated) <= keys_of(load("project_detail"))


class TestObservedContract:
	"""Facts the spike established, which the implementation relies on."""

	def test_listings_are_bare_arrays_with_no_pagination_envelope(self):
		"""Projects are the exception, and the only listing that can report truncation."""
		for name in ("organizations", "documents_list"):
			assert isinstance(load(name), list), f"{name} gained an envelope; models.py must handle it"

	def test_the_projects_listing_is_paginated(self):
		page = load("projects_v2")

		assert isinstance(page, dict) and isinstance(page["data"], list)

	def test_document_listings_carry_content(self):
		"""If this ever fails, the fallback fetch in read_document and sync starts earning its keep."""
		assert "content" in keys_of(load("documents_list"))

	def test_only_a_single_project_fetch_carries_the_instructions(self):
		"""Why get_project exists: neither the listing nor a create response has them."""
		assert "prompt_template" in keys_of(load("project_detail"))
		assert "prompt_template" not in keys_of(load("projects_v2")["data"])
		assert "prompt_template" not in keys_of(load("project_created"))

	def test_projects_report_their_privacy(self):
		assert "is_private" in keys_of(load("projects_v2")["data"])
		assert "is_private" in keys_of(load("project_created"))

	def test_the_fields_the_code_requires_are_present(self):
		assert {"uuid"} <= keys_of(load("organizations"))
		assert {"uuid"} <= keys_of(load("projects_v2")["data"])
		assert {"uuid", "file_name"} <= keys_of(load("documents_list"))
		assert {"uuid", "file_name", "content"} <= keys_of(load("document_detail"))

	def test_no_credential_or_team_content_was_committed(self):
		for path in FIXTURES.glob("*.json"):
			text = path.read_text(encoding="utf-8")
			assert "sk-ant" not in text, f"{path.name} carries a credential"
