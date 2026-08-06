"""Moving a project's documents to and from a local folder.

Claude Code is file-native: pulling once, editing with ordinary tools, and pushing backbeats pushing whole documents through tool calls one at a time.

Both directions are deliberately conservative.
Neither deletes anything the other side is missing, and neither overwrites differing content without being asked.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .client import ClaudeProjectsClient
from .errors import ClaudeProjectsError
from .filenames import deduplicate, safe_child, sanitize
from .models import Document


@dataclass(frozen=True, slots=True)
class FileResult:
	file_name: str
	status: str
	local_path: str | None = None
	detail: str | None = None
	backup_path: str | None = None


def summarise(results: list[FileResult]) -> dict[str, int]:
	counts: dict[str, int] = {}
	for result in results:
		counts[result.status] = counts.get(result.status, 0) + 1

	return counts


def pull(
	client: ClaudeProjectsClient,
	project_id: str,
	destination_directory: Path | str,
	overwrite_local: bool = False,
) -> list[FileResult]:
	"""Copy the project's documents into `destination_directory`.

	Local files that differ are kept, not clobbered — someone editing locally should not lose that work to a routine pull.
	Pass `overwrite_local` to take the remote version.
	"""
	destination = Path(destination_directory)
	destination.mkdir(parents=True, exist_ok=True)

	documents = client.list_documents(project_id)
	local_names = deduplicate({document.uuid: sanitize(document.file_name, fallback=document.uuid) for document in documents})

	results = []
	for document in documents:
		results.append(_pull_one(client, project_id, document, destination, local_names[document.uuid], overwrite_local))

	return results


def _pull_one(
	client: ClaudeProjectsClient,
	project_id: str,
	document: Document,
	destination: Path,
	local_name: str,
	overwrite_local: bool,
) -> FileResult:
	try:
		content = document.content
		if content is None:
			content = client.get_document(project_id, document.uuid).content or ""

		target = safe_child(destination, local_name)

		if target.exists():
			if target.read_text(encoding="utf-8") == content:
				return FileResult(document.file_name, "unchanged", local_path=str(target))

			if not overwrite_local:
				return FileResult(
					document.file_name,
					"skipped_exists",
					local_path=str(target),
					detail="local file differs; pass overwrite_local to take the remote version",
				)

		target.write_text(content, encoding="utf-8")
		return FileResult(document.file_name, "written", local_path=str(target))
	except (ClaudeProjectsError, OSError, UnicodeDecodeError) as exception:
		return FileResult(document.file_name, "error", detail=str(exception))


def push(
	client: ClaudeProjectsClient,
	project_id: str,
	source_directory: Path | str,
	pattern: str = "*.md",
	overwrite: bool = False,
	dry_run: bool = False,
	backup: Callable[[str, str], str] | None = None,
) -> list[FileResult]:
	"""Upload `source_directory`'s files into the project.

	Never deletes remote documents that are missing locally: a partial folder must not prune a shared project.
	Files are matched non-recursively, because a project's documents are a flat list and recursing would collide names.
	"""
	source = Path(source_directory)
	if not source.is_dir():
		raise FileNotFoundError(f"No such directory: {source}")

	remote = _index_by_name(client.list_documents(project_id))

	results = []
	for path in sorted(source.glob(pattern)):
		if not path.is_file():
			continue

		results.append(_push_one(client, project_id, path, remote, overwrite, dry_run, backup))

	return results


def _push_one(
	client: ClaudeProjectsClient,
	project_id: str,
	path: Path,
	remote: dict[str, Document],
	overwrite: bool,
	dry_run: bool,
	backup: Callable[[str, str], str] | None,
) -> FileResult:
	name = path.name
	try:
		content = path.read_text(encoding="utf-8")
	except UnicodeDecodeError:
		return FileResult(name, "error", local_path=str(path), detail="not UTF-8 text; only text documents can be uploaded")
	except OSError as exception:
		return FileResult(name, "error", local_path=str(path), detail=str(exception))

	try:
		existing = remote.get(name)

		if existing is None:
			if dry_run:
				return FileResult(name, "created", local_path=str(path), detail="dry run")

			client.create_document(project_id, name, content)
			return FileResult(name, "created", local_path=str(path))

		if existing.is_stub:
			existing = client.get_document(project_id, existing.uuid)

		if existing.content == content:
			return FileResult(name, "unchanged", local_path=str(path))

		if not overwrite:
			return FileResult(
				name,
				"skipped_exists",
				local_path=str(path),
				detail="remote document differs; pass overwrite to replace it",
			)

		if dry_run:
			return FileResult(name, "replaced", local_path=str(path), detail="dry run")

		result = client.replace_document(project_id, name, content, backup=backup)
		detail = None
		if result.failed_delete_uuids:
			detail = f"saved, but {len(result.failed_delete_uuids)} old copy could not be removed and remains as a duplicate"

		return FileResult(name, "replaced", local_path=str(path), detail=detail, backup_path=result.backup_path)
	except (ClaudeProjectsError, OSError) as exception:
		return FileResult(name, "error", local_path=str(path), detail=str(exception))


def _index_by_name(documents: list[Document]) -> dict[str, Document]:
	"""Newest wins, since `list_documents` is already newest-first."""
	index: dict[str, Document] = {}
	for document in documents:
		index.setdefault(document.file_name, document)

	return index
