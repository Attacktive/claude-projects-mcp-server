import pytest

from claude_projects_mcp.sync import pull, push

from .conftest import PROJECT


def statuses(results):
	return {result.file_name: result.status for result in results}


class TestPull:
	def test_writes_each_document_to_the_directory(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_document(PROJECT, "other.md", "world")

		results = pull(client, PROJECT, tmp_path)

		assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hello"
		assert (tmp_path / "other.md").read_text(encoding="utf-8") == "world"
		assert set(statuses(results).values()) == {"written"}

	def test_creates_the_directory(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")
		target = tmp_path / "does" / "not" / "exist"

		pull(client, PROJECT, target)

		assert (target / "notes.md").exists()

	def test_reports_the_local_path(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")

		result = pull(client, PROJECT, tmp_path)[0]

		assert result.local_path == str(tmp_path / "notes.md")

	def test_identical_content_is_left_untouched(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")

		results = pull(client, PROJECT, tmp_path)

		assert statuses(results) == {"notes.md": "unchanged"}

	def test_differing_local_content_is_kept_unless_overwrite_is_asked_for(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "remote")
		(tmp_path / "notes.md").write_text("my local edits", encoding="utf-8")

		results = pull(client, PROJECT, tmp_path)

		assert statuses(results) == {"notes.md": "skipped_exists"}
		assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "my local edits"

	def test_overwrite_local_replaces_differing_content(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "remote")
		(tmp_path / "notes.md").write_text("my local edits", encoding="utf-8")

		results = pull(client, PROJECT, tmp_path, overwrite_local=True)

		assert statuses(results) == {"notes.md": "written"}
		assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "remote"

	def test_sanitises_unsafe_remote_names(self, api, client, tmp_path):
		api.add_document(PROJECT, "../escape.md", "hello")

		results = pull(client, PROJECT, tmp_path)

		written = tmp_path / results[0].local_path
		assert written.is_relative_to(tmp_path)
		assert ".." not in str(written)

	def test_preserves_korean_names(self, api, client, tmp_path):
		api.add_document(PROJECT, "연구노트.md", "hello")

		pull(client, PROJECT, tmp_path)

		assert (tmp_path / "연구노트.md").exists()

	def test_duplicate_remote_names_get_distinct_local_files(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "first")
		api.add_document(PROJECT, "notes.md", "second")

		results = pull(client, PROJECT, tmp_path)

		written = sorted(path.name for path in tmp_path.iterdir())
		assert len(written) == 2, f"both documents should survive, got {written}"
		assert len(results) == 2

	def test_one_failing_document_does_not_stop_the_others(self, stub_api, stub_client, tmp_path):
		"""Stub mode, because that is the path with a per-document fetch that can fail."""
		from claude_projects_mcp.errors import ApiError

		stub_api.add_document(PROJECT, "good.md", "fine")
		stub_api.add_document(PROJECT, "bad.md", "unreachable")
		stub_api.fail_once("GET", r"/docs/\d{8}-0000-4000-8000-\d+$", ApiError("boom", status=500))

		results = pull(stub_client, PROJECT, tmp_path)

		assert "error" in statuses(results).values()
		assert len(results) == 2

	def test_an_empty_project_yields_no_results(self, client, tmp_path):
		assert pull(client, PROJECT, tmp_path) == []


class TestPush:
	def test_uploads_new_files(self, api, client, tmp_path):
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")

		results = push(client, PROJECT, tmp_path)

		assert statuses(results) == {"notes.md": "created"}
		assert api.content_of(PROJECT, "notes.md") == ["hello"]

	def test_identical_content_is_not_rewritten(self, api, client, tmp_path):
		"""Re-uploading would mint a new uuid and churn a shared document for nothing."""
		api.add_document(PROJECT, "notes.md", "hello")
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")

		results = push(client, PROJECT, tmp_path)

		assert statuses(results) == {"notes.md": "unchanged"}
		assert "POST" not in api.methods_logged()

	def test_differing_content_needs_overwrite(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "remote version")
		(tmp_path / "notes.md").write_text("local version", encoding="utf-8")

		results = push(client, PROJECT, tmp_path)

		assert statuses(results) == {"notes.md": "skipped_exists"}
		assert api.content_of(PROJECT, "notes.md") == ["remote version"]

	def test_overwrite_replaces_the_remote_document(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "remote version")
		(tmp_path / "notes.md").write_text("local version", encoding="utf-8")

		results = push(client, PROJECT, tmp_path, overwrite=True)

		assert statuses(results) == {"notes.md": "replaced"}
		assert api.content_of(PROJECT, "notes.md") == ["local version"]

	def test_backs_up_before_overwriting(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "remote version")
		(tmp_path / "notes.md").write_text("local version", encoding="utf-8")
		saved = []

		def backup(file_name, content):
			saved.append((file_name, content))
			return f"/backups/{file_name}"

		push(client, PROJECT, tmp_path, overwrite=True, backup=backup)

		assert saved == [("notes.md", "remote version")]

	def test_never_deletes_remote_documents_missing_locally(self, api, client, tmp_path):
		"""push is not a mirror; a partial local folder must not prune the project."""
		api.add_document(PROJECT, "keep-me.md", "important")
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")

		push(client, PROJECT, tmp_path)

		assert "keep-me.md" in api.document_names(PROJECT)

	def test_matches_only_the_requested_pattern(self, api, client, tmp_path):
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")
		(tmp_path / "ignore.txt").write_text("nope", encoding="utf-8")

		results = push(client, PROJECT, tmp_path)

		assert set(statuses(results)) == {"notes.md"}

	def test_the_pattern_can_be_widened(self, api, client, tmp_path):
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")
		(tmp_path / "data.csv").write_text("a,b", encoding="utf-8")

		results = push(client, PROJECT, tmp_path, pattern="*")

		assert set(statuses(results)) == {"notes.md", "data.csv"}

	def test_subdirectories_are_ignored(self, api, client, tmp_path):
		"""A project's documents are a flat list, so recursing would collide names."""
		nested = tmp_path / "sub"
		nested.mkdir()
		(nested / "deep.md").write_text("hello", encoding="utf-8")
		(tmp_path / "top.md").write_text("hello", encoding="utf-8")

		results = push(client, PROJECT, tmp_path)

		assert set(statuses(results)) == {"top.md"}

	def test_dry_run_reports_without_writing(self, api, client, tmp_path):
		api.add_document(PROJECT, "existing.md", "remote")
		(tmp_path / "existing.md").write_text("changed", encoding="utf-8")
		(tmp_path / "new.md").write_text("brand new", encoding="utf-8")

		results = push(client, PROJECT, tmp_path, overwrite=True, dry_run=True)

		assert statuses(results) == {"existing.md": "replaced", "new.md": "created"}
		assert "POST" not in api.methods_logged()
		assert api.content_of(PROJECT, "existing.md") == ["remote"]

	def test_a_non_utf8_file_is_reported_and_the_rest_continue(self, api, client, tmp_path):
		(tmp_path / "good.md").write_text("fine", encoding="utf-8")
		(tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00\x01")

		results = push(client, PROJECT, tmp_path)

		assert statuses(results)["good.md"] == "created"
		assert statuses(results)["binary.md"] == "error"

		detail = {result.file_name: result.detail for result in results}["binary.md"]
		assert detail is not None and "UTF-8" in detail

	def test_a_missing_directory_is_an_error(self, client, tmp_path):
		with pytest.raises(FileNotFoundError):
			push(client, PROJECT, tmp_path / "nope")

	def test_an_empty_directory_yields_no_results(self, client, tmp_path):
		assert push(client, PROJECT, tmp_path) == []

	def test_push_stops_at_first_file_exceeding_capacity(self, api, client, tmp_path):
		api.projects[PROJECT]["_search_threshold"] = 50
		(tmp_path / "a.md").write_text("a" * 10, encoding="utf-8")
		(tmp_path / "b.md").write_text("b" * 100, encoding="utf-8")
		(tmp_path / "c.md").write_text("c" * 10, encoding="utf-8")

		results = push(client, PROJECT, tmp_path)

		assert statuses(results) == {
			"a.md": "created",
			"b.md": "refused_full",
			"c.md": "skipped_full",
		}
		assert api.document_names(PROJECT) == ["a.md"]

	def test_push_with_allow_search_mode_succeeds(self, api, client, tmp_path):
		api.projects[PROJECT]["_search_threshold"] = 50
		(tmp_path / "a.md").write_text("a" * 10, encoding="utf-8")
		(tmp_path / "b.md").write_text("b" * 100, encoding="utf-8")
		(tmp_path / "c.md").write_text("c" * 10, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, allow_search_mode=True)

		assert statuses(results) == {
			"a.md": "created",
			"b.md": "created",
			"c.md": "created",
		}
