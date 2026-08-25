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
from .client import ClaudeProjectsClient, looks_like_uuid
from .config import Settings
from .errors import ClaudeProjectsError
from .models import Document, Project
from .results import with_warning
from .scheduled import register as register_scheduled_tools
from .sync import pull, push, summarise
from .transport import CurlCffiTransport

_INSTRUCTIONS = """Read and write Claude Cowork / claude.ai projects and their knowledge documents.

These are shared team documents with no server-side undo, so writes are deliberately
cautious: replacing an existing document requires overwrite=true, and the previous
content is backed up locally first. Deleting a whole project takes every document in it,
so delete_project needs the project's name typed back and backs everything up first.

For more than a couple of edits, prefer pull_documents to a folder, edit the files with normal
tools, then push_documents back — it is far cheaper than moving whole documents through tool
calls one at a time.

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

	def backup_for(project_id: str):
		def save(file_name: str, content: str) -> str:
			return str(backups.save(project_id, file_name, content))

		return save

	# ---------------------------------------------------------------- reading

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

	@server.tool(
		annotations=ToolAnnotations(read_only_hint=True),
		description="Read one project: its name, description, and instructions. Only this tool returns the instructions — list_projects does not carry them.",
	)
	def get_project(project_id: str) -> dict:
		with translated():
			return _project_dict(client.get_project(project_id))

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

	@server.tool(
		annotations=ToolAnnotations(destructive_hint=True),
		description="Delete a project and every document in it. There is no server-side undo, so confirm_name must be set to the project's exact current name. Every document is copied to the local backup directory first; if that fails, nothing is deleted.",
	)
	def delete_project(project_id: str, confirm_name: str) -> dict:
		with translated():
			project = client.get_project(project_id)
			if confirm_name != project.name:
				raise ToolError(f"confirm_name does not match. To delete this project pass confirm_name={project.name!r} exactly. Nothing has been changed.")

			# Backing up first is the precondition, not a courtesy: once the project is gone its documents are unreachable, so a failure here must stop everything.
			backup_paths = [str(path) for path in _backup_every_document(client, backups, project_id)]
			client.delete_project(project_id)

		return {
			"deleted": {
				"uuid": project.uuid,
				"name": project.name,
				"organization_uuid": project.organization_uuid,
			},
			"backup_paths": backup_paths,
		}

	@server.tool(
		annotations=ToolAnnotations(read_only_hint=True),
		description="List the documents in a project. `duplicate_file_names` flags names held by more than one document, which happens when a save is interrupted; the next write_document with overwrite=true cleans them up.",
	)
	def list_documents(project_id: str) -> dict:
		with translated():
			documents = client.list_documents(project_id)

		seen, duplicates = set(), []
		for document in documents:
			if document.file_name in seen and document.file_name not in duplicates:
				duplicates.append(document.file_name)

			seen.add(document.file_name)

		return {
			"project_id": project_id,
			"documents": [
				{
					"uuid": document.uuid,
					"file_name": document.file_name,
					"created_at": document.created_at,
					"characters": document.characters,
				}
				for document in documents
			],
			"duplicate_file_names": duplicates,
		}

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
			},
			warning,
		)

	# ---------------------------------------------------------------- writing

	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Create a document, or replace one with overwrite=true. The previous content is backed up locally before any replacement. Pass expected_uuid (from read_document) to refuse the write if a teammate has saved since you read it. Relay any `warning` in the result to the user verbatim — it flags a leftover copy or a file name with no extension.",
	)
	def write_document(
		project_id: str,
		file_name: str,
		content: str,
		overwrite: bool = False,
		expected_uuid: str | None = None,
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
				backup=backup_for(project_id),
			)

		leftover = None
		if result.failed_delete_uuids:
			leftover = f"The new content is saved, but {len(result.failed_delete_uuids)} older copy could not be removed and remains as a duplicate: {', '.join(result.failed_delete_uuids)}. The next write with overwrite=true will clean it up."

		return with_warning(
			{
				"action": result.action,
				"uuid": result.uuid,
				"file_name": result.file_name,
				"replaced_uuids": result.replaced_uuids,
				"backup_path": result.backup_path,
			},
			_joined(leftover, _extension_warning(result.file_name)),
		)

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
				backup=backup_for(project_id),
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

	# ------------------------------------------------------------------- sync

	@server.tool(
		annotations=ToolAnnotations(read_only_hint=False),
		description="Copy the project's documents into a local folder. Local files that differ are kept, not overwritten, unless overwrite_local=true.",
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

	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Upload a local folder's files into the project. Unchanged files are skipped, differing ones need overwrite=true, and remote documents missing locally are never deleted. Use dry_run=true to preview.",
	)
	def push_documents(
		project_id: str,
		source_directory: str,
		pattern: str = "*.md",
		overwrite: bool = False,
		dry_run: bool = False,
	) -> dict:
		try:
			with translated():
				results = push(
					client,
					project_id,
					Path(source_directory),
					pattern=pattern,
					overwrite=overwrite,
					dry_run=dry_run,
					backup=backup_for(project_id),
				)
		except FileNotFoundError as exception:
			raise ToolError(str(exception)) from exception

		return {
			"project_id": project_id,
			"dry_run": dry_run,
			"results": [_result_dict(result) for result in results],
			"summary": summarise(results),
		}

	# ------------------------------------------------------- scheduled tasks

	register_scheduled_tools(server, client)

	return server


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


def _result_dict(result) -> dict:
	return {
		"file_name": result.file_name,
		"status": result.status,
		"local_path": result.local_path,
		"detail": result.detail,
		"backup_path": result.backup_path,
	}
