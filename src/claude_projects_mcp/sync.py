"""Moving a project's documents to and from a local folder.

Claude Code is file-native: pulling once, editing with ordinary tools, and pushing backbeats pushing whole documents through tool calls one at a time.

Pulling brings down the documents and the files uploaded through the web UI; pushing carries text documents only, because nothing here can upload a file, so a pull-and-push copy is not a full migration.

Both directions are deliberately conservative.
Neither deletes anything the other side is missing, and neither overwrites differing content without being asked.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path, PurePath
from typing import Self

from .capacity import Verdict, admits, by_name, consequence, crossing, hint, judge, tokens_of
from .client import ClaudeProjectsClient
from .errors import ClaudeProjectsError, InvalidPatternError, KnowledgeFullError
from .filenames import deduplicate, safe_child, sanitize
from .models import Document, KnowledgeStats, UploadedFile

# Stands in for every upload in a pull's results when the files listing itself could not be fetched.
UPLOADS_PLACEHOLDER = "(uploaded files)"

_LOCAL_DIFFERS = "local file differs; pass overwrite_local to take the remote version"

# What a dry run assumes a character costs when the project holds no document to measure against.
# Observed once, on 2026-09-18: a Korean-and-English markdown document of 139,773 characters that the API counted as 42,660 tokens, so 0.305 tokens per character.
# One observation is a fallback, not a fact about every project, which is why any project holding a measured document is calibrated from that instead.
_FALLBACK_TOKENS_PER_CHARACTER = 0.3

# How many documents a dry run fetches whole to learn that rate when the listing carries token counts but no text.
_CALIBRATION_SAMPLE = 3


@dataclass(frozen=True, slots=True)
class FileResult:
	file_name: str
	status: str
	local_path: str | None = None
	detail: str | None = None
	backup_path: str | None = None
	# "document" for a text document, "upload" for a file uploaded through the web UI, which comes down as bytes.
	kind: str = "document"
	# What the model must be told about this row, which the server lifts into the result's top-level warning; not part of the row as serialized.
	warning: str | None = None


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
	"""Copy the project's documents and uploaded files into `destination_directory`.

	Local files that differ are kept, not clobbered — someone editing locally should not lose that work to a routine pull.
	Pass `overwrite_local` to take the remote version.
	"""
	destination = Path(destination_directory)
	destination.mkdir(parents=True, exist_ok=True)

	documents = client.list_documents(project_id)
	uploads, uploads_failure = _uploads_of(client, project_id)

	# One namespace for both kinds, so a document and an upload sharing a name cannot overwrite each other.
	names = {document.uuid: sanitize(document.file_name, fallback=document.uuid) for document in documents}
	# No `.md` default for an upload: that suffix exists for documents the web UI renders by extension, and would mislabel a PDF.
	names.update({upload.uuid: sanitize(upload.file_name, fallback=upload.uuid, default_suffix=None) for upload in uploads})
	local_names = deduplicate(names)

	results = []
	for document in documents:
		result = _pull_one(client, project_id, document, destination, local_names[document.uuid], overwrite_local)
		results.append(_noting_rename(result, document.file_name, local_names[document.uuid]))

	for upload in uploads:
		results.append(_pull_upload(client, upload, destination, local_names[upload.uuid], overwrite_local))

	if uploads_failure is not None:
		results.append(uploads_failure)

	return results


def _noting_rename(result: FileResult, remote_name: str, local_name: str) -> FileResult:
	"""The result of a document that lives under a name other than its remote one, with a warning attached.

	A name is changed when it holds characters a filesystem rejects, lacks an extension and gains the `.md` default, is spelled in a different Unicode normalization, is too long for a filesystem, or collides with another document's.
	`push` matches local names against remote ones, so pushing such a file back would create a second document rather than update this one, and every pull says so because every pull is a chance to push.
	"""
	if result.status == "error" or local_name == remote_name:
		return result

	note = f"lives locally as {local_name!r} rather than under its remote name; push_documents matches by name, so pushing this file back would create a new document rather than update this one"
	if result.detail is None:
		return replace(result, detail=note)

	return replace(result, detail=f"{result.detail}; {note}")


def _uploads_of(client: ClaudeProjectsClient, project_id: str) -> tuple[list[UploadedFile], FileResult | None]:
	"""The project's uploads, or an error result standing in for them when the listing itself failed.

	The documents are already worth writing by then, so the failure rides along in the results rather than throwing them away.
	"""
	uploads, failure = client.try_list_uploaded_files(project_id)
	if uploads is not None:
		return uploads, None

	return [], FileResult(UPLOADS_PLACEHOLDER, "error", detail=f"uploaded files could not be listed, so none were copied: {failure}", kind="upload")


def _pull_upload(
	client: ClaudeProjectsClient,
	upload: UploadedFile,
	destination: Path,
	local_name: str,
	overwrite_local: bool,
) -> FileResult:
	@cache
	def data() -> bytes:
		return client.download_uploaded_file(upload)

	def unchanged(path: Path) -> bool:
		# A size that disagrees settles it without moving the file; equal sizes still need the bytes to compare.
		# An upload with no original gets no shortcut, so what comes back is the client's refusal rather than advice to overwrite with a file that cannot be fetched.
		if upload.download_url is not None and upload.size_bytes is not None and path.stat().st_size != upload.size_bytes:
			return False

		return path.read_bytes() == data()

	try:
		return _place(
			upload.file_name,
			safe_child(destination, local_name),
			overwrite_local,
			"upload",
			unchanged=unchanged,
			write=lambda path: path.write_bytes(data()),
		)
	except (ClaudeProjectsError, OSError) as exception:
		return FileResult(upload.file_name, "error", detail=str(exception), kind="upload")


def _place(
	file_name: str,
	target: Path,
	overwrite_local: bool,
	kind: str,
	*,
	unchanged: Callable[[Path], bool],
	write: Callable[[Path], object],
) -> FileResult:
	"""Put a remote file in place, unless a local copy already matches, or differs without permission to replace it.

	Documents compare as text and uploads as bytes, which is all that differs between the two kinds; the rules about when to write are the same.
	"""
	if target.exists():
		if unchanged(target):
			return FileResult(file_name, "unchanged", local_path=str(target), kind=kind)

		if not overwrite_local:
			return FileResult(file_name, "skipped_exists", local_path=str(target), detail=_LOCAL_DIFFERS, kind=kind)

	write(target)
	return FileResult(file_name, "written", local_path=str(target), kind=kind)


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

		return _place(
			document.file_name,
			safe_child(destination, local_name),
			overwrite_local,
			"document",
			unchanged=lambda path: path.read_text(encoding="utf-8") == content,
			write=lambda path: path.write_text(content, encoding="utf-8"),
		)
	except (ClaudeProjectsError, OSError, UnicodeDecodeError) as exception:
		return FileResult(document.file_name, "error", detail=str(exception))


@dataclass(frozen=True, slots=True)
class _PushContext:
	"""What every file in one push shares."""

	client: ClaudeProjectsClient
	project_id: str
	# Resolved, so a file that resolves elsewhere can be told from one that merely spells the folder differently.
	source: Path
	# Every remote copy of each name, newest first; more than one copy is an interrupted save that the next replacement of that name cleans up.
	remote: dict[str, list[Document]]
	options: PushOptions
	# Present on a dry run only.
	preview: _Preview | None


@dataclass(slots=True)
class _Preview:
	"""A dry run's stand-in for the capacity gate inside `client.save_document`.

	The real gate writes, measures, and rolls back.
	A preview writes nothing, so it carries the project's size forward from file to file and estimates each file's tokens from its characters, at the rate the project's own documents show.
	"""

	# Why the stop cannot be projected; when set, every row says so and nothing else here is consulted.
	unavailable: str | None
	# None only when `unavailable` says the stats could not be fetched.
	stats: KnowledgeStats | None
	# None when no document offers both its text and a token count, in which case the fallback rate applies.
	tokens_per_character: float | None
	allow_search_mode: bool
	size: int = 0

	@classmethod
	def of(cls, client: ClaudeProjectsClient, project_id: str, documents: list[Document], options: PushOptions) -> Self | None:
		if not options.dry_run:
			return None

		stats, failure = client.try_knowledge_stats(project_id)
		if stats is None:
			return cls(f"the project's knowledge stats could not be fetched: {failure}", None, None, options.allow_search_mode)

		# A listing that counts nothing means the API is not reporting token counts, and the real gate admits a write it cannot measure rather than refuse it.
		if documents and all(document.estimated_token_count is None for document in documents):
			return cls("no listed document carries a token count, so what a write would add cannot be estimated; the real push skips its capacity check when the API reports no count either", stats, None, options.allow_search_mode, stats.size)

		return cls(None, stats, _tokens_per_character(_measured(client, project_id, documents)), options.allow_search_mode, stats.size)

	def result(self, name: str, path: Path, status: str, content: str, replacing: list[Document]) -> FileResult:
		"""The row the real push would produce for this file, or the refusal it would stop at."""
		if self.unavailable is not None:
			note = f"dry run; where the push would stop was not previewed: {self.unavailable}"
			return FileResult(name, status, local_path=str(path), detail=note, warning=note)

		added = math.ceil(len(content) * self._rate())
		removed = tokens_of(replacing)

		# `judge` reads the size as measured after the create, which is how the real gate sees it.
		after_create = replace(self.stats, size=self.size + added)
		verdict = judge(after_create, added, removed)
		if admits(verdict, self.allow_search_mode):
			self.size = after_create.size - removed
			return FileResult(name, status, local_path=str(path), detail="dry run")

		message = self._refusal(name, verdict, after_create, added, removed)
		return FileResult(name, "refused_full", local_path=str(path), detail=message, warning=message)

	def _rate(self) -> float:
		if self.tokens_per_character is None:
			return _FALLBACK_TOKENS_PER_CHARACTER

		return self.tokens_per_character

	def _basis(self) -> str:
		if self.tokens_per_character is None:
			return f"at {_FALLBACK_TOKENS_PER_CHARACTER} tokens per character, the default when no document offers both its text and a token count to measure from"

		return f"at the {self.tokens_per_character:.2f} tokens per character this project's documents average"

	def _refusal(self, name: str, verdict: Verdict, after_create: KnowledgeStats, added: int, removed: int) -> str:
		sentences = [
			crossing(name, verdict, after_create, after_create.size - removed, added),
			consequence(verdict),
			f"That figure is estimated {self._basis()}, since a dry run writes nothing to measure.",
			"The real push would stop here and write nothing more.",
			*hint(verdict),
		]

		return " ".join(sentences)


def _measured(client: ClaudeProjectsClient, project_id: str, documents: list[Document]) -> list[Document]:
	"""The documents that offer both their text and a token count, fetching a few whole when the listing counts them but hides their text.

	The API is free to stop including text in listings, and a dry run exists to know before writing, so a handful of fetches is a fair price for the rate.
	"""
	counted = [document for document in documents if document.estimated_token_count is not None]
	measured = [document for document in counted if document.content is not None]
	if measured:
		return measured

	return _sampled(client, project_id, counted[:_CALIBRATION_SAMPLE])


def _sampled(client: ClaudeProjectsClient, project_id: str, documents: list[Document]) -> list[Document]:
	"""These documents fetched whole, keeping the ones that came back with both text and a count.

	One that cannot be fetched teaches nothing and is skipped; the rate falls back if none can be.
	"""
	sampled = []
	for document in documents:
		try:
			sampled.append(client.get_document(project_id, document.uuid))
		except ClaudeProjectsError:
			continue

	return [document for document in sampled if document.content and document.estimated_token_count is not None]


def _tokens_per_character(measured: list[Document]) -> float | None:
	"""The rate these documents show, or None when there is nothing to measure."""
	characters = sum(len(document.content or "") for document in measured)
	if characters == 0:
		return None

	return tokens_of(measured) / characters


def push(
	client: ClaudeProjectsClient,
	project_id: str,
	source_directory: Path | str,
	*,
	pattern: str = "*.md",
	options: PushOptions | None = None,
) -> list[FileResult]:
	"""Upload `source_directory`'s files into the project.

	Never deletes remote documents that are missing locally: a partial folder must not prune a shared project.
	Matches only the files directly inside the folder, and refuses a pattern that reaches elsewhere, because a project's documents are a flat list and recursing would collide names.
	A file that resolves outside the folder, such as a symbolic link to somewhere else on disk, is reported rather than uploaded.
	A dry run previews where the real push would stop, from estimated token counts, since it writes nothing to measure.
	"""
	source = Path(source_directory)
	if not source.is_dir():
		raise FileNotFoundError(f"No such directory: {source}")

	if options is None:
		options = PushOptions()

	matching_paths = _matching_files(source, pattern)
	documents = client.list_documents(project_id)
	preview = None
	if matching_paths:
		preview = _Preview.of(client, project_id, documents, options)

	context = _PushContext(client, project_id, source.resolve(), by_name(documents), options, preview)

	return _push_all(context, matching_paths)


def _matching_files(source: Path, pattern: str) -> list[Path]:
	"""The files in `source` itself that match `pattern`, in name order.

	A project's documents are a flat list, so a pattern that would reach into other directories is refused rather than honored: two same-named files at different depths would land as duplicates of one name, which is the state `list_documents` flags as an interrupted save.
	The check is on the pattern's structure rather than on what it happens to match, so the same pattern gets the same answer whatever the folder holds.
	"""
	pure = PurePath(pattern)
	if not pure.parts:
		raise InvalidPatternError(f"pattern {pattern!r} names nothing; pass a file name pattern such as '*.md'.")

	if pure.anchor or len(pure.parts) > 1 or pure.parts[0] in ("..", "**"):
		raise InvalidPatternError(f"pattern {pattern!r} is a path, not a file name pattern. A project's documents are a flat list, so push_documents matches only the files directly inside source_directory; pass something like '*.md'.")

	return [path for path in sorted(source.glob(pattern)) if path.is_file()]


def _push_all(context: _PushContext, paths: list[Path]) -> list[FileResult]:
	if context.preview is None:
		not_attempted = "not attempted: the project has no room"
	else:
		not_attempted = "not attempted: the preview stopped at an earlier file"

	results = []
	for index, path in enumerate(paths):
		result = _escape_of(context, path)
		if result is None:
			result = _push_one(context, path)

		results.append(result)
		if result.status in ("refused_full", "written_over_capacity"):
			for remaining in paths[index + 1 :]:
				results.append(FileResult(remaining.name, "skipped_full", local_path=str(remaining), detail=not_attempted))

			break

	return results


def _escape_of(context: _PushContext, path: Path) -> FileResult | None:
	"""An error row for a file that resolves outside the source folder, such as a symbolic link to elsewhere on disk, or None for one that stays inside.

	The read-side twin of `safe_child`, which keeps the pull side's writes inside their folder the same way.
	"""
	if path.resolve().is_relative_to(context.source):
		return None

	return FileResult(path.name, "error", local_path=str(path), detail=f"resolves to somewhere outside the source folder {context.source}; only files inside it are uploaded")


def _push_one(context: _PushContext, path: Path) -> FileResult:
	name = path.name
	try:
		content = path.read_text(encoding="utf-8")
	except UnicodeDecodeError:
		return FileResult(name, "error", local_path=str(path), detail="not UTF-8 text; only text documents can be uploaded")
	except OSError as exception:
		return FileResult(name, "error", local_path=str(path), detail=str(exception))

	try:
		copies = context.remote.get(name, [])
		if not copies:
			return _push_new(context, path, name, content)

		return _push_existing(context, path, name, content, copies)
	except KnowledgeFullError as exception:
		return FileResult(name, "refused_full", local_path=str(path), detail=str(exception), warning=str(exception))
	except (ClaudeProjectsError, OSError) as exception:
		return FileResult(name, "error", local_path=str(path), detail=str(exception))


def _push_new(context: _PushContext, path: Path, name: str, content: str) -> FileResult:
	if context.preview is not None:
		return context.preview.result(name, path, "created", content, [])

	result = context.client.save_document(context.project_id, name, content, replacing=[], allow_search_mode=context.options.allow_search_mode)
	if result.rollback_failed:
		detail = f"write took the project past capacity and could not be undone: deleting new document {result.uuid} failed."
		return FileResult(name, "written_over_capacity", local_path=str(path), detail=detail, warning=detail)

	return FileResult(name, "created", local_path=str(path))


def _push_existing(context: _PushContext, path: Path, name: str, content: str, copies: list[Document]) -> FileResult:
	"""Compare against the newest copy; a replacement takes every copy with it, which is what the preview has to count."""
	existing = copies[0]
	if existing.is_stub:
		existing = context.client.get_document(context.project_id, existing.uuid)

	if existing.content == content:
		return FileResult(name, "unchanged", local_path=str(path))

	if not context.options.overwrite:
		return FileResult(
			name,
			"skipped_exists",
			local_path=str(path),
			detail="remote document differs; pass overwrite to replace it",
		)

	if context.preview is not None:
		return context.preview.result(name, path, "replaced", content, copies)

	result = context.client.replace_document(context.project_id, name, content, allow_search_mode=context.options.allow_search_mode, backup=context.options.backup)
	if result.rollback_failed:
		detail = f"write took the project past capacity and could not be undone: deleting new document {result.uuid} failed. The previous document {existing.uuid} was left in place."
		return FileResult(name, "written_over_capacity", local_path=str(path), detail=detail, backup_path=result.backup_path, warning=detail)

	detail = None
	if result.failed_delete_uuids:
		detail = f"saved, but {len(result.failed_delete_uuids)} old copy could not be removed and remains as a duplicate"

	return FileResult(name, "replaced", local_path=str(path), detail=detail, backup_path=result.backup_path)
