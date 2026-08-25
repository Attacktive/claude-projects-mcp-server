"""Tests for knowledge capacity limits, verdicts, candidate selection, and refusal messages."""

from claude_projects_mcp.capacity import Candidate, candidates, judge, refusal
from claude_projects_mcp.models import Document, KnowledgeStats


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
	docs = [
		Document(uuid="1", file_name="notes.md", estimated_token_count=100, created_at="2026-01-01T00:00:00Z"),
		Document(uuid="2", file_name="doc1.md", estimated_token_count=1000, created_at="2026-02-01T00:00:00Z"),
		Document(uuid="3", file_name="doc2.md", estimated_token_count=2000, created_at="2026-03-01T00:00:00Z"),
		Document(uuid="4", file_name="doc3.md", estimated_token_count=3000, created_at="2026-04-01T00:00:00Z"),
		Document(uuid="5", file_name="doc4.md", estimated_token_count=4000, created_at="2026-05-01T00:00:00Z"),
	]
	cands = candidates(docs, excluding="notes.md")
	assert len(cands) == 3
	assert [c.file_name for c in cands] == ["doc4.md", "doc3.md", "doc2.md"]


def test_candidates_orders_duplicates_first_and_handles_ties_by_age():
	docs = [
		# Non-duplicate large
		Document(uuid="1", file_name="large.md", estimated_token_count=10_000, created_at="2026-05-01T00:00:00Z"),
		# Duplicate copies of dup.md: newest (uuid=3), older (uuid=2)
		Document(uuid="2", file_name="dup.md", estimated_token_count=1_000, created_at="2026-01-01T00:00:00Z"),
		Document(uuid="3", file_name="dup.md", estimated_token_count=1_000, created_at="2026-02-01T00:00:00Z"),
		# Equal size non-duplicates with different ages
		Document(uuid="4", file_name="newer.md", estimated_token_count=5_000, created_at="2026-04-01T00:00:00Z"),
		Document(uuid="5", file_name="older.md", estimated_token_count=5_000, created_at="2026-03-01T00:00:00Z"),
	]
	cands = candidates(docs, excluding="written.md")
	# Candidates:
	# 1. dup.md (uuid=2, duplicate=True, tokens=1000)
	# 2. large.md (duplicate=False, tokens=10000)
	# 3. older.md (duplicate=False, tokens=5000, created 2026-03-01)
	assert cands[0].uuid == "2"
	assert cands[0].duplicate is True
	assert cands[1].file_name == "large.md"
	assert cands[2].file_name == "older.md"


def test_refusal_crossing_threshold():
	# Before write: 61,141 - 12,400 = 48,741 (under threshold)
	stats = KnowledgeStats(size=61_141, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	cands = [
		Candidate(file_name="design-notes.md", uuid="1", estimated_token_count=31_200, created_at="2026-05-02T00:00:00Z", duplicate=False),
		Candidate(file_name="meeting-log.md", uuid="2", estimated_token_count=9_800, created_at="2026-07-19T00:00:00Z", duplicate=True),
		Candidate(file_name="glossary.md", uuid="3", estimated_token_count=4_100, created_at="2026-08-01T00:00:00Z", duplicate=False),
	]
	msg = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		projected=61_141,
		added=12_400,
		candidates_list=cands,
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
	assert msg == expected


def test_refusal_already_past_threshold():
	# Before write: 72,832 - 12,400 = 60,432 (already past threshold 50,000)
	stats = KnowledgeStats(size=72_832, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	msg = refusal(
		file_name="notes.md",
		verdict="search_mode",
		stats=stats,
		projected=72_832,
		added=12_400,
		candidates_list=[],
	)
	assert msg.startswith("The project is already past its search threshold (60,432 of 50,000 tokens), and writing 'notes.md' would add 12,400 more.")
	assert "There is nothing else in the project to compact; shrink this content." in msg
	assert msg.endswith("To accept search mode instead, pass allow_search_mode=true.")


def test_refusal_crossing_maximum():
	stats = KnowledgeStats(size=2_050_000, max_size=2_000_000, search_threshold=50_000, search_mode=True)
	msg = refusal(
		file_name="huge.md",
		verdict="over_max",
		stats=stats,
		projected=2_050_000,
		added=60_000,
		candidates_list=[],
	)
	assert "would push the project past its maximum" in msg
	assert "Past that line the web UI refuses to add anything to the project knowledge until something is removed." in msg
	assert "allow_search_mode=true" not in msg
