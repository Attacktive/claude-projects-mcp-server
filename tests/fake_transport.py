"""An in-memory stand-in for the claude.ai API.

Stateful rather than a list of canned responses, because the operations worth testing —
upsert, push — are multi-request sequences whose correctness *is* their ordering. Canned
sequences go brittle the moment a retry is added; a fake that actually holds documents
does not.

Two affordances beyond storage earn their keep:
  * `fail_once` injects a fault at one route, for the crash-midway scenarios
  * `log` records every request, so tests can assert create-happens-before-delete
"""

import re
import urllib.parse
from typing import Any

from claude_projects_mcp.errors import ApiError, NotFoundError
from claude_projects_mcp.identifiers import chat_project_id

_ORGANIZATIONS = re.compile(r"^/organizations$")
_PROJECTS = re.compile(r"^/organizations/(?P<organization>[^/]+)/projects$")
_PROJECTS_V2 = re.compile(r"^/organizations/(?P<organization>[^/]+)/projects_v2$")
_PROJECT = re.compile(r"^/organizations/(?P<organization>[^/]+)/projects/(?P<project>[^/]+)$")
_DOCUMENTS = re.compile(r"^/organizations/(?P<organization>[^/]+)/projects/(?P<project>[^/]+)/docs$")
_DOCUMENT = re.compile(r"^/organizations/(?P<organization>[^/]+)/projects/(?P<project>[^/]+)/docs/(?P<document>[^/]+)$")
_KNOWLEDGE_STATS = re.compile(r"^/organizations/(?P<organization>[^/]+)/projects/(?P<project>[^/]+)/kb/stats$")
_SCHEDULED_TASKS = re.compile(r"^/organizations/(?P<organization>[^/]+)/cowork/scheduled_tasks$")
_SCHEDULED_TASK = re.compile(r"^/organizations/(?P<organization>[^/]+)/cowork/scheduled_tasks/(?P<task>[^/]+)$")

# Five whitespace-separated fields. The real API rejects anything else with a 400, and so must this.
_CRON = re.compile(r"^\s*(\S+\s+){4}\S+\s*$")


class FakeClaudeProjectsApi:
	"""Implements the Transport protocol against dictionaries."""

	def __init__(self, list_includes_content: bool = True):
		# Whether a documents listing carries content, or only stubs.
		# The spike (2026-08-06) observed the real API including content, so that is the default.
		# The stub path is still exercised deliberately by `stub_api`, because the client must keep coping if an undocumented API changes its mind.
		self.list_includes_content = list_includes_content

		self.organizations: list[dict] = []
		self.projects: dict[str, dict] = {}
		self.documents: dict[str, list[dict]] = {}
		self.scheduled_tasks: dict[str, dict] = {}
		self.log: list[tuple[str, str]] = []
		self.closed = False

		self._faults: list[tuple[str, re.Pattern, Exception]] = []
		self._sequence = 0

	# ------------------------------------------------------------------ setup

	def add_organization(self, uuid: str, name: str = "Organization", capabilities: list[str] | None = None) -> str:
		if capabilities is None:
			capabilities = ["chat"]

		self.organizations.append({"uuid": uuid, "name": name, "capabilities": capabilities})
		return uuid

	def add_project(
		self,
		organization_uuid: str,
		uuid: str,
		name: str = "Project",
		description: str = "",
		instructions: str = "",
		is_private: bool = False,
		search_threshold: int | None = 50_000,
		max_knowledge_size: int | None = 2_000_000,
	) -> str:
		self.projects[uuid] = {
			"uuid": uuid,
			"name": name,
			"description": description,
			"prompt_template": instructions,
			"is_private": is_private,
			"created_at": self._stamp(),
			"updated_at": self._stamp(),
			"_organization": organization_uuid,
			"_search_threshold": search_threshold,
			"_max_knowledge_size": max_knowledge_size,
		}
		self.documents.setdefault(uuid, [])
		return uuid

	def add_document(self, project_uuid: str, file_name: str, content: str, uuid: str | None = None) -> str:
		document = {
			"uuid": uuid or self._next_uuid(),
			"file_name": file_name,
			"content": content,
			"created_at": self._stamp(),
		}
		self.documents.setdefault(project_uuid, []).append(document)
		return document["uuid"]

	def add_scheduled_task(
		self,
		organization_uuid: str,
		name: str = "Task",
		prompt: str = "Do the thing.",
		project_uuid: str | None = None,
		cron_expression: str | None = None,
		enabled: bool = True,
		model: str | None = None,
	) -> str:
		"""Put a task in the fake and hand back its id.

		The id is always generated rather than accepted, like the real API's: nothing needs to choose one, and taking it as an argument only invited callers to invent ids that a real trigger could never have.
		"""
		task_id = self._next_task_id()
		self.scheduled_tasks[task_id] = {
			"id": task_id,
			"name": name,
			"prompt": prompt,
			"cron_expression": cron_expression,
			"enabled": enabled,
			"model": model,
			"created_at": self._stamp(),
			"updated_at": self._stamp(),
			"_organization": organization_uuid,
			"_project": project_uuid,
		}

		return task_id

	def fail_once(self, method: str, path_pattern: str, exception: Exception) -> None:
		"""Make the next matching request raise, then behave normally again."""
		self._faults.append((method.upper(), re.compile(path_pattern), exception))

	def document_names(self, project_uuid: str) -> list[str]:
		return [document["file_name"] for document in self.documents.get(project_uuid, [])]

	def content_of(self, project_uuid: str, file_name: str) -> list[str]:
		return [document["content"] for document in self.documents.get(project_uuid, []) if document["file_name"] == file_name]

	def methods_logged(self) -> list[str]:
		return [method for method, _ in self.log]

	# -------------------------------------------------------------- transport

	def request(self, method: str, path: str, *, json_body: dict | None = None) -> Any:
		method = method.upper()
		self.log.append((method, path))
		self._maybe_fail(method, path)

		route, _, query = path.partition("?")
		parameters = dict(urllib.parse.parse_qsl(query))

		if method == "GET":
			return self._get(route, parameters)

		if method == "POST":
			return self._post(path, json_body or {})

		if method == "PUT":
			return self._put(path, json_body or {})

		if method == "PATCH":
			return self._patch(path, json_body or {})

		if method == "DELETE":
			return self._delete(path)

		raise ApiError(f"FakeClaudeProjectsApi has no route for {method} {path}", status=405)

	def close(self) -> None:
		self.closed = True

	# ---------------------------------------------------------------- routing

	def _get(self, path: str, parameters: dict[str, str]) -> Any:
		if _ORGANIZATIONS.match(path):
			return [dict(organization) for organization in self.organizations]

		match = _PROJECTS_V2.match(path)
		if match:
			return self._projects_page(match["organization"], parameters)

		match = _PROJECT.match(path)
		if match:
			return self._public_project(self._find_project(match["project"]))

		match = _DOCUMENTS.match(path)
		if match:
			self._find_project(match["project"])
			return [self._public_document(document, with_content=self.list_includes_content) for document in self.documents.get(match["project"], [])]

		match = _DOCUMENT.match(path)
		if match:
			return self._public_document(self._find_document(match["project"], match["document"]), with_content=True)

		match = _KNOWLEDGE_STATS.match(path)
		if match:
			project = self._find_project(match["project"])
			search_threshold = project["_search_threshold"]
			max_knowledge_size = project["_max_knowledge_size"]
			if search_threshold is None or max_knowledge_size is None:
				raise NotFoundError(f"No kb stats for project {match['project']}")

			docs = self.documents.get(match["project"], [])
			knowledge_size = sum(len(doc["content"]) for doc in docs if doc.get("content") is not None)
			use_search = knowledge_size > search_threshold
			return {
				"knowledge_size": knowledge_size,
				"max_knowledge_size": max_knowledge_size,
				"project_knowledge_search_threshold": search_threshold,
				"use_project_knowledge_search": use_search,
			}

		match = _SCHEDULED_TASKS.match(path)
		if match:
			# The real listing ignores every query parameter it is given, so the fake must not pretend to filter either.
			rows = [self._public_task(task) for task in self.scheduled_tasks.values() if task["_organization"] == match["organization"]]
			return {"data": rows}

		match = _SCHEDULED_TASK.match(path)
		if match:
			return {"trigger": self._public_task(self._find_task(match["task"]))}

		raise ApiError(f"FakeClaudeProjectsApi has no route for GET {path}", status=404)

	def _post(self, path: str, body: dict) -> Any:
		match = _PROJECTS.match(path)
		if match:
			return self._create_project(match["organization"], body)

		match = _SCHEDULED_TASKS.match(path)
		if match:
			return self._create_scheduled_task(match["organization"], body)

		match = _DOCUMENTS.match(path)
		if not match:
			raise ApiError(f"FakeClaudeProjectsApi has no route for POST {path}", status=404)

		self._find_project(match["project"])
		if "file_name" not in body or "content" not in body:
			raise ApiError(f"POST body must carry file_name and content, got {sorted(body)}", status=400)

		# The real API tolerates two documents sharing a file_name, which is what makes create-before-delete safe.
		# The fake must tolerate it too.
		document = {
			"uuid": self._next_uuid(),
			"file_name": body["file_name"],
			"content": body["content"],
			"created_at": self._stamp(),
		}
		self.documents.setdefault(match["project"], []).append(document)
		return self._public_document(document, with_content=True)

	def _put(self, path: str, body: dict) -> Any:
		match = _PROJECT.match(path)
		if not match:
			raise ApiError(f"FakeClaudeProjectsApi has no route for PUT {path}", status=404)

		project = self._find_project(match["project"])

		# The real API takes a partial body: whatever is absent keeps its current value.
		for key in ("name", "description", "prompt_template", "is_private"):
			if key in body:
				project[key] = body[key]

		project["updated_at"] = self._stamp()
		return self._public_project(project)

	def _patch(self, path: str, body: dict) -> Any:
		match = _SCHEDULED_TASK.match(path)
		if not match:
			raise ApiError(f"FakeClaudeProjectsApi has no route for PATCH {path}", status=404)

		task = self._find_task(match["task"])

		if "cron_expression" in body and body["cron_expression"] is not None and not _CRON.match(str(body["cron_expression"])):
			raise ApiError("The request is invalid.", status=400)

		for key in ("name", "prompt", "cron_expression", "enabled", "model"):
			if key in body:
				task[key] = body[key]

		task["updated_at"] = self._stamp()
		return {"trigger": self._public_task(task)}

	def _delete(self, path: str) -> Any:
		match = _SCHEDULED_TASK.match(path)
		if match:
			task = self._find_task(match["task"])
			del self.scheduled_tasks[task["id"]]
			# The real API answers a delete with a body rather than an empty 204.
			return {"deleted_session_count": 0}

		match = _PROJECT.match(path)
		if match:
			project = self._find_project(match["project"])
			del self.projects[project["uuid"]]
			self.documents.pop(project["uuid"], None)
			return None

		match = _DOCUMENT.match(path)
		if not match:
			raise ApiError(f"FakeClaudeProjectsApi has no route for DELETE {path}", status=404)

		document = self._find_document(match["project"], match["document"])
		self.documents[match["project"]].remove(document)
		return None

	def _projects_page(self, organization_uuid: str, parameters: dict[str, str]) -> dict:
		"""The paginated listing, envelope and all."""
		rows = [self._public_project(project, with_instructions=False) for project in self.projects.values() if project["_organization"] == organization_uuid]
		limit = int(parameters.get("limit", 30))
		offset = int(parameters.get("offset", 0))
		window = rows[offset : offset + limit]

		return {
			"data": window,
			"pagination": {
				"total": len(rows),
				"limit": limit,
				"offset": offset,
				"has_more": offset + len(window) < len(rows),
			},
		}

	def _create_project(self, organization_uuid: str, body: dict) -> dict:
		# The real API rejects a create without a description, even an empty one.
		for required in ("name", "description"):
			if required not in body:
				raise ApiError(f"{required}: Field required", status=400)

		uuid = self._next_project_uuid()
		self.add_project(
			organization_uuid,
			uuid,
			name=body["name"],
			description=body["description"],
			is_private=bool(body.get("is_private", False)),
		)
		# The create response omits prompt_template, exactly like the listing.
		return self._public_project(self.projects[uuid], with_instructions=False)

	def _create_scheduled_task(self, organization_uuid: str, body: dict) -> dict:
		if "name" not in body:
			raise ApiError("name: Field required", status=400)

		cron = body.get("cron_expression")
		if cron is not None and not _CRON.match(str(cron)):
			raise ApiError("The request is invalid.", status=400)

		# `model` is deliberately not validated: the real API accepts any string, which is exactly why the tool layer warns about one that looks wrong.
		task_id = self.add_scheduled_task(
			organization_uuid,
			name=body["name"],
			prompt=body.get("prompt", ""),
			project_uuid=body.get("project_uuid"),
			cron_expression=cron,
			model=body.get("model"),
		)

		return {"trigger": self._public_task(self.scheduled_tasks[task_id])}

	# ---------------------------------------------------------------- helpers

	def _maybe_fail(self, method: str, path: str) -> None:
		for index, (fault_method, pattern, exception) in enumerate(self._faults):
			if fault_method == method and pattern.search(path):
				del self._faults[index]
				raise exception

	def _find_project(self, uuid: str) -> dict:
		if uuid not in self.projects:
			raise NotFoundError(f"No project {uuid}")

		return self.projects[uuid]

	def _find_task(self, task_id: str) -> dict:
		if task_id not in self.scheduled_tasks:
			raise NotFoundError(f"No scheduled task {task_id}")

		return self.scheduled_tasks[task_id]

	def _find_document(self, project_uuid: str, document_uuid: str) -> dict:
		for document in self.documents.get(project_uuid, []):
			if document["uuid"] == document_uuid:
				return document

		raise NotFoundError(f"No document {document_uuid} in project {project_uuid}")

	@staticmethod
	def _public_project(project: dict, with_instructions: bool = True) -> dict:
		public = {key: value for key, value in project.items() if not key.startswith("_")}
		if not with_instructions:
			# The real listing and create responses omit prompt_template (spike, 2026-08-06), so `instructions` parses as empty from either.
			# Only the single-project fetch and the update response carry it.
			public.pop("prompt_template", None)

		return public

	@staticmethod
	def _public_task(task: dict) -> dict:
		"""A trigger shaped the way the real API shapes one.

		The omissions are the point: a paused task carries no `enabled` key at all and a manual one carries no `cron_expression`, so a client that defaults either field the obvious way is caught here rather than in production.
		"""
		public: dict[str, Any] = {
			"id": task["id"],
			"name": task["name"],
			"created_at": task["created_at"],
			"updated_at": task["updated_at"],
			"job_config": {
				"ccr": {
					"title": task["name"],
					"events": [{"data": {"type": "user", "message": {"role": "user", "content": task["prompt"]}}}],
				}
			},
		}

		if task["enabled"]:
			public["enabled"] = True

		if task["cron_expression"]:
			public["cron_expression"] = task["cron_expression"]
			public["next_run_at"] = "2026-08-10T00:05:23.848718590Z"
		else:
			public["next_run_at"] = "0001-01-01T00:00:00Z"

		if task["model"]:
			public["job_config"]["ccr"]["session_context"] = {"model": task["model"]}

		if task["_project"]:
			public["chat_project_id"] = chat_project_id(task["_project"])

		return public

	@staticmethod
	def _public_document(document: dict, with_content: bool) -> dict:
		public = dict(document)
		content = public.get("content")
		if content is not None:
			public["estimated_token_count"] = len(content)

		if not with_content:
			del public["content"]

		return public

	def _next_uuid(self) -> str:
		self._sequence += 1
		# Uuid-shaped like the real API's, so the client's uuid-versus-file-name heuristic routes these exactly as it routes real ones.
		# The sequence sits in the first group because `filenames.deduplicate` disambiguates with `uuid[:8]`, like the random opening of a real uuid does.
		return f"{self._sequence:08d}-0000-4000-8000-000000000000"

	def _next_project_uuid(self) -> str:
		self._sequence += 1
		return f"project-{self._sequence:04d}"

	def _next_task_id(self) -> str:
		self._sequence += 1
		# Prefixed like the real trigger ids, so nothing can quietly start treating one as a uuid.
		return f"trig_{self._sequence:022d}"

	def _stamp(self) -> str:
		"""Monotonic timestamps, so 'newest' is well-defined in tests."""
		self._sequence += 1
		return f"2026-08-06T00:00:{self._sequence:02d}Z"
