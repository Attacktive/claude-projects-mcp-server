"""The claude.ai project API, expressed in terms of models rather than URLs.

This is the only module that knows the endpoint layout, which keeps the blast radius small if the undocumented API moves.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .capacity import candidates, judge, refusal
from .errors import AmbiguousDocError, ApiError, ClaudeProjectsError, ConcurrentEditError, ConfigError, DocExistsError, KnowledgeFullError, NotFoundError, RateLimitedError
from .identifiers import chat_project_id
from .models import Document, KnowledgeStats, Organization, Project, ScheduledTask
from .transport import HttpMethod, Transport

_UUID_LIKE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{4,}$")

_MAX_RATE_LIMIT_RETRIES = 3
_DEFAULT_RETRY_DELAY = 2.0

# An MCP call that blocks for minutes is worse than one that fails with a clear reason.
_MAX_RETRY_DELAY = 30.0

# The listing's own default is 30; a larger page means one request for any realistic account.
# The page cap is a runaway guard, not an expected limit.
_PROJECT_PAGE_SIZE = 100
_MAX_PROJECT_PAGES = 100


def looks_like_uuid(value: str) -> bool:
	return bool(_UUID_LIKE.match(value.strip()))


@dataclass(frozen=True, slots=True)
class _SaveContext:
	project_id: str
	file_name: str
	created: Document
	stats: KnowledgeStats
	backup_path: str | None = None


@dataclass
class ReplaceResult:
	"""What a save actually did, including the parts that did not go to plan."""

	uuid: str
	file_name: str
	replaced_uuids: list[str] = field(default_factory=list)
	failed_delete_uuids: list[str] = field(default_factory=list)
	backup_path: str | None = None
	knowledge: KnowledgeStats | None = None
	entered_search_mode: bool = False
	rollback_failed: bool = False

	@property
	def action(self) -> str:
		if self.replaced_uuids:
			return "replaced"

		return "created"


@dataclass
class RenameResult:
	"""What a rename actually did, including the parts that did not go to plan."""

	uuid: str
	old_uuid: str
	old_file_name: str
	new_file_name: str
	replaced_uuids: list[str] = field(default_factory=list)
	failed_delete_uuids: list[str] = field(default_factory=list)
	backup_paths: list[str] = field(default_factory=list)


@dataclass
class ProjectScheduledTasks:
	"""One project's scheduled tasks, alongside everything else in its organization.

	Both halves are kept because the filter is computed rather than served: the API lists tasks per organization and names their project only through an encoded id.
	Holding the unfiltered listing too is what lets a caller tell "this project has nothing scheduled" apart from "the encoding stopped matching", which would otherwise look identical.
	"""

	project_id: str
	matched: list[ScheduledTask] = field(default_factory=list)
	in_organization: list[ScheduledTask] = field(default_factory=list)

	@property
	def mapping_looks_broken(self) -> bool:
		"""True when tasks exist nearby but none claim this project.

		Not proof of a break — every task in the organization may genuinely belong elsewhere — which is why this only ever drives a warning, never an error.
		"""
		return not self.matched and bool(self.in_organization)


class ClaudeProjectsClient:
	def __init__(
		self,
		transport: Transport,
		sleep: Callable[[float], None] = time.sleep,
	):
		self._transport = transport
		self._sleep = sleep
		self._orgs: list[Organization] | None = None
		self._project_organizations: dict[str, str] = {}
		self._task_organizations: dict[str, str] = {}

	# --------------------------------------------------------------- plumbing

	def _request(self, method: HttpMethod, path: str, json_body: dict | None = None):
		"""Retry only rate limits. Everything else is a real answer, not a hiccup."""
		attempt = 0
		while True:
			try:
				return self._transport.request(method, path, json_body=json_body)
			except RateLimitedError as exception:
				attempt += 1
				if attempt > _MAX_RATE_LIMIT_RETRIES:
					raise

				delay = exception.retry_after
				if delay is None:
					delay = _DEFAULT_RETRY_DELAY * attempt

				self._sleep(min(delay, _MAX_RETRY_DELAY))

	# ------------------------------------------------------------------- organizations

	def list_organizations(self) -> list[Organization]:
		"""Chat-capable organizations only; the others hold no projects to reach."""
		organizations = self._orgs
		if organizations is None:
			raw = self._request("GET", "/organizations")
			organizations = [organization for organization in Organization.parse_list(raw) if organization.is_chat_capable]
			self._orgs = organizations

		return organizations

	def resolve_organization_for_project(self, project_id: str) -> str:
		"""Which organization owns this project.

		Needed because an account can belong to several organizations and a project uuid alone does not say which one it lives in.
		Every organization is searched, and every owner found along the way is cached, so this costs at most one listing per organization per session.
		"""
		if project_id in self._project_organizations:
			return self._project_organizations[project_id]

		for organization in self.list_organizations():
			self._projects_in(organization.uuid)
			if project_id in self._project_organizations:
				return self._project_organizations[project_id]

		raise NotFoundError(f"No project {project_id!r} in any organization on this account. Check the uuid against the one in the claude.ai URL, and that the account can still reach the project.")

	def _organization_for_new_project(self, organization_id: str | None) -> str:
		"""Where a project with no home yet should be created.

		Unlike resolving an existing project, there is nothing to search for, so an ambiguous account has to be asked rather than guessed at: creating a team project in the wrong organization is not something the caller would notice quickly.
		"""
		if organization_id:
			return organization_id

		organizations = self.list_organizations()
		if not organizations:
			raise ConfigError("This account has no chat-capable organization, so there is nowhere to create a project.")

		if len(organizations) == 1:
			return organizations[0].uuid

		listed = ", ".join(f"{organization.uuid} ({organization.name})" for organization in organizations)
		raise ConfigError(f"This account belongs to {len(organizations)} organizations, so it is not clear where the project should go: {listed}. Pass organization_id to say which.")

	# --------------------------------------------------------------- projects

	def list_projects(self, organization_id: str | None = None) -> list[Project]:
		if organization_id:
			organization_ids = [organization_id]
		else:
			organization_ids = [organization.uuid for organization in self.list_organizations()]

		projects = []
		for uuid in organization_ids:
			projects.extend(self._projects_in(uuid))

		return projects

	def _projects_in(self, organization_id: str) -> list[Project]:
		"""Every project in an organization, walking the paginated listing to the end.

		Neither `filter` nor `is_archived` is sent, though the web UI sends both.
		They narrow the listing — `is_archived=true` returns *only* archived projects rather than a superset — and anything missing from the listing is a project this client cannot resolve an organization for.
		Asking for all of them is the only answer that cannot make a project unreachable.
		"""
		projects: list[Project] = []
		offset = 0
		pages = 0
		while True:
			raw = self._request("GET", f"/organizations/{organization_id}/projects_v2?limit={_PROJECT_PAGE_SIZE}&offset={offset}")
			page, has_more = Project.parse_page(raw, organization_uuid=organization_id)
			projects.extend(page)
			for project in page:
				self._project_organizations[project.uuid] = organization_id

			if not has_more or not page:
				return projects

			offset += len(page)
			pages += 1
			if pages >= _MAX_PROJECT_PAGES:
				# Better to say so than to quietly return a prefix, which would read as "this organization has no such project" further up.
				raise ApiError(f"The projects listing for organization {organization_id} still reported more after {_MAX_PROJECT_PAGES} pages ({len(projects)} projects). Refusing to keep paging.", status=0)

	def get_project(self, project_id: str) -> Project:
		"""One project, with its instructions — which a listing does not carry."""
		organization_id = self.resolve_organization_for_project(project_id)
		raw = self._request("GET", f"/organizations/{organization_id}/projects/{project_id}")
		return Project.parse(raw, organization_uuid=organization_id)

	def create_project(
		self,
		name: str,
		description: str = "",
		instructions: str = "",
		organization_id: str | None = None,
		is_private: bool = False,
	) -> Project:
		"""Create a project.
		`description` is sent even when empty, as the API demands it.

		`instructions` cost a second call after the create, so a failure there leaves the new project behind without them; the error names its uuid, and the recovery is update_project, not a second create.
		"""
		organization_id = self._organization_for_new_project(organization_id)
		raw = self._request(
			"POST",
			f"/organizations/{organization_id}/projects",
			{"name": name, "description": description, "is_private": is_private},
		)
		project = Project.parse(raw, organization_uuid=organization_id)
		self._project_organizations[project.uuid] = organization_id

		if instructions:
			# The create endpoint has no field for instructions, so they cost a second call.
			# Doing it here keeps "create a project with instructions" one step for the caller, which is how it reads in the web UI.
			try:
				project = self.update_project(project.uuid, instructions=instructions)
			except ClaudeProjectsError as exception:
				raise ApiError(f"The project was created (uuid {project.uuid}) but setting its instructions failed: {exception} Set them with update_project rather than calling create_project again, which would leave a duplicate.", status=0) from exception

		return project

	def update_project(
		self,
		project_id: str,
		name: str | None = None,
		description: str | None = None,
		instructions: str | None = None,
	) -> Project:
		"""Change project metadata.
		Only the arguments given are sent.

		None means "leave it alone" and an empty string means "make it empty", so a field can be cleared deliberately without every other field being dragged along.
		"""
		payload: dict[str, str] = {}
		if name is not None:
			payload["name"] = name

		if description is not None:
			payload["description"] = description

		if instructions is not None:
			payload["prompt_template"] = instructions

		if not payload:
			raise ValueError("Nothing to update: pass at least one of name, description, or instructions.")

		organization_id = self.resolve_organization_for_project(project_id)
		raw = self._request("PUT", f"/organizations/{organization_id}/projects/{project_id}", payload)
		return Project.parse(raw, organization_uuid=organization_id)

	def delete_project(self, project_id: str) -> bool:
		"""True if this call removed it, False if it was already gone.

		This destroys every document in the project and there is no server-side undo.
		The caller is expected to have backed the documents up first; see the `delete_project` tool, which will not run without the project's name typed back.
		"""
		try:
			organization_id = self.resolve_organization_for_project(project_id)
		except NotFoundError:
			return False

		try:
			self._request("DELETE", f"/organizations/{organization_id}/projects/{project_id}")
		except NotFoundError:
			return False
		finally:
			# The uuid is meaningless now, and a stale mapping would answer for whatever takes its place.
			self._project_organizations.pop(project_id, None)

		return True

	# -------------------------------------------------------------- documents

	def _documents_path(self, project_id: str) -> str:
		organization_id = self.resolve_organization_for_project(project_id)
		return f"/organizations/{organization_id}/projects/{project_id}/docs"

	def knowledge_stats(self, project_id: str) -> KnowledgeStats:
		organization_id = self.resolve_organization_for_project(project_id)
		raw = self._request("GET", f"/organizations/{organization_id}/projects/{project_id}/kb/stats")
		return KnowledgeStats.parse(raw)

	def list_documents(self, project_id: str) -> list[Document]:
		"""Newest first. Entries may be stubs, depending on what the API includes."""
		raw = self._request("GET", self._documents_path(project_id))
		return _newest_first(Document.parse_list(raw))

	def get_document(self, project_id: str, document_uuid: str) -> Document:
		raw = self._request("GET", f"{self._documents_path(project_id)}/{document_uuid}")
		return Document.parse(raw)

	def find_documents_by_name(self, project_id: str, file_name: str) -> list[Document]:
		"""Every document with this name, newest first.

		Usually one, but the API tolerates duplicates and a crash midway through a save can leave them, so callers must decide what to do about more than one.
		"""
		return [document for document in self.list_documents(project_id) if document.file_name == file_name]

	def read_document(self, project_id: str, document: str) -> Document:
		"""Fetch by uuid or file name, always with content."""
		if looks_like_uuid(document):
			return self.get_document(project_id, document)

		matches = self.find_documents_by_name(project_id, document)
		if not matches:
			raise NotFoundError(f"No document named {document!r} in project {project_id}.")

		newest = matches[0]
		if newest.is_stub:
			return self.get_document(project_id, newest.uuid)

		return newest

	def one_document(self, project_id: str, document: str) -> Document:
		"""Resolve a uuid or a file name to exactly one document, for the operations that must not guess.

		A file name shared by several documents is refused rather than resolved to the newest, unlike read_document: reading the wrong duplicate is recoverable, deleting or renaming it is not.
		"""
		if looks_like_uuid(document):
			return self.get_document(project_id, document)

		matches = self.find_documents_by_name(project_id, document)
		if not matches:
			raise NotFoundError(f"No document named {document!r} in this project. Use list_documents to see what is there.")

		if len(matches) > 1:
			uuids = [match.uuid for match in matches]
			raise AmbiguousDocError(
				f"{document!r} names {len(matches)} documents in this project ({', '.join(uuids)}). Pass the uuid of the one you mean.",
				file_name=document,
				uuids=uuids,
			)

		return matches[0]

	def create_document(self, project_id: str, file_name: str, content: str) -> Document:
		raw = self._request(
			"POST",
			self._documents_path(project_id),
			{"file_name": file_name, "content": content},
		)
		return Document.parse(raw)

	def delete_document(self, project_id: str, document_uuid: str) -> bool:
		"""True if this call removed it, False if it was already gone.

		A teammate deleting it first reached the same end state, so that is not a failure.
		"""
		try:
			self._request("DELETE", f"{self._documents_path(project_id)}/{document_uuid}")
		except NotFoundError:
			return False

		return True

	# -------------------------------------------------------- scheduled tasks

	def _scheduled_tasks_path(self, organization_id: str) -> str:
		return f"/organizations/{organization_id}/cowork/scheduled_tasks"

	def _tasks_in(self, organization_id: str) -> list[ScheduledTask]:
		"""Every scheduled task in an organization, remembering which organization each came from.

		The listing ignores query parameters — three different spellings of a project filter returned identical bodies (2026-08-08) — so narrowing happens here rather than upstream.
		"""
		raw = self._request("GET", self._scheduled_tasks_path(organization_id))
		tasks = ScheduledTask.parse_list(raw)
		for task in tasks:
			self._task_organizations[task.id] = organization_id

		return tasks

	def resolve_organization_for_task(self, task_id: str) -> str:
		"""Which organization holds this task, searching and caching exactly as projects do."""
		if task_id in self._task_organizations:
			return self._task_organizations[task_id]

		for organization in self.list_organizations():
			self._tasks_in(organization.uuid)
			if task_id in self._task_organizations:
				return self._task_organizations[task_id]

		raise NotFoundError(f"No scheduled task {task_id!r} in any organization on this account. Use list_scheduled_tasks to see what is there.")

	def list_scheduled_tasks(self, organization_id: str | None = None) -> list[ScheduledTask]:
		if organization_id:
			return self._tasks_in(organization_id)

		tasks: list[ScheduledTask] = []
		for organization in self.list_organizations():
			tasks.extend(self._tasks_in(organization.uuid))

		return tasks

	def scheduled_tasks_for_project(self, project_id: str) -> ProjectScheduledTasks:
		"""The tasks belonging to one project, matched by the id the API reports them under."""
		organization_id = self.resolve_organization_for_project(project_id)
		in_organization = self._tasks_in(organization_id)
		wanted = chat_project_id(project_id)

		return ProjectScheduledTasks(
			project_id=project_id,
			matched=[task for task in in_organization if task.chat_project_id == wanted],
			in_organization=in_organization,
		)

	def get_scheduled_task(self, task_id: str) -> ScheduledTask:
		organization_id = self.resolve_organization_for_task(task_id)
		raw = self._request("GET", f"{self._scheduled_tasks_path(organization_id)}/{task_id}")

		return ScheduledTask.parse_trigger(raw)

	def create_scheduled_task(
		self,
		project_id: str,
		name: str,
		prompt: str,
		cron_expression: str | None = None,
		model: str | None = None,
	) -> ScheduledTask:
		"""Create a task against a project.

		Omitting `cron_expression` leaves the task manual-only, which is what the web UI calls a Manual frequency; the API expresses that by having no schedule at all rather than a special value.
		"""
		organization_id = self.resolve_organization_for_project(project_id)

		payload: dict[str, Any] = {"name": name, "prompt": prompt, "project_uuid": project_id}
		if cron_expression is not None:
			payload["cron_expression"] = cron_expression

		if model is not None:
			payload["model"] = model

		raw = self._request("POST", self._scheduled_tasks_path(organization_id), payload)
		created = ScheduledTask.parse_trigger(raw)
		self._task_organizations[created.id] = organization_id

		return created

	def update_scheduled_task(
		self,
		task_id: str,
		name: str | None = None,
		prompt: str | None = None,
		cron_expression: str | None = None,
		enabled: bool | None = None,
		model: str | None = None,
	) -> ScheduledTask:
		"""Change a task in place, sending only the arguments given.

		None means "leave it alone" throughout, which is why there is no way to clear a schedule here: `cron_expression=None` is indistinguishable from not passing it.
		Pausing is what `enabled=False` is for, and it is the better answer anyway — it keeps the prompt.
		"""
		changes: dict[str, Any] = {
			"name": name,
			"prompt": prompt,
			"cron_expression": cron_expression,
			"enabled": enabled,
			"model": model,
		}
		payload = {field: value for field, value in changes.items() if value is not None}

		if not payload:
			raise ValueError("Nothing to update: pass at least one of name, prompt, cron_expression, enabled, or model.")

		organization_id = self.resolve_organization_for_task(task_id)
		raw = self._request("PATCH", f"{self._scheduled_tasks_path(organization_id)}/{task_id}", payload)

		return ScheduledTask.parse_trigger(raw)

	def delete_scheduled_task(self, task_id: str) -> bool:
		"""True if this call removed it, False if it was already gone.

		A teammate deleting it first reached the same end state, exactly as with documents.
		"""
		try:
			organization_id = self.resolve_organization_for_task(task_id)
		except NotFoundError:
			return False

		try:
			self._request("DELETE", f"{self._scheduled_tasks_path(organization_id)}/{task_id}")
		except NotFoundError:
			return False
		finally:
			# The id is meaningless now, and a stale mapping would answer for whatever takes its place.
			self._task_organizations.pop(task_id, None)

		return True

	# ----------------------------------------------------------------- upsert

	def _hydrated(self, project_id: str, document: Document) -> Document:
		"""The document with its content present, fetched if the listing only carried a stub."""
		if document.is_stub:
			return self.get_document(project_id, document.uuid)

		return document

	def _delete_documents(self, project_id: str, documents: list[Document]) -> tuple[list[str], list[str]]:
		"""Delete each document, returning which uuids went and which would not.

		By the time this runs the replacement content is already live, so a failed delete is a leftover duplicate, not a lost write; it is reported rather than raised.
		"""
		deleted, failed = [], []
		for document in documents:
			try:
				self.delete_document(project_id, document.uuid)
				deleted.append(document.uuid)
			except Exception:
				failed.append(document.uuid)

		return deleted, failed

	def _try_knowledge_stats(self, project_id: str) -> KnowledgeStats | None:
		try:
			return self.knowledge_stats(project_id)
		except NotFoundError:
			return None

	def save_document(
		self,
		project_id: str,
		file_name: str,
		content: str,
		replacing: list[Document],
		allow_search_mode: bool = False,
		backup: Callable[[str, str], str] | None = None,
	) -> ReplaceResult:
		"""The single gated write primitive: backup replacing[0] if any, create, measure, keep or revert."""
		backup_path = self._backup_before_save(project_id, file_name, replacing, backup)
		created = self.create_document(project_id, file_name, content)
		stats = self._try_knowledge_stats(project_id)

		if stats is None or created.estimated_token_count is None:
			replaced, failed = self._delete_documents(project_id, replacing)
			return ReplaceResult(
				uuid=created.uuid,
				file_name=file_name,
				replaced_uuids=replaced,
				failed_delete_uuids=failed,
				backup_path=backup_path,
				knowledge=None,
			)

		added = created.estimated_token_count
		removed = sum(doc.estimated_token_count for doc in replacing if doc.estimated_token_count is not None)
		verdict = judge(stats, added, removed)

		context = _SaveContext(project_id, file_name, created, stats, backup_path)
		if verdict == "fits" or (verdict == "search_mode" and allow_search_mode):
			return self._admit_save(context, replacing, allow_search_mode, verdict)

		return self._rollback_or_raise(context, added, removed, verdict)

	def _admit_save(
		self,
		context: _SaveContext,
		replacing: list[Document],
		allow_search_mode: bool,
		verdict: str,
	) -> ReplaceResult:
		replaced, failed = self._delete_documents(context.project_id, replacing)
		actually_removed = sum(doc.estimated_token_count for doc in replacing if doc.uuid in replaced and doc.estimated_token_count is not None)
		final_size = context.stats.size - actually_removed
		projected_stats = KnowledgeStats(
			size=final_size,
			max_size=context.stats.max_size,
			search_threshold=context.stats.search_threshold,
			search_mode=final_size > context.stats.search_threshold,
		)
		entered_search_mode = (verdict == "search_mode") and allow_search_mode
		return ReplaceResult(
			uuid=context.created.uuid,
			file_name=context.file_name,
			replaced_uuids=replaced,
			failed_delete_uuids=failed,
			backup_path=context.backup_path,
			knowledge=projected_stats,
			entered_search_mode=entered_search_mode,
		)

	def _backup_before_save(self, project_id: str, file_name: str, replacing: list[Document], backup: Callable[[str, str], str] | None) -> str | None:
		if not replacing or backup is None:
			return None

		previous = self._hydrated(project_id, replacing[0])
		return backup(file_name, previous.content or "")

	def _rollback_or_raise(
		self,
		context: _SaveContext,
		added: int,
		removed: int,
		verdict: str,
	) -> ReplaceResult:
		try:
			self.delete_document(context.project_id, context.created.uuid)
		except Exception:
			projected_stats = KnowledgeStats(
				size=context.stats.size,
				max_size=context.stats.max_size,
				search_threshold=context.stats.search_threshold,
				search_mode=context.stats.size > context.stats.search_threshold,
			)
			return ReplaceResult(
				uuid=context.created.uuid,
				file_name=context.file_name,
				replaced_uuids=[],
				failed_delete_uuids=[],
				backup_path=context.backup_path,
				knowledge=projected_stats,
				rollback_failed=True,
			)

		existing_docs = self.list_documents(context.project_id)
		cands = candidates(existing_docs, excluding=context.file_name)
		projected = context.stats.size - removed
		limit_val = context.stats.max_size if verdict == "over_max" else context.stats.search_threshold
		msg = refusal(context.file_name, verdict, context.stats, projected, added, cands)
		raise KnowledgeFullError(msg, file_name=context.file_name, verdict=verdict, projected=projected, limit=limit_val)

	def replace_document(
		self,
		project_id: str,
		file_name: str,
		content: str,
		expected_uuid: str | None = None,
		allow_search_mode: bool = False,
		backup: Callable[[str, str], str] | None = None,
	) -> ReplaceResult:
		"""Save `content` under `file_name`, replacing any document already using it."""
		existing = self.find_documents_by_name(project_id, file_name)

		if expected_uuid is not None:
			actual = existing[0].uuid if existing else None
			if actual != expected_uuid:
				raise ConcurrentEditError(
					f"{file_name!r} changed since it was read (expected {expected_uuid}, found {actual or 'no document at all'}). Somebody else saved in the meantime; re-read it before writing.",
					file_name=file_name,
					expected_uuid=expected_uuid,
					actual_uuid=actual,
				)

		return self.save_document(
			project_id,
			file_name,
			content,
			replacing=existing,
			allow_search_mode=allow_search_mode,
			backup=backup,
		)

	def _rename_occupants(self, project_id: str, new_file_name: str, overwrite: bool) -> list[Document]:
		"""The documents already holding new_file_name, refused unless `overwrite` says to replace them."""
		occupants = self.find_documents_by_name(project_id, new_file_name)
		if occupants and not overwrite:
			uuids = ", ".join(occupant.uuid for occupant in occupants)
			raise DocExistsError(
				f"{new_file_name!r} already exists in this project ({uuids}). Pass overwrite=true to replace it — the current content will be backed up first. Use read_document to see it before deciding.",
				file_name=new_file_name,
				uuid=occupants[0].uuid,
			)

		return occupants

	def _rename_backups(self, project_id: str, source: Document, occupants: list[Document], backup: Callable[[str, str], str] | None) -> list[str]:
		"""Back up everything the rename will delete: the source, then the newest holder of the new name.

		Nothing has been mutated yet when this runs, so a backup that raises aborts the rename cleanly.
		"""
		if backup is None:
			return []

		paths = [backup(source.file_name, source.content or "")]
		if occupants:
			newest = self._hydrated(project_id, occupants[0])
			paths.append(backup(newest.file_name, newest.content or ""))

		return paths

	def rename_document(
		self,
		project_id: str,
		document: str,
		new_file_name: str,
		overwrite: bool = False,
		backup: Callable[[str, str], str] | None = None,
	) -> RenameResult:
		"""Move a document to a new file name, re-creating the content server-side.

		The API has no rename endpoint, so this creates the document under the new name and only then deletes the original — the same ordering argument as replace_document: nothing is deleted until the content exists remotely under the new name and everything doomed has been backed up locally.
		A crash between the create and the deletes leaves the document under both names; that is visible, recoverable residue rather than a lost write, and it will not appear in duplicate_file_names because the names differ.

		A new_file_name already in use is refused unless `overwrite` is set, in which case every document holding it is replaced, write-style: the newest is backed up first.
		`backup` receives (file_name, content) and returns where it was saved; if it raises, nothing is mutated.
		"""
		source = self.one_document(project_id, document)

		if source.file_name == new_file_name:
			raise ClaudeProjectsError(f"That document is already named {new_file_name!r}; nothing to rename.")

		occupants = self._rename_occupants(project_id, new_file_name, overwrite)
		source = self._hydrated(project_id, source)
		backup_paths = self._rename_backups(project_id, source, occupants, backup)
		created = self.create_document(project_id, new_file_name, source.content or "")

		deleted, failed = self._delete_documents(project_id, [source, *occupants])
		replaced = [uuid for uuid in deleted if uuid != source.uuid]

		return RenameResult(
			uuid=created.uuid,
			old_uuid=source.uuid,
			old_file_name=source.file_name,
			new_file_name=new_file_name,
			replaced_uuids=replaced,
			failed_delete_uuids=failed,
			backup_paths=backup_paths,
		)


def _newest_first(documents: list[Document]) -> list[Document]:
	return sorted(documents, key=lambda document: document.created_at or "", reverse=True)
