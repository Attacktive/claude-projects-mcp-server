from pathlib import Path

import pytest

from claude_projects_mcp.errors import UnsafePathError
from claude_projects_mcp.filenames import deduplicate, safe_child, sanitize


class TestSanitize:
	def test_leaves_an_ordinary_name_alone(self):
		assert sanitize("notes.md") == "notes.md"

	def test_preserves_non_ascii(self):
		"""The team's documents are Korean; mangling them to ASCII would be useless."""
		assert sanitize("연구노트_20260805.md") == "연구노트_20260805.md"

	def test_strips_directory_separators(self):
		assert "/" not in sanitize("a/b/c.md")
		assert "\\" not in sanitize("a\\b\\c.md")

	def test_defuses_parent_traversal(self):
		result = sanitize("../../etc/passwd")

		assert ".." not in result
		assert "/" not in result

	def test_strips_control_characters_and_nulls(self):
		result = sanitize("bad\x00name\x1f.md")

		assert "\x00" not in result
		assert "\x1f" not in result

	def test_strips_characters_windows_rejects(self):
		result = sanitize('a:b*c?d"e<f>g|h.md')

		for char in ':*?"<>|':
			assert char not in result

	def test_leading_dots_are_dropped_so_files_are_not_hidden(self):
		assert not sanitize("...hidden.md").startswith(".")

	def test_empty_or_all_stripped_name_uses_the_fallback(self):
		assert sanitize("", fallback="document-1") == "document-1.md"
		assert sanitize("///", fallback="document-2") == "document-2.md"

	def test_adds_a_markdown_suffix_when_there_is_no_extension(self):
		assert sanitize("notes") == "notes.md"

	def test_keeps_a_non_markdown_extension(self):
		assert sanitize("data.csv") == "data.csv"

	def test_no_suffix_is_added_when_the_name_is_a_directory(self):
		assert sanitize("project-1", default_suffix=None) == "project-1"

	def test_collapses_runs_of_replacement_characters(self):
		assert "--" not in sanitize("a///b.md")


class TestDedupe:
	def test_distinct_names_are_untouched(self):
		result = deduplicate({"u1": "a.md", "u2": "b.md"})

		assert result == {"u1": "a.md", "u2": "b.md"}

	def test_colliding_names_get_a_uuid_suffix(self):
		result = deduplicate({"uuid-aaa": "notes.md", "uuid-bbb": "notes.md"})

		assert result["uuid-aaa"] != result["uuid-bbb"]
		assert len(set(result.values())) == 2
		assert all(name.endswith(".md") for name in result.values())

	def test_suffix_derives_from_the_uuid_so_it_is_stable_across_runs(self):
		first = deduplicate({"uuid-aaa": "notes.md", "uuid-bbb": "notes.md"})
		second = deduplicate({"uuid-bbb": "notes.md", "uuid-aaa": "notes.md"})

		assert first == second, "a re-pull must not rename files just because order changed"

	def test_collision_detection_ignores_case(self):
		"""macOS and Windows filesystems would treat these as one file."""
		result = deduplicate({"u1": "Notes.md", "u2": "notes.md"})

		assert len({name.casefold() for name in result.values()}) == 2


class TestSafeChild:
	def test_returns_a_path_inside_the_base(self, tmp_path):
		assert safe_child(tmp_path, "notes.md") == tmp_path / "notes.md"

	def test_rejects_an_escape_attempt(self, tmp_path):
		with pytest.raises(UnsafePathError):
			safe_child(tmp_path, "../escaped.md")

	def test_rejects_an_absolute_path(self, tmp_path):
		with pytest.raises(UnsafePathError):
			safe_child(tmp_path, "/etc/passwd")

	def test_rejects_a_nested_escape(self, tmp_path):
		with pytest.raises(UnsafePathError):
			safe_child(tmp_path, "sub/../../out.md")

	def test_base_is_resolved_so_a_symlinked_parent_still_matches(self, tmp_path):
		real = tmp_path / "real"
		real.mkdir()
		link = tmp_path / "link"
		link.symlink_to(real)

		assert safe_child(link, "notes.md") == Path(real / "notes.md")
