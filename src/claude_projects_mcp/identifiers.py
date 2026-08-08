"""Translating a project uuid into the id the scheduled-task API answers with.

The two halves of that API speak different id spaces: a task is created with a `project_uuid`,
but every task read back names its project as a `chat_project_id` — `claude_proj_01` followed by
the uuid in base58, padded to a fixed width. Nothing in the API maps between them, and the
listing cannot be filtered server-side, so a caller asking for one project's tasks has no way
through except to compute the id and match on it.

This is a reimplementation of an undocumented encoding, so it is kept in one small module that
`test_identifiers.py` can pin down against the real pair captured on 2026-08-08. Decoding refuses
anything it does not recognise rather than guessing: a wrong answer here would silently attach a
task to the wrong project.
"""

import uuid as uuid_module

# Bitcoin's base58 alphabet: no 0, O, I, or l, so an id cannot be misread aloud.
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

_PREFIX = "claude_proj_01"

# Real ids are always this long: `env_011111111111111111111117` decodes to 6, which is only possible if short values are left-padded.
_BODY_WIDTH = 22

_UUID_CEILING = 1 << 128


def chat_project_id(project_uuid: str) -> str:
	"""The `claude_proj_…` id the scheduled-task API uses for this project.

	Raises ValueError if the argument is not a uuid, which is the caller passing the wrong thing rather than anything to recover from.
	"""
	value = uuid_module.UUID(project_uuid).int

	digits = ""
	while value:
		value, remainder = divmod(value, 58)
		digits = _ALPHABET[remainder] + digits

	return _PREFIX + digits.rjust(_BODY_WIDTH, _ALPHABET[0])


def project_uuid_from(identifier: str) -> str | None:
	"""The project uuid inside a `claude_proj_…` id, or None if it is not one.

	None rather than an exception because the caller's job is to match ids, not to validate them:
	a task belonging to no project, or to something this encoding does not cover, is a fact to skip past rather than an error.
	"""
	if not identifier.startswith(_PREFIX):
		return None

	value = 0
	for character in identifier[len(_PREFIX) :]:
		digit = _ALPHABET.find(character)
		if digit < 0:
			return None

		value = value * 58 + digit

	if value >= _UUID_CEILING:
		return None

	return str(uuid_module.UUID(int=value))
