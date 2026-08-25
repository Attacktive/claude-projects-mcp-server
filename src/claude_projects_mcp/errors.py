"""Typed failures, each carrying enough context for a tool to tell the user what to do.

Every error that crosses into the MCP layer should be one of these, so `server.py` can translate it into actionable text instead of surfacing a bare HTTP status.
"""


class ClaudeProjectsError(Exception):
	"""Base for everything this package raises deliberately."""


class ConfigError(ClaudeProjectsError):
	"""Environment is missing or malformed."""


class UnsafePathError(ClaudeProjectsError):
	"""A document name would have escaped the directory it was meant to be written into."""


class BackupError(ClaudeProjectsError):
	"""Could not preserve content that is about to be replaced or deleted.

	Always fatal to the operation that raised it: without the backup there is nothing to recover from, and the API offers no undo.
	"""


class AuthExpiredError(ClaudeProjectsError):
	"""The session key was rejected. They expire; a fresh one must be copied."""


class CloudflareBlockedError(ClaudeProjectsError):
	"""Bot protection rejected the request before it reached the API.

	Distinct from AuthExpiredError because the fix is different: upgrade curl_cffi so a newer browser fingerprint is impersonated, rather than re-copying the cookie.
	"""


class RateLimitedError(ClaudeProjectsError):
	def __init__(self, message: str, retry_after: float | None = None):
		super().__init__(message)
		self.retry_after = retry_after


class NotFoundError(ClaudeProjectsError):
	"""The organization, project, or document does not exist (or is not visible to this account)."""


class ApiError(ClaudeProjectsError):
	"""An unexpected response. `body` is already truncated and redacted."""

	def __init__(self, message: str, status: int, body: str = ""):
		super().__init__(message)
		self.status = status
		self.body = body


class DocExistsError(ClaudeProjectsError):
	"""A write would replace an existing document without `overwrite` being set."""

	def __init__(self, message: str, file_name: str, uuid: str):
		super().__init__(message)
		self.file_name = file_name
		self.uuid = uuid


class AmbiguousDocError(ClaudeProjectsError):
	"""A file name matches several documents, and the operation needs exactly one."""

	def __init__(self, message: str, file_name: str, uuids: list[str]):
		super().__init__(message)
		self.file_name = file_name
		self.uuids = uuids


class ConcurrentEditError(ClaudeProjectsError):
	"""The document changed since it was read, so the write was refused.

	Every save mints a new uuid, so a uuid that no longer matches means somebody else saved in the meantime.
	"""

	def __init__(self, message: str, file_name: str, expected_uuid: str, actual_uuid: str | None):
		super().__init__(message)
		self.file_name = file_name
		self.expected_uuid = expected_uuid
		self.actual_uuid = actual_uuid


class KnowledgeFullError(ClaudeProjectsError):
	"""A write was refused because the project knowledge capacity or search threshold was exceeded."""

	def __init__(self, message: str, file_name: str, verdict: str, projected: int, limit: int):
		super().__init__(message)
		self.file_name = file_name
		self.verdict = verdict
		self.projected = projected
		self.limit = limit
