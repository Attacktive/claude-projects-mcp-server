# Claude Projects MCP Server

An MCP server that gives Claude Code read and write access to Claude Cowork / claude.ai projects and the knowledge documents ("Context" files) inside them.

Cowork keeps a team's shared documents inside the web UI, where Claude Code cannot see them.
This server closes that gap, so notes written in Cowork can be read, edited, and written back from the terminal — and the projects that hold them can be created, renamed, and retired without leaving the editor.

> **Unofficial.** This uses the same undocumented endpoints the claude.ai browser app uses, authenticated with a `sessionKey` cookie.
> Anthropic can change or break them without notice.
> Endpoint shapes were derived from [guidodinello/claude-client](https://github.com/guidodinello/claude-client).

## Status

All eleven tools are implemented and covered by 246 tests, and the full read-and-write path has been verified against the real API — `tests/live/test_contract.py` round-trips a document through create, read, replace, and delete, and a project through create, read, update, and delete.

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

Response shapes captured from the real API live in `tests/fixtures/` and are asserted against by `tests/test_fixtures.py`, which stops the in-memory fake drifting away from what claude.ai actually sends.

## Setup

```bash
uv sync
cp .env.example .env   # then fill in CLAUDE_PROJECTS_SESSION_KEY
```

Get the session key from claude.ai in a browser: DevTools → Application → Cookies → `sessionKey` (starts with `sk-ant-sid02-`, or `sid01-` on older accounts).
It expires periodically; when a tool reports a 401, copy a fresh one.

Register with Claude Code:

```bash
claude mcp add claude-projects -- uv run --directory /path/to/claude-projects-mcp-server claude-projects-mcp
```

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
| `delete_document` | Remove a document (always backed up first) |
| `pull_documents` | Copy a project's documents into a local folder |
| `push_documents` | Upload a local folder's documents into a project |

Every tool that acts on a project takes an explicit `project_id`; only `list_projects` and `create_project` are account-wide.
Start with `list_projects` to find the uuid, or take it from the URL: `https://claude.ai/cowork/project/<this-part>`.

The session key is the only required setting; `CLAUDE_PROJECTS_BACKUP_DIRECTORY`, `CLAUDE_PROJECTS_BASE_URL`, and `CLAUDE_PROJECTS_IMPERSONATE` optionally override where backups land, which host is spoken to, and which browser fingerprint `curl_cffi` presents.
Which organization owns a project is worked out by searching — one listing per organization per session, cached — so a project is reachable wherever on the account it lives, and nothing can be pointed at the wrong place.

## Safety

These are shared team documents, and the API has no server-side undo, so:

- replacing an existing document requires an explicit `overwrite=true`
- the previous content is written to a local backup directory *before* any replacement
- `write_document` accepts an `expected_uuid` to refuse the write if a teammate changed the document since you read it
- `push_documents` never deletes remote documents that are missing locally — it is not a mirror

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
