"""``avdfleet gui`` contract: start-if-needed daemon + browser launcher.

The command is exercised with the network-facing seams replaced (health probe,
background server, health wait, browser opener and the Ctrl+C wait), so no
socket is ever bound and no real browser opens.  The assertions pin the four
fixed behaviours from BRIEF section2:

* a healthy daemon is reused (no second daemon is started);
* otherwise the same serve implementation is started, polled until healthy and
  the browser is opened at http://host:port/;
* ``--no-browser`` and ``--json`` keep the browser closed;
* failures (remote bind, startup timeout, daemon crash) are clear and non-zero.
"""

from __future__ import annotations

import json

import pytest

from avdfleet.cli.commands import gui as gui_module

from . import invoke, invoke_json, output_text, prepare_env


@pytest.fixture()
def env(monkeypatch, tmp_path):
    return prepare_env(monkeypatch, tmp_path)


class _Recorder:
    """Callable that records its calls and returns a fixed value."""

    def __init__(self, result=True):
        self.calls: list[tuple[tuple, dict]] = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _FakeServer:
    """Minimal stand-in for gui._BackgroundServer."""

    def __init__(self, error: str = "") -> None:
        self.errors: list[BaseException] = []
        self._error = error
        self.join_calls = 0

    def start(self):
        return self

    def is_alive(self) -> bool:
        return True

    def join(self, timeout=None) -> None:
        self.join_calls += 1

    def error_message(self) -> str:
        return self._error


def _arm_started_daemon(
    monkeypatch,
    *,
    healthy: bool = True,
    interrupted: bool = True,
    opened: bool = True,
    server: _FakeServer | None = None,
) -> tuple[_Recorder, _Recorder, _Recorder]:
    """Patch gui's seams for the "no daemon yet" path; return the recorders."""
    fake_server = server if server is not None else _FakeServer()
    monkeypatch.setattr(gui_module, "_probe_health", lambda url, timeout_s=1.0: False)
    monkeypatch.setattr(gui_module, "_build_daemon_app", lambda context: "fake-app")
    starter = _Recorder(result=fake_server)
    monkeypatch.setattr(gui_module, "_start_background_server", starter)
    monkeypatch.setattr(
        gui_module, "_wait_until_healthy", lambda url, *, timeout_s, server: healthy
    )
    shutdown = _Recorder(result=interrupted)
    monkeypatch.setattr(gui_module, "_wait_for_shutdown", shutdown)
    opener = _Recorder(result=opened)
    monkeypatch.setattr(gui_module, "_open_browser", opener)
    # Default: the one-time-code exchange fails, so the normal GUI URL is opened.
    monkeypatch.setattr(gui_module, "_daemon_token", lambda paths: "fake-daemon-token")
    monkeypatch.setattr(
        gui_module,
        "_request_bootstrap_code",
        lambda base_url, token, timeout_s=10.0: (None, "unavailable in this test"),
    )
    return starter, shutdown, opener


def _arm_auto_auth(monkeypatch, code: str = "one-time-code-0123456789"):
    """Patch the bootstrap seams so the browser is opened with a sign-in link."""
    monkeypatch.setattr(gui_module, "_daemon_token", lambda paths: "fake-daemon-token")

    class _CodeRequester:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple, dict]] = []

        def __call__(self, base_url, token, timeout_s=10.0):
            self.calls.append(((base_url, token), {"timeout_s": timeout_s}))
            return code, ""

    requester = _CodeRequester()
    monkeypatch.setattr(gui_module, "_request_bootstrap_code", requester)
    return requester


def test_gui_starts_daemon_waits_for_health_and_opens_browser(monkeypatch, env) -> None:
    starter, shutdown, opener = _arm_started_daemon(monkeypatch)
    result = invoke(["gui", "--host", "127.0.0.1", "--port", "8901"])
    assert result.exit_code == 0, output_text(result)
    text = output_text(result)
    assert "http://127.0.0.1:8901" in text
    assert "daemon started in the background" in text
    assert "Press Ctrl+C to stop" in text
    # The serve implementation is reused: one server started on the requested address.
    assert len(starter.calls) == 1
    args, kwargs = starter.calls[0]
    assert args[0] == "fake-app"
    assert kwargs == {"host": "127.0.0.1", "port": 8901}
    assert opener.calls[0][0][0] == "http://127.0.0.1:8901/"
    assert shutdown.calls  # stays alive until Ctrl+C
    # The token (or any hint of its value) is never printed.
    assert "token" not in text.lower()


def test_gui_reuses_running_daemon_without_starting_one(monkeypatch, env) -> None:
    monkeypatch.setenv("AVDFLEET_CONTAINER", "tests.cli:broken_container_factory")
    monkeypatch.setattr(gui_module, "_probe_health", lambda url, timeout_s=1.0: True)
    monkeypatch.setattr(
        gui_module, "_start_background_server", lambda *a, **k: pytest.fail("must not start a daemon")
    )
    monkeypatch.setattr(
        gui_module, "_wait_for_shutdown", lambda server: pytest.fail("must not keep the process alive")
    )
    opener = _Recorder()
    monkeypatch.setattr(gui_module, "_open_browser", opener)
    monkeypatch.setattr(gui_module, "_daemon_token", lambda paths: "fake-daemon-token")
    monkeypatch.setattr(
        gui_module,
        "_request_bootstrap_code",
        lambda base_url, token, timeout_s=10.0: (None, "unavailable in this test"),
    )
    result = invoke(["gui"])
    assert result.exit_code == 0, output_text(result)
    text = output_text(result)
    assert "already running" in text
    expected_url = f"http://{env.config.api_host}:{env.config.api_port}/"
    assert opener.calls[0][0][0] == expected_url
    assert "token" not in text.lower()


def test_gui_json_reports_url_without_opening_browser(monkeypatch, env) -> None:
    monkeypatch.setattr(gui_module, "_probe_health", lambda url, timeout_s=1.0: True)
    monkeypatch.setattr(
        gui_module, "_start_background_server", lambda *a, **k: pytest.fail("must not start a daemon")
    )
    opener = _Recorder()
    monkeypatch.setattr(gui_module, "_open_browser", opener)
    result, payload = invoke_json(["gui", "--port", "8899"])
    assert result.exit_code == 0, output_text(result)
    assert payload["url"] == "http://127.0.0.1:8899/"
    assert payload["already_running"] is True
    assert payload["started"] is False
    assert payload["opened"] is False
    assert payload["healthy"] is True
    assert opener.calls == []


def test_gui_no_browser_keeps_daemon_running(monkeypatch, env) -> None:
    _, shutdown, opener = _arm_started_daemon(monkeypatch)
    result = invoke(["gui", "--no-browser"])
    assert result.exit_code == 0, output_text(result)
    assert "Press Ctrl+C to stop" in output_text(result)
    assert opener.calls == []
    assert shutdown.calls


def test_gui_startup_timeout_is_environment_error(monkeypatch, env) -> None:
    _, shutdown, _ = _arm_started_daemon(monkeypatch, healthy=False)
    result = invoke(["--json", "gui"])
    assert result.exit_code == 3, output_text(result)
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "environment_error"
    assert "did not become healthy" in payload["error"]["message"]
    assert shutdown.calls == []


def test_gui_refuses_to_start_remote_daemon(monkeypatch, env) -> None:
    monkeypatch.setattr(gui_module, "_probe_health", lambda url, timeout_s=1.0: False)
    monkeypatch.setattr(
        gui_module, "_start_background_server", lambda *a, **k: pytest.fail("must not bind remotely")
    )
    result = invoke(["--json", "gui", "--host", "0.0.0.0", "--port", "8902"])
    assert result.exit_code == 2, output_text(result)
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"
    assert "allow-remote" in payload["error"]["hint"]
    assert "non-loopback" in payload["error"]["message"]


def test_gui_daemon_crash_exits_environment_error(monkeypatch, env) -> None:
    server = _FakeServer(error="uvicorn failed: address already in use")
    _arm_started_daemon(monkeypatch, interrupted=False, server=server)
    result = invoke(["gui"])
    assert result.exit_code == 3, output_text(result)
    assert "address already in use" in output_text(result)


def test_gui_invalid_port_is_usage_error(env) -> None:
    result = invoke(["--json", "gui", "--port", "70000"])
    assert result.exit_code == 2, output_text(result)
    assert json.loads(result.stdout)["error"]["code"] == "usage_error"


def test_gui_opens_one_time_bootstrap_link(monkeypatch, env) -> None:
    _, shutdown, opener = _arm_started_daemon(monkeypatch)
    requester = _arm_auto_auth(monkeypatch, code="bootstrap-code-abc123")
    result = invoke(["gui", "--port", "8903"])
    assert result.exit_code == 0, output_text(result)
    assert requester.calls, "the CLI should request a one-time code"
    base_url, token = requester.calls[0][0]
    assert base_url == "http://127.0.0.1:8903"
    assert token == "fake-daemon-token"
    browser_url = opener.calls[0][0][0]
    assert browser_url == "http://127.0.0.1:8903/auth/bootstrap#code=bootstrap-code-abc123"
    text = output_text(result)
    assert "one-time sign-in link" in text
    assert "bootstrap-code-abc123" not in text
    assert "fake-daemon-token" not in text
    assert shutdown.calls


def test_gui_no_auto_auth_opens_plain_url(monkeypatch, env) -> None:
    _, shutdown, opener = _arm_started_daemon(monkeypatch)
    requester = _arm_auto_auth(monkeypatch)
    result = invoke(["gui", "--port", "8904", "--no-auto-auth"])
    assert result.exit_code == 0, output_text(result)
    assert requester.calls == []
    assert opener.calls[0][0][0] == "http://127.0.0.1:8904/"
    assert "Connect screen prompt" in output_text(result)
    assert shutdown.calls


def test_gui_does_not_auto_auth_a_non_loopback_daemon(monkeypatch, env) -> None:
    monkeypatch.setattr(gui_module, "_probe_health", lambda url, timeout_s=1.0: True)
    monkeypatch.setattr(
        gui_module, "_start_background_server", lambda *a, **k: pytest.fail("must not start a daemon")
    )
    monkeypatch.setattr(
        gui_module, "_wait_for_shutdown", lambda server: pytest.fail("must not keep the process alive")
    )
    opener = _Recorder()
    requester = _arm_auto_auth(monkeypatch)
    monkeypatch.setattr(gui_module, "_open_browser", opener)
    result = invoke(["gui", "--host", "192.0.2.10", "--port", "8906"])
    assert result.exit_code == 0, output_text(result)
    assert requester.calls == []
    assert opener.calls[0][0][0] == "http://192.0.2.10:8906/"
    assert "restricted to loopback" in output_text(result)


def test_gui_falls_back_to_connect_screen_when_code_exchange_fails(monkeypatch, env) -> None:
    _arm_started_daemon(monkeypatch)
    result = invoke(["gui", "--port", "8905"])
    assert result.exit_code == 0, output_text(result)
    text = output_text(result)
    assert "http://127.0.0.1:8905/" in text
    assert "Connect screen is shown" in text or "browser opened" in text


def test_request_bootstrap_code_sends_bearer_and_returns_code() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib callback name
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization")
            body = b'{"code":"server-code","expires_in":120}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: D401, ARG002 - silence test server
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        code, error = gui_module._request_bootstrap_code(base_url, "secret-token", timeout_s=5)
        assert (code, error) == ("server-code", "")
        assert seen["path"] == "/api/v1/auth/bootstrap-code"
        assert seen["authorization"] == "Bearer secret-token"
        assert "secret-token" not in str(seen.get("path"))
    finally:
        server.shutdown()
        server.server_close()
