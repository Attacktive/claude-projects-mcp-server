"""Transport tests, run against a real localhost HTTP server.

curl_cffi does its I/O inside libcurl, invisible to `responses`, `respx`, `vcrpy`, and socket-level mockers alike.
A real server is the only honest way to test it, so these are deliberately the only socket-touching tests in the suite; everything above the transport uses the in-memory fake instead.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import curl_cffi
import pytest
from curl_cffi.requests.exceptions import Timeout
from werkzeug.wrappers import Response

from claude_projects_mcp import transport as transport_module
from claude_projects_mcp.errors import (
	ApiError,
	AuthExpiredError,
	CloudflareBlockedError,
	NotFoundError,
	RateLimitedError,
)
from claude_projects_mcp.transport import CurlCffiTransport

SESSION_KEY = "sk-ant-sid01-supersecret"

CHALLENGE_HTML = "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>Cloudflare Ray ID: 123</body></html>"


@pytest.fixture
def transport(httpserver):
	instance = CurlCffiTransport(SESSION_KEY, base_url=httpserver.url_for("/api"))
	yield instance
	instance.close()


@pytest.fixture
def captured(httpserver):
	"""Record the requests that actually reach the wire."""
	requests = []

	def record(request):
		requests.append(request)
		return Response(json.dumps({"ok": True}), status=200, content_type="application/json")

	httpserver.expect_request("/api/echo").respond_with_handler(record)
	return requests


def test_sends_the_session_key_as_a_cookie(transport, captured):
	transport.request("GET", "/echo")

	assert captured[0].cookies["sessionKey"] == SESSION_KEY


def test_sends_browser_origin_headers(transport, captured):
	"""claude.ai treats these as same-origin XHR; without them the request looks scripted."""
	transport.request("GET", "/echo")

	headers = captured[0].headers
	assert headers["Origin"] == "https://claude.ai"
	assert headers["Referer"].startswith("https://claude.ai")
	assert "application/json" in headers["Accept"]


def test_get_returns_parsed_json(transport, httpserver):
	httpserver.expect_request("/api/organizations").respond_with_json([{"uuid": "o1"}])

	assert transport.request("GET", "/organizations") == [{"uuid": "o1"}]


def test_post_sends_a_json_body(transport, httpserver):
	seen = {}

	def record(request):
		seen.update(request.get_json())
		return Response(json.dumps({"uuid": "d1"}), status=201, content_type="application/json")

	httpserver.expect_request("/api/docs", method="POST").respond_with_handler(record)
	result = transport.request("POST", "/docs", json_body={"file_name": "a.md", "content": "hi"})

	assert seen == {"file_name": "a.md", "content": "hi"}
	assert result == {"uuid": "d1"}


def test_patch_sends_a_partial_json_body(transport, httpserver):
	"""The scheduled-task API updates with PATCH, which no other endpoint in this client uses."""
	seen = {}

	def record(request):
		seen.update(request.get_json())
		return Response(json.dumps({"trigger": {"id": "trig_1"}}), status=200, content_type="application/json")

	httpserver.expect_request("/api/scheduled_tasks/trig_1", method="PATCH").respond_with_handler(record)
	result = transport.request("PATCH", "/scheduled_tasks/trig_1", json_body={"enabled": False})

	assert seen == {"enabled": False}
	assert result == {"trigger": {"id": "trig_1"}}


def test_upload_file_sends_multipart_form_data_the_way_the_web_ui_does(transport, httpserver):
	"""The upload request (captured 2026-09-26) is multipart form data with one part named `file` carrying the filename and the file's own content type, not the JSON the session defaults to."""
	seen = {}

	def record(request):
		part = request.files["file"]
		seen.update(content_type=request.content_type, filename=part.filename, part_type=part.content_type, data=part.read())
		return Response(json.dumps({"file_uuid": "f1"}), status=200, content_type="application/json")

	httpserver.expect_request("/api/projects/p1/upload", method="POST").respond_with_handler(record)
	result = transport.upload_file("/projects/p1/upload", file_name="1.png", data=b"\x89PNG\r\n", content_type="image/png")

	assert seen["content_type"].startswith("multipart/form-data; boundary=")
	assert seen["filename"] == "1.png"
	assert seen["part_type"] == "image/png"
	assert seen["data"] == b"\x89PNG\r\n"
	assert result == {"file_uuid": "f1"}


def test_upload_file_retries_a_challenge_on_a_cold_connection(transport, httpserver):
	"""A challenge page means Cloudflare answered instead of the API, so nothing was uploaded and the replay cannot duplicate it."""
	seen = _challenge_then(httpserver, "/api/projects/p1/upload", {"file_uuid": "f1"}, method="POST")

	assert transport.upload_file("/projects/p1/upload", file_name="1.png", data=b"\x89PNG", content_type="image/png") == {"file_uuid": "f1"}
	assert len(seen) == 2


def _challenge_then(httpserver, path, payload, challenges=1, method="GET"):
	"""Serve Cloudflare's challenge page `challenges` times, then the real answer."""
	seen = []

	def handler(request):
		seen.append(request.method)
		if len(seen) <= challenges:
			return Response(CHALLENGE_HTML, status=403, content_type="text/html")

		return Response(json.dumps(payload), status=200, content_type="application/json")

	httpserver.expect_request(path, method=method).respond_with_handler(handler)
	return seen


def test_a_challenge_on_a_cold_connection_is_retried(transport, httpserver):
	"""Cloudflare challenges a share of cold connections; the retry rides the warmed one."""
	seen = _challenge_then(httpserver, "/api/organizations", [{"uuid": "o1"}])

	assert transport.request("GET", "/organizations") == [{"uuid": "o1"}]
	assert len(seen) == 2


def test_a_challenged_write_is_replayed_because_it_never_arrived(transport, httpserver):
	"""A challenge page means Cloudflare answered, so nothing was created to duplicate."""
	seen = _challenge_then(httpserver, "/api/projects", {"uuid": "p1"}, method="POST")

	assert transport.request("POST", "/projects", json_body={"name": "x"}) == {"uuid": "p1"}
	assert len(seen) == 2


def test_a_persistent_challenge_still_reports_the_fingerprint(transport, httpserver):
	_challenge_then(httpserver, "/api/organizations", [], challenges=99)

	with pytest.raises(CloudflareBlockedError) as exception_info:
		transport.request("GET", "/organizations")

	assert "curl_cffi" in str(exception_info.value)


def test_a_proven_connection_does_not_retry_a_later_challenge(transport, httpserver):
	"""Once the API has answered, a challenge is real news rather than a cold start."""
	httpserver.expect_request("/api/organizations").respond_with_json([{"uuid": "o1"}])
	transport.request("GET", "/organizations")

	seen = _challenge_then(httpserver, "/api/docs", [], challenges=99)
	with pytest.raises(CloudflareBlockedError):
		transport.request("GET", "/docs")

	assert len(seen) == 1, "no retry once the connection has proven itself"


@pytest.mark.parametrize("warm_worker_first", [False, True])
def test_a_new_threads_connection_gets_its_own_cold_retry(transport, httpserver, warm_worker_first):
	httpserver.expect_request("/api/warm").respond_with_json([])
	cold = _challenge_then(httpserver, "/api/cold", [{"uuid": "o1"}])
	later = _challenge_then(httpserver, "/api/later", [], challenges=99)

	with ThreadPoolExecutor(max_workers=1) as worker:

		def on_worker(path):
			return worker.submit(transport.request, "GET", path).result(timeout=5)

		def on_main(path):
			return transport.request("GET", path)

		warm_request, cold_request = (on_worker, on_main) if warm_worker_first else (on_main, on_worker)
		assert warm_request("/warm") == []
		assert cold_request("/cold") == [{"uuid": "o1"}]
		assert len(cold) == 2

		with pytest.raises(CloudflareBlockedError, match="already answered"):
			cold_request("/later")
		assert len(later) == 1


def test_204_returns_none(transport, httpserver):
	httpserver.expect_request("/api/docs/d1", method="DELETE").respond_with_data("", status=204)

	assert transport.request("DELETE", "/docs/d1") is None


def test_empty_200_body_returns_none(transport, httpserver):
	httpserver.expect_request("/api/docs/d2", method="DELETE").respond_with_data("", status=200)

	assert transport.request("DELETE", "/docs/d2") is None


def test_401_is_auth_expired_with_recovery_instructions(transport, httpserver):
	httpserver.expect_request("/api/organizations").respond_with_json({"error": "unauthorized"}, status=401)

	with pytest.raises(AuthExpiredError) as exception_info:
		transport.request("GET", "/organizations")

	assert "sessionKey" in str(exception_info.value), "should say how to recover"


def test_403_with_json_body_is_auth_not_cloudflare(transport, httpserver):
	"""The API itself answered, so this is a permission problem, not bot protection."""
	httpserver.expect_request("/api/organizations").respond_with_json({"error": "forbidden"}, status=403)

	with pytest.raises(AuthExpiredError):
		transport.request("GET", "/organizations")


def test_403_with_html_body_is_cloudflare(transport, httpserver):
	httpserver.expect_request("/api/organizations").respond_with_data(CHALLENGE_HTML, status=403, content_type="text/html")

	with pytest.raises(CloudflareBlockedError) as exception_info:
		transport.request("GET", "/organizations")

	assert "curl_cffi" in str(exception_info.value), "should point at the fingerprint fix"


def test_404_is_not_found(transport, httpserver):
	httpserver.expect_request("/api/docs/gone").respond_with_json({}, status=404)

	with pytest.raises(NotFoundError):
		transport.request("GET", "/docs/gone")


def test_429_carries_retry_after(transport, httpserver):
	httpserver.expect_request("/api/organizations").respond_with_response(Response("slow down", status=429, headers={"Retry-After": "7"}))

	with pytest.raises(RateLimitedError) as exception_info:
		transport.request("GET", "/organizations")

	assert exception_info.value.retry_after == 7.0


def test_429_without_retry_after_leaves_it_none(transport, httpserver):
	httpserver.expect_request("/api/organizations").respond_with_data("slow down", status=429)

	with pytest.raises(RateLimitedError) as exception_info:
		transport.request("GET", "/organizations")

	assert exception_info.value.retry_after is None


def test_500_is_an_api_error_carrying_the_status(transport, httpserver):
	httpserver.expect_request("/api/organizations").respond_with_data("boom", status=500)

	with pytest.raises(ApiError) as exception_info:
		transport.request("GET", "/organizations")

	assert exception_info.value.status == 500
	assert "boom" in exception_info.value.body
	assert "boom" in str(exception_info.value)


def test_error_bodies_are_truncated(transport, httpserver):
	httpserver.expect_request("/api/organizations").respond_with_data("x" * 5000, status=500)

	with pytest.raises(ApiError) as exception_info:
		transport.request("GET", "/organizations")

	assert len(exception_info.value.body) < 1000
	assert "x" * 500 in str(exception_info.value)
	assert "x" * 501 not in str(exception_info.value)


def test_the_session_key_never_appears_in_an_error(transport, httpserver):
	"""A traceback reaching the model's context must not carry a live credential."""
	httpserver.expect_request("/api/organizations").respond_with_data(f"rejected token {SESSION_KEY} for this request", status=500)

	with pytest.raises(ApiError) as exception_info:
		transport.request("GET", "/organizations")

	assert SESSION_KEY not in exception_info.value.body
	assert SESSION_KEY not in str(exception_info.value)
	assert "sk-ant-sid01" not in exception_info.value.body
	assert "<redacted-session-key>" in str(exception_info.value)


def test_unparseable_success_body_is_an_api_error(transport, httpserver):
	"""A 200 of HTML means an interstitial, not data."""
	httpserver.expect_request("/api/organizations").respond_with_data("<html>nope</html>", status=200)

	with pytest.raises(ApiError):
		transport.request("GET", "/organizations")


PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"

# The download URL as the files listing hands it out (observed 2026-09-18): host-relative, and starting with the `/api` that the base URL already ends with.
ASSET = "/api/organization-1/files/file-1/document_pdf"


def test_request_bytes_returns_the_body_untouched(transport, httpserver):
	"""Served at /api/..., which is what the origin sees; appending to the base URL would ask for /api/api/... instead."""
	httpserver.expect_request(ASSET).respond_with_data(PDF, content_type="application/pdf")

	assert transport.request_bytes(ASSET) == PDF


def test_request_bytes_sends_the_session_key_and_accepts_any_content_type(transport, httpserver):
	seen = []

	def record(request):
		seen.append(request)
		return Response(PDF, status=200, content_type="application/pdf")

	httpserver.expect_request(ASSET).respond_with_handler(record)
	transport.request_bytes(ASSET)

	assert f"sessionKey={SESSION_KEY}" in seen[0].headers["Cookie"]
	assert seen[0].headers["Accept"] == "*/*"


def test_request_bytes_retries_a_challenge_on_a_cold_connection(transport, httpserver):
	seen = []

	def handler(request):
		seen.append(request.method)
		if len(seen) == 1:
			return Response(CHALLENGE_HTML, status=403, content_type="text/html")

		return Response(PDF, status=200, content_type="application/pdf")

	httpserver.expect_request(ASSET).respond_with_handler(handler)

	assert transport.request_bytes(ASSET) == PDF
	assert len(seen) == 2


def test_request_bytes_404_is_not_found(transport, httpserver):
	httpserver.expect_request(ASSET).respond_with_data("gone", status=404)

	with pytest.raises(NotFoundError):
		transport.request_bytes(ASSET)


def test_request_bytes_401_is_auth_expired(transport, httpserver):
	httpserver.expect_request(ASSET).respond_with_json({"error": "unauthorized"}, status=401)

	with pytest.raises(AuthExpiredError):
		transport.request_bytes(ASSET)


def test_request_bytes_500_is_an_api_error_carrying_the_status(transport, httpserver):
	httpserver.expect_request(ASSET).respond_with_data("boom", status=500)

	with pytest.raises(ApiError) as exception_info:
		transport.request_bytes(ASSET)

	assert exception_info.value.status == 500


def test_the_session_key_never_appears_in_a_download_error(transport, httpserver):
	httpserver.expect_request(ASSET).respond_with_data(f"leaked {SESSION_KEY}", status=500)

	with pytest.raises(ApiError) as exception_info:
		transport.request_bytes(ASSET)

	assert SESSION_KEY not in str(exception_info.value)
	assert SESSION_KEY not in exception_info.value.body


def test_request_bytes_rejects_an_html_page_served_as_a_file(transport, httpserver):
	"""A stale session can be answered with a 200 login page; treating that as the PDF would back up the page and lose the file."""
	httpserver.expect_request(ASSET).respond_with_data("<!DOCTYPE html><html><body>Log in</body></html>", status=200, content_type="text/html")

	with pytest.raises(ApiError) as exception_info:
		transport.request_bytes(ASSET)

	assert "HTML" in str(exception_info.value)


def test_request_bytes_refuses_a_url_on_another_origin(transport, httpserver):
	"""The session cookie rides every request, so a listing that hands out a foreign URL must not be followed with it."""
	with pytest.raises(ApiError) as exception_info:
		transport.request_bytes("https://files.example.invalid/api/organization-1/files/file-1/document_pdf")

	assert "origin" in str(exception_info.value)
	assert not httpserver.log, "nothing may be sent"


def test_request_bytes_refuses_a_scheme_relative_url_on_another_origin(transport, httpserver):
	"""`//host/path` looks relative and is not: urljoin resolves it onto the other host, so the check has to run on what was resolved rather than on what was handed out."""
	with pytest.raises(ApiError) as exception_info:
		transport.request_bytes("//files.example.invalid/api/organization-1/files/file-1/document_pdf")

	assert "origin" in str(exception_info.value)
	assert not httpserver.log, "nothing may be sent"


SHORT_TIMEOUT_SECONDS = 0.2
LONG_TIMEOUT_SECONDS = 30
SLOW_SECONDS = 0.4


@pytest.fixture
def slowly(httpserver):
	"""Build handlers that give the right answer, but only after the shortened timeout has passed, and leave the server idle afterwards.

	The server is shared by the whole session and answers one request at a time.
	A request whose client gave up would otherwise still be waiting there when the next test starts, and would reach that test's handler.
	"""
	released = threading.Event()

	def build(payload, content_type):
		def handler(request):
			released.wait(SLOW_SECONDS)

			return Response(payload, status=200, content_type=content_type)

		return handler

	yield build

	released.set()

	# This request joins the back of the queue, so its answer means every abandoned one has been served.
	httpserver.expect_request("/idle").respond_with_data("")
	curl_cffi.requests.get(httpserver.url_for("/idle"), timeout=LONG_TIMEOUT_SECONDS)


@pytest.fixture(params=["download", "upload"])
def slow_file_transfer(request, transport, httpserver, slowly):
	"""A download or an upload whose answer comes late, as a call that makes the transfer and checks what came back."""
	if request.param == "download":
		httpserver.expect_request(ASSET).respond_with_handler(slowly(PDF, "application/pdf"))

		def download():
			assert transport.request_bytes(ASSET) == PDF

		return download

	httpserver.expect_request("/api/projects/p1/upload", method="POST").respond_with_handler(slowly(json.dumps({"file_uuid": "f1"}), "application/json"))

	def upload():
		assert transport.upload_file("/projects/p1/upload", file_name="1.png", data=b"\x89PNG", content_type="image/png") == {"file_uuid": "f1"}

	return upload


@pytest.fixture
def short_request_timeout(monkeypatch):
	"""Shrink the limit a JSON call gets, so that outlasting it takes a fraction of a second instead of half a minute."""
	monkeypatch.setattr(transport_module, "_REQUEST_TIMEOUT_SECONDS", SHORT_TIMEOUT_SECONDS)


def test_a_file_transfer_is_not_cut_off_at_the_request_timeout(slow_file_transfer, short_request_timeout):
	"""A file takes as long as its size and the link allow, so the limit that fits a JSON call must not end a download or an upload."""
	slow_file_transfer()


def test_request_is_still_cut_off_at_the_request_timeout(transport, httpserver, slowly, short_request_timeout):
	"""The longer limit is for files alone: a JSON call that hangs is cut off at the request timeout."""
	httpserver.expect_request("/api/organizations").respond_with_handler(slowly("[]", "application/json"))

	with pytest.raises(ApiError) as exception_info:
		transport.request("GET", "/organizations")

	assert isinstance(exception_info.value.__cause__, Timeout)


def test_a_file_transfer_that_hangs_is_cut_off(slow_file_transfer, monkeypatch):
	"""The longer limit is still a limit: a download or an upload whose answer does not come in time ends."""
	monkeypatch.setattr(transport_module, "_FILE_TRANSFER_TIMEOUT_SECONDS", (SHORT_TIMEOUT_SECONDS / 2, SHORT_TIMEOUT_SECONDS / 2))

	with pytest.raises(ApiError) as exception_info:
		slow_file_transfer()

	assert isinstance(exception_info.value.__cause__, Timeout)


@pytest.mark.parametrize("pair", [(SHORT_TIMEOUT_SECONDS, LONG_TIMEOUT_SECONDS), (LONG_TIMEOUT_SECONDS, SHORT_TIMEOUT_SECONDS)])
def test_neither_number_of_the_pair_alone_cuts_a_file_transfer_off(slow_file_transfer, monkeypatch, pair):
	"""Without streaming, curl_cffi adds a (connect, read) pair into a cap on the whole transfer, so a transfer outlasts whichever number is the shorter."""
	monkeypatch.setattr(transport_module, "_FILE_TRANSFER_TIMEOUT_SECONDS", pair)

	slow_file_transfer()


def test_a_file_transfer_is_allowed_longer_than_a_json_call():
	"""The other tests each shorten one of the two limits, so only this one notices the real file limit falling back to the request one."""
	connect, read = transport_module._FILE_TRANSFER_TIMEOUT_SECONDS

	assert connect + read > transport_module._REQUEST_TIMEOUT_SECONDS
