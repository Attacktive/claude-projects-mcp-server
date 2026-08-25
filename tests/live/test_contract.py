"""The canary for an undocumented API.

Skipped unless CLAUDE_PROJECTS_LIVE_TESTS=1.
Run it by hand when something breaks: it is the fastest way to tell "claude.ai changed" apart from "we have a bug", because it exercises the real endpoints end to end and nothing else in the suite touches the network.

The document and project it creates have unmistakable names and are deleted in finally blocks, so a failure mid-test leaves at most one obvious stray behind.
"""

import os

import pytest

from claude_projects_mcp.client import ClaudeProjectsClient
from claude_projects_mcp.config import Settings, load_env_file
from claude_projects_mcp.errors import NotFoundError
from claude_projects_mcp.transport import CurlCffiTransport

pytestmark = pytest.mark.live

CONTRACT_DOCUMENT = "__claude_projects_mcp_contract_test__.md"
CONTRACT_PROJECT = "__claude_projects_mcp_contract_test_project__"
DOCUMENT_HOST_PROJECT = "__claude_projects_mcp_contract_test_docs__"
CONTRACT_TASK = "__claude_projects_mcp_contract_test_task__"

# Monday at midnight UTC: days away whatever day this runs, so a task deleted moments later can never have fired.
CONTRACT_CRON = "0 0 * * 1"

skip_unless_live = pytest.mark.skipif(
	os.environ.get("CLAUDE_PROJECTS_LIVE_TESTS") != "1",
	reason="set CLAUDE_PROJECTS_LIVE_TESTS=1 (and CLAUDE_PROJECTS_SESSION_KEY) to hit the real API",
)


@pytest.fixture(scope="module")
def settings():
	# Same source the server itself uses, so running this proves the real configuration works rather than a separately exported one.
	load_env_file()
	return Settings.from_env(os.environ)


@pytest.fixture(scope="module")
def transport(settings):
	"""One connection for the whole module, like a running server has.

	A transport per test opens a new TLS connection each time, and a burst of those from one address gets challenged by Cloudflare often enough to make the suite flaky.
	"""
	instance = CurlCffiTransport(
		settings.session_key,
		base_url=settings.base_url,
		impersonate=settings.impersonate,
	)
	yield instance
	instance.close()


@pytest.fixture
def client(transport):
	# The client is per-test even though the connection is not, so one test's cached organization lookups cannot make another test pass.
	return ClaudeProjectsClient(transport)


@pytest.fixture
def project(client):
	"""A throwaway private project, so the document tests never touch the team's own.

	It used to borrow a project named in the environment, which meant running the suite wrote into a real shared one.
	Now that projects can be created, there is no reason to.
	"""
	created = client.create_project(
		DOCUMENT_HOST_PROJECT,
		description="contract test, safe to delete",
		is_private=True,
	)
	yield created.uuid
	client.delete_project(created.uuid)


@skip_unless_live
def test_the_account_has_a_chat_capable_org(client):
	"""If this fails with CloudflareBlockedError, upgrade curl_cffi."""
	assert client.list_organizations(), "no chat-capable organization on this account"


@skip_unless_live
def test_cowork_projects_are_classic_projects(client):
	"""The assumption the whole design rests on.

	Deliberately checks the account's own projects rather than one this suite created: the point is that the projects made in Cowork's UI are reachable at /organizations/{organization}/projects, which a self-made project could never demonstrate.
	"""
	assert client.list_projects(), "no projects visible on this account"


@skip_unless_live
def test_documents_can_be_listed(client, project):
	documents = client.list_documents(project)

	assert isinstance(documents, list)
	for document in documents:
		assert document.uuid and document.file_name


@skip_unless_live
def test_kb_stats_matches_document_token_counts(client, project):
	created = client.create_document(project, CONTRACT_DOCUMENT, "contract test content")
	try:
		stats = client.knowledge_stats(project)
		assert stats.size > 0
		assert stats.max_size > 0
		assert stats.search_threshold > 0
		assert isinstance(stats.search_mode, bool)

		docs = client.list_documents(project)
		total_tokens = sum(doc.estimated_token_count for doc in docs if doc.estimated_token_count is not None)
		assert stats.size == total_tokens
	finally:
		client.delete_document(project, created.uuid)


@skip_unless_live
def test_a_document_round_trips(client, project):
	"""Create, read back, replace, then delete — the full write path against the real API."""
	created = None
	try:
		created = client.create_document(project, CONTRACT_DOCUMENT, "first revision")
		assert created.uuid

		fetched = client.read_document(project, CONTRACT_DOCUMENT)
		assert fetched.content == "first revision"

		result = client.replace_document(project, CONTRACT_DOCUMENT, "second revision")
		assert result.action == "replaced"
		assert client.read_document(project, CONTRACT_DOCUMENT).content == "second revision"
		created = None
	finally:
		for stray in client.find_documents_by_name(project, CONTRACT_DOCUMENT):
			client.delete_document(project, stray.uuid)

		if created is not None:
			client.delete_document(project, created.uuid)

	assert client.find_documents_by_name(project, CONTRACT_DOCUMENT) == [], "contract test left a document behind — delete it in the web UI"


@skip_unless_live
def test_a_scheduled_task_round_trips(client, project):
	"""Create, read, schedule, pause, then delete — the full task path against the real API.

	Created manual-only, so the task cannot run at all until the update gives it a cron, and that cron is days out.
	The delete is in a finally block for the same reason every other contract test's is: a stray here is a task that would eventually run on its own.
	"""
	created = None
	try:
		created = client.create_scheduled_task(project, CONTRACT_TASK, "Contract test. Do nothing.")
		assert created.id
		assert created.is_manual, "a task created without a cron must not be scheduled"
		assert created.next_run_at is None, "the zero-time sentinel must not parse as a date"
		assert created.project_uuid == project, "chat_project_id must decode back to the project it was created against"

		fetched = client.get_scheduled_task(created.id)
		assert fetched.name == CONTRACT_TASK
		assert fetched.prompt == "Contract test. Do nothing.", "the prompt must still be reachable inside job_config"

		scheduled = client.update_scheduled_task(created.id, cron_expression=CONTRACT_CRON)
		assert scheduled.cron_expression == CONTRACT_CRON
		assert scheduled.next_run_at is not None, "a scheduled task must report when it next runs"

		paused = client.update_scheduled_task(created.id, enabled=False)
		assert paused.enabled is False, "the API omits `enabled` when false; absent must not parse as active"
		assert paused.cron_expression == CONTRACT_CRON, "pausing must not discard the schedule"

		assert client.get_scheduled_task(created.id).enabled is False, "the pause must survive a re-read"
	finally:
		for stray in client.list_scheduled_tasks():
			if stray.name == CONTRACT_TASK:
				client.delete_scheduled_task(stray.id)

	assert [task for task in client.list_scheduled_tasks() if task.name == CONTRACT_TASK] == [], "contract test left a scheduled task behind — delete it in the web UI"

	if created is not None:
		assert client.delete_scheduled_task(created.id) is False, "deleting something already gone is not a failure"


@skip_unless_live
def test_a_projects_scheduled_tasks_can_be_found_by_project(client, project):
	"""The derived chat_project_id has to match what the API really reports.

	This is the one assertion the offline suite cannot make honestly: there, both sides of the comparison come from our own encoder.
	"""
	created = None
	try:
		created = client.create_scheduled_task(project, CONTRACT_TASK, "Contract test. Do nothing.")

		found = client.scheduled_tasks_for_project(project)

		assert [task.id for task in found.matched] == [created.id]
		assert found.mapping_looks_broken is False, "the chat_project_id encoding no longer matches what claude.ai sends"
	finally:
		if created is not None:
			client.delete_scheduled_task(created.id)


@skip_unless_live
def test_a_project_round_trips(client):
	"""Create, read, update, then delete — the full project path against the real API.

	Created private, so a failure that outlives the finally block leaves a stray only the account owner can see rather than something the whole team notices.
	"""
	try:
		created = client.create_project(
			CONTRACT_PROJECT,
			description="contract test, safe to delete",
			is_private=True,
		)
		assert created.uuid

		# The create response carries no prompt_template, so instructions need the fetch.
		assert client.get_project(created.uuid).name == CONTRACT_PROJECT

		updated = client.update_project(
			created.uuid,
			description="second revision",
			instructions="contract test instructions",
		)
		assert updated.description == "second revision"

		refetched = client.get_project(created.uuid)
		assert refetched.instructions == "contract test instructions"
		assert refetched.name == CONTRACT_PROJECT, "a partial update must not blank the name"
	finally:
		for stray in client.list_projects():
			if stray.name == CONTRACT_PROJECT:
				client.delete_project(stray.uuid)

	with pytest.raises(NotFoundError):
		client.get_project(created.uuid)
