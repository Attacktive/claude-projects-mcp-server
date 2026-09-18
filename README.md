# Claude Projects MCP Server

An MCP server that gives Claude Code read and write access to Claude Cowork / claude.ai projects, the knowledge documents ("Context" files) inside them, and the scheduled tasks that run against them.

Cowork keeps a team's shared documents inside the web UI, where Claude Code cannot see them.
This server closes that gap, so notes written in Cowork can be read, edited, and written back from the terminal — and the projects that hold them can be created, renamed, and retired without leaving the editor.

> **Unofficial.** This uses the same undocumented endpoints the claude.ai browser app uses, authenticated with a `sessionKey` cookie.
> Anthropic can change or break them without notice.
> Endpoint shapes were derived from [guidodinello/claude-client](https://github.com/guidodinello/claude-client).

## Status

All seventeen tools are implemented and covered by 438 tests, and the read-and-write path for text documents, projects, and scheduled tasks has been verified against the real API — `tests/live/test_contract.py` round-trips a document through create, read, replace, and delete, a project through create, read, update, and delete, and a scheduled task through create, read, schedule, pause, and delete.
Files uploaded through the web UI, such as PDFs, are listed, pulled, and backed up before a deletion, but that path is built against a captured response shape and has not yet been run against a real upload (see To do).
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

Uploaded files (observed 2026-09-18) sit at `/organizations/{organization}/projects/{uuid}/files`, a sibling of `/docs`, and are a different kind of thing from a document:

- The listing is a bare JSON array like the others.
  Each entry carries `file_name`, `file_kind` (`document` for a PDF), `created_at`, `size_bytes`, the same value under `uuid` and `file_uuid`, and a `document_asset` with `page_count` but a null `token_count`, so nothing in it can be added up to the knowledge size.
- They **count toward `knowledge_size` without appearing in the documents listing**.
  Found while copying nine projects between two organizations: in eight of them the documents' summed `estimated_token_count` matched the reported knowledge size exactly, and in the ninth the two Markdown documents summed to 13,757 against 19,641, with two PDF files in the web UI to account for the gap.
  That exact match is why `list_documents` treats a shortfall with no listed upload to explain it as a warning rather than rounding.
- `document_asset.url` is `/api/{organization}/files/{file_uuid}/document_pdf`: host-relative, outside the project path, and already starting with the `/api` that the base URL ends with.
  The transport resolves it against the origin rather than appending it, refuses any other origin so the session key never travels, reads the body as bytes rather than JSON, and rejects an HTML page served in its place, which is what a stale session gets.
  The client then checks the byte count against `size_bytes`, the one thing the listing says about the file's contents.
- Only a `document_asset` whose `file_variant` is `original` counts as the file.
  A PDF has one; an image has a preview and a thumbnail, which are renditions, so it is listed but can be neither pulled nor backed up.
- Nothing here can upload one.
  The endpoint the web UI uses for that has not been observed, so `push_documents` carries text documents only.

Response shapes captured from the real API live in `tests/fixtures/` and are asserted against by `tests/test_fixtures.py`, which stops the in-memory fake drifting away from what claude.ai actually sends.

## To do

- The download path has not been run against a real upload.
  Listing, `pull_documents`, and the pre-delete backup are built against the captured listing shape and the in-memory fake; `tests/live/test_contract.py` has opt-in checks that list a throwaway project's uploads and download the first real one on the account.
  Run them with `CLAUDE_PROJECTS_LIVE_TESTS=1` against an account holding a PDF before trusting a pull or a deletion with uploads in it.
- Uploaded files cannot be written from here.
  Adding or replacing a PDF means the web UI until the upload endpoint is observed, and a pull-and-push copy therefore carries the documents only.
- Uploads that are not documents cannot be read either.
  An image offers only a preview and a thumbnail, neither of which is the file, so `pull_documents` reports it as an error and `delete_project` refuses until it is removed in the web UI; only a PDF has been observed so far, so what other kinds offer is unknown.
- Capacity refusals rank documents only.
  `capacity.py` cannot name an upload worth removing, so a project that is full because of uploads is told to compact the wrong things, or that there is nothing else to compact; `list_documents` at least shows the uploads with their sizes.

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
claude mcp add claude-projects -- uvx --refresh --env-file /path/to/.env --from 'git+https://github.com/Attacktive/claude-projects-mcp-server@main' claude-projects-mcp
```

`--refresh` makes `uv` re-resolve `main` every time the server starts, so relaunching Claude Code picks up whatever has been pushed since — at the cost of a git fetch on each launch, and of needing the network for it.
Without it, `uv` keeps the commit it first resolved until `uv cache clean claude-projects-mcp-server` is run.
To move only on releases instead, drop `--refresh` and pin a tag in place of `@main`.

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
| `delete_project` | Remove a project **and everything in it**; every document and uploaded file is backed up first (see Safety) |
| `list_documents` | Documents and uploaded files in a project, reporting knowledge capacity usage and flagging duplicate file names |
| `read_document` | One document by uuid or file name |
| `write_document` | Create, or replace with `overwrite=true` (gated by knowledge capacity) |
| `rename_document` | Move a document to a new file name; a name already in use needs `overwrite=true` |
| `delete_document` | Remove a document (always backed up first) |
| `pull_documents` | Copy a project's documents and uploaded files into a local folder |
| `push_documents` | Upload a local folder's text documents into a project (gated by knowledge capacity); uploaded files cannot be pushed (see To do) |
| `list_scheduled_tasks` | Scheduled tasks, for one project or the whole account |
| `get_scheduled_task` | One task, including the prompt it will send |
| `create_scheduled_task` | Schedule a prompt against a project, or leave it manual-only |
| `update_scheduled_task` | Change a task, or pause it with `enabled=false` |
| `delete_scheduled_task` | Remove a task (**not** backed up first — see Safety) |

Every tool that acts on a project takes an explicit `project_id`; only `list_projects` and `create_project` are account-wide.
Scheduled tasks are addressed by their own `task_id` once they exist, so only `create_scheduled_task` names a project; on `list_scheduled_tasks` a `project_id` narrows the listing and omitting it widens the search to the account.
Start with `list_projects` to find the uuid, or take it from the URL: `https://claude.ai/cowork/project/<this-part>`.

Give documents a file extension: the web UI picks its renderer by name, so `notes` displays as plain text where `notes.md` renders as markdown.
`write_document` and `rename_document` warn when a name has none — a bare trailing period counts as none — and suggest the `.md` form; the write itself still goes ahead.

A result carries a `warning` key only when there is something to hear, and it comes first.
The model is the only reader a tool result is guaranteed to have, so the tool descriptions and the server instructions tell it to relay any `warning` to the user verbatim — nothing else in the chain will.

The session key is the only required setting; `CLAUDE_PROJECTS_BACKUP_DIRECTORY`, `CLAUDE_PROJECTS_BASE_URL`, and `CLAUDE_PROJECTS_IMPERSONATE` optionally override where backups land, which host is spoken to, and which browser fingerprint `curl_cffi` presents.
Which organization owns a project is worked out by searching — one listing per organization per session, cached — so a project is reachable wherever on the account it lives, and nothing can be pointed at the wrong place.

### Capacity

A project's knowledge has two lines:

- **Search threshold** (`project_knowledge_search_threshold`): past this line, Claude in the web UI retrieves from the knowledge instead of reading all of it, so documents can go unseen.
- **Maximum capacity** (`max_knowledge_size`): past this line, the web UI refuses further uploads until content is removed or compacted.

The API enforces neither line on writes, so this server enforces them:

- A write that would grow the project past a line is undone and refused, naming up to three candidate documents most worth compacting (duplicates first, then by size and age).
- Passing `allow_search_mode=true` accepts crossing the search threshold (with a warning); nothing accepts exceeding the maximum capacity.
- `list_documents` reports current knowledge capacity usage under the `knowledge` key, and the uploaded files that count toward it under `uploaded_files`.
- Uploaded files are never named as compaction candidates, because only the web UI can remove one (see To do).

## Safety

These are shared team documents, and the API has no server-side undo, so:

- replacing an existing document requires an explicit `overwrite=true`
- the previous content is written to a local backup directory *before* any replacement
- `write_document` accepts an `expected_uuid` to refuse the write if a teammate changed the document since you read it
- `rename_document` re-creates the content under the new name before deleting the original — the API has no rename, so a crash midway leaves the document under both names rather than under none
- `push_documents` never deletes remote documents that are missing locally — it is not a mirror
- `pull_documents` copies a project's uploaded files, such as PDFs, down as bytes, but `push_documents` cannot send them back, so a pull-and-push copy carries the documents only (see To do)

**Scheduled tasks are the deliberate exception to the backup rule.**
`delete_scheduled_task` writes nothing to the backup directory before deleting, because a task is a name, a prompt, and a cron line — config that is cheap to retype — rather than content that cannot be reconstructed.
If you only want a task to stop running, `update_scheduled_task` with `enabled=false` pauses it and keeps both the prompt and the schedule, which is nearly always the better move.

Running a task is not exposed at all.
The API has an endpoint for it, but starting a billable Claude run is not something a tool call should be able to do by accident; set a schedule and let Cowork run it, or press the button in the web UI.

`delete_project` is the sharpest tool here, because it takes every document and every uploaded file with it.
It is deliberately awkward: `confirm_name` must match the project's current name exactly, and every text document and every uploaded file is copied to the backup directory before anything is deleted.
If that copy fails for any of them, or the files listing itself cannot be fetched, the project is left standing.
An upload with no downloadable original blocks the deletion too: an image offers only a preview, which is a rendition rather than the file, so remove it in the web UI first or delete the project there.

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
