"""The claude.ai project API, expressed in terms of models rather than URLs.

This is the only module that knows the endpoint layout, which keeps the blast radius small if the undocumented API moves.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import ApiError, ClaudeProjectsError, ConcurrentEditError, ConfigError, NotFoundError, RateLimitedError
from .models import Document, Organization, Project
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


@dataclass
class ReplaceResult:
	"""What a save actually did, including the parts that did not go to plan."""

	uuid: str
	file_name: str
	replaced_uuids: list[str] = field(default_factory=list)
	failed_delete_uuids: list[str] = field(default_factory=list)
	backup_path: str | None = None

	@property
	def action(self) -> str:
		if self.replaced_uuids:
			return "replaced"

		return "created"


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

	# ----------------------------------------------------------------- upsert

	def replace_document(
		self,
		project_id: str,
		file_name: str,
		content: str,
		expected_uuid: str | None = None,
		backup: Callable[[str, str], str] | None = None,
	) -> ReplaceResult:
		"""Save `content` under `file_name`, replacing any document already using it.

		The API has no update endpoint, so this creates the replacement first and only then deletes what it replaced.
		That ordering is the whole safety argument: no path deletes the original until the new content exists remotely and the old content has been backed up locally.

		`backup` receives (file_name, old_content) and returns where it was saved.
		If it raises, nothing is mutated.
		"""
		existing = self.find_documents_by_name(project_id, file_name)

		if expected_uuid is not None:
			actual = None
			if existing:
				actual = existing[0].uuid

			if actual != expected_uuid:
				raise ConcurrentEditError(
					f"{file_name!r} changed since it was read (expected {expected_uuid}, found {actual or 'no document at all'}). Somebody else saved in the meantime; re-read it before writing.",
					file_name=file_name,
					expected_uuid=expected_uuid,
					actual_uuid=actual,
				)

		backup_path = None
		if existing and backup is not None:
			previous = existing[0]
			if previous.is_stub:
				previous = self.get_document(project_id, previous.uuid)

			backup_path = backup(file_name, previous.content or "")

		created = self.create_document(project_id, file_name, content)

		replaced, failed = [], []
		for document in existing:
			try:
				self.delete_document(project_id, document.uuid)
				replaced.append(document.uuid)
			except Exception:
				# The new content is already live, so a failed cleanup is a leftover duplicate, not a lost write.
				# Report it rather than raising.
				failed.append(document.uuid)

		return ReplaceResult(
			uuid=created.uuid,
			file_name=file_name,
			replaced_uuids=replaced,
			failed_delete_uuids=failed,
			backup_path=backup_path,
		)


def _newest_first(documents: list[Document]) -> list[Document]:
	return sorted(documents, key=lambda document: document.created_at or "", reverse=True)
