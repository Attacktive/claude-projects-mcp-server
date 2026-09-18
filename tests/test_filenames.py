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

	def test_a_name_too_long_for_a_filesystem_is_cut_to_fit_and_keeps_its_extension(self):
		"""ext4 caps a name at 255 bytes and Hangul is three bytes a character, so a long Korean title overflows long before it looks long."""
		stem = "분기별사업계획검토보고서" * 10
		result = sanitize(f"{stem}.pdf")

		assert len(result.encode("utf-8")) <= 200
		assert result.endswith(".pdf")
		assert stem.startswith(result.removesuffix(".pdf")), "the cut lands on a character boundary and keeps the front of the name"

	def test_a_name_that_fits_is_not_cut(self):
		name = "a" * 200
		assert sanitize(name, default_suffix=None) == name

	def test_the_default_suffix_counts_toward_the_budget(self):
		result = sanitize("가" * 100)

		assert result.endswith(".md")
		assert len(result.encode("utf-8")) <= 200

	def test_a_long_fallback_is_cut_too(self):
		assert len(sanitize("", fallback="x" * 300, default_suffix=None).encode("utf-8")) <= 200

	def test_a_compound_extension_survives_the_cut(self):
		assert sanitize("보고서" * 80 + ".tar.gz", default_suffix=None).endswith(".tar.gz")

	def test_a_dotted_title_keeps_only_its_real_extension_when_cut(self):
		"""Every dot is a suffix boundary to pathlib, so a version number in a title must not be mistaken for an extension worth keeping."""
		result = sanitize("가" * 80 + ".v1.2 final draft.pdf")

		assert result.endswith(".pdf")
		assert not result.endswith("draft.pdf")


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
