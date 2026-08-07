"""Renaming is create-under-the-new-name, then delete.

The API has no rename endpoint, so the content is re-created under the new name and the original deleted, never the other way round.
The ordering is the safety argument, so it is asserted directly against the fake's request log, exactly as for the upsert.
"""

import pytest

from claude_projects_mcp.errors import AmbiguousDocError, ApiError, BackupError, ClaudeProjectsError, DocExistsError, NotFoundError

from .conftest import PROJECT


def backup_to(record: list):
	def backup(file_name: str, content: str) -> str:
		record.append((file_name, content))
		return f"/backups/{file_name}"

	return backup


class TestRenaming:
	def test_renames_by_uuid(self, api, client):
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		result = client.rename_document(PROJECT, uuid, "plan.md")

		assert api.document_names(PROJECT) == ["plan.md"]
		assert api.content_of(PROJECT, "plan.md") == ["hello"]
		assert result.old_uuid == uuid
		assert result.uuid != uuid, "the rename mints a new uuid"
		assert result.old_file_name == "notes.md"
		assert result.new_file_name == "plan.md"

	def test_renames_by_unambiguous_name(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")

		client.rename_document(PROJECT, "notes.md", "plan.md")

		assert api.document_names(PROJECT) == ["plan.md"]

	def test_creates_before_it_deletes(self, api, client):
		"""If this order ever inverts, a failed create loses the document outright."""
		api.add_document(PROJECT, "notes.md", "hello")

		client.rename_document(PROJECT, "notes.md", "plan.md")

		methods = api.methods_logged()
		assert methods.index("POST") < methods.index("DELETE")

	def test_backs_up_the_source_before_deleting_it(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		saved = []

		result = client.rename_document(PROJECT, "notes.md", "plan.md", backup=backup_to(saved))

		assert saved == [("notes.md", "hello")]
		assert result.backup_paths == ["/backups/notes.md"]

	def test_fetches_content_when_the_listing_gives_stubs(self, stub_api, stub_client):
		stub_api.add_document(PROJECT, "notes.md", "hello")

		stub_client.rename_document(PROJECT, "notes.md", "plan.md")

		assert stub_api.content_of(PROJECT, "plan.md") == ["hello"]

	def test_leaves_other_documents_alone(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_document(PROJECT, "other.md", "untouched")

		client.rename_document(PROJECT, "notes.md", "plan.md")

		assert sorted(api.document_names(PROJECT)) == ["other.md", "plan.md"]


class TestRefusals:
	def test_a_missing_source_is_not_found(self, api, client):
		with pytest.raises(NotFoundError):
			client.rename_document(PROJECT, "absent.md", "plan.md")

	def test_an_ambiguous_source_name_is_refused(self, api, client):
		first = api.add_document(PROJECT, "notes.md", "one")
		second = api.add_document(PROJECT, "notes.md", "two")

		with pytest.raises(AmbiguousDocError) as exception_info:
			client.rename_document(PROJECT, "notes.md", "plan.md")

		assert set(exception_info.value.uuids) == {first, second}
		assert len(api.document_names(PROJECT)) == 2

	def test_the_current_name_is_refused(self, api, client):
		"""Renaming a document to its own name would replace it with itself and churn its uuid for nothing."""
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		with pytest.raises(ClaudeProjectsError, match="already named"):
			client.rename_document(PROJECT, uuid, "notes.md")

		assert api.document_names(PROJECT) == ["notes.md"]

	def test_a_taken_name_is_refused_without_overwrite(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		occupant = api.add_document(PROJECT, "plan.md", "occupied")

		with pytest.raises(DocExistsError) as exception_info:
			client.rename_document(PROJECT, "notes.md", "plan.md")

		assert exception_info.value.uuid == occupant
		assert sorted(api.document_names(PROJECT)) == ["notes.md", "plan.md"]

	def test_a_refusal_backs_nothing_up(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_document(PROJECT, "plan.md", "occupied")
		saved = []

		with pytest.raises(DocExistsError):
			client.rename_document(PROJECT, "notes.md", "plan.md", backup=backup_to(saved))

		assert saved == []


class TestOverwriting:
	def test_overwrite_replaces_the_occupant(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		occupant = api.add_document(PROJECT, "plan.md", "doomed")

		result = client.rename_document(PROJECT, "notes.md", "plan.md", overwrite=True)

		assert api.content_of(PROJECT, "plan.md") == ["hello"]
		assert result.replaced_uuids == [occupant]

	def test_overwrite_backs_up_the_occupant_too(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_document(PROJECT, "plan.md", "doomed")
		saved = []

		result = client.rename_document(PROJECT, "notes.md", "plan.md", overwrite=True, backup=backup_to(saved))

		assert ("notes.md", "hello") in saved
		assert ("plan.md", "doomed") in saved
		assert len(result.backup_paths) == 2

	def test_overwrite_removes_every_occupant_sharing_the_name(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_document(PROJECT, "plan.md", "older")
		api.add_document(PROJECT, "plan.md", "newer")

		client.rename_document(PROJECT, "notes.md", "plan.md", overwrite=True)

		assert api.content_of(PROJECT, "plan.md") == ["hello"]

	def test_the_occupant_backup_captures_the_newest(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_document(PROJECT, "plan.md", "older")
		api.add_document(PROJECT, "plan.md", "newer")
		saved = []

		client.rename_document(PROJECT, "notes.md", "plan.md", overwrite=True, backup=backup_to(saved))

		assert ("plan.md", "newer") in saved


class TestFailureModes:
	def test_a_failed_backup_aborts_before_anything_is_mutated(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")

		def exploding(file_name: str, content: str) -> str:
			raise BackupError("disk full")

		with pytest.raises(BackupError):
			client.rename_document(PROJECT, "notes.md", "plan.md", backup=exploding)

		assert api.document_names(PROJECT) == ["notes.md"]

	def test_a_failed_create_leaves_the_original_intact(self, api, client):
		api.add_document(PROJECT, "notes.md", "hello")
		api.fail_once("POST", "/docs$", ApiError("claude.ai returned HTTP 500.", status=500))

		with pytest.raises(ApiError):
			client.rename_document(PROJECT, "notes.md", "plan.md")

		assert api.document_names(PROJECT) == ["notes.md"]

	def test_a_failed_delete_is_reported_but_does_not_fail_the_rename(self, api, client):
		uuid = api.add_document(PROJECT, "notes.md", "hello")
		api.fail_once("DELETE", f"/docs/{uuid}$", ApiError("claude.ai returned HTTP 500.", status=500))

		result = client.rename_document(PROJECT, "notes.md", "plan.md")

		assert result.failed_delete_uuids == [uuid]
		assert sorted(api.document_names(PROJECT)) == ["notes.md", "plan.md"], "the leftover stays visible until cleaned up"
