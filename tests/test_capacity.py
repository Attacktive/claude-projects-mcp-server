"""Tests for knowledge capacity limits, verdicts, candidate selection, and refusal messages."""

from claude_projects_mcp.capacity import Candidate, candidates, judge, refusal
from claude_projects_mcp.models import Document, KnowledgeStats, UploadedFile


def test_judge_fits_when_added_less_than_or_equal_to_removed():
	# Post-create size is 100,000; removing 100,000 leaves projected at 0 (and added=50 <= removed=100)
	stats = KnowledgeStats(size=100_000, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	# Added 50 <= Removed 100 -> fits even though projected size > search_threshold
	assert judge(stats, added=50, removed=100) == "fits"


def test_judge_fits_when_under_threshold():
	# Post-create size is 30,000 (20,000 added)
	stats = KnowledgeStats(size=30_000, max_size=2_000_000, search_threshold=50_000, search_mode=False)
	assert judge(stats, added=20_000, removed=0) == "fits"


def test_judge_search_mode_when_over_threshold_and_under_max():
	# Post-create size is 60,000 (40,000 before + 20,000 added)
	stats = KnowledgeStats(size=60_000, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	assert judge(stats, added=20_000, removed=0) == "search_mode"


def test_judge_over_max_when_projected_exceeds_max():
	# Post-create size is 2,010,000 (1,990,000 before + 20,000 added)
	stats = KnowledgeStats(size=2_010_000, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	assert judge(stats, added=20_000, removed=0) == "over_max"


def test_judge_shrinks_while_over_the_cap():
	# Project is at 2,495,000 post-create (2,500,000 before + 5,000 added), replacing 10,000 tokens
	stats = KnowledgeStats(size=2_495_000, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	assert judge(stats, added=5_000, removed=10_000) == "fits"


def test_candidates_excludes_written_file_and_limits_to_three():
	documents = [
		Document(uuid="1", file_name="notes.md", estimated_token_count=100, created_at="2026-01-01T00:00:00Z"),
		Document(uuid="2", file_name="doc1.md", estimated_token_count=1000, created_at="2026-02-01T00:00:00Z"),
		Document(uuid="3", file_name="doc2.md", estimated_token_count=2000, created_at="2026-03-01T00:00:00Z"),
		Document(uuid="4", file_name="doc3.md", estimated_token_count=3000, created_at="2026-04-01T00:00:00Z"),
		Document(uuid="5", file_name="doc4.md", estimated_token_count=4000, created_at="2026-05-01T00:00:00Z"),
	]
	result = candidates(documents, excluding="notes.md")
	assert len(result) == 3
	assert [candidate.file_name for candidate in result] == ["doc4.md", "doc3.md", "doc2.md"]


def test_candidates_orders_duplicates_first_and_handles_ties_by_age():
	documents = [
		# Non-duplicate large
		Document(uuid="1", file_name="large.md", estimated_token_count=10_000, created_at="2026-05-01T00:00:00Z"),
		# Duplicate copies of dup.md: newest (uuid=3), older (uuid=2)
		Document(uuid="2", file_name="dup.md", estimated_token_count=1_000, created_at="2026-01-01T00:00:00Z"),
		Document(uuid="3", file_name="dup.md", estimated_token_count=1_000, created_at="2026-02-01T00:00:00Z"),
		# Equal size non-duplicates with different ages
		Document(uuid="4", file_name="newer.md", estimated_token_count=5_000, created_at="2026-04-01T00:00:00Z"),
		Document(uuid="5", file_name="older.md", estimated_token_count=5_000, created_at="2026-03-01T00:00:00Z"),
	]
	result = candidates(documents, excluding="written.md")
	# Candidates:
	# 1. dup.md (uuid=2, duplicate=True, tokens=1000)
	# 2. large.md (duplicate=False, tokens=10000)
	# 3. older.md (duplicate=False, tokens=5000, created 2026-03-01)
	assert result[0].uuid == "2"
	assert result[0].duplicate is True
	assert result[1].file_name == "large.md"
	assert result[2].file_name == "older.md"


def test_refusal_crossing_threshold():
	# Before write: 61,141 - 12,400 = 48,741 (under threshold)
	stats = KnowledgeStats(size=61_141, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	compaction_candidates = [
		Candidate(file_name="design-notes.md", uuid="1", estimated_token_count=31_200, created_at="2026-05-02T00:00:00Z", duplicate=False),
		Candidate(file_name="meeting-log.md", uuid="2", estimated_token_count=9_800, created_at="2026-07-19T00:00:00Z", duplicate=True),
		Candidate(file_name="glossary.md", uuid="3", estimated_token_count=4_100, created_at="2026-08-01T00:00:00Z", duplicate=False),
	]

	message = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		added=12_400,
		removed=0,
		candidates_list=compaction_candidates,
		uploads=[],
	)

	expected = (
		"Writing 'notes.md' (12,400 tokens) would push the project past its search threshold: 61,141 of 50,000 tokens, 11,141 over. "
		"Past that line Claude in the web UI retrieves from the project knowledge instead of reading all of it, so a document can go unseen. "
		"The write was undone; nothing changed. "
		"To make room, shrink this content, or compact one of these with write_document overwrite=true: "
		"'design-notes.md' (31,200 tokens, last rewritten 2026-05-02); "
		"'meeting-log.md' (9,800 tokens, 2026-07-19, an older duplicate that the next overwrite of that name removes anyway); "
		"'glossary.md' (4,100 tokens, last rewritten 2026-08-01). "
		"To accept search mode instead, pass allow_search_mode=true."
	)

	assert message == expected


def test_refusal_already_past_threshold():
	# Before write: 72,832 - 12,400 = 60,432 (already past threshold 50,000)
	stats = KnowledgeStats(size=72_832, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	message = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		added=12_400,
		removed=0,
		candidates_list=[],
		uploads=[],
	)

	assert message.startswith("The project is already past its search threshold (60,432 of 50,000 tokens), and writing 'notes.md' would add 12,400 more.")
	assert "There is no other document to compact; shrink this content." in message
	assert "upload" not in message
	assert message.endswith("To accept search mode instead, pass allow_search_mode=true.")


def test_refusal_already_past_threshold_reports_replacement_net_growth():
	# Before write: 51,000.
	# Creating the replacement adds 11,000, then deleting the old 10,000 leaves 52,000.
	stats = KnowledgeStats(size=62_000, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	message = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		added=11_000,
		removed=10_000,
		candidates_list=[],
		uploads=[],
	)

	assert message.startswith("The project is already past its search threshold (51,000 of 50,000 tokens), and writing 'notes.md' (11,000 tokens) would add 1,000 more, net of the 10,000 tokens it replaces.")


def test_refusal_crossing_threshold_names_replaced_tokens():
	# Before write: 45.
	# Creating the replacement adds 60, then deleting the old 40 leaves 65.
	stats = KnowledgeStats(size=105, max_size=2_000_000, search_threshold=50, search_mode=True)
	message = refusal(
		file_name="big.md",
		verdict="search_mode",
		stats=stats,
		added=60,
		removed=40,
		candidates_list=[],
		uploads=[],
	)

	assert message.startswith("Writing 'big.md' (60 tokens, replacing 40 tokens) would push the project past its search threshold: 65 of 50 tokens, 15 over.")


def test_refusal_crossing_maximum():
	stats = KnowledgeStats(size=2_050_000, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	message = refusal(
		file_name="huge.md",
		verdict="over_max",
		stats=stats,
		added=60_000,
		removed=0,
		candidates_list=[],
		uploads=[],
	)

	assert "would push the project past its maximum" in message
	assert "Past that line the web UI refuses to add anything to the project knowledge until something is removed." in message
	assert "allow_search_mode=true" not in message


def test_refusal_names_the_upload_that_fills_the_project():
	# Before write: 72,832 - 12,400 = 60,432, most of it a PDF the documents listing never shows.
	stats = KnowledgeStats(size=72_832, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	message = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		added=12_400,
		removed=0,
		candidates_list=[],
		uploads=[UploadedFile(uuid="u1", file_name="handbook.pdf", size_bytes=1_054_702, page_count=12)],
	)

	assert "There is no other document to compact; shrink this content. The project also holds an uploaded file, which counts toward its size but can be removed only in the web UI: 'handbook.pdf' (1,054,702 bytes, 12 pages). To accept search mode instead" in message


def test_refusal_names_uploads_largest_first_and_one_without_a_size_last():
	stats = KnowledgeStats(size=72_832, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	uploads = [
		UploadedFile(uuid="u1", file_name="photo.png", file_kind="image"),
		UploadedFile(uuid="u2", file_name="brief.pdf", size_bytes=90_000, page_count=1),
		UploadedFile(uuid="u3", file_name="handbook.pdf", size_bytes=1_054_702, page_count=12),
	]

	message = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		added=12_400,
		removed=0,
		candidates_list=[],
		uploads=uploads,
	)

	assert "The project also holds 3 uploaded files, which count toward its size but can be removed only in the web UI: 'handbook.pdf' (1,054,702 bytes, 12 pages); 'brief.pdf' (90,000 bytes, 1 page); 'photo.png' (size not listed)." in message


def test_refusal_names_only_the_three_largest_of_many_uploads():
	stats = KnowledgeStats(size=72_832, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	uploads = [UploadedFile(uuid=f"u{size}", file_name=f"{size}.pdf", size_bytes=size) for size in (2_000, 413_287, 90_000, 1_054_702, 5_000)]

	message = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		added=12_400,
		removed=0,
		candidates_list=[],
		uploads=uploads,
	)

	assert "The project also holds 5 uploaded files, which count toward its size but can be removed only in the web UI; the largest are '1054702.pdf' (1,054,702 bytes); '413287.pdf' (413,287 bytes); '90000.pdf' (90,000 bytes)." in message


def test_refusal_says_when_the_uploads_could_not_be_listed():
	stats = KnowledgeStats(size=72_832, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	message = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		added=12_400,
		removed=0,
		candidates_list=[],
		uploads=None,
	)

	assert "The project's uploaded files could not be listed, so any that take up room are not named here." in message
