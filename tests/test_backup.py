from datetime import UTC, datetime

import pytest

from claude_projects_mcp.backup import BackupStore
from claude_projects_mcp.errors import BackupError

PROJECT = "project-1"


def clock_at(*stamps: str):
	"""A clock that returns each stamp in turn, repeating the last one forever."""
	times = [datetime.fromisoformat(stamp).replace(tzinfo=UTC) for stamp in stamps]
	calls = iter(range(len(times)))

	def now():
		try:
			return times[next(calls)]
		except StopIteration:
			return times[-1]

	return now


@pytest.fixture
def store(tmp_path):
	return BackupStore(tmp_path, now=clock_at("2026-08-06T16:24:53"))


def test_writes_the_content_and_returns_the_path(store, tmp_path):
	path = store.save(PROJECT, "notes.md", "the old text")

	assert path.read_text(encoding="utf-8") == "the old text"
	assert path.is_relative_to(tmp_path)


def test_files_are_grouped_by_project(store):
	path = store.save(PROJECT, "notes.md", "x")

	assert path.parent.name == PROJECT


def test_filename_carries_a_sortable_timestamp_and_the_original_name(store):
	path = store.save(PROJECT, "notes.md", "x")

	assert path.name == "20260806T162453Z--notes.md"


def test_creates_the_directory_tree_on_demand(tmp_path):
	store = BackupStore(tmp_path / "deep" / "nested", now=clock_at("2026-08-06T16:24:53"))
	path = store.save(PROJECT, "notes.md", "x")

	assert path.exists()


def test_never_overwrites_an_earlier_backup(tmp_path):
	"""Two saves in the same second must both survive; this store is append-only."""
	store = BackupStore(tmp_path, now=clock_at("2026-08-06T16:24:53"))
	first = store.save(PROJECT, "notes.md", "version one")
	second = store.save(PROJECT, "notes.md", "version two")

	assert first != second
	assert first.read_text(encoding="utf-8") == "version one"
	assert second.read_text(encoding="utf-8") == "version two"


def test_sanitizes_the_document_name(store, tmp_path):
	path = store.save(PROJECT, "../../escape.md", "x")

	assert path.is_relative_to(tmp_path)
	assert ".." not in str(path)


def test_sanitizes_the_project_id(store, tmp_path):
	path = store.save("../evil", "notes.md", "x")

	assert path.is_relative_to(tmp_path)


def test_preserves_korean_names(store):
	path = store.save(PROJECT, "연구노트.md", "x")

	assert "연구노트.md" in path.name


def test_an_unwritable_location_raises_backup_error(tmp_path):
	"""Callers rely on this to abort BEFORE mutating anything remote."""
	blocker = tmp_path / "blocked"
	blocker.write_text("I am a file, not a directory")
	store = BackupStore(blocker, now=clock_at("2026-08-06T16:24:53"))

	with pytest.raises(BackupError):
		store.save(PROJECT, "notes.md", "x")
