"""Turning remote document names into safe local filenames.

A `file_name` from claude.ai is arbitrary user input that has never been near a filesystem, so it is never used as a path directly.
"""

import re
import unicodedata
from pathlib import Path

from .errors import UnsafePathError

# Path separators, the characters Windows rejects, and C0 controls.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')

_DEFAULT_SUFFIX = ".md"

# ext4, APFS, and NTFS all stop a name at 255 bytes or characters.
# The budget stays well under that because callers build on the name: the backup store prefixes a timestamp and may append a counter, and `deduplicate` appends part of a uuid.
_MAX_NAME_BYTES = 200

# To pathlib every dot starts a suffix, so a version number in a title reads as one; suffixes this short taken together are a real compound extension such as `.tar.gz`.
_MAX_COMPOUND_EXTENSION_BYTES = 16


def sanitize(name: str, fallback: str = "untitled", default_suffix: str | None = _DEFAULT_SUFFIX) -> str:
	"""A filesystem-safe version of a remote document name.

	Non-ASCII is preserved deliberately — the documents this serves are Korean, and transliterating them would make the local folder unreadable.
	A name longer than 200 bytes of UTF-8 is cut to fit, keeping its extension, because a filesystem stops at 255 and Hangul is three bytes a character.

	Pass `default_suffix=None` for a name that is not a file, such as a directory.
	"""
	# NFC first, so the same name from macOS and Linux lands on one spelling.
	cleaned = unicodedata.normalize("NFC", name).strip()
	cleaned = _UNSAFE.sub("-", cleaned)
	cleaned = re.sub(r"\.{2,}", "-", cleaned)
	cleaned = re.sub(r"-{2,}", "-", cleaned)
	cleaned = cleaned.strip("-. ")

	if not cleaned:
		cleaned = fallback.strip("-. ") or "untitled"

	if default_suffix and not Path(cleaned).suffix:
		cleaned += default_suffix

	# Last, so that the budget holds whatever was added above.
	return _fit(cleaned, _MAX_NAME_BYTES)


def _fit(name: str, budget: int) -> str:
	"""`name` cut down to at most `budget` UTF-8 bytes, on a character boundary, keeping its extension.

	Hangul is three bytes a character, so a Korean title overflows a filesystem's limit long before it looks long.
	"""
	if len(name.encode("utf-8")) <= budget:
		return name

	suffix = _extension_of(Path(name))
	stem = name.removesuffix(suffix)
	room = budget - len(suffix.encode("utf-8"))
	if room <= 0:
		# An "extension" that fills the budget on its own is not one worth keeping.
		suffix = ""
		stem = name
		room = budget

	# Decoding with errors ignored drops the partial character a byte-level cut may leave at the end.
	stem = stem.encode("utf-8")[:room].decode("utf-8", errors="ignore").rstrip("-. ")

	return stem + suffix


def _extension_of(path: Path) -> str:
	"""The extension worth keeping through a cut: every suffix when together they are short enough to be one compound extension, otherwise the last one alone."""
	compound = "".join(path.suffixes)
	if len(compound.encode("utf-8")) <= _MAX_COMPOUND_EXTENSION_BYTES:
		return compound

	return path.suffix


def deduplicate(names: dict[str, str]) -> dict[str, str]:
	"""Make a uuid -> filename mapping unique, case-insensitively.

	The suffix comes from the uuid rather than a counter so that pulling twice produces the same filenames even if the API returns documents in a different order.
	"""
	counts: dict[str, int] = {}
	for name in names.values():
		key = name.casefold()
		counts[key] = counts.get(key, 0) + 1

	resolved = {}
	for uuid, name in names.items():
		if counts[name.casefold()] == 1:
			resolved[uuid] = name
			continue

		path = Path(name)
		resolved[uuid] = f"{path.stem}-{uuid[:8]}{path.suffix}"

	return resolved


def safe_child(base: Path, name: str) -> Path:
	"""`base / name`, guaranteed not to escape `base`.

	Raises rather than silently sanitizing: by the time a path is being built the name should already have gone through `sanitize`, so an escape here means a bug or an attack, not a merely awkward filename.
	"""
	base_resolved = Path(base).resolve()
	candidate = (base_resolved / name).resolve()

	if not candidate.is_relative_to(base_resolved):
		raise UnsafePathError(f"Refusing to write outside {base_resolved}: {name!r}")

	if candidate == base_resolved:
		raise UnsafePathError(f"{name!r} does not name a file inside {base_resolved}")

	return candidate
