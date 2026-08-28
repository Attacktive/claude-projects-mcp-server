"""The tools as an MCP client sees them.

`server.call_tool` exercises the real registration and schema path without any session plumbing, so these are end-to-end over the whole stack down to the fake API.
"""

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from claude_projects_mcp.client import ClaudeProjectsClient
from claude_projects_mcp.config import Settings
from claude_projects_mcp.errors import ApiError
from claude_projects_mcp.server import build_server

from .conftest import ORGANIZATION, PROJECT, call

pytestmark = pytest.mark.anyio


@pytest.fixture
def server(settings: Settings, client: ClaudeProjectsClient) -> MCPServer:
	return build_server(settings, client=client)


class TestRegistration:
	async def test_exposes_exactly_the_seventeen_tools(self, server):
		tools = await server.list_tools()

		assert {tool.name for tool in tools} == {
			"list_projects",
			"get_project",
			"create_project",
			"update_project",
			"delete_project",
			"list_documents",
			"read_document",
			"write_document",
			"rename_document",
			"delete_document",
			"pull_documents",
			"push_documents",
			"list_scheduled_tasks",
			"get_scheduled_task",
			"create_scheduled_task",
			"update_scheduled_task",
			"delete_scheduled_task",
		}

	async def test_every_tool_is_described(self, server):
		for tool in await server.list_tools():
			assert tool.description, f"{tool.name} has no description"

	async def test_every_tool_that_needs_a_project_demands_one(self, server):
		"""There is no default project, so nothing can act on the wrong one by omission.

		Scheduled tasks are addressed by their own id once they exist, so most of those tools never name a project at all; `list_scheduled_tasks` is the one place a project is optional, because leaving it out widens the search rather than picking a project on the caller's behalf.
		"""
		account_wide = {"list_projects", "create_project", "get_scheduled_task", "update_scheduled_task", "delete_scheduled_task"}
		project_is_a_filter = {"list_scheduled_tasks"}

		for tool in await server.list_tools():
			required = tool.input_schema.get("required") or []
			if tool.name in account_wide:
				assert "project_id" not in tool.input_schema["properties"], tool.name
			elif tool.name in project_is_a_filter:
				assert "project_id" in tool.input_schema["properties"], tool.name
				assert "project_id" not in required, tool.name
			else:
				assert "project_id" in required, tool.name

	async def test_destructive_tools_are_annotated_as_such(self, server):
		tools = {tool.name: tool for tool in await server.list_tools()}

		delete_annotations = tools["delete_document"].annotations
		assert delete_annotations is not None and delete_annotations.destructive_hint is True

		read_annotations = tools["read_document"].annotations
		assert read_annotations is not None and read_annotations.read_only_hint is True

		rename_annotations = tools["rename_document"].annotations
		assert rename_annotations is not None and rename_annotations.destructive_hint is False, "the destructive path is opt-in and backed up, like write_document's"

	async def test_every_tool_that_can_warn_asks_for_the_warning_to_be_relayed(self, server):
		"""The model is the only reader a tool result is guaranteed to have, so the description has to say what to do with a warning."""
		can_warn = {"list_documents", "read_document", "write_document", "rename_document", "push_documents", "list_scheduled_tasks", "create_scheduled_task", "update_scheduled_task", "delete_scheduled_task"}
		tools = {tool.name: tool for tool in await server.list_tools()}

		for name in can_warn:
			assert "relay" in tools[name].description.lower(), name

	async def test_the_server_instructions_ask_for_warnings_to_be_relayed(self, server):
		assert "warning" in server.instructions and "verbatim" in server.instructions


class TestProjectResolution:
	async def test_the_named_project_is_the_one_acted_on(self, api, server):
		api.add_project(ORGANIZATION, "project-2")

		result = await call(server, "list_documents", project_id="project-2")

		assert result["project_id"] == "project-2"

	async def test_an_unknown_project_is_a_tool_error(self, server):
		with pytest.raises(ToolError):
			await call(server, "list_documents", project_id="project-nope")


class TestListProjects:
	async def test_lists_projects_with_their_org(self, server):
		result = await call(server, "list_projects")

		assert result["projects"][0]["uuid"] == PROJECT
		assert result["projects"][0]["organization_uuid"] == ORGANIZATION

	async def test_includes_the_project_name(self, server):
		result = await call(server, "list_projects")

		assert result["projects"][0]["name"] == "팀 지식 베이스"


class TestListDocs:
	async def test_lists_documents(self, api, server):
		api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "list_documents", project_id=PROJECT)

		assert [document["file_name"] for document in result["documents"]] == ["notes.md"]

	async def test_reports_duplicate_names(self, api, server):
		"""A crash midway through a save leaves duplicates; every listing surfaces them."""
		api.add_document(PROJECT, "notes.md", "one")
		api.add_document(PROJECT, "notes.md", "two")

		result = await call(server, "list_documents", project_id=PROJECT)

		assert result["duplicate_file_names"] == ["notes.md"]

	async def test_no_duplicates_reports_an_empty_list(self, api, server):
		uuid = api.add_document(PROJECT, "notes.md", "one")

		result = await call(server, "list_documents", project_id=PROJECT)

		assert result["duplicate_file_names"] == []
		assert result["documents"] == [
			{
				"uuid": uuid,
				"file_name": "notes.md",
				"created_at": result["documents"][0]["created_at"],
				"characters": 3,
				"estimated_token_count": 3,
			}
		]

	async def test_list_documents_past_max_capacity_warns(self, api, server):
		api.projects[PROJECT]["_max_knowledge_size"] = 100
		api.add_document(PROJECT, "big.md", "a" * 150)

		result = await call(server, "list_documents", project_id=PROJECT)

		assert "past its maximum" in result["warning"]
		assert result["knowledge"]["size"] == 150

	async def test_search_mode_alone_is_not_a_list_documents_warning(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 50
		api.projects[PROJECT]["_max_knowledge_size"] = 200
		api.add_document(PROJECT, "notes.md", "a" * 80)

		result = await call(server, "list_documents", project_id=PROJECT)

		assert result["knowledge"]["search_mode"] is True
		assert "warning" not in result


class TestReadDoc:
	async def test_reads_by_name(self, api, server):
		api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "read_document", project_id=PROJECT, document="notes.md")

		assert result["content"] == "hello"

	async def test_reads_by_uuid(self, api, server):
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "read_document", project_id=PROJECT, document=uuid)

		assert result["content"] == "hello"

	async def test_a_missing_document_is_a_tool_error(self, server):
		with pytest.raises(ToolError):
			await call(server, "read_document", project_id=PROJECT, document="absent.md")

	async def test_duplicates_return_the_newest_and_warn(self, api, server):
		api.add_document(PROJECT, "notes.md", "older")
		newer = api.add_document(PROJECT, "notes.md", "newer")

		result = await call(server, "read_document", project_id=PROJECT, document="notes.md")

		assert result["content"] == "newer"
		assert result["uuid"] == newer
		assert "warning" in result and result["warning"]

	async def test_a_single_document_carries_no_warning(self, api, server):
		api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "read_document", project_id=PROJECT, document="notes.md")

		assert "warning" not in result


class TestWriteDoc:
	async def test_creates_a_new_document(self, api, server):
		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="hello")

		assert result["action"] == "created"
		assert api.content_of(PROJECT, "notes.md") == ["hello"]
		assert "warning" not in result

	async def test_a_name_without_an_extension_gets_a_warning(self, api, server):
		"""The web UI renders by extension, so an extension-less name silently comes out as plain text — invisible from a tool call, where nothing looks like a file."""
		result = await call(server, "write_document", project_id=PROJECT, file_name="notes", content="# hello")

		assert api.content_of(PROJECT, "notes") == ["# hello"], "the write itself still goes ahead"
		assert "extension" in result["warning"]
		assert "'notes.md'" in result["warning"]

	async def test_a_name_ending_in_a_period_is_also_extension_less(self, api, server):
		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.", content="# hello")

		assert "extension" in result["warning"]
		assert "'notes.md'" in result["warning"], "the suggestion must not double the period"

	async def test_a_warning_leads_the_result(self, api, server):
		"""First is where it gets read — after the success fields, everything already looks fine."""
		result = await call(server, "write_document", project_id=PROJECT, file_name="notes", content="# hello")

		assert next(iter(result)) == "warning"

	async def test_refuses_to_replace_without_overwrite(self, api, server):
		api.add_document(PROJECT, "notes.md", "precious")

		with pytest.raises(ToolError) as exception_info:
			await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="new")

		assert "overwrite" in str(exception_info.value)
		assert api.content_of(PROJECT, "notes.md") == ["precious"]

	async def test_replaces_when_overwrite_is_given(self, api, server):
		api.add_document(PROJECT, "notes.md", "old")

		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="new", overwrite=True)

		assert result["action"] == "replaced"
		assert api.content_of(PROJECT, "notes.md") == ["new"]

	async def test_the_replaced_content_is_backed_up(self, api, server, tmp_path):
		api.add_document(PROJECT, "notes.md", "the old text")

		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="new", overwrite=True)

		backup = tmp_path / "trash"
		written = [path for path in backup.rglob("*") if path.is_file()]
		assert len(written) == 1
		assert written[0].read_text(encoding="utf-8") == "the old text"
		assert result["backup_path"] == str(written[0])

	async def test_a_stale_expected_uuid_refuses_the_write(self, api, server):
		api.add_document(PROJECT, "notes.md", "a teammate saved this")

		with pytest.raises(ToolError) as exception_info:
			await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="mine", overwrite=True, expected_uuid="stale")

		assert "re-read" in str(exception_info.value)
		assert api.content_of(PROJECT, "notes.md") == ["a teammate saved this"]

	async def test_write_fits_carries_knowledge(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 500
		api.projects[PROJECT]["_max_knowledge_size"] = 2000

		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="hello")

		assert result["action"] == "created"
		assert result["knowledge"] == {
			"size": 5,
			"search_threshold": 500,
			"max_size": 2000,
			"search_mode": False,
		}

	async def test_write_crossing_threshold_is_refused_and_rolled_back(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 100
		api.add_document(PROJECT, "cand1.md", "a" * 80)
		api.add_document(PROJECT, "cand2.md", "b" * 10)

		with pytest.raises(ToolError) as exception_info:
			await call(server, "write_document", project_id=PROJECT, file_name="new.md", content="c" * 30)

		message = str(exception_info.value)
		assert "search threshold" in message
		assert "'cand1.md'" in message
		assert api.content_of(PROJECT, "new.md") == []
		assert any(method == "DELETE" and f"/organizations/{ORGANIZATION}/projects/{PROJECT}/docs/" in path for method, path in api.log)

	async def test_write_crossing_threshold_with_allow_search_mode(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 100

		result = await call(server, "write_document", project_id=PROJECT, file_name="new.md", content="a" * 120, allow_search_mode=True)

		assert result["action"] == "created"
		assert result["knowledge"]["search_mode"] is True
		assert "search mode" in result["warning"]

	async def test_write_crossing_cap_with_allow_search_mode_still_refused(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 100
		api.projects[PROJECT]["_max_knowledge_size"] = 150

		with pytest.raises(ToolError) as exception_info:
			await call(server, "write_document", project_id=PROJECT, file_name="new.md", content="a" * 200, allow_search_mode=True)

		assert "maximum" in str(exception_info.value)

	async def test_shrinking_overwrite_in_project_already_over_cap_is_admitted(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 50
		api.projects[PROJECT]["_max_knowledge_size"] = 100
		api.add_document(PROJECT, "notes.md", "a" * 150)

		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="a" * 20, overwrite=True)

		assert result["action"] == "replaced"
		assert api.content_of(PROJECT, "notes.md") == ["a" * 20]
		assert "warning" not in result

	async def test_502_on_kb_stats_succeeds_and_warns(self, api, server):
		api.add_document(PROJECT, "notes.md", "old content")
		api.fail_once("GET", r"/kb/stats$", ApiError("claude.ai returned HTTP 502.", status=502))

		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="new longer content", overwrite=True)

		assert result["action"] == "replaced"
		assert "knowledge" not in result
		assert "Capacity was not checked" in result["warning"]
		assert api.content_of(PROJECT, "notes.md") == ["new longer content"]

	async def test_write_crossing_threshold_refusal_candidate_ordering(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 100
		api.add_document(PROJECT, "dup.md", "a" * 10)
		api.add_document(PROJECT, "dup.md", "a" * 10)
		api.add_document(PROJECT, "large.md", "b" * 50)

		with pytest.raises(ToolError) as exception_info:
			await call(server, "write_document", project_id=PROJECT, file_name="new.md", content="c" * 80)

		message = str(exception_info.value)
		duplicate_index = message.find("dup.md")
		large_index = message.find("large.md")
		assert duplicate_index != -1 and large_index != -1
		assert duplicate_index < large_index

	async def test_small_growing_write_in_project_already_in_search_mode(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 50
		api.add_document(PROJECT, "big.md", "a" * 60)

		with pytest.raises(ToolError) as exception_info:
			await call(server, "write_document", project_id=PROJECT, file_name="small.md", content="hello")

		assert "already past" in str(exception_info.value)

	async def test_rollback_delete_fails_reports_done_with_warning(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = 50
		existing_uuid = api.add_document(PROJECT, "notes.md", "a" * 40)
		api.fail_once("DELETE", r"/docs/[^/]+$", ApiError("500", status=500))

		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="a" * 60, overwrite=True)

		warning = result["warning"]
		assert "could not be undone" in warning
		assert existing_uuid in warning
		assert result["uuid"] in warning
		assert len(api.documents[PROJECT]) == 2

	async def test_kb_stats_404_warns_capacity_not_checked(self, api, server):
		api.projects[PROJECT]["_search_threshold"] = None
		api.projects[PROJECT]["_max_knowledge_size"] = None

		result = await call(server, "write_document", project_id=PROJECT, file_name="notes.md", content="hello")

		assert "knowledge" not in result
		assert "Capacity was not checked" in result["warning"]


class TestRenameDocument:
	async def test_renames_a_document(self, api, server):
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "rename_document", project_id=PROJECT, document="notes.md", new_file_name="plan.md")

		assert api.document_names(PROJECT) == ["plan.md"]
		assert api.content_of(PROJECT, "plan.md") == ["hello"]
		assert result["old_uuid"] == uuid
		assert result["new_file_name"] == "plan.md"
		assert "warning" not in result

	async def test_a_new_name_without_an_extension_gets_a_warning(self, api, server):
		api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "rename_document", project_id=PROJECT, document="notes.md", new_file_name="plan")

		assert api.document_names(PROJECT) == ["plan"], "the rename itself still goes ahead"
		assert "extension" in result["warning"]
		assert "'plan.md'" in result["warning"]

	async def test_the_source_is_backed_up(self, api, server, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "rename_document", project_id=PROJECT, document="notes.md", new_file_name="plan.md")

		written = [path for path in (tmp_path / "trash").rglob("*") if path.is_file()]
		assert len(written) == 1
		assert written[0].read_text(encoding="utf-8") == "hello"
		assert result["backup_paths"] == [str(written[0])]

	async def test_a_taken_name_is_a_tool_error_naming_the_occupant(self, api, server):
		api.add_document(PROJECT, "notes.md", "hello")
		occupant = api.add_document(PROJECT, "plan.md", "occupied")

		with pytest.raises(ToolError) as exception_info:
			await call(server, "rename_document", project_id=PROJECT, document="notes.md", new_file_name="plan.md")

		message = str(exception_info.value)
		assert occupant in message and "overwrite" in message

	async def test_overwrite_replaces_the_occupant(self, api, server):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_document(PROJECT, "plan.md", "doomed")

		result = await call(server, "rename_document", project_id=PROJECT, document="notes.md", new_file_name="plan.md", overwrite=True)

		assert api.content_of(PROJECT, "plan.md") == ["hello"]
		assert len(result["replaced_uuids"]) == 1

	async def test_renaming_to_the_same_name_is_a_tool_error(self, api, server):
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		with pytest.raises(ToolError, match="already named"):
			await call(server, "rename_document", project_id=PROJECT, document=uuid, new_file_name="notes.md")

		assert api.document_names(PROJECT) == ["notes.md"]

	async def test_an_ambiguous_source_is_a_tool_error_listing_the_candidates(self, api, server):
		first = api.add_document(PROJECT, "notes.md", "one")
		second = api.add_document(PROJECT, "notes.md", "two")

		with pytest.raises(ToolError) as exception_info:
			await call(server, "rename_document", project_id=PROJECT, document="notes.md", new_file_name="plan.md")

		message = str(exception_info.value)
		assert first in message and second in message

	async def test_a_missing_source_is_a_tool_error(self, server):
		with pytest.raises(ToolError, match="absent.md"):
			await call(server, "rename_document", project_id=PROJECT, document="absent.md", new_file_name="plan.md")


class TestDeleteDoc:
	async def test_deletes_by_uuid(self, api, server):
		uuid = api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "delete_document", project_id=PROJECT, document=uuid)

		assert api.document_names(PROJECT) == []
		assert result["deleted"][0]["uuid"] == uuid

	async def test_deletes_by_unambiguous_name(self, api, server):
		api.add_document(PROJECT, "notes.md", "hello")

		await call(server, "delete_document", project_id=PROJECT, document="notes.md")

		assert api.document_names(PROJECT) == []

	async def test_backs_up_before_deleting(self, api, server, tmp_path):
		api.add_document(PROJECT, "notes.md", "about to vanish")

		result = await call(server, "delete_document", project_id=PROJECT, document="notes.md")

		written = [path for path in (tmp_path / "trash").rglob("*") if path.is_file()]
		assert written[0].read_text(encoding="utf-8") == "about to vanish"
		assert result["backup_path"] == str(written[0])

	async def test_an_ambiguous_name_refuses_and_lists_the_candidates(self, api, server):
		"""Deleting is the most dangerous tool; it must not guess."""
		first = api.add_document(PROJECT, "notes.md", "one")
		second = api.add_document(PROJECT, "notes.md", "two")

		with pytest.raises(ToolError) as exception_info:
			await call(server, "delete_document", project_id=PROJECT, document="notes.md")

		message = str(exception_info.value)
		assert first in message and second in message
		assert len(api.document_names(PROJECT)) == 2

	async def test_a_missing_document_is_a_tool_error(self, server):
		with pytest.raises(ToolError):
			await call(server, "delete_document", project_id=PROJECT, document="absent.md")


class TestPullDocs:
	async def test_writes_files_and_summarises(self, api, server, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")
		destination = tmp_path / "out"

		result = await call(server, "pull_documents", project_id=PROJECT, destination_directory=str(destination))

		assert (destination / "notes.md").read_text(encoding="utf-8") == "hello"
		assert result["summary"] == {"written": 1}

	async def test_reports_per_file_results(self, api, server, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")

		result = await call(server, "pull_documents", project_id=PROJECT, destination_directory=str(tmp_path / "out"))

		assert result["results"][0]["file_name"] == "notes.md"
		assert result["results"][0]["status"] == "written"


class TestPushDocs:
	async def test_uploads_and_summarises(self, api, server, tmp_path):
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")

		result = await call(server, "push_documents", project_id=PROJECT, source_directory=str(tmp_path))

		assert api.content_of(PROJECT, "notes.md") == ["hello"]
		assert result["summary"] == {"created": 1}

	async def test_dry_run_changes_nothing(self, api, server, tmp_path):
		(tmp_path / "notes.md").write_text("hello", encoding="utf-8")

		result = await call(server, "push_documents", project_id=PROJECT, source_directory=str(tmp_path), dry_run=True)

		assert api.document_names(PROJECT) == []
		assert result["summary"] == {"created": 1}

	async def test_a_missing_directory_is_a_tool_error(self, server, tmp_path):
		with pytest.raises(ToolError) as exception_info:
			await call(server, "push_documents", project_id=PROJECT, source_directory=str(tmp_path / "nope"))

		assert "nope" in str(exception_info.value)

	async def test_push_documents_lifts_refusal_into_warning(self, api, server, tmp_path):
		api.projects[PROJECT]["_search_threshold"] = 50
		(tmp_path / "a.md").write_text("a" * 10, encoding="utf-8")
		(tmp_path / "b.md").write_text("b" * 100, encoding="utf-8")

		result = await call(server, "push_documents", project_id=PROJECT, source_directory=str(tmp_path))

		assert "warning" in result
		assert "search threshold" in result["warning"]


class TestProjectTools:
	async def test_creates_a_project(self, api, server):
		result = await call(server, "create_project", name="Nový projekt", description="shared notes")

		assert result["uuid"] in api.projects
		assert result["name"] == "Nový projekt"

	async def test_creates_a_project_with_instructions(self, server):
		result = await call(server, "create_project", name="Guided", instructions="Answer in Korean.")

		assert result["instructions"] == "Answer in Korean."

	async def test_a_new_project_can_be_private(self, server):
		assert (await call(server, "create_project", name="Mine", is_private=True))["is_private"] is True

	async def test_reads_one_project_including_its_instructions(self, api, server):
		api.projects[PROJECT]["prompt_template"] = "Be brief."

		result = await call(server, "get_project", project_id=PROJECT)

		assert result["uuid"] == PROJECT
		assert result["instructions"] == "Be brief."
		assert result["organization_uuid"] == ORGANIZATION

	async def test_renames_a_project(self, api, server):
		result = await call(server, "update_project", project_id=PROJECT, name="Renamed")

		assert api.projects[PROJECT]["name"] == "Renamed"
		assert result["name"] == "Renamed"

	async def test_updating_nothing_says_what_to_pass(self, server):
		with pytest.raises(ToolError) as exception_info:
			await call(server, "update_project", project_id=PROJECT)

		message = str(exception_info.value)
		assert "name" in message and "description" in message and "instructions" in message

	async def test_deleting_needs_the_name_typed_back(self, api, server):
		with pytest.raises(ToolError) as exception_info:
			await call(server, "delete_project", project_id=PROJECT, confirm_name="wrong")

		assert PROJECT in api.projects, "a mistyped confirmation must change nothing"
		assert "팀 지식 베이스" in str(exception_info.value), "should quote the name it wanted"

	async def test_deletes_the_project_when_the_name_matches(self, api, server):
		result = await call(server, "delete_project", project_id=PROJECT, confirm_name="팀 지식 베이스")

		assert PROJECT not in api.projects
		assert result["deleted"]["uuid"] == PROJECT

	async def test_every_document_is_backed_up_before_the_project_goes(self, api, server, tmp_path):
		api.add_document(PROJECT, "notes.md", "hello")
		api.add_document(PROJECT, "plan.md", "later")

		result = await call(server, "delete_project", project_id=PROJECT, confirm_name="팀 지식 베이스")

		saved = {Path(path).read_text(encoding="utf-8") for path in result["backup_paths"]}
		assert saved == {"hello", "later"}

	async def test_a_failed_backup_leaves_the_project_standing(self, api, server):
		"""The backup is the precondition for deleting, not a courtesy afterwards."""
		api.add_document(PROJECT, "notes.md", "hello")
		api.fail_once("GET", "/docs$", ApiError("claude.ai returned HTTP 500.", status=500))

		with pytest.raises(ToolError):
			await call(server, "delete_project", project_id=PROJECT, confirm_name="팀 지식 베이스")

		assert PROJECT in api.projects

	async def test_an_empty_project_deletes_without_backups(self, api, server):
		result = await call(server, "delete_project", project_id=PROJECT, confirm_name="팀 지식 베이스")

		assert result["backup_paths"] == []
		assert PROJECT not in api.projects

	async def test_listed_projects_report_their_privacy(self, server):
		listed = (await call(server, "list_projects"))["projects"]

		assert all("is_private" in project for project in listed)


class TestErrorTranslation:
	async def test_an_expired_session_says_how_to_recover(self, api, server):
		from claude_projects_mcp.errors import AuthExpiredError

		api.fail_once("GET", "/organizations$", AuthExpiredError("claude.ai rejected the session key. Copy a fresh sessionKey cookie."))

		with pytest.raises(ToolError) as exception_info:
			await call(server, "list_projects")

		assert "sessionKey" in str(exception_info.value)
