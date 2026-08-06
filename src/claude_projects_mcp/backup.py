"""Local copies of document content that is about to be replaced or deleted.

The claude.ai API has no undo and no version history, so this is the only thing standing between an agent's mistake and lost team work.
It is append-only, and a failed save must abort the operation that requested it rather than proceeding unprotected.

Its reach is narrow, and the README says so: it captures only what this tool overwrites, it lives on one machine, and it knows nothing about edits made in the web UI.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .errors import BackupError
from .filenames import safe_child, sanitize

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _utc_now() -> datetime:
	return datetime.now(UTC)


class BackupStore:
	def __init__(self, root: Path, now: Callable[[], datetime] = _utc_now):
		self._root = Path(root)
		self._now = now

	def save(self, project_id: str, file_name: str, content: str) -> Path:
		"""Write `content` to a new file and return its path.

		Raises BackupError rather than returning None on failure, because every caller treats a successful backup as the precondition for mutating anything remote.
		"""
		stamp = self._now().astimezone(UTC).strftime(_STAMP_FORMAT)
		directory = safe_child(self._root, sanitize(project_id, fallback="project", default_suffix=None))
		target = safe_child(directory, f"{stamp}--{sanitize(file_name)}")

		try:
			directory.mkdir(parents=True, exist_ok=True)
			path = _unique(target)
			path.write_text(content, encoding="utf-8")
		except OSError as exception:
			raise BackupError(f"Could not write a backup to {target}: {exception}") from exception

		return path

	@property
	def root(self) -> Path:
		return self._root


def _unique(path: Path) -> Path:
	"""The first free name at or after `path`.

	Two saves can land in the same second, and losing the earlier one would defeat the point of keeping backups at all.
	"""
	if not path.exists():
		return path

	for counter in range(1, 1000):
		candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
		if not candidate.exists():
			return candidate

	raise BackupError(f"Too many backups already exist for {path.name}")
