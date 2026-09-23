"""Pure logic for knowledge capacity limits, verdicts, and compaction candidate ranking.

No network or file I/O lives here.
"""

from dataclasses import dataclass
from typing import Literal

from .models import Document, KnowledgeStats

Verdict = Literal["fits", "search_mode", "over_max"]

_SEARCH_MODE_HINT = "To accept search mode instead, pass allow_search_mode=true."


@dataclass(frozen=True, slots=True)
class Candidate:
	file_name: str
	uuid: str
	estimated_token_count: int | None
	created_at: str | None
	duplicate: bool


def judge(stats: KnowledgeStats, added: int, removed: int) -> Verdict:
	"""Determine whether adding a write fits within limits, enters search mode, or exceeds maximum capacity."""
	if added <= removed:
		return "fits"

	projected = stats.size - removed
	if projected > stats.max_size:
		return "over_max"

	if projected > stats.search_threshold:
		return "search_mode"

	return "fits"


def line_of(stats: KnowledgeStats, verdict: Verdict) -> tuple[str, int]:
	"""The line a verdict is about, as the name a message calls it and its value in tokens."""
	if verdict == "over_max":
		return "its maximum", stats.max_size

	return "its search threshold", stats.search_threshold


def crossing(file_name: str, verdict: Verdict, stats: KnowledgeStats, added: int, removed: int) -> str:
	"""The sentence saying a write would cross the line, or that the project was already past it and a growing write would add more.

	`stats.size` is the size as measured after the write, which is how the gate sees it.
	The size before the write and the final size after replaced copies are removed are recovered from `added` and `removed`.
	"""
	line_name, limit = line_of(stats, verdict)
	previous_size = stats.size - added
	projected = stats.size - removed
	if previous_size > limit:
		if removed:
			net_growth = added - removed
			return f"The project is already past {line_name} ({previous_size:,} of {limit:,} tokens), and writing {file_name!r} ({added:,} tokens) would add {net_growth:,} more, net of the {removed:,} tokens it replaces."

		return f"The project is already past {line_name} ({previous_size:,} of {limit:,} tokens), and writing {file_name!r} would add {added:,} more."

	if removed:
		return f"Writing {file_name!r} ({added:,} tokens, replacing {removed:,} tokens) would push the project past {line_name}: {projected:,} of {limit:,} tokens, {projected - limit:,} over."

	return f"Writing {file_name!r} ({added:,} tokens) would push the project past {line_name}: {projected:,} of {limit:,} tokens, {projected - limit:,} over."


def admits(verdict: Verdict, allow_search_mode: bool) -> bool:
	"""Whether a write with this verdict goes through: anything that fits, and search mode when the caller accepts it."""
	return verdict == "fits" or (verdict == "search_mode" and allow_search_mode)


def tokens_of(documents: list[Document]) -> int:
	"""These documents' token counts added up, skipping any the listing gave none for."""
	return sum(document.estimated_token_count for document in documents if document.estimated_token_count is not None)


def by_name(documents: list[Document]) -> dict[str, list[Document]]:
	"""The documents grouped under their file names, in the order given; more than one under a name is an interrupted save."""
	grouped: dict[str, list[Document]] = {}
	for document in documents:
		grouped.setdefault(document.file_name, []).append(document)

	return grouped


def consequence(verdict: Verdict) -> str:
	"""What lies past the line a verdict names."""
	if verdict == "over_max":
		return "Past that line the web UI refuses to add anything to the project knowledge until something is removed."

	return "Past that line Claude in the web UI retrieves from the project knowledge instead of reading all of it, so a document can go unseen."


def hint(verdict: Verdict) -> list[str]:
	"""The way past a refusal, as zero or one sentence: search mode can be accepted, the maximum cannot."""
	if verdict == "over_max":
		return []

	return [_SEARCH_MODE_HINT]


def candidates(documents: list[Document], excluding: str) -> list[Candidate]:
	"""Return up to three documents most worth compacting, excluding the file name being written.

	Order: duplicates first (older copies of multi-copy file names),
	then by estimated_token_count descending (default 0 if None),
	older created_at first on ties (default empty string if None).
	"""
	all_candidates: list[Candidate] = []
	for file_name, copies in by_name(documents).items():
		if file_name == excluding:
			continue

		sorted_copies = sorted(copies, key=lambda document: document.created_at or "", reverse=True)
		newest = sorted_copies[0]
		older_copies = sorted_copies[1:]

		all_candidates.append(
			Candidate(
				file_name=newest.file_name,
				uuid=newest.uuid,
				estimated_token_count=newest.estimated_token_count,
				created_at=newest.created_at,
				duplicate=False,
			)
		)

		for copy in older_copies:
			all_candidates.append(
				Candidate(
					file_name=copy.file_name,
					uuid=copy.uuid,
					estimated_token_count=copy.estimated_token_count,
					created_at=copy.created_at,
					duplicate=True,
				)
			)

	# Timsort is stable: sort secondary criterion (oldest created_at first) then primary criteria descending.
	all_candidates.sort(key=lambda candidate: candidate.created_at or "")
	all_candidates.sort(
		key=lambda candidate: (candidate.duplicate, candidate.estimated_token_count if candidate.estimated_token_count is not None else 0),
		reverse=True,
	)

	return all_candidates[:3]


def refusal(
	file_name: str,
	verdict: Verdict,
	stats: KnowledgeStats,
	added: int,
	removed: int,
	candidates_list: list[Candidate],
) -> str:
	"""Format the refusal message for the model when a write exceeds search threshold or maximum size."""
	if not candidates_list:
		candidates_sentence = "There is nothing else in the project to compact; shrink this content."
	else:
		formatted_candidates = [_format_candidate(candidate) for candidate in candidates_list]
		candidates_sentence = f"To make room, shrink this content, or compact one of these with write_document overwrite=true: {'; '.join(formatted_candidates)}."

	parts = [
		crossing(file_name, verdict, stats, added, removed),
		consequence(verdict),
		"The write was undone; nothing changed.",
		candidates_sentence,
		*hint(verdict),
	]

	return " ".join(parts)


def _format_candidate(candidate: Candidate) -> str:
	if candidate.estimated_token_count is not None:
		tokens = candidate.estimated_token_count
	else:
		tokens = 0

	date_part = (candidate.created_at or "")[:10]
	if candidate.duplicate:
		return f"{candidate.file_name!r} ({tokens:,} tokens, {date_part}, an older duplicate that the next overwrite of that name removes anyway)"

	return f"{candidate.file_name!r} ({tokens:,} tokens, last rewritten {date_part})"
