# Claude Projects MCP Server

An MCP server that gives Claude Code read and write access to Claude Cowork / claude.ai projects, the knowledge documents ("Context" files) inside them, and the scheduled tasks that run against them.

Cowork keeps a team's shared documents inside the web UI, where Claude Code cannot see them.
This server closes that gap, so notes written in Cowork can be read, edited, and written back from the terminal — and the projects that hold them can be created, renamed, and retired without leaving the editor.

> **Unofficial.** This uses the same undocumented endpoints the claude.ai browser app uses, authenticated with a `sessionKey` cookie.
> Anthropic can change or break them without notice.
> Endpoint shapes were derived from [guidodinello/claude-client](https://github.com/guidodinello/claude-client).

## Status

All seventeen tools are implemented and covered by 349 tests, and the full read-and-write path has been verified against the real API — `tests/live/test_contract.py` round-trips a document through create, read, replace, and delete, a project through create, read, update, and delete, and a scheduled task through create, read, schedule, pause, and delete.
That live suite also checks the derived `chat_project_id` against what claude.ai really sends, which is the one thing the offline tests cannot prove: there, both sides of the comparison come from this repository's own encoder.

What that established, and what the implementation now relies on:

- A Cowork project is a classic claude.ai project, at `/organizations/{organization}/projects/{uuid}`.
  There is no separate Cowork API surface.
- Organization and document listings are bare JSON arrays with no pagination envelope.
- Projects are listed through `projects_v2`, which wraps its rows in `{data, pagination}`.
  That envelope is the reason for preferring it: a bare array cannot distinguish "that is all of them" from "that is the first thirty", so a truncated answer would be indistinguishable from a complete one.
  The client walks every page.
- Document listings include `content`, so reading costs one request rather than two.
- Two documents may share a `file_name`, which is what lets a save create the replacement before deleting the original rather than the other way round.
- Projects have no such limitation: `PUT` takes a partial body, so an update touches only the fields it is given.
- Project instructions (`prompt_template`) come back only from a single-project fetch — never from a listing or a create response, which is why `get_project` exists separately.

Scheduled tasks (observed 2026-08-08) sit at `/organizations/{organization}/cowork/scheduled_tasks` and behave unlike anything else here:

- They are **organization-scoped, not project-scoped**, and the listing ignores every query parameter it is given — three spellings of a project filter returned byte-identical bodies. Narrowing to one project happens client-side.
- A task names its project as a `chat_project_id` (`claude_proj_01…`) and never as a uuid, though it is *created* with a `project_uuid`. The two are the same value in different clothes: `claude_proj_01` followed by the uuid in base58, left-padded to 22 characters. `identifiers.py` is that mapping, and it is the one piece of this server that reimplements somebody else's encoding rather than reading a field.
- Schedules are **cron expressions in UTC**. The web UI's Manual / Hourly / Daily / Weekdays / Weekly menu is presentation: choosing Weekly, Monday, 09:00 in a UTC+9 browser sends `0 0 * * 1`. A task with no schedule omits the field rather than carrying an empty one.
- `enabled` is **absent when false**. A paused task has no `enabled` key at all, so anything defaulting it to true reports every paused task as running.
- `next_run_at` is `0001-01-01T00:00:00Z` — Go's zero time — for a task that has no schedule, and carries a few minutes of scheduler jitter otherwise.
- The API validates a cron expression (400 on nonsense) but **not** a model id: an invented one is stored with a 200 and only fails when the task runs. `create_scheduled_task` warns about a model that does not look like an id rather than refusing, so a model newer than this code still works.

Response shapes captured from the real API live in `tests/fixtures/` and are asserted against by `tests/test_fixtures.py`, which stops the in-memory fake drifting away from what claude.ai actually sends.

## Setup

Everything below runs through [`uv`](https://docs.astral.sh/uv/) — `uvx` ships with it — so install that first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Other installation methods (Windows, Homebrew, pip) are in [uv's installation documentation](https://docs.astral.sh/uv/getting-started/installation/).

Get the session key from claude.ai in a browser: DevTools → Application → Cookies → `sessionKey` (starts with `sk-ant-sid02-`, or `sid01-` on older accounts).
It expires periodically; when a tool reports a 401, copy a fresh one.

Put it in an env file anywhere (`.env.example` lists every key the server reads):

```bash
CLAUDE_PROJECTS_SESSION_KEY=sk-ant-sid02-...
```

Register with Claude Code straight from this repository — no clone needed:

```bash
claude mcp add claude-projects -- uvx --env-file /path/to/.env --from 'git+https://github.com/Attacktive/claude-projects-mcp-server@main' claude-projects-mcp
```

`uv` caches the commit it resolves, so a later push to `main` is not picked up automatically; run `uv cache clean claude-projects-mcp-server` to update, or pin a tag instead of `@main` to move only on releases.

### From a local checkout (development)

```bash
uv sync
cp .env.example .env   # then fill in CLAUDE_PROJECTS_SESSION_KEY
claude mcp add claude-projects -- uv run --directory /path/to/claude-projects-mcp-server claude-projects-mcp
```

Run this way, the server finds the `.env` sitting next to the project by itself; `--env-file` is only needed for the git-URL form, whose install location is an ephemeral virtual environment managed by `uv`.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_projects` | Projects across every chat-capable organization, each tagged with its organization |
| `get_project` | One project, including its instructions — which a listing does not carry |
| `create_project` | Start a project, optionally private and with instructions |
| `update_project` | Change name, description, or instructions; untouched fields are left alone |
| `delete_project` | Remove a project **and every document in it** (see Safety) |
| `list_documents` | Documents in a project, flagging any duplicate file names |
| `read_document` | One document by uuid or file name |
| `write_document` | Create, or replace with `overwrite=true` |
| `rename_document` | Move a document to a new file name; a name already in use needs `overwrite=true` |
| `delete_document` | Remove a document (always backed up first) |
| `pull_documents` | Copy a project's documents into a local folder |
| `push_documents` | Upload a local folder's documents into a project |
| `list_scheduled_tasks` | Scheduled tasks, for one project or the whole account |
| `get_scheduled_task` | One task, including the prompt it will send |
| `create_scheduled_task` | Schedule a prompt against a project, or leave it manual-only |
| `update_scheduled_task` | Change a task, or pause it with `enabled=false` |
| `delete_scheduled_task` | Remove a task (**not** backed up first — see Safety) |

Every tool that acts on a project takes an explicit `project_id`; only `list_projects` and `create_project` are account-wide.
Scheduled tasks are addressed by their own `task_id` once they exist, so only `create_scheduled_task` names a project; on `list_scheduled_tasks` a `project_id` narrows the listing and omitting it widens the search to the account.
Start with `list_projects` to find the uuid, or take it from the URL: `https://claude.ai/cowork/project/<this-part>`.

The session key is the only required setting; `CLAUDE_PROJECTS_BACKUP_DIRECTORY`, `CLAUDE_PROJECTS_BASE_URL`, and `CLAUDE_PROJECTS_IMPERSONATE` optionally override where backups land, which host is spoken to, and which browser fingerprint `curl_cffi` presents.
Which organization owns a project is worked out by searching — one listing per organization per session, cached — so a project is reachable wherever on the account it lives, and nothing can be pointed at the wrong place.

## Safety

These are shared team documents, and the API has no server-side undo, so:

- replacing an existing document requires an explicit `overwrite=true`
- the previous content is written to a local backup directory *before* any replacement
- `write_document` accepts an `expected_uuid` to refuse the write if a teammate changed the document since you read it
- `rename_document` re-creates the content under the new name before deleting the original — the API has no rename, so a crash midway leaves the document under both names rather than under none
- `push_documents` never deletes remote documents that are missing locally — it is not a mirror

**Scheduled tasks are the deliberate exception to the backup rule.**
`delete_scheduled_task` writes nothing to the backup directory before deleting, because a task is a name, a prompt, and a cron line — config that is cheap to retype — rather than content that cannot be reconstructed.
If you only want a task to stop running, `update_scheduled_task` with `enabled=false` pauses it and keeps both the prompt and the schedule, which is nearly always the better move.

Running a task is not exposed at all.
The API has an endpoint for it, but starting a billable Claude run is not something a tool call should be able to do by accident; set a schedule and let Cowork run it, or press the button in the web UI.

`delete_project` is the sharpest tool here, because it takes every document with it.
It is deliberately awkward: `confirm_name` must match the project's current name exactly, and every document is copied to the backup directory before anything is deleted.
If that copy fails for any document, the project is left standing.

**The backup directory is not an undo feature.**
It captures only what *this tool* overwrites, it lives on one machine, and it knows nothing about edits made by teammates in the web UI.
One known gap: when an interrupted save has left several documents sharing a name, a replacing write backs up only the newest before removing them all — if a teammate may have edited an older duplicate, check it with `read_document` first, as its warning suggests.
Do not describe it to the team as a safety net.

### Credentials

`CLAUDE_PROJECTS_SESSION_KEY` is a full personal claude.ai account credential — it can read every conversation on the account and act as you.
Keep it in `.env` (git-ignored), never share it, and never deploy a hosted instance that serves several people from one key.

## Development

```bash
uv run pytest tests/ -v                              # full suite, no network
CLAUDE_PROJECTS_LIVE_TESTS=1 uv run pytest tests/live -v      # real round-trip against claude.ai
uv run ruff check .
```

Tests never touch the network except `tests/transport` (a local HTTP server) and `tests/live` (opt-in).
Everything else runs against an in-memory fake of the API.
