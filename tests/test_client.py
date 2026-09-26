from dataclasses import replace

import pytest

from claude_projects_mcp.client import ClaudeProjectsClient
from claude_projects_mcp.errors import ApiError, BackupError, KnowledgeFullError, NotFoundError, RateLimitedError
from claude_projects_mcp.models import UploadedFile

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


class TestUploadedFiles:
	"""Files uploaded through the web UI, which the documents endpoint never lists (observed 2026-09-18)."""

	def test_lists_uploads_newest_first(self, api, client):
		older = api.add_upload(PROJECT, "older.pdf", b"%PDF-1.4 older")
		newer = api.add_upload(PROJECT, "newer.pdf", b"%PDF-1.4 newer")

		uploads = client.list_uploaded_files(PROJECT)

		assert [upload.uuid for upload in uploads] == [newer, older]

	def test_an_upload_reports_bytes_and_pages_rather_than_content(self, api, client):
		api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report", page_count=7)

		upload = client.list_uploaded_files(PROJECT)[0]

		assert upload.file_kind == "document"
		assert upload.size_bytes == len(b"%PDF-1.4 report")
		assert upload.page_count == 7

	def test_a_project_without_uploads_lists_none(self, client):
		assert client.list_uploaded_files(PROJECT) == []

	def test_an_unknown_project_is_a_not_found_error(self, client):
		with pytest.raises(NotFoundError):
			client.list_uploaded_files("no-such-project")

	def test_downloads_the_original_bytes(self, api, client):
		api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")
		upload = client.list_uploaded_files(PROJECT)[0]

		assert client.download_uploaded_file(upload) == b"%PDF-1.4 report"

	def test_an_upload_with_nothing_to_download_is_a_not_found_error(self, client):
		upload = UploadedFile(uuid="file-1", file_name="mystery.bin")

		with pytest.raises(NotFoundError) as exception_info:
			client.download_uploaded_file(upload)

		assert "mystery.bin" in str(exception_info.value)

	def test_a_rate_limited_download_is_retried(self, api):
		slept = []
		client = ClaudeProjectsClient(api, sleep=slept.append)
		api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")
		upload = client.list_uploaded_files(PROJECT)[0]
		api.fail_once("GET", "/document_pdf$", RateLimitedError("slow down", retry_after=2))

		assert client.download_uploaded_file(upload) == b"%PDF-1.4 report"
		assert slept == [2]

	def test_a_download_whose_size_disagrees_with_the_listing_is_an_api_error(self, api, client):
		"""size_bytes is the one check the listing offers on what came back; a short or padded body is not the file."""
		api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")
		upload = replace(client.list_uploaded_files(PROJECT)[0], size_bytes=3)

		with pytest.raises(ApiError) as exception_info:
			client.download_uploaded_file(upload)

		assert "3" in str(exception_info.value)

	def test_an_empty_download_is_an_api_error_even_when_the_listing_gives_no_size(self, api, client):
		"""With no size_bytes to compare against, an empty 200 is the one thing that can still be ruled out: no file has zero bytes."""
		api.add_upload(PROJECT, "report.pdf", b"")
		upload = replace(client.list_uploaded_files(PROJECT)[0], size_bytes=None)

		with pytest.raises(ApiError) as exception_info:
			client.download_uploaded_file(upload)

		assert "empty" in str(exception_info.value)

	def test_an_empty_download_matches_a_listing_that_says_zero_bytes(self, api, client):
		"""When the listing does give a size, the size comparison is the check; it is only the sizeless case that has to rule emptiness out on its own."""
		api.add_upload(PROJECT, "empty.pdf", b"")
		upload = client.list_uploaded_files(PROJECT)[0]

		assert upload.size_bytes == 0
		assert client.download_uploaded_file(upload) == b""

	def test_try_listing_hands_back_the_failure_instead_of_raising(self, api, client):
		api.fail_once("GET", "/files$", ApiError("claude.ai returned HTTP 500.", status=500))

		uploads, failure = client.try_list_uploaded_files(PROJECT)

		assert uploads is None
		assert isinstance(failure, ApiError)

	def test_try_listing_hands_back_the_uploads_when_it_can(self, api, client):
		api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")

		uploads, failure = client.try_list_uploaded_files(PROJECT)

		assert [upload.file_name for upload in uploads] == ["report.pdf"]
		assert failure is None


class TestUploadingFiles:
	"""Sending a file the way the web UI adds one to a project's knowledge (captured 2026-09-26), and removing one the way it does too."""

	def test_uploads_a_file_and_lists_it(self, api, client):
		result = client.upload_file(PROJECT, "photo.png", b"\x89PNG\r\n", "image/png")

		[upload] = client.list_uploaded_files(PROJECT)
		assert result.uuid == upload.uuid
		assert result.action == "created"
		assert upload.file_name == "photo.png"
		assert upload.file_kind == "image"
		assert upload.size_bytes == len(b"\x89PNG\r\n")

	def test_a_pdf_lists_as_a_document_upload_with_its_original(self, api, client):
		client.upload_file(PROJECT, "report.pdf", b"%PDF-1.4 report", "application/pdf")

		[upload] = client.list_uploaded_files(PROJECT)
		assert upload.file_kind == "document"
		assert client.download_uploaded_file(upload) == b"%PDF-1.4 report"

	def test_the_name_the_server_kept_is_reported(self, api, client):
		"""Observed 2026-09-26: a file sent as `Coffeevore (scaled).png` was listed as `Coffeevore scaled.png`, so the caller has to be told the stored name rather than assume its own."""
		result = client.upload_file(PROJECT, "Coffeevore (scaled).png", b"\x89PNG", "image/png")

		assert result.file_name == "Coffeevore scaled.png"
		assert [upload.file_name for upload in client.list_uploaded_files(PROJECT)] == ["Coffeevore scaled.png"]

	def test_a_rate_limited_upload_is_retried(self, api):
		slept = []
		client = ClaudeProjectsClient(api, sleep=slept.append)
		api.fail_once("POST", "/upload$", RateLimitedError("slow down", retry_after=1))

		client.upload_file(PROJECT, "photo.png", b"\x89PNG", "image/png")

		assert slept == [1]
		assert len(client.list_uploaded_files(PROJECT)) == 1

	def test_deletes_an_upload(self, api, client):
		uuid = api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")

		assert client.delete_uploaded_file(PROJECT, uuid) is True
		assert client.list_uploaded_files(PROJECT) == []

	def test_deleting_an_upload_already_gone_is_not_an_error(self, client):
		assert client.delete_uploaded_file(PROJECT, "never-existed") is False

	def test_an_upload_is_deleted_through_the_documents_route_with_its_uuid_in_the_body(self, api, client):
		"""Captured 2026-09-26: the web UI removes an upload with `DELETE .../docs/{uuid}` and a body of `{"docUuid": uuid}`, the documents route rather than a files one."""
		uuid = api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")

		client.delete_uploaded_file(PROJECT, uuid)

		assert ("DELETE", f"/organizations/{ORGANIZATION}/projects/{PROJECT}/docs/{uuid}") in api.log
		assert api.bodies_logged()[-1] == {"docUuid": uuid}

	def test_an_upload_crossing_the_search_threshold_is_refused_and_removed(self, api, client):
		"""The API reports no token count for an upload, so its cost is the change in the knowledge size across the upload; a refused one is deleted again."""
		api.projects[PROJECT]["_search_threshold"] = 50

		with pytest.raises(KnowledgeFullError) as exception_info:
			client.upload_file(PROJECT, "big.pdf", b"x" * 100, "application/pdf")

		assert "search threshold" in str(exception_info.value)
		assert "'big.pdf'" in str(exception_info.value)
		assert "100 tokens" in str(exception_info.value)
		assert client.list_uploaded_files(PROJECT) == []

	def test_an_upload_crossing_the_search_threshold_is_kept_when_search_mode_is_allowed(self, api, client):
		api.projects[PROJECT]["_search_threshold"] = 50

		result = client.upload_file(PROJECT, "big.pdf", b"x" * 100, "application/pdf", allow_search_mode=True)

		assert result.entered_search_mode is True
		assert len(client.list_uploaded_files(PROJECT)) == 1

	def test_an_upload_past_the_maximum_is_refused_whatever_the_caller_allows(self, api, client):
		api.projects[PROJECT]["_max_knowledge_size"] = 50

		with pytest.raises(KnowledgeFullError) as exception_info:
			client.upload_file(PROJECT, "big.pdf", b"x" * 100, "application/pdf", allow_search_mode=True)

		assert "maximum" in str(exception_info.value)
		assert client.list_uploaded_files(PROJECT) == []

	def test_a_refusal_survives_a_documents_listing_that_fails(self, api, client):
		"""The listing only feeds the refusal's advice on what to compact; a failure there must not turn the refusal into some other error, or a push would carry on into a full project."""
		api.projects[PROJECT]["_search_threshold"] = 50
		api.fail_once("GET", "/docs$", ApiError("claude.ai returned HTTP 500.", status=500))

		with pytest.raises(KnowledgeFullError) as exception_info:
			client.upload_file(PROJECT, "big.pdf", b"x" * 100, "application/pdf")

		assert "could not be listed" in str(exception_info.value)
		assert client.list_uploaded_files(PROJECT) == []

	def test_a_refused_upload_whose_removal_fails_is_reported_rather_than_raised(self, api, client):
		api.projects[PROJECT]["_search_threshold"] = 50
		api.fail_once("DELETE", "/docs/", ApiError("claude.ai returned HTTP 500.", status=500))

		result = client.upload_file(PROJECT, "big.pdf", b"x" * 100, "application/pdf")

		assert result.rollback_failed is True
		assert len(client.list_uploaded_files(PROJECT)) == 1

	def test_without_knowledge_stats_an_upload_goes_through_unchecked(self, api, client):
		api.projects[PROJECT]["_search_threshold"] = None

		result = client.upload_file(PROJECT, "photo.png", b"\x89PNG", "image/png")

		assert result.knowledge is None
		assert len(client.list_uploaded_files(PROJECT)) == 1

	def test_a_knowledge_size_that_did_not_move_is_an_unchecked_upload_rather_than_a_fit(self, api, client):
		"""A count that did not change cannot be told from one not yet updated, so the result must not claim a verdict it never reached."""
		api.projects[PROJECT]["_max_knowledge_size"] = 50
		api.add_document(PROJECT, "full.md", "x" * 60)

		result = client.upload_file(PROJECT, "empty.pdf", b"", "application/pdf")

		assert result.knowledge is None
		assert len(client.list_uploaded_files(PROJECT)) == 1

	def test_replacing_backs_the_old_upload_up_before_sending_and_removes_it_after(self, api, client):
		old_uuid = api.add_upload(PROJECT, "report.pdf", b"%PDF old")
		[old] = client.list_uploaded_files(PROJECT)
		saved = []

		def backup(file_name, data):
			saved.append((file_name, data, api.methods_logged().count("POST")))
			return f"/backups/{file_name}"

		result = client.upload_file(PROJECT, "report.pdf", b"%PDF new", "application/pdf", replacing=[old], backup=backup)

		assert saved == [("report.pdf", b"%PDF old", 0)], "backed up before anything was sent"
		assert result.action == "replaced"
		assert result.replaced_uuids == [old_uuid]
		assert result.backup_path == "/backups/report.pdf"
		[remaining] = client.list_uploaded_files(PROJECT)
		assert remaining.uuid == result.uuid
		assert client.download_uploaded_file(remaining) == b"%PDF new"

	def test_a_backup_that_fails_stops_the_upload_before_anything_is_sent(self, api, client):
		api.add_upload(PROJECT, "report.pdf", b"%PDF old")
		[old] = client.list_uploaded_files(PROJECT)

		def backup(file_name, data):
			raise BackupError("disk full")

		with pytest.raises(BackupError):
			client.upload_file(PROJECT, "report.pdf", b"%PDF new", "application/pdf", replacing=[old], backup=backup)

		assert "POST" not in api.methods_logged()
		assert [upload.uuid for upload in client.list_uploaded_files(PROJECT)] == [old.uuid]

	def test_replacing_an_upload_with_no_original_is_refused_before_anything_is_sent(self, api, client):
		"""An image offers no original to back up, and no path here deletes what it could not save first."""
		api.add_upload(PROJECT, "photo.png", b"old png", file_kind="image")
		[old] = client.list_uploaded_files(PROJECT)

		with pytest.raises(BackupError) as exception_info:
			client.upload_file(PROJECT, "photo.png", b"new png", "image/png", replacing=[old], backup=lambda file_name, data: "/backups/photo.png")

		assert "no downloadable original" in str(exception_info.value)
		assert "POST" not in api.methods_logged()
		assert [upload.uuid for upload in client.list_uploaded_files(PROJECT)] == [old.uuid]

	def test_replacing_refuses_when_any_older_copy_has_no_original(self, api, client):
		"""Every copy in `replacing` is deleted afterward, so every one of them has to be backable, not only the newest."""
		api.add_upload(PROJECT, "chart.png", b"old png", file_kind="image")
		api.add_upload(PROJECT, "chart.png", b"%PDF misnamed")
		copies = client.list_uploaded_files(PROJECT)
		assert copies[0].download_url is not None, "the newest copy is the one with an original"

		with pytest.raises(BackupError) as exception_info:
			client.upload_file(PROJECT, "chart.png", b"new png", "image/png", replacing=copies, backup=lambda file_name, data: "/backups/chart.png")

		assert "no downloadable original" in str(exception_info.value)
		assert "POST" not in api.methods_logged()
		assert len(client.list_uploaded_files(PROJECT)) == 2

	def test_a_replacement_whose_old_copy_will_not_delete_is_reported(self, api, client):
		old_uuid = api.add_upload(PROJECT, "report.pdf", b"%PDF old")
		[old] = client.list_uploaded_files(PROJECT)
		api.fail_once("DELETE", f"/docs/{old_uuid}$", ApiError("claude.ai returned HTTP 500.", status=500))

		result = client.upload_file(PROJECT, "report.pdf", b"%PDF new", "application/pdf", replacing=[old])

		assert result.replaced_uuids == []
		assert result.failed_delete_uuids == [old_uuid]
		assert len(client.list_uploaded_files(PROJECT)) == 2
