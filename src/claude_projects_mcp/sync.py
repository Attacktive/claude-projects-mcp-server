"""Moving a project's documents to and from a local folder.

Claude Code is file-native: pulling once, editing with ordinary tools, and pushing backbeats pushing whole documents through tool calls one at a time.

Both directions are deliberately conservative.
Neither deletes anything the other side is missing, and neither overwrites differing content without being asked.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .client import ClaudeProjectsClient
from .errors import ClaudeProjectsError, KnowledgeFullError
from .filenames import deduplicate, safe_child, sanitize
from .models import Document


@dataclass(frozen=True, slots=True)
class FileResult:
	file_name: str
	status: str
	local_path: str | None = None
	detail: str | None = None
	backup_path: str | None = None


@dataclass(frozen=True, slots=True)
class PushOptions:
	overwrite: bool = False
	dry_run: bool = False
	allow_search_mode: bool = False
	backup: Callable[[str, str], str] | None = None


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
	allow_search_mode: bool = False,
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
	options = PushOptions(overwrite=overwrite, dry_run=dry_run, allow_search_mode=allow_search_mode, backup=backup)

	matching_paths = [path for path in sorted(source.glob(pattern)) if path.is_file()]
	results = []
	for index, path in enumerate(matching_paths):
		result = _push_one(client, project_id, path, remote, options)
		results.append(result)
		if result.status in ("refused_full", "written_over_capacity"):
			for remaining in matching_paths[index + 1 :]:
				results.append(FileResult(remaining.name, "skipped_full", local_path=str(remaining), detail="not attempted: the project has no room"))

			break

	return results


def _push_one(
	client: ClaudeProjectsClient,
	project_id: str,
	path: Path,
	remote: dict[str, Document],
	options: PushOptions,
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
			return _push_new(client, project_id, path, name, content, options)

		return _push_existing(client, project_id, path, name, content, existing, options)
	except KnowledgeFullError as exception:
		return FileResult(name, "refused_full", local_path=str(path), detail=str(exception))
	except (ClaudeProjectsError, OSError) as exception:
		return FileResult(name, "error", local_path=str(path), detail=str(exception))


def _push_new(client: ClaudeProjectsClient, project_id: str, path: Path, name: str, content: str, options: PushOptions) -> FileResult:
	if options.dry_run:
		return FileResult(name, "created", local_path=str(path), detail="dry run")

	result = client.save_document(project_id, name, content, replacing=[], allow_search_mode=options.allow_search_mode)
	if result.rollback_failed:
		return FileResult(
			name,
			"written_over_capacity",
			local_path=str(path),
			detail=f"write took the project past capacity and could not be undone: deleting new document {result.uuid} failed.",
		)

	return FileResult(name, "created", local_path=str(path))


def _push_existing(
	client: ClaudeProjectsClient,
	project_id: str,
	path: Path,
	name: str,
	content: str,
	existing: Document,
	options: PushOptions,
) -> FileResult:
	if existing.is_stub:
		existing = client.get_document(project_id, existing.uuid)

	if existing.content == content:
		return FileResult(name, "unchanged", local_path=str(path))

	if not options.overwrite:
		return FileResult(
			name,
			"skipped_exists",
			local_path=str(path),
			detail="remote document differs; pass overwrite to replace it",
		)

	if options.dry_run:
		return FileResult(name, "replaced", local_path=str(path), detail="dry run")

	result = client.replace_document(project_id, name, content, allow_search_mode=options.allow_search_mode, backup=options.backup)
	if result.rollback_failed:
		return FileResult(
			name,
			"written_over_capacity",
			local_path=str(path),
			detail=f"write took the project past capacity and could not be undone: deleting new document {result.uuid} failed. The previous document {existing.uuid} was left in place.",
			backup_path=result.backup_path,
		)

	detail = None
	if result.failed_delete_uuids:
		detail = f"saved, but {len(result.failed_delete_uuids)} old copy could not be removed and remains as a duplicate"

	return FileResult(name, "replaced", local_path=str(path), detail=detail, backup_path=result.backup_path)


def _index_by_name(documents: list[Document]) -> dict[str, Document]:
	"""Newest wins, since `list_documents` is already newest-first."""
	index: dict[str, Document] = {}
	for document in documents:
		index.setdefault(document.file_name, document)

	return index
