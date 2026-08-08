"""The MCP surface over Cowork scheduled tasks.

Kept out of `server.py` because scheduled tasks share nothing with documents — no backup store,
no file names, no overwrite semantics — so the only thing the two would gain from sitting together
is a longer file.

These tools manage what a task *is*. Running one is deliberately absent: the API has an endpoint
for it, but starting a billable Claude run is not something a tool call should be able to do by
accident. Setting a schedule and letting Cowork run it is the whole point of the feature anyway.
"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from .client import ClaudeProjectsClient
from .errors import ApiError, ClaudeProjectsError, NotFoundError
from .models import ScheduledTask

_CRON_HELP = "cron_expression must be five space-separated fields in UTC — minute hour day-of-month month day-of-week — for example '0 9 * * 1-5' for weekdays at 09:00 UTC. Omit it entirely for a task that only runs when somebody asks."

_MODEL_HELP = "Model ids look like 'claude-sonnet-5', or 'default' to follow the project's own setting."


def _model_warning(model: str | None) -> str | None:
	"""Flag a model id the API will accept but the scheduler probably cannot use.

	The API does no validation here — an invented id is stored with a 200 — so a typo would otherwise stay invisible until the task ran and failed.
	This warns rather than refuses, because a model released after this code was written must still be usable.
	"""
	if model is None or model == "default" or model.startswith("claude-"):
		return None

	return f"{model!r} does not look like a model id, and claude.ai accepts it without checking, so the task may fail when it runs. {_MODEL_HELP}"


def _task_dict(task: ScheduledTask) -> dict:
	return {
		"task_id": task.id,
		"name": task.name,
		"prompt": task.prompt,
		"cron_expression": task.cron_expression,
		"is_manual": task.is_manual,
		"enabled": task.enabled,
		"next_run_at": task.next_run_at,
		"model": task.model,
		"project_id": task.project_uuid,
		"created_at": task.created_at,
		"updated_at": task.updated_at,
	}


class translated:
	"""Turn a typed ClaudeProjectsError into the ToolError the model will actually read.

	Duplicated from `server.py` rather than shared, because the two differ: this one also rewrites the API's bare "The request is invalid." into something a caller can act on, which is only ever about a cron expression here.
	"""

	def __exit__(self, exception_type, exception, traceback):
		if exception is None:
			return False

		if isinstance(exception, ApiError) and exception.status == 400:
			raise ToolError(f"claude.ai rejected the request: {exception} {_CRON_HELP}") from exception

		if isinstance(exception, ClaudeProjectsError):
			raise ToolError(str(exception)) from exception

		return False

	def __enter__(self):
		return self


def register(server: MCPServer, client: ClaudeProjectsClient) -> None:
	"""Add the scheduled-task tools to an already-built server."""

	@server.tool(
		annotations=ToolAnnotations(read_only_hint=True),
		description="List Cowork scheduled tasks. Pass project_id for one project's tasks, or nothing for every task on the account. Schedules are cron expressions in UTC; a task with none runs only when started by hand.",
	)
	def list_scheduled_tasks(project_id: str | None = None, organization_id: str | None = None) -> dict:
		with translated():
			if project_id is None:
				return {
					"tasks": [_task_dict(task) for task in client.list_scheduled_tasks(organization_id=organization_id)],
					"project_id": None,
					"warning": None,
				}

			found = client.scheduled_tasks_for_project(project_id)

		# A project id is matched against an id the API derives, so an empty result has two very different causes.
		# Falling back to the whole listing keeps "the encoding changed" from reading as "nothing is scheduled here".
		if found.mapping_looks_broken:
			return {
				"tasks": [_task_dict(task) for task in found.in_organization],
				"project_id": project_id,
				"warning": f"None of the {len(found.in_organization)} scheduled tasks in this organization could be matched to project {project_id}. They may all belong to other projects, or the way claude.ai names a task's project may have changed. Every task in the organization is listed instead; check each task's project_id before acting on it.",
			}

		return {
			"tasks": [_task_dict(task) for task in found.matched],
			"project_id": project_id,
			"warning": None,
		}

	@server.tool(
		annotations=ToolAnnotations(read_only_hint=True),
		description="Read one scheduled task by its id, including the prompt it will send.",
	)
	def get_scheduled_task(task_id: str) -> dict:
		with translated():
			return _task_dict(client.get_scheduled_task(task_id))

	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Create a scheduled task in a project. Omit cron_expression for a task that only runs when started by hand. cron_expression is five fields in UTC, not local time — '0 0 * * 1' is Monday 00:00 UTC. The answer carries next_run_at so the schedule can be checked before it matters.",
	)
	def create_scheduled_task(
		project_id: str,
		name: str,
		prompt: str,
		cron_expression: str | None = None,
		model: str | None = None,
	) -> dict:
		with translated():
			created = client.create_scheduled_task(
				project_id,
				name,
				prompt,
				cron_expression=cron_expression,
				model=model,
			)

		return {**_task_dict(created), "warning": _model_warning(model)}

	@server.tool(
		annotations=ToolAnnotations(destructive_hint=False),
		description="Change a scheduled task. Only the fields you pass are touched. Pass enabled=false to pause a task without losing its prompt or schedule, which is almost always better than deleting it. A schedule cannot be removed here — pause it instead.",
	)
	def update_scheduled_task(
		task_id: str,
		name: str | None = None,
		prompt: str | None = None,
		cron_expression: str | None = None,
		enabled: bool | None = None,
		model: str | None = None,
	) -> dict:
		if name is None and prompt is None and cron_expression is None and enabled is None and model is None:
			raise ToolError("Nothing to update. Pass at least one of name, prompt, cron_expression, enabled, or model. Use get_scheduled_task to see the current values.")

		with translated():
			updated = client.update_scheduled_task(
				task_id,
				name=name,
				prompt=prompt,
				cron_expression=cron_expression,
				enabled=enabled,
				model=model,
			)

		return {**_task_dict(updated), "warning": _model_warning(model)}

	@server.tool(
		annotations=ToolAnnotations(destructive_hint=True),
		description="Delete a scheduled task. Unlike documents, nothing is backed up first — a task is a prompt and a schedule, not content — so prefer update_scheduled_task with enabled=false if you only want it to stop running.",
	)
	def delete_scheduled_task(task_id: str) -> dict:
		with translated():
			try:
				# Read it first so the answer can name what went, which is the only record of it afterwards.
				target = client.get_scheduled_task(task_id)
			except NotFoundError:
				return {
					"deleted": False,
					"task_id": task_id,
					"name": None,
					"warning": f"No scheduled task {task_id!r} on this account, so nothing was deleted. It may already be gone. Use list_scheduled_tasks to see what is there.",
				}

			client.delete_scheduled_task(task_id)

		return {
			"deleted": True,
			"task_id": target.id,
			"name": target.name,
			"prompt": target.prompt,
			"cron_expression": target.cron_expression,
			"warning": None,
		}
