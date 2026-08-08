"""Transport tests, run against a real localhost HTTP server.

curl_cffi does its I/O inside libcurl, invisible to `responses`, `respx`, `vcrpy`, and socket-level mockers alike.
A real server is the only honest way to test it, so these are deliberately the only socket-touching tests in the suite; everything above the transport uses the in-memory fake instead.
"""

import json

import pytest
from werkzeug.wrappers import Response

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


def test_error_bodies_are_truncated(transport, httpserver):
	httpserver.expect_request("/api/organizations").respond_with_data("x" * 5000, status=500)

	with pytest.raises(ApiError) as exception_info:
		transport.request("GET", "/organizations")

	assert len(exception_info.value.body) < 1000


def test_the_session_key_never_appears_in_an_error(transport, httpserver):
	"""A traceback reaching the model's context must not carry a live credential."""
	httpserver.expect_request("/api/organizations").respond_with_data(f"rejected token {SESSION_KEY} for this request", status=500)

	with pytest.raises(ApiError) as exception_info:
		transport.request("GET", "/organizations")

	assert SESSION_KEY not in exception_info.value.body
	assert SESSION_KEY not in str(exception_info.value)
	assert "sk-ant-sid01" not in exception_info.value.body


def test_unparseable_success_body_is_an_api_error(transport, httpserver):
	"""A 200 of HTML means an interstitial, not data."""
	httpserver.expect_request("/api/organizations").respond_with_data("<html>nope</html>", status=200)

	with pytest.raises(ApiError):
		transport.request("GET", "/organizations")
