"""The MCP surface: seventeen tools over claude.ai / Claude Cowork projects, their documents, and their scheduled tasks.

`build_server` is the injection seam.
It knows nothing about how it will be served, so adding a Streamable HTTP entrypoint later is a new `main`, not a refactor.

The scheduled-task tools live in `scheduled.py` and are registered at the end of `_assemble`; they share none of this module's machinery, since nothing about them touches backups or file names.
"""

from importlib import metadata
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from .backup import BackupStore
from .client import ClaudeProjectsClient, ReplaceResult, looks_like_uuid
from .config import Settings
from .errors import BackupError, ClaudeProjectsError, NotFoundError
from .models import Document, KnowledgeStats, Project, UploadedFile
from .results import with_warning
from .scheduled import register as register_scheduled_tools
from .sync import FileResult, PushOptions, pull, push, summarise
from .transport import CurlCffiTransport

_INSTRUCTIONS = """Read and write Claude Cowork / claude.ai projects and their knowledge documents.

These are shared team documents with no server-side undo, so writes are deliberately
cautious: replacing an existing document requires overwrite=true, and the previous
content is backed up locally first. Deleting a whole project takes every document in it,
so delete_project needs the project's name typed back and backs every document and upload up first.

For more than a couple of edits, prefer pull_documents to a folder, edit the files with normal
tools, then push_documents back — it is far cheaper than moving whole documents through tool
calls one at a time.

Files uploaded through the web UI, such as PDFs, count toward the project's knowledge size
and are a different kind of thing from a document: list_documents shows them under
uploaded_files, pull_documents copies them down as bytes, delete_project backs them up, and
push_documents sends a PDF or an image in the folder up as one, the way the web UI adds a file.
Nothing here can compact one. A deletion stops with nothing deleted if any upload
cannot be backed up, and the error names the file and why. An image offers no original to
download, so it can be neither pulled nor replaced; a pull-and-push copy carries everything
but images.

A project's knowledge has two lines, both reported by list_documents: a search threshold, past
which Claude in the web UI retrieves from the knowledge instead of reading all of it, and a
maximum, past which the web UI refuses uploads.
The API enforces neither, so write_document and push_documents do: a write that would grow
the project past a line is undone and refused, naming the documents most worth compacting
and the largest uploads, which only the web UI can remove.
allow_search_mode=true accepts the threshold; nothing accepts the maximum.

Scheduled tasks run prompts against a project on a cron schedule. Those schedules are in UTC,
not local time, and pausing a task with enabled=false is nearly always better than deleting it.
Nothing here can start a run: setting the schedule is the whole job.

A result that carries a `warning` key is saying something the user needs to hear — a duplicate
name, a leftover copy, a file name with no extension. The key is only present when there is
something to say; relay it to the user verbatim rather than summarising past it."""


def _version() -> str:
	try:
		return metadata.version("claude-projects-mcp-server")
	except metadata.PackageNotFoundError:
		# Running from a source tree that was never installed.
		return "0.0.0+unknown"


def build_server(settings: Settings, client: ClaudeProjectsClient | None = None) -> MCPServer:
	"""Assemble the server. Pass `client` to substitute a fake in tests."""
	if client is None:
		transport = CurlCffiTransport(
			settings.session_key,
			base_url=settings.base_url,
			impersonate=settings.impersonate,
		)
		client = ClaudeProjectsClient(transport)

	return _assemble(settings, client)


def _assemble(settings: Settings, client: ClaudeProjectsClient) -> MCPServer:
	"""The server body, with the client already resolved to a real instance.

	Split from build_server so the tool closures capture a client that is never None, which both readers and type checkers can rely on.
	"""
	backups = BackupStore(settings.backup_directory)
	server = MCPServer(name="claude-projects", version=_version(), instructions=_INSTRUCTIONS)

	_register_list_projects(server, client)
	_register_get_project(server, client)
	_register_create_project(server, client)
	_register_update_project(server, client)
	_register_delete_project(server, client, backups)

	_register_list_documents(server, client)
	_register_read_document(server, client)
	_register_write_document(server, client, backups)
	_register_rename_document(server, client, backups)
	_register_delete_document(server, client, backups)

	_register_pull_documents(server, client)
	_register_push_documents(server, client, backups)

	register_scheduled_tools(server, client)

	return server


def _backup_for(backups: BackupStore, project_id: str):
	def save(file_name: str, content: str) -> str:
		return str(backups.save(project_id, file_name, content))

	return save


def _backup_bytes_for(backups: BackupStore, project_id: str):
	def save(file_name: str, data: bytes) -> str:
		return str(backups.save_bytes(project_id, file_name, data))

	return save


def _register_list_projects(server: MCPServer, client: ClaudeProjectsClient) -> None:
	@server.tool(
		annotations=ToolAnnotations(read_only_hint=True),
		description="List the claude.ai / Claude Cowork projects on this account, each tagged with the organization that owns it. Use this to find a project uuid.",
	)
	def list_projects(organization_id: str | None = None) -> dict:
		with translated():
			projects = client.list_projects(organization_id=organization_id)

		return {
			"projects": [
				{
					"uuid": project.uuid,
					"name": project.name,
					"description": project.description,
					"is_private": project.is_private,
					"updated_at": project.updated_at,
					"organization_uuid": project.organization_uuid,
				}
				for project in projects
			]
		}


def _register_get_project(server: MCPServer, client: ClaudeProjectsClient) -> None:
	@server.tool(
		annotations=ToolAnnotations(read_only_hint=True),
		description="Read one project: its name, description, and instructions. Only this tool returns the instructions — list_projects does not carry them.",
	)
	def get_project(project_id: str) -> dict:
		with translated():
			return _project_dict(client.get_project(project_id))


def _register_create_project(server: MCPServer, client: ClaudeProjectsClient) -> None:
	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Create a project. organization_id is needed only when the account belongs to several organizations, and the error will name them if so. A private project is visible to you alone; a normal one is visible to the whole organization.",
	)
	def create_project(
		name: str,
		description: str = "",
		instructions: str = "",
		is_private: bool = False,
		organization_id: str | None = None,
	) -> dict:
		with translated():
			return _project_dict(
				client.create_project(
					name,
					description=description,
					instructions=instructions,
					organization_id=organization_id,
					is_private=is_private,
				)
			)


def _register_update_project(server: MCPServer, client: ClaudeProjectsClient) -> None:
	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Change a project's name, description, or instructions. Only the fields you pass are touched; the rest keep their current values. Pass an empty string to clear one.",
	)
	def update_project(
		project_id: str,
		name: str | None = None,
		description: str | None = None,
		instructions: str | None = None,
	) -> dict:
		if name is None and description is None and instructions is None:
			raise ToolError("Nothing to update. Pass at least one of name, description, or instructions. Use get_project to see the current values.")

		with translated():
			return _project_dict(
				client.update_project(
					project_id,
					name=name,
					description=description,
					instructions=instructions,
				)
			)


def _register_delete_project(server: MCPServer, client: ClaudeProjectsClient, backups: BackupStore) -> None:
	@server.tool(
		annotations=ToolAnnotations(destructive_hint=True),
		description="Delete a project and everything in it. There is no server-side undo, so confirm_name must be set to the project's exact current name. Every text document and every file uploaded through the web UI is copied to the local backup directory first; if any of that fails, nothing is deleted. An upload with no downloadable original, such as an image, or one the listing gives no size for, blocks the deletion until it is removed in the web UI.",
	)
	def delete_project(project_id: str, confirm_name: str) -> dict:
		with translated():
			project = client.get_project(project_id)
			if confirm_name != project.name:
				raise ToolError(f"confirm_name does not match. To delete this project pass confirm_name={project.name!r} exactly. Nothing has been changed.")

			# Backing up first is the precondition, not a courtesy: once the project is gone its documents and uploads are unreachable, so a failure here must stop everything.
			# The uploads are checked before anything is written, because the store is append-only and a refusal after the documents were copied would leave a fresh set of orphans on every retry.
			_uploads_to_back_up(client, project_id)
			backup_paths = [str(path) for path in _backup_every_document(client, backups, project_id)]
			# Listed again once the documents are done, so an upload added meanwhile is backed up too rather than deleted off a stale list.
			uploads = _uploads_to_back_up(client, project_id)
			backup_paths.extend(str(path) for path in _backup_uploads(client, backups, project_id, uploads))
			client.delete_project(project_id)

		return {
			"deleted": {
				"uuid": project.uuid,
				"name": project.name,
				"organization_uuid": project.organization_uuid,
			},
			"backup_paths": backup_paths,
		}


def _find_duplicates(documents: list[Document]) -> list[str]:
	seen, duplicates = set(), []
	for document in documents:
		if document.file_name in seen and document.file_name not in duplicates:
			duplicates.append(document.file_name)

		seen.add(document.file_name)

	return duplicates


def _list_documents_warning(stats: KnowledgeStats | None) -> str | None:
	if stats is None:
		return "Capacity was not checked: claude.ai did not report the project's knowledge size."

	if stats.size > stats.max_size:
		return f"The project is past its maximum: {stats.size:,} of {stats.max_size:,} tokens. The web UI is refusing to add to the project knowledge until something is removed or compacted, and write_document will refuse any write that grows it."

	return None


def _shortfall_warning(stats: KnowledgeStats | None, documents: list[Document], uploads: list[UploadedFile] | None) -> str | None:
	"""The reported knowledge size against what the listed documents account for.

	Observed 2026-09-18: in eight projects the two matched exactly, and in the ninth the difference was the uploads this server could not yet see, so an unexplained shortfall is a real signal rather than rounding.
	Listed uploads explain a shortfall without attributing it, since the files listing reports no token counts.
	"""
	if stats is None:
		return None

	warning = _uncounted_warning(documents)
	if warning is not None:
		return warning

	return _unexplained_shortfall(stats, documents, uploads)


def _uncounted_warning(documents: list[Document]) -> str | None:
	uncounted = [document for document in documents if document.estimated_token_count is None]
	if not uncounted:
		return None

	return f"Whether the listed documents account for the knowledge size could not be checked: {len(uncounted)} of them carry no estimated_token_count, or one that is not a whole number."


def _unexplained_shortfall(stats: KnowledgeStats, documents: list[Document], uploads: list[UploadedFile] | None) -> str | None:
	accounted = sum(document.estimated_token_count or 0 for document in documents)
	shortfall = stats.size - accounted
	if shortfall <= 0 or uploads:
		return None

	if uploads is None:
		explanation = "the files listing that would explain the difference could not be checked"
	else:
		explanation = "the project's files listing shows no uploads to explain the difference"

	return f"The listed documents account for {accounted:,} of {stats.size:,} tokens; {shortfall:,} tokens are unaccounted for, and {explanation}. Something in the project knowledge is invisible to this server."


def _try_list_uploads(client: ClaudeProjectsClient, project_id: str) -> tuple[list[UploadedFile] | None, str | None]:
	"""The project's uploads, or None with a warning when the files listing could not be fetched.

	The documents are the point of a listing, so a failure here is reported beside them rather than allowed to fail the whole call.
	"""
	uploads, failure = client.try_list_uploaded_files(project_id)
	if uploads is not None:
		return uploads, None

	return None, f"Uploaded files could not be checked, so if the web UI shows PDFs or other uploads this server cannot see them: {failure}"


def _upload_dict(upload: UploadedFile) -> dict:
	return {
		"uuid": upload.uuid,
		"file_name": upload.file_name,
		"file_kind": upload.file_kind,
		"created_at": upload.created_at,
		"size_bytes": upload.size_bytes,
		"page_count": upload.page_count,
	}


def _register_list_documents(server: MCPServer, client: ClaudeProjectsClient) -> None:
	@server.tool(
		annotations=ToolAnnotations(read_only_hint=True),
		description="List the text documents in a project, and under `uploaded_files` the files uploaded through the web UI, such as PDFs, which count toward `knowledge` but are not documents: pull_documents copies them, push_documents sends a PDF or an image up as one, and delete_project backs them up, but nothing here can compact one. `knowledge` reports the project's size against its search threshold and its maximum. `duplicate_file_names` flags names held by more than one document, which happens when a save is interrupted; the next write_document with overwrite=true cleans them up. Relay any `warning` in the result to the user verbatim.",
	)
	def list_documents(project_id: str) -> dict:
		with translated():
			documents = client.list_documents(project_id)
			try:
				stats = client.knowledge_stats(project_id)
			except NotFoundError:
				stats = None

			uploads, uploads_warning = _try_list_uploads(client, project_id)

		body: dict = {
			"project_id": project_id,
			"documents": [
				{
					"uuid": document.uuid,
					"file_name": document.file_name,
					"created_at": document.created_at,
					"characters": document.characters,
					"estimated_token_count": document.estimated_token_count,
				}
				for document in documents
			],
			"duplicate_file_names": _find_duplicates(documents),
		}

		# Absent means unknown; an empty list would claim there are none.
		if uploads is not None:
			body["uploaded_files"] = [_upload_dict(upload) for upload in uploads]

		if stats is not None:
			body["knowledge"] = {
				"size": stats.size,
				"search_threshold": stats.search_threshold,
				"max_size": stats.max_size,
				"search_mode": stats.search_mode,
			}

		return with_warning(body, _joined(_list_documents_warning(stats), _shortfall_warning(stats, documents, uploads), uploads_warning))


def _register_read_document(server: MCPServer, client: ClaudeProjectsClient) -> None:
	@server.tool(
		annotations=ToolAnnotations(read_only_hint=True),
		description="Read one document, by file name or uuid. If several documents share the name, the newest is returned and `warning` names the others; relay any `warning` to the user verbatim.",
	)
	def read_document(project_id: str, document: str) -> dict:
		with translated():
			found = client.read_document(project_id, document)
			warning = None
			if not looks_like_uuid(document):
				warning = _duplicate_warning(client.find_documents_by_name(project_id, document), found)

		return with_warning(
			{
				"uuid": found.uuid,
				"file_name": found.file_name,
				"content": found.content,
				"created_at": found.created_at,
				"estimated_token_count": found.estimated_token_count,
			},
			warning,
		)


def _leftover_warning(result: ReplaceResult) -> str | None:
	if not result.failed_delete_uuids:
		return None

	return f"The new content is saved, but {len(result.failed_delete_uuids)} older copy could not be removed and remains as a duplicate: {', '.join(result.failed_delete_uuids)}. The next write with overwrite=true will clean it up."


def _write_capacity_warning(result: ReplaceResult, existing: list[Document]) -> str | None:
	if result.knowledge is None:
		return "Capacity was not checked: claude.ai did not report the project's knowledge size. The write went ahead; check list_documents for where the project stands."

	if result.rollback_failed:
		if result.knowledge.size > result.knowledge.max_size:
			line_name = "its maximum"
			line_limit = result.knowledge.max_size
		else:
			line_name = "its search threshold"
			line_limit = result.knowledge.search_threshold

		if existing:
			conflict_message = f"The previous {result.file_name!r} ({existing[0].uuid}) was left in place, so two documents now share the name. Remove one with delete_document, then compact."
		else:
			conflict_message = "Remove it with delete_document, then compact."

		return f"This write took the project past {line_name} ({result.knowledge.size:,} of {line_limit:,} tokens) and could not be undone: deleting the new document {result.uuid} failed. {conflict_message}"

	if result.entered_search_mode:
		return f"The project is in search mode: {result.knowledge.size:,} of {result.knowledge.search_threshold:,} tokens. Claude in the web UI now retrieves from the project knowledge instead of reading all of it, so a document can go unseen; compact something with write_document overwrite=true to leave it."

	return None


def _register_write_document(server: MCPServer, client: ClaudeProjectsClient, backups: BackupStore) -> None:
	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Create a document, or replace one with overwrite=true. The previous content is backed up locally before any replacement. Pass expected_uuid (from read_document) to refuse the write if a teammate has saved since you read it. Refused when the write would grow the project past its search threshold or its maximum, naming the documents most worth compacting and the largest uploaded files, which only the web UI can remove; allow_search_mode=true accepts the threshold, never the maximum. Relay any `warning` in the result to the user verbatim — it flags a leftover copy or a file name with no extension.",
	)
	def write_document(
		project_id: str,
		file_name: str,
		content: str,
		overwrite: bool = False,
		expected_uuid: str | None = None,
		allow_search_mode: bool = False,
	) -> dict:
		with translated():
			existing = client.find_documents_by_name(project_id, file_name)
			if existing and not overwrite:
				raise ToolError(f"{file_name!r} already exists in this project (uuid {existing[0].uuid}). Pass overwrite=true to replace it — the current content will be backed up first. Use read_document to see it before deciding.")

			result = client.replace_document(
				project_id,
				file_name,
				content,
				expected_uuid=expected_uuid,
				allow_search_mode=allow_search_mode,
				backup=_backup_for(backups, project_id),
			)

		body: dict = {
			"action": result.action,
			"uuid": result.uuid,
			"file_name": result.file_name,
			"replaced_uuids": result.replaced_uuids,
			"backup_path": result.backup_path,
		}

		if result.knowledge is not None:
			body["knowledge"] = {
				"size": result.knowledge.size,
				"search_threshold": result.knowledge.search_threshold,
				"max_size": result.knowledge.max_size,
				"search_mode": result.knowledge.search_mode,
			}

		return with_warning(
			body,
			_joined(_leftover_warning(result), _extension_warning(result.file_name), _write_capacity_warning(result, existing)),
		)


def _register_rename_document(server: MCPServer, client: ClaudeProjectsClient, backups: BackupStore) -> None:
	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Rename a document, by uuid or by an unambiguous file name. The content is re-created under the new name before the original is deleted, with local backups first, so nothing is lost midway. A new name already in use is refused unless overwrite=true, which replaces its holder (backed up first). Relay any `warning` in the result to the user verbatim.",
	)
	def rename_document(project_id: str, document: str, new_file_name: str, overwrite: bool = False) -> dict:
		with translated():
			result = client.rename_document(
				project_id,
				document,
				new_file_name,
				overwrite=overwrite,
				backup=_backup_for(backups, project_id),
			)

		leftover = None
		if result.failed_delete_uuids:
			leftover = f"The document now exists as {result.new_file_name!r}, but {len(result.failed_delete_uuids)} old copy could not be removed and remains: {', '.join(result.failed_delete_uuids)}. Remove it with delete_document."

		return with_warning(
			{
				"uuid": result.uuid,
				"old_uuid": result.old_uuid,
				"old_file_name": result.old_file_name,
				"new_file_name": result.new_file_name,
				"replaced_uuids": result.replaced_uuids,
				"backup_paths": result.backup_paths,
			},
			_joined(leftover, _extension_warning(result.new_file_name)),
		)


def _register_delete_document(server: MCPServer, client: ClaudeProjectsClient, backups: BackupStore) -> None:
	@server.tool(
		annotations=ToolAnnotations(destructive_hint=True),
		description="Delete a document, by uuid or by an unambiguous file name. The content is backed up locally first. A name shared by several documents is refused: pass the uuid to say which one.",
	)
	def delete_document(project_id: str, document: str) -> dict:
		with translated():
			target = client.one_document(project_id, document)
			if target.is_stub:
				target = client.get_document(project_id, target.uuid)

			backup_path = backups.save(project_id, target.file_name, target.content or "")
			client.delete_document(project_id, target.uuid)

		return {
			"deleted": [{"uuid": target.uuid, "file_name": target.file_name}],
			"backup_path": str(backup_path),
		}


def _register_pull_documents(server: MCPServer, client: ClaudeProjectsClient) -> None:
	@server.tool(
		annotations=ToolAnnotations(read_only_hint=False),
		description="Copy the project's documents and uploaded files into a local folder. Uploads such as PDFs come down as bytes, and each result row says which `kind` it was. Local files that differ are kept, not overwritten, unless overwrite_local=true.",
	)
	def pull_documents(project_id: str, destination_directory: str, overwrite_local: bool = False) -> dict:
		try:
			with translated():
				results = pull(client, project_id, Path(destination_directory), overwrite_local=overwrite_local)
		except OSError as exception:
			raise ToolError(f"Could not use {destination_directory!r} as the destination folder: {exception}") from exception

		return {
			"project_id": project_id,
			"results": [_result_dict(result) for result in results],
			"summary": summarise(results),
		}


def _push_warning(results: list[FileResult]) -> str | None:
	"""What the model must relay about a push: where it stopped, or that a dry run could not say where it would."""
	for result in results:
		if result.warning is not None:
			return result.warning

	return None


def _register_push_documents(server: MCPServer, client: ClaudeProjectsClient, backups: BackupStore) -> None:
	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Send a local folder's files into the project: a PDF or an image goes up as an uploaded file, the way the web UI adds one, and every other file becomes a text document and must be UTF-8. The default pattern `*.md` matches no upload, so pass pattern='*' to send everything in the folder. Unchanged files are skipped, differing ones need overwrite=true (the replaced version is backed up locally first), and nothing remote that is missing locally is ever deleted. An image already in the project is left alone, since it offers no original to compare against or back up. Use dry_run=true to preview, including where the push would stop; that stop is an estimate, since a preview writes nothing to measure, and it cannot count uploads at all. Stops at the first file that would grow the project past its search threshold or its maximum (allow_search_mode=true accepts the threshold); files already pushed stay. Relay any `warning` in the result to the user verbatim.",
	)
	def push_documents(
		project_id: str,
		source_directory: str,
		pattern: str = "*.md",
		overwrite: bool = False,
		dry_run: bool = False,
		allow_search_mode: bool = False,
	) -> dict:
		try:
			with translated():
				options = PushOptions(
					overwrite=overwrite,
					dry_run=dry_run,
					allow_search_mode=allow_search_mode,
					backup=_backup_for(backups, project_id),
					backup_bytes=_backup_bytes_for(backups, project_id),
				)

				results = push(
					client,
					project_id,
					Path(source_directory),
					pattern=pattern,
					options=options,
				)
		except OSError as exception:
			raise ToolError(f"Could not use {source_directory!r} as the source folder: {exception}") from exception

		return with_warning(
			{
				"project_id": project_id,
				"dry_run": dry_run,
				"results": [_result_dict(result) for result in results],
				"summary": summarise(results),
			},
			_push_warning(results),
		)


class translated:
	"""Turn a typed ClaudeProjectsError into the ToolError the model will actually read.

	The errors already carry recovery instructions, so this only needs to change the type, not invent a message.
	"""

	def __enter__(self):
		return self

	def __exit__(self, exception_type, exception, traceback):
		if exception is None or not isinstance(exception, ClaudeProjectsError):
			return False

		raise ToolError(str(exception)) from exception


def _project_dict(project: Project) -> dict:
	return {
		"uuid": project.uuid,
		"name": project.name,
		"description": project.description,
		"instructions": project.instructions,
		"is_private": project.is_private,
		"created_at": project.created_at,
		"updated_at": project.updated_at,
		"organization_uuid": project.organization_uuid,
	}


def _backup_every_document(client: ClaudeProjectsClient, backups: BackupStore, project_id: str) -> list[Path]:
	"""Copy every document in the project to the backup directory, raising on the first failure.

	Deleting a project is the one operation that cannot be undone document by document, so any failure here must stop the deletion: proceeding on a partial backup would leave it reading like a complete one later.
	Documents already saved stay saved — the store is append-only — but nothing gets deleted.
	"""
	documents = client.list_documents(project_id)
	saved = []
	for document in documents:
		if document.is_stub:
			document = client.get_document(project_id, document.uuid)

		saved.append(backups.save(project_id, document.file_name, document.content or ""))

	return saved


def _uploads_to_back_up(client: ClaudeProjectsClient, project_id: str) -> list[UploadedFile]:
	"""The project's uploads, once the listing shows every one of them can be backed up; raises BackupError otherwise so the deletion stops before anything is written.

	The listing itself failing raises too: a 404 there is unexplained rather than "no uploads", since the endpoint answers an empty array for a project without any.
	"""
	try:
		uploads = client.list_uploaded_files(project_id)
	except ClaudeProjectsError as exception:
		# Left bare, a 404 here would read as "the project does not exist" rather than as the listing that failed.
		raise BackupError(f"Could not list the project's uploaded files to back them up, so nothing was deleted: {exception}") from exception

	problems = [problem for upload in uploads if (problem := _unbackable_because(upload)) is not None]
	if problems:
		# Every offender at once, since one attempt per file would cost a full round trip each and the listing already shows them all.
		raise BackupError(f"These uploaded files cannot be backed up, so nothing was deleted: {' '.join(problems)} Remove them in the web UI first, or delete the project there.")

	return uploads


def _unbackable_because(upload: UploadedFile) -> str | None:
	"""Why an upload cannot be backed up, or None when it can."""
	if upload.download_url is None:
		return f"{upload.file_name!r} ({upload.uuid}) has no downloadable original; only a document such as a PDF offers one."

	# The byte count is the only check on what comes back, and an unverifiable backup is no backup on the one path that cannot be undone.
	# Zero verifies nothing either, since no file has zero bytes.
	if not upload.size_bytes:
		return f"{upload.file_name!r} ({upload.uuid}) has no size in the files listing, or a size of zero, so a backup of it could not be verified."

	return None


def _backup_uploads(client: ClaudeProjectsClient, backups: BackupStore, project_id: str, uploads: list[UploadedFile]) -> list[Path]:
	"""Fetch every upload's bytes into the backup directory, raising on the first failure so the deletion stops."""
	saved = []
	for upload in uploads:
		try:
			data = client.download_uploaded_file(upload)
		except ClaudeProjectsError as exception:
			raise BackupError(f"Could not fetch the uploaded file {upload.file_name!r} ({upload.uuid}) to back it up, so nothing was deleted: {exception} Remove that file in the web UI first, or delete the project there.") from exception

		saved.append(backups.save_bytes(project_id, upload.file_name, data))

	return saved


def _duplicate_warning(matches: list[Document], returned: Document) -> str | None:
	others = [match.uuid for match in matches if match.uuid != returned.uuid]
	if not others:
		return None

	return f"{len(matches)} documents share this name. Returned the newest ({returned.uuid}); the others are {', '.join(others)}. This usually means an interrupted save — a write with overwrite=true will clean it up, but check the others first in case a teammate edited one."


def _extension_warning(file_name: str) -> str | None:
	"""The web UI picks its renderer by file extension, and nothing in a tool call hints that a name needs one.

	Spelled out rather than taken from pathlib, whose `suffix` reports a bare trailing period as an extension on Python 3.14; here `notes.` is as extension-less as `notes`.
	"""
	stem, _, extension = file_name.rpartition(".")
	if stem and extension:
		return None

	suggestion = f"{file_name.rstrip('.')}.md"
	return f"{file_name!r} has no file extension, so the claude.ai UI will show it as plain text rather than rendered markdown. rename_document can give it one, such as {suggestion!r}."


def _joined(*warnings: str | None) -> str | None:
	"""One warning out of whichever apply, or None when none do."""
	present = [warning for warning in warnings if warning]
	if not present:
		return None

	return " ".join(present)


def _result_dict(result: FileResult) -> dict:
	return {
		"file_name": result.file_name,
		"status": result.status,
		"local_path": result.local_path,
		"detail": result.detail,
		"backup_path": result.backup_path,
		"kind": result.kind,
	}
