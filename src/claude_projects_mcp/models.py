"""Parsed shapes of the claude.ai responses.

Deliberately tolerant of fields we do not know about and intolerant of fields we need: an undocumented API grows keys without warning, but a key that vanishes is a contract break we want to hear about immediately rather than debug later as a None.
"""

from dataclasses import dataclass
from typing import Any, Self

from .errors import ApiError
from .identifiers import project_uuid_from

_CHAT_CAPABILITIES = frozenset({"chat", "claude_pro"})

# What a scheduled task reports as its next run when there is no schedule to run on.
_NEVER = "0001-01-01"


def _require(raw: Any, key: str, kind: str) -> Any:
	if not isinstance(raw, dict):
		raise ApiError(f"Expected a JSON object for {kind}, got {type(raw).__name__}.", status=0)

	if key not in raw:
		raise ApiError(f"{kind} response is missing the {key!r} field. The claude.ai API shape may have changed; keys present: {sorted(raw)}", status=0)

	return raw[key]


def _parse_list(raw: Any, kind: str, build) -> list:
	if not isinstance(raw, list):
		raise ApiError(f"Expected a JSON list of {kind}, got {type(raw).__name__}. The claude.ai API may have added a pagination envelope.", status=0)

	return [build(item) for item in raw]


@dataclass(frozen=True, slots=True)
class Organization:
	uuid: str
	name: str
	capabilities: tuple[str, ...] = ()

	@classmethod
	def parse(cls, raw: Any) -> Self:
		return cls(
			uuid=_require(raw, "uuid", "Organization"),
			name=raw.get("name") or "",
			capabilities=tuple(raw.get("capabilities") or ()),
		)

	@classmethod
	def parse_list(cls, raw: Any) -> list[Self]:
		return _parse_list(raw, "organizations", cls.parse)

	@property
	def is_chat_capable(self) -> bool:
		"""Orgs without these capabilities have no projects to reach."""
		return bool(_CHAT_CAPABILITIES & set(self.capabilities))


@dataclass(frozen=True, slots=True)
class Project:
	uuid: str
	name: str
	description: str = ""
	instructions: str = ""
	is_private: bool = False
	created_at: str | None = None
	updated_at: str | None = None
	organization_uuid: str | None = None

	@classmethod
	def parse(cls, raw: Any, organization_uuid: str | None = None) -> Self:
		return cls(
			uuid=_require(raw, "uuid", "Project"),
			name=raw.get("name") or "",
			description=raw.get("description") or "",
			# The API calls the project instructions `prompt_template`, and sends them only on a single-project fetch — a listing or a create response omits the field, so empty here means "not told", not "empty upstream".
			instructions=raw.get("prompt_template") or "",
			is_private=bool(raw.get("is_private", False)),
			created_at=raw.get("created_at"),
			updated_at=raw.get("updated_at"),
			organization_uuid=organization_uuid,
		)

	@classmethod
	def parse_list(cls, raw: Any, organization_uuid: str | None = None) -> list[Self]:
		return _parse_list(raw, "projects", lambda item: cls.parse(item, organization_uuid=organization_uuid))

	@classmethod
	def parse_page(cls, raw: Any, organization_uuid: str | None = None) -> tuple[list[Self], bool]:
		"""One page of the projects listing, plus whether more remain.

		The listing wraps its rows in `{data, pagination}`.
		That envelope is the whole reason for preferring it to the older bare-array endpoint: an array cannot express the difference between "that is all of them" and "that is the first thirty", so a truncated answer would be indistinguishable from a complete one.
		"""
		if not isinstance(raw, dict):
			raise ApiError(f"Expected a JSON object for a page of projects, got {type(raw).__name__}.", status=0)

		rows = _require(raw, "data", "Projects page")
		pagination = raw.get("pagination") or {}

		return cls.parse_list(rows, organization_uuid=organization_uuid), bool(pagination.get("has_more"))


@dataclass(frozen=True, slots=True)
class Document:
	uuid: str
	file_name: str
	content: str | None = None
	created_at: str | None = None

	@classmethod
	def parse(cls, raw: Any) -> Self:
		return cls(
			uuid=_require(raw, "uuid", "Document"),
			file_name=_require(raw, "file_name", "Document"),
			content=raw.get("content"),
			created_at=raw.get("created_at"),
		)

	@classmethod
	def parse_list(cls, raw: Any) -> list[Self]:
		return _parse_list(raw, "documents", cls.parse)

	@property
	def is_stub(self) -> bool:
		"""True when this came from a listing that omits content, so a fetch is needed."""
		return self.content is None

	@property
	def characters(self) -> int | None:
		if self.content is None:
			return None

		return len(self.content)


def _prompt_of(ccr: Any) -> str | None:
	"""The instruction a scheduled task will send, from inside the job config.

	It sits four levels down in a replayed event log rather than in a field of its own, so every step is treated as optional: a task whose prompt cannot be found is still a task worth listing, and raising here would make the whole listing fail over one odd row.
	"""
	events = ccr.get("events") or []
	if not events or not isinstance(events[0], dict):
		return None

	message = (events[0].get("data") or {}).get("message") or {}
	content = message.get("content")

	if isinstance(content, str):
		return content

	return None


def _scheduled_timestamp(value: Any) -> str | None:
	"""A next-run time, or None when the API means "never".

	A manual task reports `0001-01-01T00:00:00Z` — Go's zero time — which would otherwise read as a real date two thousand years ago.
	"""
	if not isinstance(value, str) or not value or value.startswith(_NEVER):
		return None

	return value


@dataclass(frozen=True, slots=True)
class ScheduledTask:
	"""A Cowork scheduled task, which the API calls a trigger."""

	id: str
	name: str
	prompt: str | None = None
	cron_expression: str | None = None
	enabled: bool = False
	next_run_at: str | None = None
	created_at: str | None = None
	updated_at: str | None = None
	chat_project_id: str | None = None
	model: str | None = None

	@classmethod
	def parse(cls, raw: Any) -> Self:
		task_id = _require(raw, "id", "Scheduled task")
		name = _require(raw, "name", "Scheduled task")

		job_config = raw.get("job_config")
		ccr = job_config.get("ccr") if isinstance(job_config, dict) else None
		if not isinstance(ccr, dict):
			ccr = {}

		return cls(
			id=task_id,
			name=name,
			prompt=_prompt_of(ccr),
			# Absent means manual: a task with no schedule has no cron field rather than an empty one.
			cron_expression=raw.get("cron_expression"),
			# Absent means paused. The API omits this field entirely instead of sending false, so defaulting it to True would report every paused task as running.
			enabled=bool(raw.get("enabled", False)),
			next_run_at=_scheduled_timestamp(raw.get("next_run_at")),
			created_at=raw.get("created_at"),
			updated_at=raw.get("updated_at"),
			chat_project_id=raw.get("chat_project_id"),
			model=(ccr.get("session_context") or {}).get("model"),
		)

	@classmethod
	def parse_trigger(cls, raw: Any) -> Self:
		"""One task, from the `{trigger}` envelope every single-task response uses."""
		return cls.parse(_require(raw, "trigger", "Scheduled task"))

	@classmethod
	def parse_list(cls, raw: Any) -> list[Self]:
		"""The listing, which wraps its rows in `{data}` but carries no pagination."""
		rows = _require(raw, "data", "Scheduled tasks")
		return _parse_list(rows, "scheduled tasks", cls.parse)

	@property
	def is_manual(self) -> bool:
		"""True when this task only ever runs because somebody asked it to."""
		return self.cron_expression is None

	@property
	def project_uuid(self) -> str | None:
		"""The project this task belongs to, decoded from its chat_project_id."""
		if not self.chat_project_id:
			return None

		return project_uuid_from(self.chat_project_id)
