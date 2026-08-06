"""The create-then-delete save.

The API has no update endpoint, so saving means creating a replacement and deleting what it replaced.
The ordering is the safety argument, so it is asserted directly against the fake's request log rather than inferred from the end state.
"""

import pytest

from claude_projects_mcp.errors import ApiError, BackupError, ConcurrentEditError

from .conftest import PROJECT


def backup_to(record: list):
	def backup(file_name: str, content: str) -> str:
		record.append((file_name, content))
		return f"/backups/{file_name}"

	return backup


class TestCreating:
	def test_a_new_name_is_simply_created(self, api, client):
		result = client.replace_document(PROJECT, "notes.md", "hello")

		assert result.action == "created"
		assert result.replaced_uuids == []
		assert api.content_of(PROJECT, "notes.md") == ["hello"]

	def test_creating_does_not_back_anything_up(self, api, client):
		saved = []
		client.replace_document(PROJECT, "notes.md", "hello", backup=backup_to(saved))

		assert saved == [], "there was no previous content to lose"


class TestReplacing:
	def test_replaces_the_content(self, api, client):
		api.add_document(PROJECT, "notes.md", "old")

		result = client.replace_document(PROJECT, "notes.md", "new")

		assert result.action == "replaced"
		assert api.content_of(PROJECT, "notes.md") == ["new"]

	def test_backs_up_the_previous_content_first(self, api, client):
		api.add_document(PROJECT, "notes.md", "old")
		saved = []

		result = client.replace_document(PROJECT, "notes.md", "new", backup=backup_to(saved))

		assert saved == [("notes.md", "old")]
		assert result.backup_path == "/backups/notes.md"

	def test_creates_before_it_deletes(self, api, client):
		"""If this order ever inverts, a failed create loses the original outright."""
		api.add_document(PROJECT, "notes.md", "old")

		client.replace_document(PROJECT, "notes.md", "new")

		methods = api.methods_logged()
		assert methods.index("POST") < methods.index("DELETE")

	def test_reports_the_uuid_it_replaced(self, api, client):
		old_uuid = api.add_document(PROJECT, "notes.md", "old")

		result = client.replace_document(PROJECT, "notes.md", "new")

		assert result.replaced_uuids == [old_uuid]
		assert result.uuid != old_uuid, "every save mints a new uuid"

	def test_leaves_other_documents_alone(self, api, client):
		api.add_document(PROJECT, "notes.md", "old")
		api.add_document(PROJECT, "other.md", "untouched")

		client.replace_document(PROJECT, "notes.md", "new")

		assert api.content_of(PROJECT, "other.md") == ["untouched"]

	def test_fetches_content_for_the_backup_when_the_listing_gives_stubs(self, stub_api, stub_client):
		stub_api.add_document(PROJECT, "notes.md", "old")
		saved = []

		stub_client.replace_document(PROJECT, "notes.md", "new", backup=backup_to(saved))

		assert saved == [("notes.md", "old")], "without the extra fetch this backs up an empty string"


class TestFailureModes:
	def test_a_failed_backup_aborts_before_anything_is_mutated(self, api, client):
		api.add_document(PROJECT, "notes.md", "old")

		def explode(_file_name, _content):
			raise BackupError("disk full")

		with pytest.raises(BackupError):
			client.replace_document(PROJECT, "notes.md", "new", backup=explode)

		assert api.content_of(PROJECT, "notes.md") == ["old"]
		assert "POST" not in api.methods_logged(), "nothing may be written without a backup"

	def test_a_failed_create_leaves_the_original_intact(self, api, client):
		api.add_document(PROJECT, "notes.md", "old")
		api.fail_once("POST", "/docs$", ApiError("boom", status=500))

		with pytest.raises(ApiError):
			client.replace_document(PROJECT, "notes.md", "new")

		assert api.content_of(PROJECT, "notes.md") == ["old"]
		assert "DELETE" not in api.methods_logged()

	def test_a_failed_delete_is_reported_but_does_not_fail_the_save(self, api, client):
		"""The new content is live; a failed cleanup is a leftover, not a lost write."""
		old_uuid = api.add_document(PROJECT, "notes.md", "old")
		api.fail_once("DELETE", "/docs/", ApiError("boom", status=500))

		result = client.replace_document(PROJECT, "notes.md", "new")

		assert result.failed_delete_uuids == [old_uuid]
		assert "new" in api.content_of(PROJECT, "notes.md")

	def test_an_already_deleted_original_is_not_a_failure(self, api, client):
		"""A teammate deleting it first reached the same end state."""
		from claude_projects_mcp.errors import NotFoundError

		api.add_document(PROJECT, "notes.md", "old")
		api.fail_once("DELETE", "/docs/", NotFoundError("already gone"))

		result = client.replace_document(PROJECT, "notes.md", "new")

		assert result.failed_delete_uuids == []


class TestDuplicateHealing:
	def test_replacing_removes_every_document_sharing_the_name(self, api, client):
		"""A crash between create and delete leaves duplicates; the next save heals them."""
		api.add_document(PROJECT, "notes.md", "older")
		api.add_document(PROJECT, "notes.md", "newer")

		result = client.replace_document(PROJECT, "notes.md", "newest")

		assert api.content_of(PROJECT, "notes.md") == ["newest"]
		assert len(result.replaced_uuids) == 2

	def test_the_backup_captures_the_newest_of_the_duplicates(self, api, client):
		api.add_document(PROJECT, "notes.md", "older")
		api.add_document(PROJECT, "notes.md", "newer")
		saved = []

		client.replace_document(PROJECT, "notes.md", "newest", backup=backup_to(saved))

		assert saved == [("notes.md", "newer")]


class TestOptimisticConcurrency:
	def test_a_matching_expected_uuid_allows_the_write(self, api, client):
		uuid = api.add_document(PROJECT, "notes.md", "old")

		client.replace_document(PROJECT, "notes.md", "new", expected_uuid=uuid)

		assert api.content_of(PROJECT, "notes.md") == ["new"]

	def test_a_stale_expected_uuid_refuses_the_write(self, api, client):
		api.add_document(PROJECT, "notes.md", "a teammate already saved this")

		with pytest.raises(ConcurrentEditError) as exception_info:
			client.replace_document(PROJECT, "notes.md", "mine", expected_uuid="the-one-i-read")

		assert api.content_of(PROJECT, "notes.md") == ["a teammate already saved this"]
		assert "re-read" in str(exception_info.value)

	def test_expecting_a_document_that_has_since_been_deleted_refuses_the_write(self, api, client):
		with pytest.raises(ConcurrentEditError) as exception_info:
			client.replace_document(PROJECT, "notes.md", "mine", expected_uuid="the-one-i-read")

		assert exception_info.value.actual_uuid is None

	def test_the_check_happens_before_the_backup(self, api, client):
		api.add_document(PROJECT, "notes.md", "old")
		saved = []

		with pytest.raises(ConcurrentEditError):
			client.replace_document(PROJECT, "notes.md", "new", expected_uuid="stale", backup=backup_to(saved))

		assert saved == [], "a refused write should not litter the backup directory"
