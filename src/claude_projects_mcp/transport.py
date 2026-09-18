"""HTTP to claude.ai, and the only place in the package that knows about status codes.

Everything above this module deals in typed errors and parsed JSON.
That boundary is what keeps the claude.ai-specific auth swappable if an official API ever arrives.
"""

import json
from collections.abc import Callable
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests

from .config import DEFAULT_BASE_URL, DEFAULT_IMPERSONATE
from .errors import (
	ApiError,
	AuthExpiredError,
	ClaudeProjectsError,
	CloudflareBlockedError,
	NotFoundError,
	RateLimitedError,
)

# The subset of curl_cffi's accepted methods this client ever sends.
# PATCH is only used by the scheduled-task endpoints; projects update with PUT.
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

_MAX_BODY_CHARACTERS = 500

_AUTH_HELP = "claude.ai rejected the session key (HTTP {status}). It has probably expired: copy a fresh sessionKey cookie from claude.ai (DevTools -> Application -> Cookies), update CLAUDE_PROJECTS_SESSION_KEY, and restart the MCP server."

_CLOUDFLARE_HELP_COLD = "claude.ai's bot protection answered instead of the API (HTTP {status}, HTML challenge page), twice in a row on a fresh connection. A single challenge on a cold connection is normal and was retried automatically, so a second suggests the impersonated browser fingerprint has aged out — upgrade curl_cffi (`uv lock --upgrade-package curl-cffi && uv sync`). Failing that, wait a few minutes before retrying rather than looping, or the address may be blocked outright. This is not an authentication problem; re-copying the cookie will not help."

_CLOUDFLARE_HELP_ESTABLISHED = "claude.ai's bot protection answered instead of the API (HTTP {status}, HTML challenge page) on a connection the API had already answered. That is not a cold-start challenge, so it was not retried; it usually means the address is being rate-limited or blocked. Wait a few minutes before trying again rather than looping. If it keeps happening, upgrade curl_cffi (`uv lock --upgrade-package curl-cffi && uv sync`). This is not an authentication problem; re-copying the cookie will not help."


class Transport(Protocol):
	"""The seam the rest of the package is written against."""

	def request(self, method: HttpMethod, path: str, *, json_body: dict | None = None) -> Any: ...

	def request_bytes(self, url: str) -> bytes:
		"""GET a host-relative URL, as the files listing hands them out, and return the body untouched.

		Resolved against the base URL's origin rather than appended to its path: the URLs already start with the `/api` that the default base URL ends with.
		A base URL override that carries a path of its own therefore loses that path here, while `request` keeps it; the setting is documented as choosing the host, not a prefix.
		"""
		...

	def close(self) -> None: ...


def redact(text: str, session_key: str) -> str:
	"""Strip the credential from anything that might reach a log or a traceback."""
	if not text:
		return text

	cleaned = text.replace(session_key, "<redacted-session-key>")

	# Belt and braces: catch any other token-shaped string, in case the server echoes a variant of the key rather than the exact value.
	import re

	return re.sub(r"sk-ant-[A-Za-z0-9_-]+", "<redacted-session-key>", cleaned)


def _origin_of(url: str) -> tuple[str, str]:
	parts = urlsplit(url)
	return parts.scheme.lower(), parts.netloc.lower()


def _looks_like_html(body: str, content_type: str) -> bool:
	if "html" in content_type.lower():
		return True

	return body.lstrip()[:200].lower().startswith(("<!doctype", "<html"))


class CurlCffiTransport:
	"""curl_cffi session with a browser TLS fingerprint.

	The fingerprint matters as much as the headers: claude.ai sits behind Cloudflare, which rejects the TLS handshake of ordinary Python HTTP clients before any header is read.
	"""

	def __init__(
		self,
		session_key: str,
		base_url: str = DEFAULT_BASE_URL,
		impersonate: str = DEFAULT_IMPERSONATE,
	):
		self._session_key = session_key
		self._base_url = base_url.rstrip("/")

		# Whether this connection has ever got an answer from the API rather than from Cloudflare.
		# Until it has, one challenge is treated as a cold start, not a verdict.
		self._established = False
		self._session = requests.Session(impersonate=impersonate)
		self._session.headers.update(
			{
				"Accept": "application/json",
				"Content-Type": "application/json",
				"Origin": "https://claude.ai",
				"Referer": "https://claude.ai/",
				# Set as a plain header rather than through the cookie jar, which would apply curl's domain matching and drop the cookie for any non-claude.ai base_url.
				"Cookie": f"sessionKey={session_key}",
			}
		)

	def request(self, method: HttpMethod, path: str, *, json_body: dict | None = None) -> Any:
		return self._with_cold_retry(lambda: self._send(method, path, json_body=json_body))

	def request_bytes(self, url: str) -> bytes:
		return self._with_cold_retry(lambda: self._send_bytes(url))

	def _with_cold_retry(self, send: Callable[[], Any]) -> Any:
		try:
			return send()
		except CloudflareBlockedError:
			if self._established:
				raise

			# Cloudflare challenges a share of cold connections regardless of what the request is (observed 2026-08-06: sometimes the opening GET, sometimes a POST).
			# The retry rides the connection that the challenge itself set up.
			# Replaying a write is safe here precisely because a challenge page means Cloudflare answered instead of the API, so nothing was created or deleted.
			# That reasoning holds only for this error: no other failure may be retried.
			return send()

	def _send(self, method: HttpMethod, path: str, *, json_body: dict | None = None) -> Any:
		response = self._perform(method, f"{self._base_url}{path}", json=json_body)
		self._check_status(response)
		return self._decode_json(response)

	def _send_bytes(self, url: str) -> bytes:
		# The files listing hands out host-relative URLs that already start with the `/api` the base URL ends with (observed 2026-09-18), so they resolve against the origin rather than appending to the path.
		resolved = urljoin(self._base_url, url)

		# The session cookie is a plain header on every request this session sends, so a listing that ever hands out a foreign URL must not be asked for with it.
		# A redirect issued by claude.ai itself is still followed, and libcurl drops the header on a host change; only what we send first is guarded here.
		if _origin_of(resolved) != _origin_of(self._base_url):
			raise ApiError(f"Refusing to fetch {url!r}: it is on a different origin from {self._base_url}, and the session key must not travel there.", status=0)

		# The session's Accept header asks for JSON, which is not what a PDF is.
		response = self._perform("GET", resolved, headers={"Accept": "*/*"})
		self._check_status(response)

		# A stale session can be answered with a 200 login page; as a file, that would be backed up in place of the real one.
		content = response.content
		if _looks_like_html(content[:200].decode("utf-8", errors="replace"), response.headers.get("content-type", "")):
			raise ApiError("Expected a file from claude.ai but got an HTML page. This usually means a login or interstitial page was served instead of the file; the session key may have expired.", status=response.status_code)

		return content

	def _perform(self, method: HttpMethod, url: str, **options: Any) -> Any:
		try:
			return self._session.request(method, url, timeout=30, **options)
		except ClaudeProjectsError:
			raise
		except Exception as exception:
			raise ApiError(
				f"Could not reach claude.ai: {self._safe(str(exception))}",
				status=0,
			) from exception

	def close(self) -> None:
		self._session.close()

	def _safe(self, text: str) -> str:
		return redact(text, self._session_key)[:_MAX_BODY_CHARACTERS]

	def _check_status(self, response: Any) -> None:
		"""Raise the typed error for any status that is not a success, and mark the connection proven otherwise.

		The body is only decoded as text on an error, so a successful download never pays for decoding a binary as UTF-8.
		"""
		self._raise_for_challenge(response)

		# Past that check the API itself replied, even if it replied with an error, so the connection is proven and any later challenge is a real one rather than a cold start.
		self._established = True

		self._raise_for_error(response)

	def _raise_for_challenge(self, response: Any) -> None:
		# An HTML body on a 401/403 is a challenge page: Cloudflare answered instead of the API.
		# A JSON body means the API answered, so it is about the credential.
		status = response.status_code
		if status not in (401, 403):
			return

		if not _looks_like_html(self._safe(response.text or ""), response.headers.get("content-type", "")):
			return

		# On a cold connection the first challenge never escapes — `request` retries it — so the cold message can honestly say "twice in a row".
		if self._established:
			raise CloudflareBlockedError(_CLOUDFLARE_HELP_ESTABLISHED.format(status=status))

		raise CloudflareBlockedError(_CLOUDFLARE_HELP_COLD.format(status=status))

	def _raise_for_error(self, response: Any) -> None:
		status = response.status_code
		if status in (401, 403):
			raise AuthExpiredError(_AUTH_HELP.format(status=status))

		if status == 404:
			raise NotFoundError(f"Not found (HTTP 404): {response.url}")

		if status == 429:
			raise RateLimitedError(
				"claude.ai rate-limited the request (HTTP 429).",
				retry_after=_parse_retry_after(response.headers.get("retry-after")),
			)

		if status >= 400:
			raise ApiError(f"claude.ai returned HTTP {status}.", status=status, body=self._safe(response.text or ""))

	def _decode_json(self, response: Any) -> Any:
		if not response.text or not response.text.strip():
			return None

		try:
			return json.loads(response.text)
		except ValueError as exception:
			content_type = response.headers.get("content-type", "")
			raise ApiError(
				f"Expected JSON from claude.ai but got {content_type or 'an unparseable body'}. This usually means an interstitial or login page was served instead of the API.",
				status=response.status_code,
				body=self._safe(response.text or ""),
			) from exception


def _parse_retry_after(value: str | None) -> float | None:
	if not value:
		return None

	try:
		return float(value)
	except ValueError:
		# The header also permits an HTTP date.
		# Backing off by a default beats crashing.
		return None
