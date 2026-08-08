"""The scheduled-task tools as an MCP client sees them.

Same end-to-end path as `test_tools.py`: real registration, real schemas, fake API underneath.
"""

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from claude_projects_mcp.client import ClaudeProjectsClient
from claude_projects_mcp.config import Settings
from claude_projects_mcp.server import build_server

from .conftest import ORGANIZATION
from .test_scheduled_tasks import OTHER_PROJECT_UUID, PROJECT_UUID

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
	return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
	return Settings.from_env(
		{
			"CLAUDE_PROJECTS_SESSION_KEY": "sk-ant-sid01-test",
			"CLAUDE_PROJECTS_BACKUP_DIRECTORY": str(tmp_path / "trash"),
		}
	)


@pytest.fixture
def scheduled_api(api):
	api.add_project(ORGANIZATION, PROJECT_UUID, name="Scheduled host")
	api.add_project(ORGANIZATION, OTHER_PROJECT_UUID, name="Somebody else's project")
	return api


@pytest.fixture
def server(settings: Settings, scheduled_api) -> MCPServer:
	return build_server(settings, client=ClaudeProjectsClient(scheduled_api))


async def call(server, tool, **arguments):
	result = await server.call_tool(tool, arguments)
	return json.loads(result.content[0].text)


class TestListing:
	async def test_a_project_filter_returns_only_that_projects_tasks(self, server, scheduled_api):
		scheduled_api.add_scheduled_task(ORGANIZATION, name="Mine", project_uuid=PROJECT_UUID)
		scheduled_api.add_scheduled_task(ORGANIZATION, name="Theirs", project_uuid=OTHER_PROJECT_UUID)

		result = await call(server, "list_scheduled_tasks", project_id=PROJECT_UUID)

		assert [task["name"] for task in result["tasks"]] == ["Mine"]
		assert result["warning"] is None

	async def test_without_a_project_every_task_on_the_account_is_listed(self, server, scheduled_api):
		scheduled_api.add_scheduled_task(ORGANIZATION, name="Mine", project_uuid=PROJECT_UUID)
		scheduled_api.add_scheduled_task(ORGANIZATION, name="Theirs", project_uuid=OTHER_PROJECT_UUID)

		result = await call(server, "list_scheduled_tasks")

		assert sorted(task["name"] for task in result["tasks"]) == ["Mine", "Theirs"]

	async def test_an_empty_project_reports_no_tasks_without_alarm(self, server):
		result = await call(server, "list_scheduled_tasks", project_id=PROJECT_UUID)

		assert result["tasks"] == []
		assert result["warning"] is None

	async def test_tasks_that_match_no_project_fall_back_to_the_whole_listing(self, server, scheduled_api):
		"""The degrade-loudly path: never let a changed encoding read as "nothing scheduled here"."""
		scheduled_api.add_scheduled_task(ORGANIZATION, name="Unattached", project_uuid=None)

		result = await call(server, "list_scheduled_tasks", project_id=PROJECT_UUID)

		assert [task["name"] for task in result["tasks"]] == ["Unattached"]
		assert "could be matched" in result["warning"]
		assert PROJECT_UUID in result["warning"], "the warning has to name the project it failed to match"

	async def test_an_empty_project_beside_a_busy_one_also_warns(self, server, scheduled_api):
		"""The accepted cost of degrading loudly.

		Nothing distinguishes "this project has nothing scheduled" from "the encoding stopped matching" when neither produces a match, so a project with no tasks in an organization that has some gets the warning too.
		It names the benign explanation first, and every task carries its own project_id, so the caller can see which case this is.
		"""
		scheduled_api.add_scheduled_task(ORGANIZATION, name="Theirs", project_uuid=OTHER_PROJECT_UUID)

		result = await call(server, "list_scheduled_tasks", project_id=PROJECT_UUID)

		assert result["warning"] is not None
		assert "may all belong to other projects" in result["warning"]
		assert [task["project_id"] for task in result["tasks"]] == [OTHER_PROJECT_UUID], "the fallback listing has to show whose tasks these actually are"

	async def test_a_listed_task_carries_its_schedule_and_state(self, server, scheduled_api):
		scheduled_api.add_scheduled_task(ORGANIZATION, name="Digest", project_uuid=PROJECT_UUID, cron_expression="0 0 * * 1")

		result = await call(server, "list_scheduled_tasks", project_id=PROJECT_UUID)
		task = result["tasks"][0]

		assert task["cron_expression"] == "0 0 * * 1"
		assert task["enabled"] is True
		assert task["is_manual"] is False
		assert task["project_id"] == PROJECT_UUID

	async def test_a_paused_task_is_reported_as_paused(self, server, scheduled_api):
		scheduled_api.add_scheduled_task(ORGANIZATION, name="Quiet", project_uuid=PROJECT_UUID, enabled=False)

		result = await call(server, "list_scheduled_tasks", project_id=PROJECT_UUID)

		assert result["tasks"][0]["enabled"] is False


class TestReading:
	async def test_a_task_is_read_by_id(self, server, scheduled_api):
		task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Digest", prompt="Summarise.", project_uuid=PROJECT_UUID)

		result = await call(server, "get_scheduled_task", task_id=task_id)

		assert result["name"] == "Digest"
		assert result["prompt"] == "Summarise."

	async def test_an_unknown_task_is_a_tool_error(self, server):
		with pytest.raises(ToolError, match="trig_nope"):
			await call(server, "get_scheduled_task", task_id="trig_nope")


class TestCreating:
	async def test_a_manual_task_is_created(self, server):
		result = await call(
			server,
			"create_scheduled_task",
			project_id=PROJECT_UUID,
			name="Digest",
			prompt="Summarise the week.",
		)

		assert result["name"] == "Digest"
		assert result["is_manual"] is True
		assert result["next_run_at"] is None
		assert result["warning"] is None

	async def test_a_scheduled_task_reports_when_it_will_next_run(self, server):
		result = await call(
			server,
			"create_scheduled_task",
			project_id=PROJECT_UUID,
			name="Digest",
			prompt="Summarise.",
			cron_expression="0 0 * * 1",
		)

		assert result["cron_expression"] == "0 0 * * 1"
		assert result["next_run_at"] is not None

	async def test_an_unparseable_cron_is_a_tool_error(self, server):
		with pytest.raises(ToolError, match="(?i)cron"):
			await call(
				server,
				"create_scheduled_task",
				project_id=PROJECT_UUID,
				name="Digest",
				prompt="Summarise.",
				cron_expression="every monday please",
			)

	async def test_a_model_that_does_not_look_like_one_is_flagged(self, server):
		"""The API accepts any string here, so a typo would only surface when the task ran."""
		result = await call(
			server,
			"create_scheduled_task",
			project_id=PROJECT_UUID,
			name="Digest",
			prompt="Summarise.",
			model="sonnet",
		)

		assert result["warning"] is not None
		assert "sonnet" in result["warning"]

	async def test_a_real_model_id_passes_without_comment(self, server):
		result = await call(
			server,
			"create_scheduled_task",
			project_id=PROJECT_UUID,
			name="Digest",
			prompt="Summarise.",
			model="claude-sonnet-5",
		)

		assert result["model"] == "claude-sonnet-5"
		assert result["warning"] is None


class TestUpdating:
	async def test_a_name_can_be_changed_alone(self, server, scheduled_api):
		task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Before", prompt="Original.", project_uuid=PROJECT_UUID)

		result = await call(server, "update_scheduled_task", task_id=task_id, name="After")

		assert result["name"] == "After"
		assert result["prompt"] == "Original."

	async def test_a_task_can_be_paused(self, server, scheduled_api):
		task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Noisy", project_uuid=PROJECT_UUID, cron_expression="0 0 * * 1")

		result = await call(server, "update_scheduled_task", task_id=task_id, enabled=False)

		assert result["enabled"] is False
		assert result["cron_expression"] == "0 0 * * 1", "pausing must keep the schedule so it can be resumed"

	async def test_an_update_with_nothing_to_change_is_a_tool_error(self, server, scheduled_api):
		task_id = scheduled_api.add_scheduled_task(ORGANIZATION, project_uuid=PROJECT_UUID)

		with pytest.raises(ToolError, match="at least one"):
			await call(server, "update_scheduled_task", task_id=task_id)


class TestDeleting:
	async def test_a_task_is_deleted_and_named_in_the_answer(self, server, scheduled_api):
		task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Doomed", project_uuid=PROJECT_UUID)

		result = await call(server, "delete_scheduled_task", task_id=task_id)

		assert result["deleted"] is True
		assert result["name"] == "Doomed"
		assert scheduled_api.scheduled_tasks == {}

	async def test_deleting_something_already_gone_says_so_rather_than_failing(self, server):
		result = await call(server, "delete_scheduled_task", task_id="trig_nope")

		assert result["deleted"] is False
		assert result["warning"] is not None

	async def test_deleting_does_not_write_a_backup(self, server, scheduled_api, settings):
		"""Unlike documents, a task definition is not copied anywhere before it goes.

		Deliberate: a task is a prompt and a schedule, cheap to retype, and the backup directory is for content that cannot be reconstructed.
		"""
		task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Doomed", project_uuid=PROJECT_UUID)

		await call(server, "delete_scheduled_task", task_id=task_id)

		assert not list(Path(settings.backup_directory).rglob("*Doomed*"))
