"""Pure logic for knowledge capacity limits, verdicts, and compaction candidate ranking.

No network or file I/O lives here.
"""

from dataclasses import dataclass
from typing import Literal

from .models import Document, KnowledgeStats

Verdict = Literal["fits", "search_mode", "over_max"]


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


def candidates(documents: list[Document], excluding: str) -> list[Candidate]:
	"""Return up to three documents most worth compacting, excluding the file name being written.

	Order: duplicates first (older copies of multi-copy file names),
	then by estimated_token_count descending (default 0 if None),
	older created_at first on ties (default empty string if None).
	"""
	by_name: dict[str, list[Document]] = {}
	for document in documents:
		by_name.setdefault(document.file_name, []).append(document)

	all_candidates: list[Candidate] = []
	for file_name, copies in by_name.items():
		if file_name == excluding:
			continue

		sorted_copies = sorted(copies, key=lambda doc: doc.created_at or "", reverse=True)
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
	all_candidates.sort(key=lambda c: c.created_at or "")
	all_candidates.sort(
		key=lambda c: (c.duplicate, c.estimated_token_count if c.estimated_token_count is not None else 0),
		reverse=True,
	)
	return all_candidates[:3]


def refusal(
	file_name: str,
	verdict: Verdict,
	stats: KnowledgeStats,
	projected: int,
	added: int,
	candidates_list: list[Candidate],
) -> str:
	"""Format the refusal message for the model when a write exceeds search threshold or maximum size."""
	is_max = verdict == "over_max"
	line_name = "its maximum" if is_max else "its search threshold"
	line_val = stats.max_size if is_max else stats.search_threshold
	over_amount = projected - line_val

	prev_size = stats.size - added
	was_already_past = prev_size > line_val

	if was_already_past:
		first_sentence = f"The project is already past {line_name} ({prev_size:,} of {line_val:,} tokens), and writing {file_name!r} would add {added:,} more."
	else:
		first_sentence = f"Writing {file_name!r} ({added:,} tokens) would push the project past {line_name}: {projected:,} of {line_val:,} tokens, {over_amount:,} over."

	if is_max:
		second_sentence = "Past that line the web UI refuses to add anything to the project knowledge until something is removed."
	else:
		second_sentence = "Past that line Claude in the web UI retrieves from the project knowledge instead of reading all of it, so a document can go unseen."

	third_sentence = "The write was undone; nothing changed."

	if not candidates_list:
		candidates_sentence = "There is nothing else in the project to compact; shrink this content."
	else:
		formatted_candidates = [_format_candidate(c) for c in candidates_list]
		candidates_sentence = f"To make room, shrink this content, or compact one of these with write_document overwrite=true: {'; '.join(formatted_candidates)}."

	parts = [first_sentence, second_sentence, third_sentence, candidates_sentence]
	if not is_max:
		parts.append("To accept search mode instead, pass allow_search_mode=true.")

	return " ".join(parts)


def _format_candidate(candidate: Candidate) -> str:
	tokens = candidate.estimated_token_count if candidate.estimated_token_count is not None else 0
	date_part = (candidate.created_at or "")[:10]
	if candidate.duplicate:
		return f"{candidate.file_name!r} ({tokens:,} tokens, {date_part}, an older duplicate that the next overwrite of that name removes anyway)"
	return f"{candidate.file_name!r} ({tokens:,} tokens, last rewritten {date_part})"
