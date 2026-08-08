"""Parsing the scheduled-task shape claude.ai actually sends.

The shapes here were captured from the real API on 2026-08-08; the two that matter most are the
ones a reasonable reader would get backwards. `enabled` is absent rather than false when a task is
paused, and `next_run_at` carries a zero-time sentinel rather than nothing when a task is manual.
"""

import pytest

from claude_projects_mcp.client import ClaudeProjectsClient
from claude_projects_mcp.errors import ApiError, NotFoundError
from claude_projects_mcp.models import ScheduledTask

from .conftest import ORGANIZATION

# Scheduled tasks name their project through an encoded uuid, so these tests need projects whose ids really are uuids — unlike the short stand-ins the document tests get away with.
PROJECT_UUID = "019fda83-e171-70e3-8e2d-e698c7feb1c2"
OTHER_PROJECT_UUID = "019fdb00-1111-70e3-8e2d-e698c7feb1c3"


def raw_task(**overrides) -> dict:
	"""A trigger as the API sends it, with the deep nesting the prompt and model really live in."""
	task = {
		"id": "trig_01QALV7ipmVEo8TmK9vwEA5s",
		"name": "Weekly digest",
		"enabled": True,
		"cron_expression": "0 0 * * 1",
		"next_run_at": "2026-08-10T00:05:23.848718590Z",
		"created_at": "2026-08-08T03:37:03.580029Z",
		"updated_at": "2026-08-08T03:43:27.036112Z",
		"chat_project_id": "claude_proj_011Cdnmd5MfLyEFPnPDRUwNq",
		"job_config": {
			"ccr": {
				"title": "Weekly digest",
				"events": [{"data": {"type": "user", "message": {"role": "user", "content": "Summarise this week."}}}],
				"session_context": {"model": "claude-sonnet-5"},
			}
		},
	}
	task.update(overrides)
	return task


def test_the_plain_fields_parse():
	task = ScheduledTask.parse(raw_task())

	assert task.id == "trig_01QALV7ipmVEo8TmK9vwEA5s"
	assert task.name == "Weekly digest"
	assert task.cron_expression == "0 0 * * 1"
	assert task.chat_project_id == "claude_proj_011Cdnmd5MfLyEFPnPDRUwNq"


def test_a_paused_task_has_no_enabled_field_at_all():
	"""The API omits `enabled` when it is false, so absent must not read as active.

	Defaulting the other way would report every paused task as running.
	"""
	raw = raw_task()
	del raw["enabled"]

	assert ScheduledTask.parse(raw).enabled is False


def test_an_active_task_says_so():
	assert ScheduledTask.parse(raw_task(enabled=True)).enabled is True


def test_a_manual_task_has_no_cron_expression():
	raw = raw_task()
	del raw["cron_expression"]

	task = ScheduledTask.parse(raw)

	assert task.cron_expression is None
	assert task.is_manual is True


def test_a_scheduled_task_is_not_manual():
	assert ScheduledTask.parse(raw_task()).is_manual is False


def test_the_zero_time_next_run_is_not_a_date():
	"""A manual task reports `0001-01-01T00:00:00Z`, which means "never", not "the year 1"."""
	assert ScheduledTask.parse(raw_task(next_run_at="0001-01-01T00:00:00Z")).next_run_at is None


def test_a_real_next_run_survives():
	assert ScheduledTask.parse(raw_task()).next_run_at == "2026-08-10T00:05:23.848718590Z"


def test_the_prompt_is_dug_out_of_the_event_log():
	assert ScheduledTask.parse(raw_task()).prompt == "Summarise this week."


def test_a_missing_prompt_is_none_rather_than_a_crash():
	"""Four levels of undocumented nesting is not something to raise over: the task still exists and still needs listing."""
	assert ScheduledTask.parse(raw_task(job_config={"ccr": {}})).prompt is None


def test_an_absent_job_config_is_survivable():
	raw = raw_task()
	del raw["job_config"]

	task = ScheduledTask.parse(raw)

	assert task.prompt is None
	assert task.model is None


def test_the_model_is_dug_out_of_the_session_context():
	assert ScheduledTask.parse(raw_task()).model == "claude-sonnet-5"


def test_a_task_left_on_the_default_model_reports_none():
	raw = raw_task()
	del raw["job_config"]["ccr"]["session_context"]

	assert ScheduledTask.parse(raw).model is None


def test_the_project_uuid_is_decoded_from_the_chat_project_id():
	assert ScheduledTask.parse(raw_task()).project_uuid == "019fda83-e171-70e3-8e2d-e698c7feb1c2"


def test_a_task_belonging_to_no_project_has_no_project_uuid():
	raw = raw_task()
	del raw["chat_project_id"]

	assert ScheduledTask.parse(raw).project_uuid is None


def test_a_listing_is_unwrapped_from_its_data_envelope():
	tasks = ScheduledTask.parse_list({"data": [raw_task(), raw_task(id="trig_02", name="Second")]})

	assert [task.name for task in tasks] == ["Weekly digest", "Second"]


def test_an_empty_listing_parses():
	assert ScheduledTask.parse_list({"data": []}) == []


def test_a_single_task_is_unwrapped_from_its_trigger_envelope():
	"""Every single-task response nests the task under `trigger`, unlike the listing's `data`."""
	assert ScheduledTask.parse_trigger({"trigger": raw_task()}).name == "Weekly digest"


def test_a_single_task_without_its_envelope_is_a_contract_break():
	with pytest.raises(ApiError):
		ScheduledTask.parse_trigger(raw_task())


def test_a_listing_without_its_envelope_is_a_contract_break():
	with pytest.raises(ApiError):
		ScheduledTask.parse_list([raw_task()])


def test_a_task_without_an_id_is_a_contract_break():
	raw = raw_task()
	del raw["id"]

	with pytest.raises(ApiError):
		ScheduledTask.parse(raw)


def test_a_task_without_a_name_is_a_contract_break():
	raw = raw_task()
	del raw["name"]

	with pytest.raises(ApiError):
		ScheduledTask.parse(raw)


# --------------------------------------------------------------------- client


@pytest.fixture
def scheduled_api(api):
	api.add_project(ORGANIZATION, PROJECT_UUID, name="Scheduled host")
	api.add_project(ORGANIZATION, OTHER_PROJECT_UUID, name="Somebody else's project")
	return api


@pytest.fixture
def scheduled_client(scheduled_api) -> ClaudeProjectsClient:
	return ClaudeProjectsClient(scheduled_api)


def test_listing_returns_every_task_in_the_organization(scheduled_api, scheduled_client):
	scheduled_api.add_scheduled_task(ORGANIZATION, name="One", project_uuid=PROJECT_UUID)
	scheduled_api.add_scheduled_task(ORGANIZATION, name="Two", project_uuid=OTHER_PROJECT_UUID)

	assert sorted(task.name for task in scheduled_client.list_scheduled_tasks()) == ["One", "Two"]


def test_listing_spans_every_organization_on_the_account(scheduled_api, scheduled_client):
	scheduled_api.add_organization("organization-2", name="Second", capabilities=["chat"])
	scheduled_api.add_scheduled_task(ORGANIZATION, name="Here")
	scheduled_api.add_scheduled_task("organization-2", name="Elsewhere")

	assert sorted(task.name for task in scheduled_client.list_scheduled_tasks()) == ["Elsewhere", "Here"]


def test_a_project_filter_keeps_only_that_projects_tasks(scheduled_api, scheduled_client):
	scheduled_api.add_scheduled_task(ORGANIZATION, name="Mine", project_uuid=PROJECT_UUID)
	scheduled_api.add_scheduled_task(ORGANIZATION, name="Theirs", project_uuid=OTHER_PROJECT_UUID)

	found = scheduled_client.scheduled_tasks_for_project(PROJECT_UUID)

	assert [task.name for task in found.matched] == ["Mine"]
	assert found.mapping_looks_broken is False


def test_a_project_with_no_tasks_is_not_a_broken_mapping(scheduled_api, scheduled_client):
	"""An organization with nothing scheduled anywhere is genuinely empty, not evidence the encoding moved."""
	found = scheduled_client.scheduled_tasks_for_project(PROJECT_UUID)

	assert found.matched == []
	assert found.mapping_looks_broken is False


def test_tasks_that_match_nothing_look_like_a_broken_mapping(scheduled_api, scheduled_client):
	"""Tasks exist in the organization but none resolve to this project.

	That is what a changed chat_project_id encoding would look like, and it must never be reported as a quiet empty list.
	"""
	scheduled_api.add_scheduled_task(ORGANIZATION, name="Unattached", project_uuid=None)

	found = scheduled_client.scheduled_tasks_for_project(PROJECT_UUID)

	assert found.matched == []
	assert found.mapping_looks_broken is True
	assert [task.name for task in found.in_organization] == ["Unattached"]


def test_a_task_is_fetched_by_id(scheduled_api, scheduled_client):
	task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Digest", project_uuid=PROJECT_UUID)

	assert scheduled_client.get_scheduled_task(task_id).name == "Digest"


def test_an_unknown_task_id_is_not_found(scheduled_client):
	with pytest.raises(NotFoundError):
		scheduled_client.get_scheduled_task("trig_nope")


def test_creating_a_task_sends_the_project_uuid(scheduled_api, scheduled_client):
	created = scheduled_client.create_scheduled_task(PROJECT_UUID, "Digest", "Summarise the week.")

	assert created.name == "Digest"
	assert created.prompt == "Summarise the week."
	assert created.project_uuid == PROJECT_UUID
	assert created.is_manual is True


def test_a_created_task_without_a_cron_is_manual(scheduled_client):
	created = scheduled_client.create_scheduled_task(PROJECT_UUID, "Digest", "Summarise.")

	assert created.cron_expression is None
	assert created.next_run_at is None


def test_a_cron_and_model_are_sent_on_create(scheduled_client):
	created = scheduled_client.create_scheduled_task(
		PROJECT_UUID,
		"Digest",
		"Summarise.",
		cron_expression="0 0 * * 1",
		model="claude-sonnet-5",
	)

	assert created.cron_expression == "0 0 * * 1"
	assert created.model == "claude-sonnet-5"
	assert created.next_run_at is not None


def test_an_invalid_cron_is_refused_by_the_api(scheduled_client):
	with pytest.raises(ApiError):
		scheduled_client.create_scheduled_task(PROJECT_UUID, "Digest", "Summarise.", cron_expression="every monday please")


def test_an_update_touches_only_the_fields_it_is_given(scheduled_api, scheduled_client):
	task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Before", prompt="Original.", project_uuid=PROJECT_UUID, cron_expression="0 0 * * 1")

	updated = scheduled_client.update_scheduled_task(task_id, name="After")

	assert updated.name == "After"
	assert updated.prompt == "Original."
	assert updated.cron_expression == "0 0 * * 1"


def test_an_update_can_pause_a_task_without_deleting_it(scheduled_api, scheduled_client):
	task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Noisy", project_uuid=PROJECT_UUID, cron_expression="0 0 * * 1")

	assert scheduled_client.update_scheduled_task(task_id, enabled=False).enabled is False
	assert scheduled_client.get_scheduled_task(task_id).enabled is False


def test_a_paused_task_can_be_resumed(scheduled_api, scheduled_client):
	task_id = scheduled_api.add_scheduled_task(ORGANIZATION, name="Quiet", project_uuid=PROJECT_UUID, enabled=False)

	assert scheduled_client.update_scheduled_task(task_id, enabled=True).enabled is True


def test_an_update_with_nothing_to_change_is_refused(scheduled_api, scheduled_client):
	task_id = scheduled_api.add_scheduled_task(ORGANIZATION, project_uuid=PROJECT_UUID)

	with pytest.raises(ValueError):
		scheduled_client.update_scheduled_task(task_id)


def test_deleting_a_task_reports_that_it_went(scheduled_api, scheduled_client):
	task_id = scheduled_api.add_scheduled_task(ORGANIZATION, project_uuid=PROJECT_UUID)

	assert scheduled_client.delete_scheduled_task(task_id) is True
	assert scheduled_api.scheduled_tasks == {}


def test_deleting_a_task_that_is_already_gone_is_not_a_failure(scheduled_api, scheduled_client):
	"""A teammate deleting it first reached the same end state, exactly as with documents."""
	task_id = scheduled_api.add_scheduled_task(ORGANIZATION, project_uuid=PROJECT_UUID)
	scheduled_client.delete_scheduled_task(task_id)

	assert scheduled_client.delete_scheduled_task(task_id) is False
