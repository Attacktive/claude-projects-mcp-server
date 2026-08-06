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


def sanitize(name: str, fallback: str = "untitled", default_suffix: str | None = _DEFAULT_SUFFIX) -> str:
	"""A filesystem-safe version of a remote document name.

	Non-ASCII is preserved deliberately — the documents this serves are Korean, and transliterating them would make the local folder unreadable.

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

	return cleaned


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
