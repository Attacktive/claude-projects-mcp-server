import pytest

from claude_projects_mcp.errors import ApiError
from claude_projects_mcp.models import Document, Organization, Project, UploadedFile


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


class TestDocumentCounts:
	def test_a_token_count_that_is_not_a_number_reads_as_unknown(self):
		"""list_documents sums these; a string would fail the whole listing with a bare TypeError rather than the "could not be checked" warning."""
		document = Document.parse({"uuid": "document-1", "file_name": "notes.md", "estimated_token_count": "1234"})

		assert document.estimated_token_count is None


class TestUploadedFile:
	def test_parses_the_fields_it_needs(self):
		upload = UploadedFile.parse(
			{
				"uuid": "file-1",
				"file_uuid": "file-1",
				"file_name": "report.pdf",
				"file_kind": "document",
				"created_at": "2026-08-06T04:17:33.022454Z",
				"size_bytes": 1054702,
				"preview_asset": None,
				"document_asset": {"url": "/api/organization-1/files/file-1/document_pdf", "file_variant": "original", "page_count": 7, "token_count": None},
				"unknown": "ignored",
			}
		)

		assert upload.uuid == "file-1"
		assert upload.file_name == "report.pdf"
		assert upload.file_kind == "document"
		assert upload.created_at == "2026-08-06T04:17:33.022454Z"
		assert upload.size_bytes == 1054702
		assert upload.page_count == 7
		assert upload.download_url == "/api/organization-1/files/file-1/document_pdf"

	def test_falls_back_to_file_uuid_when_uuid_is_absent(self):
		upload = UploadedFile.parse({"file_uuid": "file-1", "file_name": "report.pdf"})

		assert upload.uuid == "file-1"

	def test_a_missing_identifier_fails_loudly(self):
		with pytest.raises(ApiError):
			UploadedFile.parse({"file_name": "report.pdf"})

	def test_a_missing_file_name_fails_loudly(self):
		with pytest.raises(ApiError):
			UploadedFile.parse({"uuid": "file-1"})

	def test_a_preview_is_not_what_downloads(self):
		upload = UploadedFile.parse(
			{
				"uuid": "file-1",
				"file_name": "photo.png",
				"file_kind": "image",
				"document_asset": None,
				"preview_asset": {"url": "/api/organization-1/files/file-1/preview", "file_variant": "preview"},
			}
		)

		assert upload.download_url is None, "a preview is a rendition, not the file"
		assert upload.page_count is None

	def test_without_any_asset_there_is_nothing_to_download(self):
		upload = UploadedFile.parse({"uuid": "file-1", "file_name": "mystery.bin"})

		assert upload.download_url is None

	def test_parse_list_requires_a_bare_array(self):
		with pytest.raises(ApiError):
			UploadedFile.parse_list({"data": []})

	def test_a_size_or_page_count_that_is_not_a_number_reads_as_unknown(self):
		"""The client does arithmetic on these, so a string from a changed API has to land in the typed None path rather than raise a bare ValueError later."""
		upload = UploadedFile.parse(
			{
				"uuid": "file-1",
				"file_name": "report.pdf",
				"size_bytes": "1054702",
				"document_asset": {"url": "/api/organization-1/files/file-1/document_pdf", "file_variant": "original", "page_count": "7"},
			}
		)

		assert upload.size_bytes is None
		assert upload.page_count is None

	def test_a_document_asset_that_is_not_the_original_is_not_downloadable(self):
		upload = UploadedFile.parse(
			{
				"uuid": "file-1",
				"file_name": "report.pdf",
				"document_asset": {"url": "/api/organization-1/files/file-1/preview", "file_variant": "preview"},
			}
		)

		assert upload.download_url is None

	def test_a_document_asset_without_a_variant_is_trusted(self):
		"""Tolerant of a field going missing, intolerant of one saying the wrong thing."""
		upload = UploadedFile.parse({"uuid": "file-1", "file_name": "report.pdf", "document_asset": {"url": "/api/organization-1/files/file-1/document_pdf"}})

		assert upload.download_url == "/api/organization-1/files/file-1/document_pdf"
