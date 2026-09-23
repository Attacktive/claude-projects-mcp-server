import sys
from pathlib import Path

import pytest

from claude_projects_mcp.errors import ApiError, InvalidPatternError, NotFoundError
from claude_projects_mcp.sync import PushOptions, pull, push

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

	def test_a_document_stored_under_another_name_says_so(self, api, client, tmp_path):
		"""push matches local names against remote ones, so a renamed pull would come back as a second document; the result has to warn before that happens."""
		api.add_document(PROJECT, "분기별사업계획검토보고서" * 10 + ".md", "hello")

		[result] = pull(client, PROJECT, tmp_path)

		assert result.status == "written"
		assert len(Path(result.local_path).name.encode("utf-8")) <= 200
		assert "push_documents" in result.detail

	def test_a_document_stored_under_its_own_name_carries_no_detail(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")

		assert pull(client, PROJECT, tmp_path)[0].detail is None

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

		results = push(client, PROJECT, tmp_path, options=PushOptions(overwrite=True))

		assert statuses(results) == {"notes.md": "replaced"}
		assert api.content_of(PROJECT, "notes.md") == ["local version"]

	def test_backs_up_before_overwriting(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "remote version")
		(tmp_path / "notes.md").write_text("local version", encoding="utf-8")
		saved = []

		def backup(file_name, content):
			saved.append((file_name, content))
			return f"/backups/{file_name}"

		push(client, PROJECT, tmp_path, options=PushOptions(overwrite=True, backup=backup))

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

		results = push(client, PROJECT, tmp_path, options=PushOptions(overwrite=True, dry_run=True))

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

	@pytest.mark.parametrize("pattern", ["**/*.md", "sub/*.md", "../*.md", "/somewhere/*.md", "/", "..", "**"])
	def test_a_pattern_that_reaches_into_directories_is_refused(self, api, client, tmp_path, pattern):
		"""A project's documents are a flat list, so two same-named files at different depths would land as duplicates of one name."""
		nested = tmp_path / "sub"
		nested.mkdir()
		(nested / "top.md").write_text("deep", encoding="utf-8")
		(tmp_path / "top.md").write_text("shallow", encoding="utf-8")

		with pytest.raises(InvalidPatternError) as exception_info:
			push(client, PROJECT, tmp_path, pattern=pattern)

		assert repr(pattern) in str(exception_info.value)
		assert api.document_names(PROJECT) == []

	@pytest.mark.parametrize("pattern", ["", "."])
	def test_a_pattern_that_names_nothing_is_refused_by_name(self, client, tmp_path, pattern):
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")

		with pytest.raises(InvalidPatternError) as exception_info:
			push(client, PROJECT, tmp_path, pattern=pattern)

		assert repr(pattern) in str(exception_info.value)

	@pytest.mark.skipif(sys.platform == "win32", reason="creating a symbolic link needs a privilege on Windows")
	def test_a_symbolic_link_out_of_the_folder_is_not_uploaded(self, api, client, tmp_path):
		"""The pull side refuses to write outside its folder; the push side must refuse to read outside its own."""
		outside = tmp_path / "outside"
		outside.mkdir()
		(outside / "secret.md").write_text("not yours", encoding="utf-8")
		source = tmp_path / "source"
		source.mkdir()
		(source / "link.md").symlink_to(outside / "secret.md")
		(source / "own.md").write_text("mine", encoding="utf-8")

		results = push(client, PROJECT, source)

		assert statuses(results) == {"link.md": "error", "own.md": "created"}
		assert "outside" in {result.file_name: result.detail for result in results}["link.md"]
		assert api.document_names(PROJECT) == ["own.md"]

	@pytest.mark.skipif(sys.platform == "win32", reason="creating a symbolic link needs a privilege on Windows")
	def test_a_symbolic_link_within_the_folder_is_uploaded(self, api, client, tmp_path):
		(tmp_path / "real.md").write_text("shared", encoding="utf-8")
		(tmp_path / "alias.md").symlink_to(tmp_path / "real.md")

		results = push(client, PROJECT, tmp_path)

		assert statuses(results) == {"alias.md": "created", "real.md": "created"}
		assert api.content_of(PROJECT, "alias.md") == ["shared"]

	def test_a_dry_run_stops_where_the_real_push_would(self, api, client, tmp_path):
		"""The stop is the one outcome worth previewing, so the preview projects the project's size file by file instead of writing and measuring."""
		api.projects[PROJECT]["_search_threshold"] = 50
		api.add_document(PROJECT, "seed.md", "s" * 10)
		(tmp_path / "a.md").write_text("a" * 10, encoding="utf-8")
		(tmp_path / "b.md").write_text("b" * 100, encoding="utf-8")
		(tmp_path / "c.md").write_text("c" * 10, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {
			"a.md": "created",
			"b.md": "refused_full",
			"c.md": "skipped_full",
		}

		assert "POST" not in api.methods_logged()
		assert api.document_names(PROJECT) == ["seed.md"]
		assert "preview" in {result.file_name: result.detail for result in results}["c.md"]

	def test_a_dry_run_refusal_says_it_is_an_estimate_and_names_the_line(self, api, client, tmp_path):
		api.projects[PROJECT]["_search_threshold"] = 50
		api.add_document(PROJECT, "seed.md", "s" * 10)
		(tmp_path / "b.md").write_text("b" * 100, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		detail = results[0].detail
		assert detail is not None
		assert "dry run" in detail
		assert "estimated" in detail
		assert "search threshold" in detail
		assert "Past that line" in detail
		assert "allow_search_mode" in detail

	def test_a_dry_run_past_the_maximum_names_that_line_and_offers_no_search_mode(self, api, client, tmp_path):
		api.projects[PROJECT]["_search_threshold"] = 20
		api.projects[PROJECT]["_max_knowledge_size"] = 50
		api.add_document(PROJECT, "seed.md", "s" * 10)
		(tmp_path / "b.md").write_text("b" * 100, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True, allow_search_mode=True))

		assert statuses(results) == {"b.md": "refused_full"}
		detail = results[0].detail
		assert detail is not None
		assert "its maximum" in detail
		assert "allow_search_mode" not in detail

	def test_a_dry_run_with_allow_search_mode_admits_what_the_real_push_would(self, api, client, tmp_path):
		api.projects[PROJECT]["_search_threshold"] = 50
		api.add_document(PROJECT, "seed.md", "s" * 10)
		(tmp_path / "a.md").write_text("a" * 10, encoding="utf-8")
		(tmp_path / "b.md").write_text("b" * 100, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True, allow_search_mode=True))

		assert statuses(results) == {"a.md": "created", "b.md": "created"}

	def test_a_dry_run_counts_the_document_a_replacement_removes(self, api, client, tmp_path):
		"""Replacing frees the old copy's tokens, so a rewrite that grows a document a little still fits."""
		api.projects[PROJECT]["_search_threshold"] = 50
		api.add_document(PROJECT, "big.md", "b" * 40)
		(tmp_path / "big.md").write_text("B" * 45, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True, overwrite=True))

		assert statuses(results) == {"big.md": "replaced"}

	def test_a_dry_run_counts_every_copy_a_replacement_removes(self, api, client, tmp_path):
		"""An interrupted save leaves two copies of a name; the real replacement deletes both, so the preview must free both."""
		api.projects[PROJECT]["_search_threshold"] = 60
		api.add_document(PROJECT, "big.md", "b" * 40)
		api.add_document(PROJECT, "big.md", "b" * 40)
		(tmp_path / "big.md").write_text("B" * 45, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True, overwrite=True))

		assert statuses(results) == {"big.md": "replaced"}

	def test_a_dry_run_over_a_project_already_past_the_line_says_so(self, api, client, tmp_path):
		"""The real refusal blames the state, not the file, when the project was already over; the preview must not contradict it."""
		api.projects[PROJECT]["_search_threshold"] = 50
		api.add_document(PROJECT, "seed.md", "s" * 60)
		(tmp_path / "a.md").write_text("a" * 10, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {"a.md": "refused_full"}
		detail = results[0].detail
		assert detail is not None
		assert "already past" in detail

	def test_a_dry_run_estimates_at_the_rate_the_project_shows(self, api, client, tmp_path):
		"""The fake counts one token per character; a project holding such a document teaches the preview that rate, and a file it would refuse at that rate is refused."""
		api.projects[PROJECT]["_search_threshold"] = 50
		api.add_document(PROJECT, "seed.md", "s" * 10)
		(tmp_path / "b.md").write_text("b" * 60, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {"b.md": "refused_full"}

	def test_a_dry_run_measures_a_listing_that_hides_text_by_fetching_a_sample(self, stub_api, stub_client, tmp_path):
		"""A listing with token counts but no text can still teach the rate: a few documents are fetched whole, since knowing before writing is the whole point of a dry run."""
		stub_api.projects[PROJECT]["_search_threshold"] = 50
		stub_api.add_document(PROJECT, "seed.md", "s" * 10)
		(tmp_path / "b.md").write_text("b" * 60, encoding="utf-8")

		results = push(stub_client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {"b.md": "refused_full"}

	def test_a_dry_run_whose_sample_fetch_fails_falls_back_to_the_default_rate(self, stub_api, stub_client, tmp_path):
		"""A document that cannot be fetched teaches nothing, and the preview is not worth failing the whole dry run over."""
		stub_api.projects[PROJECT]["_search_threshold"] = 50
		stub_api.add_document(PROJECT, "seed.md", "s" * 10)
		stub_api.fail_once("GET", "/docs/[^/]+$", NotFoundError("gone"))
		(tmp_path / "b.md").write_text("b" * 60, encoding="utf-8")

		results = push(stub_client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {"b.md": "created"}

	def test_a_dry_run_over_an_uncounted_listing_says_the_stop_cannot_be_previewed(self, api, client, tmp_path):
		"""When the API reports no token counts, the real gate admits every write unchecked, so a preview that refused on a guess would contradict it."""
		api.list_includes_token_counts = False
		api.projects[PROJECT]["_search_threshold"] = 50
		api.add_document(PROJECT, "seed.md", "s" * 10)
		(tmp_path / "b.md").write_text("b" * 200, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {"b.md": "created"}
		detail = results[0].detail
		assert detail is not None
		assert "not previewed" in detail
		assert "token count" in detail

	def test_a_dry_run_into_an_empty_project_estimates_at_a_default_rate(self, api, client, tmp_path):
		"""With no document to measure, the preview falls back to a rate well under one token per character, and says so."""
		api.projects[PROJECT]["_search_threshold"] = 50
		(tmp_path / "b.md").write_text("b" * 60, encoding="utf-8")
		(tmp_path / "c.md").write_text("c" * 600, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {"b.md": "created", "c.md": "refused_full"}
		assert "no document" in {result.file_name: result.detail for result in results}["c.md"]

	def test_a_dry_run_over_an_empty_folder_fetches_nothing_but_the_listing(self, api, client, tmp_path):
		"""With no file to preview there is nothing to project, so the stats and any calibration fetch are not worth a request."""
		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert results == []
		assert all("kb/stats" not in path for _, path in api.log)

	def test_a_dry_run_does_not_fetch_a_document_whose_empty_text_is_already_listed(self, api, client, tmp_path):
		"""Empty text is text the listing already gave; fetching the document whole would only learn the same nothing."""
		api.add_document(PROJECT, "empty.md", "")
		(tmp_path / "b.md").write_text("b" * 60, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {"b.md": "created"}
		document_fetches = [path for _, path in api.log if path.rsplit("/", 1)[0].endswith("/docs")]
		assert document_fetches == []

	def test_a_dry_run_without_knowledge_stats_still_previews(self, api, client, tmp_path):
		"""A project whose stats endpoint is missing still gets its rows, each saying the stop was not previewed rather than passing as one that fits."""
		api.projects[PROJECT]["_search_threshold"] = None
		(tmp_path / "a.md").write_text("a" * 10, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(dry_run=True))

		assert statuses(results) == {"a.md": "created"}
		detail = results[0].detail
		assert detail is not None
		assert "dry run" in detail
		assert "not previewed" in detail

	def test_push_with_allow_search_mode_succeeds(self, api, client, tmp_path):
		api.projects[PROJECT]["_search_threshold"] = 50
		(tmp_path / "a.md").write_text("a" * 10, encoding="utf-8")
		(tmp_path / "b.md").write_text("b" * 100, encoding="utf-8")
		(tmp_path / "c.md").write_text("c" * 10, encoding="utf-8")

		results = push(client, PROJECT, tmp_path, options=PushOptions(allow_search_mode=True))

		assert statuses(results) == {
			"a.md": "created",
			"b.md": "created",
			"c.md": "created",
		}


class TestPullUploads:
	"""Files uploaded through the web UI come down as bytes, next to the documents, so a pull is a whole copy of the project."""

	def test_writes_an_upload_as_bytes_next_to_the_documents(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")

		results = pull(client, PROJECT, tmp_path)

		assert (tmp_path / "report.pdf").read_bytes() == b"%PDF-1.4 report"
		assert statuses(results) == {"notes.md": "written", "report.pdf": "written"}

	def test_an_upload_is_marked_as_such(self, api, client, tmp_path):
		api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")

		result = pull(client, PROJECT, tmp_path)[0]

		assert result.kind == "upload"
		assert result.local_path == str(tmp_path / "report.pdf")

	def test_a_document_is_marked_as_such(self, api, client, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")

		assert pull(client, PROJECT, tmp_path)[0].kind == "document"

	def test_identical_bytes_are_left_untouched(self, api, client, tmp_path):
		api.add_upload(PROJECT, "report.pdf", b"%PDF same")
		(tmp_path / "report.pdf").write_bytes(b"%PDF same")

		assert statuses(pull(client, PROJECT, tmp_path)) == {"report.pdf": "unchanged"}

	def test_differing_local_bytes_are_kept_unless_overwrite_is_asked_for(self, api, client, tmp_path):
		api.add_upload(PROJECT, "report.pdf", b"%PDF remote")
		(tmp_path / "report.pdf").write_bytes(b"%PDF local")

		assert statuses(pull(client, PROJECT, tmp_path)) == {"report.pdf": "skipped_exists"}
		assert (tmp_path / "report.pdf").read_bytes() == b"%PDF local"
		assert statuses(pull(client, PROJECT, tmp_path, overwrite_local=True)) == {"report.pdf": "written"}
		assert (tmp_path / "report.pdf").read_bytes() == b"%PDF remote"

	def test_a_local_copy_of_another_size_is_skipped_without_a_download(self, api, client, tmp_path):
		"""The listing's size_bytes settles "differs" on its own, so a routine re-pull of a big PDF does not move it only to learn that it is being kept."""
		api.add_upload(PROJECT, "report.pdf", b"%PDF remote")
		(tmp_path / "report.pdf").write_bytes(b"%PDF local copy")

		assert statuses(pull(client, PROJECT, tmp_path)) == {"report.pdf": "skipped_exists"}
		assert not [entry for entry in api.log if "document_pdf" in entry[1]], "nothing needed downloading"

	def test_a_local_copy_of_the_same_size_is_still_compared_byte_for_byte(self, api, client, tmp_path):
		api.add_upload(PROJECT, "report.pdf", b"%PDF remote")
		(tmp_path / "report.pdf").write_bytes(b"%PDF loca1!")

		assert statuses(pull(client, PROJECT, tmp_path)) == {"report.pdf": "skipped_exists"}
		assert [entry for entry in api.log if "document_pdf" in entry[1]], "the same size is not the same bytes"

	def test_a_document_and_an_upload_sharing_a_name_get_distinct_files(self, api, client, tmp_path):
		api.add_document(PROJECT, "report.pdf", "text that only pretends to be a PDF")
		api.add_upload(PROJECT, "report.pdf", b"%PDF real")

		results = pull(client, PROJECT, tmp_path)

		paths = {result.local_path for result in results}
		assert len(paths) == 2
		assert all(Path(path).exists() for path in paths)

	def test_a_failing_download_does_not_stop_the_others(self, api, client, tmp_path):
		api.add_upload(PROJECT, "fine.pdf", b"one")
		api.add_upload(PROJECT, "broken.pdf", b"two")
		# Newest first, so the fault lands on broken.pdf.
		api.fail_once("GET", "/document_pdf$", ApiError("claude.ai returned HTTP 500.", status=500))

		results = statuses(pull(client, PROJECT, tmp_path))

		assert results == {"broken.pdf": "error", "fine.pdf": "written"}

	def test_an_unavailable_files_listing_is_one_error_result_beside_the_documents(self, api, client, tmp_path):
		"""The documents are already worth writing by then, so the failure rides along in the results instead of throwing them away."""
		api.add_document(PROJECT, "notes.md", "hello")
		api.fail_once("GET", "/files$", NotFoundError("Not found (HTTP 404)"))

		results = pull(client, PROJECT, tmp_path)

		assert statuses(results)["notes.md"] == "written"
		errors = [result for result in results if result.status == "error"]
		assert len(errors) == 1
		assert errors[0].kind == "upload"
		assert "could not be listed" in errors[0].detail

	def test_an_upload_without_a_downloadable_original_is_an_error_result(self, api, client, tmp_path):
		"""An image offers only a preview, which is not the file, so nothing is written under its name."""
		api.add_upload(PROJECT, "photo.png", b"png bytes", file_kind="image")

		results = pull(client, PROJECT, tmp_path)

		assert statuses(results) == {"photo.png": "error"}
		assert not (tmp_path / "photo.png").exists()

	def test_a_local_copy_beside_an_upload_with_no_original_is_still_an_error(self, api, client, tmp_path):
		"""The size shortcut must not turn "nothing to download" into advice to pass overwrite_local, since there is no remote version to take."""
		api.add_upload(PROJECT, "photo.png", b"png bytes", file_kind="image")
		(tmp_path / "photo.png").write_bytes(b"a different png")

		[result] = pull(client, PROJECT, tmp_path)

		assert result.status == "error"
		assert "no downloadable original" in result.detail

	def test_an_upload_without_an_extension_keeps_its_name(self, api, client, tmp_path):
		"""Documents get `.md` by default because the web UI renders by extension; a binary upload is whatever it is."""
		api.add_upload(PROJECT, "scan", b"%PDF-1.4 scan")

		results = pull(client, PROJECT, tmp_path)

		assert statuses(results) == {"scan": "written"}
		assert (tmp_path / "scan").read_bytes() == b"%PDF-1.4 scan"

	def test_an_upload_whose_size_is_unknown_is_still_pulled(self, api, client, tmp_path):
		"""A pull is reversible, so an unverified copy is worth having; only delete_project holds out for a size to check against."""
		api.list_includes_sizes = False
		api.add_upload(PROJECT, "report.pdf", b"%PDF-1.4 report")

		assert statuses(pull(client, PROJECT, tmp_path)) == {"report.pdf": "written"}
