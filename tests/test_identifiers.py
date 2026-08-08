"""The project uuid <-> chat_project_id mapping.

Worth testing hard: it is a reimplementation of somebody else's encoding, and the whole
project filter in `list_scheduled_tasks` rests on it.
"""

import pytest

from claude_projects_mcp.identifiers import chat_project_id, project_uuid_from

# Captured together from one real create response (2026-08-08), so this pair is ground truth rather than a round-trip of our own arithmetic.
REAL_PROJECT_UUID = "019fda83-e171-70e3-8e2d-e698c7feb1c2"
REAL_CHAT_PROJECT_ID = "claude_proj_011Cdnmd5MfLyEFPnPDRUwNq"


def test_the_observed_pair_encodes_exactly():
	assert chat_project_id(REAL_PROJECT_UUID) == REAL_CHAT_PROJECT_ID


def test_the_observed_pair_decodes_exactly():
	assert project_uuid_from(REAL_CHAT_PROJECT_ID) == REAL_PROJECT_UUID


def test_encoding_round_trips():
	uuid = "6fa459ea-ee8a-3ca4-894e-db77e160355e"

	assert project_uuid_from(chat_project_id(uuid)) == uuid


def test_the_body_is_padded_to_a_fixed_width():
	"""A small uuid must not produce a short id.

	`env_011111111111111111111117` decodes to 6, which is only possible if the body is padded to 22 characters with the base58 zero digit.
	"""
	encoded = chat_project_id("00000000-0000-0000-0000-000000000006")

	assert encoded == "claude_proj_011111111111111111111117"


def test_a_zero_uuid_still_encodes():
	encoded = chat_project_id("00000000-0000-0000-0000-000000000000")

	assert encoded == "claude_proj_011111111111111111111111"
	assert project_uuid_from(encoded) == "00000000-0000-0000-0000-000000000000"


def test_uuids_are_accepted_in_any_spelling():
	"""Braces, upper case, and no dashes are all the same uuid, and the caller should not have to care."""
	assert chat_project_id("019FDA83E17170E38E2DE698C7FEB1C2") == REAL_CHAT_PROJECT_ID


def test_a_foreign_prefix_does_not_decode():
	"""Trigger ids share the encoding but name a different kind of thing, so they must not be mistaken for projects."""
	assert project_uuid_from("trig_01QALV7ipmVEo8TmK9vwEA5s") is None


def test_a_body_with_non_base58_characters_does_not_decode():
	# '0' is deliberately absent from the base58 alphabet, so this cannot be a real id.
	assert project_uuid_from("claude_proj_0100000000000000000000") is None


def test_an_oversized_body_does_not_decode():
	"""Something that decodes past 128 bits is not a uuid, however well-formed it looks."""
	assert project_uuid_from("claude_proj_01zzzzzzzzzzzzzzzzzzzzzzzz") is None


def test_a_malformed_uuid_is_refused():
	with pytest.raises(ValueError):
		chat_project_id("not-a-uuid")
