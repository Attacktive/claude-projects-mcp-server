import pytest

from claude_projects_mcp.errors import ApiError
from claude_projects_mcp.models import Document, Organization, Project


class TestOrg:
	def test_parses_the_fields_it_needs(self):
		organization = Organization.parse({"uuid": "o1", "name": "Acme", "capabilities": ["chat", "claude_pro"]})

		assert organization.uuid == "o1"
		assert organization.name == "Acme"
		assert organization.capabilities == ("chat", "claude_pro")

	def test_ignores_unknown_fields(self):
		"""An undocumented API grows fields without warning; that must not break us."""
		organization = Organization.parse({"uuid": "o1", "name": "x", "capabilities": [], "settings": {"a": 1}})

		assert organization.uuid == "o1"

	def test_a_missing_required_field_fails_loudly(self):
		with pytest.raises(ApiError) as exception_info:
			Organization.parse({"name": "x"})

		assert "uuid" in str(exception_info.value)

	def test_missing_capabilities_is_treated_as_none_rather_than_an_error(self):
		assert Organization.parse({"uuid": "o1", "name": "x"}).capabilities == ()

	@pytest.mark.parametrize(
		"capabilities,expected",
		[
			(["chat"], True),
			(["claude_pro"], True),
			(["chat", "api"], True),
			(["api"], False),
			([], False),
		],
	)
	def test_chat_capability(self, capabilities, expected):
		organization = Organization.parse({"uuid": "o1", "name": "x", "capabilities": capabilities})

		assert organization.is_chat_capable is expected


class TestProject:
	def test_parses_and_renames_prompt_template_to_instructions(self):
		project = Project.parse(
			{
				"uuid": "p1",
				"name": "Infra",
				"description": "d",
				"prompt_template": "be terse",
				"created_at": "2026-08-01T00:00:00Z",
				"updated_at": "2026-08-05T00:00:00Z",
			}
		)

		assert project.uuid == "p1"
		assert project.instructions == "be terse"

	def test_optional_text_fields_default_to_empty(self):
		project = Project.parse({"uuid": "p1", "name": "Infra"})

		assert project.description == ""
		assert project.instructions == ""
		assert project.updated_at is None

	def test_carries_the_owning_org_when_given_one(self):
		"""A project uuid alone is not actionable on a multi-organization account."""
		project = Project.parse({"uuid": "p1", "name": "Infra"}, organization_uuid="o1")

		assert project.organization_uuid == "o1"

	def test_a_missing_uuid_fails_loudly(self):
		with pytest.raises(ApiError):
			Project.parse({"name": "Infra"})


class TestDoc:
	def test_parses_a_full_document(self):
		document = Document.parse(
			{
				"uuid": "d1",
				"file_name": "notes.md",
				"content": "hello",
				"created_at": "2026-08-01T00:00:00Z",
			}
		)

		assert document.uuid == "d1"
		assert document.file_name == "notes.md"
		assert document.content == "hello"

	def test_a_listing_entry_without_content_parses_as_a_stub(self):
		document = Document.parse({"uuid": "d1", "file_name": "notes.md", "created_at": "x"})

		assert document.content is None
		assert document.is_stub is True

	def test_a_document_with_content_is_not_a_stub(self):
		document = Document.parse({"uuid": "d1", "file_name": "n.md", "content": "", "created_at": "x"})

		assert document.is_stub is False, "empty content is still content"

	def test_a_missing_file_name_fails_loudly(self):
		with pytest.raises(ApiError):
			Document.parse({"uuid": "d1"})

	def test_chars_reports_content_length_only_when_known(self):
		assert Document.parse({"uuid": "d", "file_name": "n.md", "content": "abc"}).characters == 3
		assert Document.parse({"uuid": "d", "file_name": "n.md"}).characters is None


def test_a_list_response_that_is_not_a_list_fails_loudly():
	"""If the API ever adds a pagination envelope, that must surface immediately."""
	with pytest.raises(ApiError) as exception_info:
		Document.parse_list({"data": [], "pagination": {"has_more": False}})

	assert "list" in str(exception_info.value).lower()


def test_parse_list_builds_each_item():
	documents = Document.parse_list(
		[
			{"uuid": "d1", "file_name": "a.md"},
			{"uuid": "d2", "file_name": "b.md"},
		]
	)

	assert [document.uuid for document in documents] == ["d1", "d2"]
