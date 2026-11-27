"""GuestEgressVerifier tests: host-side proxy checks + in-guest HTTP evidence.

Guest commands are exercised through an injected recording runner (the default
runner composes ``adb -s <serial> shell ...``), and once through a real fake
``adb`` executable built from the current Python interpreter.  No external
network is used: every check targets loopback doubles from ``test_bridge``.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from test_bridge import (
    HttpProxyDouble,
    OriginServer,
    SilentServer,
    Socks5Double,
    TlsOriginServer,
    generate_self_signed_cert,
    make_http_proxy,
    make_socks_proxy,
)

from avdfleet.core.errors import ValidationError
from avdfleet.core.interfaces import ProxyVerifier
from avdfleet.core.models import ProxyConfig
from avdfleet.proxy.verify import (
    CommandResult,
    GuestEgressVerifier,
    SubprocessGuestRunner,
    extract_egress_ip,
    mask_token_url,
)

IP_JSON = json.dumps({"ip": "198.51.100.4", "token": "tok-abc"})


class RecordingRunner:
    """Injected guest runner that records argv and scripts responses."""

    def __init__(
        self,
        handler: Callable[[str, tuple[str, ...]], CommandResult] | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[str, ...], float]] = []
        self._handler = handler or self._success

    @staticmethod
    def _success(_serial: str, command: tuple[str, ...]) -> CommandResult:
        return CommandResult(argv=command, exit_code=0, stdout=IP_JSON)

    def run(self, serial: str, command: Sequence[str], *, timeout_s: float) -> CommandResult:
        argv = tuple(command)
        self.calls.append((serial, argv, timeout_s))
        return self._handler(serial, argv)


def _fake_adb_script(tmp_path: Path) -> list[str]:
    """Build a fake adb executable as ``[python, script.py]`` (portable)."""
    script = tmp_path / "fake_adb.py"
    script.write_text(
        "\n".join(
            [
                "import json, sys",
                "args = sys.argv[1:]",
                'if "devices" in args:',
                '    print("List of devices attached")',
                '    print("emulator-5554\\tdevice")',
                "    sys.exit(0)",
                'if "emu" in args:',
                '    print("avd-01")',
                "    sys.exit(0)",
                'if "shell" in args:',
                '    print(json.dumps({"ip": "198.51.100.9", "token": "tok-abc"}))',
                "    sys.exit(0)",
                'print("unexpected fake adb invocation", file=sys.stderr)',
                "sys.exit(2)",
            ]
        ),
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


# ---------------------------------------------------------------------------
# verify_proxy (host side)
# ---------------------------------------------------------------------------


def test_verify_proxy_http_success_and_egress_ip() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5)
        assert isinstance(verifier, ProxyVerifier)
        proxy = make_http_proxy(upstream.port)

        result = verifier.verify_proxy(proxy)

        assert result.ok is True
        assert result.proxy_id == proxy.id
        assert result.status_code == 200
        assert result.egress_ip == "203.0.113.7"
        assert result.error == ""
        assert result.latency_ms is not None and result.latency_ms >= 0
        assert upstream.requests and upstream.requests[0]["auth_ok"] is True
        assert origin.requests and origin.requests[0]["path"] == "/ip"

        details = verifier.proxy_check_details(proxy.id)
        assert details is not None and details["ok"] is True


def test_verify_proxy_wrong_credentials_are_rejected_and_redacted() -> None:
    secret = "wrong-upstream-password"
    with HttpProxyDouble() as upstream, OriginServer() as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5)
        result = verifier.verify_proxy(make_http_proxy(upstream.port, password=secret))
        assert result.ok is False
        assert "407" in result.error
        assert secret not in result.error
        assert origin.requests == []  # the proxy refused before forwarding


def test_verify_proxy_status_4xx_is_failure() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/notfound"), timeout_s=5)
        result = verifier.verify_proxy(make_http_proxy(upstream.port))
        assert result.ok is False
        assert result.status_code == 404
        assert "404" in result.error


def test_verify_proxy_timeout() -> None:
    with SilentServer() as silent:
        proxy = ProxyConfig(id="silent", scheme="http", host="127.0.0.1", port=silent.port)
        verifier = GuestEgressVerifier(verify_url="http://127.0.0.1:9/ip", timeout_s=0.5)
        result = verifier.verify_proxy(proxy)
        assert result.ok is False
        assert "timed out" in result.error.lower()
        assert silent.connections >= 1


def test_verify_proxy_ignores_environment_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with HttpProxyDouble() as upstream, OriginServer() as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5)
        result = verifier.verify_proxy(make_http_proxy(upstream.port))
        assert result.ok is True
        assert result.egress_ip == "203.0.113.7"


def test_verify_proxy_socks5_success() -> None:
    with Socks5Double() as socks, OriginServer() as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5)
        proxy = make_socks_proxy(socks.port)
        result = verifier.verify_proxy(proxy)

        assert result.ok is True
        assert result.status_code == 200
        assert result.egress_ip == "203.0.113.7"
        assert socks.requests and socks.requests[0]["host"] == "127.0.0.1"
        assert socks.requests[0]["auth_user"] == "user"


def test_verify_proxy_socks5_auth_failure_redacted() -> None:
    secret = "bad-socks-password"
    with Socks5Double() as socks, OriginServer() as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5)
        result = verifier.verify_proxy(make_socks_proxy(socks.port, password=secret))
        assert result.ok is False
        assert "authentication" in result.error.lower() or "credential" in result.error.lower()
        assert secret not in result.error
        assert origin.requests == []


def test_verify_proxy_invalid_timeout_rejected() -> None:
    verifier = GuestEgressVerifier(verify_url="http://127.0.0.1:9/ip")
    proxy = ProxyConfig(id="x", scheme="http", host="127.0.0.1", port=9)
    with pytest.raises(ValidationError):
        verifier.verify_proxy(proxy, timeout_s=0)
    with pytest.raises(ValidationError):
        verifier.verify_proxy(proxy, timeout_s=9999)


def test_verify_proxy_https_target_through_http_proxy(tmp_path: Path) -> None:
    cert, key = generate_self_signed_cert(tmp_path)
    with HttpProxyDouble() as upstream, TlsOriginServer(cert, key) as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5, ssl_verify=False)
        result = verifier.verify_proxy(make_http_proxy(upstream.port))
        assert result.ok is True
        assert result.status_code == 200
        assert result.egress_ip == "203.0.113.7"
        assert upstream.requests[0]["method"] == "CONNECT"
        assert origin.requests[0]["path"] == "/ip"


def test_verify_proxy_https_target_untrusted_certificate_fails(tmp_path: Path) -> None:
    cert, key = generate_self_signed_cert(tmp_path)
    with HttpProxyDouble() as upstream, TlsOriginServer(cert, key) as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5)
        result = verifier.verify_proxy(make_http_proxy(upstream.port))
        assert result.ok is False
        lowered = result.error.lower()
        assert "certificate" in lowered or "ssl" in lowered


def test_verify_proxy_socks5_https_target(tmp_path: Path) -> None:
    cert, key = generate_self_signed_cert(tmp_path)
    with Socks5Double() as socks, TlsOriginServer(cert, key) as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5, ssl_verify=False)
        result = verifier.verify_proxy(make_socks_proxy(socks.port))
        assert result.ok is True
        assert result.egress_ip == "203.0.113.7"
        assert socks.requests[0]["port"] == origin.port


# ---------------------------------------------------------------------------
# verify_guest (injected runner)
# ---------------------------------------------------------------------------


def test_verify_guest_curl_success_parses_ip_token_and_serial() -> None:
    token_url = "http://listener.invalid/probe?token=tok-abc"
    runner = RecordingRunner()
    verifier = GuestEgressVerifier(runner=runner, timeout_s=10)
    proxy = make_http_proxy(5555, password="never-on-cmdline")

    result = verifier.verify_guest("emulator-5554", proxy, token_url=token_url)

    assert result.ok is True
    assert result.egress_ip == "198.51.100.4"
    assert result.proxy_id == proxy.id
    serial, command, runner_timeout = runner.calls[0]
    assert serial == "emulator-5554"
    assert command == ("curl", "-sS", "--max-time", "10", token_url)
    assert runner_timeout == 15.0  # guest timeout + subprocess grace
    assert "never-on-cmdline" not in " ".join(command)

    details = verifier.guest_details("emulator-5554")
    assert details is not None
    assert details.serial == "emulator-5554"
    assert details.token_expected == "tok-abc"
    assert details.token_echoed is True
    assert "tok-abc" not in details.token_url_masked
    # The stored detail argv is masked so it is safe to surface/audit.
    assert all("tok-abc" not in part for part in details.attempts[0].command)


def test_verify_guest_falls_back_to_wget_when_curl_missing() -> None:
    def handler(_serial: str, command: tuple[str, ...]) -> CommandResult:
        if command[0] == "curl":
            return CommandResult(
                argv=command,
                exit_code=127,
                stderr="sh: curl: inaccessible or not found",
            )
        return CommandResult(argv=command, exit_code=0, stdout=IP_JSON)

    runner = RecordingRunner(handler)
    verifier = GuestEgressVerifier(runner=runner, timeout_s=7)
    result = verifier.verify_guest(
        "emulator-5554", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )

    assert result.ok is True
    assert result.egress_ip == "198.51.100.4"
    details = verifier.guest_details("emulator-5554")
    assert details is not None
    assert [attempt.label for attempt in details.attempts] == ["curl", "wget"]
    assert details.attempts[0].missing_client is True
    assert runner.calls[1][1][:2] == ("wget", "-qO-")


def test_verify_guest_reports_all_clients_missing() -> None:
    def handler(_serial: str, command: tuple[str, ...]) -> CommandResult:
        return CommandResult(
            argv=command,
            exit_code=127,
            stderr=f"sh: {command[0]}: inaccessible or not found",
        )

    runner = RecordingRunner(handler)
    verifier = GuestEgressVerifier(runner=runner, timeout_s=5)
    result = verifier.verify_guest(
        "emulator-5554", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )

    assert result.ok is False
    assert "no usable HTTP client" in result.error
    details = verifier.guest_details("emulator-5554")
    assert details is not None
    assert [attempt.label for attempt in details.attempts] == ["curl", "wget", "toybox-wget"]
    assert details.attempts[2].command[:2] == ("toybox", "wget")
    assert all(attempt.missing_client for attempt in details.attempts)


def test_verify_guest_timeout() -> None:
    def handler(_serial: str, command: tuple[str, ...]) -> CommandResult:
        return CommandResult(argv=command, exit_code=None, timed_out=True)

    runner = RecordingRunner(handler)
    verifier = GuestEgressVerifier(runner=runner, timeout_s=3)
    result = verifier.verify_guest(
        "emulator-5554", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )

    assert result.ok is False
    assert "timed out" in result.error.lower()
    details = verifier.guest_details("emulator-5554")
    assert details is not None
    assert len(details.attempts) == 1  # a timeout stops the fallback chain
    assert details.attempts[0].timed_out is True


def test_verify_guest_transport_failure_reports_exit_code() -> None:
    def handler(_serial: str, command: tuple[str, ...]) -> CommandResult:
        return CommandResult(
            argv=command,
            exit_code=7,
            stderr="curl: (7) Failed to connect to listener.invalid port 80",
        )

    verifier = GuestEgressVerifier(runner=RecordingRunner(handler), timeout_s=5)
    result = verifier.verify_guest(
        "emulator-5554", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )
    assert result.ok is False
    assert "exit 7" in result.error


def test_verify_guest_expected_token_must_be_echoed() -> None:
    runner = RecordingRunner()  # body echoes tok-abc
    verifier = GuestEgressVerifier(runner=runner, timeout_s=5)
    result = verifier.verify_guest(
        "emulator-5554",
        make_http_proxy(5555),
        token_url="http://listener.invalid/probe",
        expected_token="tok-expected-but-missing",
    )
    assert result.ok is False
    assert "expected listener token" in result.error


def test_verify_guest_auto_token_echo_is_recorded_but_not_required() -> None:
    runner = RecordingRunner()  # body echoes tok-abc
    verifier = GuestEgressVerifier(runner=runner, timeout_s=5)
    result = verifier.verify_guest(
        "emulator-5554",
        make_http_proxy(5555),
        token_url="http://listener.invalid/probe?token=tok-abc",
    )
    assert result.ok is True
    details = verifier.guest_details("emulator-5554")
    assert details is not None and details.token_echoed is True


def test_verify_guest_serial_resolver_injection() -> None:
    runner = RecordingRunner()
    verifier = GuestEgressVerifier(
        runner=runner,
        serial_resolver=lambda name: "emulator-7777" if name == "avd-01" else "",
        timeout_s=5,
    )
    result = verifier.verify_guest(
        "avd-01", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )
    assert result.ok is True
    assert runner.calls[0][0] == "emulator-7777"


def test_verify_guest_rejects_credentials_in_token_url() -> None:
    runner = RecordingRunner()
    verifier = GuestEgressVerifier(runner=runner, timeout_s=5)
    result = verifier.verify_guest(
        "emulator-5554",
        make_http_proxy(5555),
        token_url="http://user:secret@127.0.0.1/probe",
    )
    assert result.ok is False
    assert "must not contain credentials" in result.error
    assert runner.calls == []


def test_verify_guest_reports_missing_adb_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("avdfleet.proxy.verify.verifier._discover_adb", lambda _config: None)
    verifier = GuestEgressVerifier(runner=None, adb_path=None, timeout_s=5)
    assert verifier._runner is None
    result = verifier.verify_guest(
        "emulator-5554", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )
    assert result.ok is False
    assert "no adb runner" in result.error


# ---------------------------------------------------------------------------
# verify_guest (real subprocess fake adb)
# ---------------------------------------------------------------------------


def test_verify_guest_with_fake_adb_executable(tmp_path: Path) -> None:
    adb = _fake_adb_script(tmp_path)
    verifier = GuestEgressVerifier(adb_path=adb, timeout_s=5)
    assert isinstance(verifier._runner, SubprocessGuestRunner)

    result = verifier.verify_guest(
        "emulator-5556", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )
    assert result.ok is True
    assert result.egress_ip == "198.51.100.9"
    details = verifier.guest_details("emulator-5556")
    assert details is not None and details.serial == "emulator-5556"


def test_verify_guest_default_serial_resolution_with_fake_adb(tmp_path: Path) -> None:
    adb = _fake_adb_script(tmp_path)
    verifier = GuestEgressVerifier(adb_path=adb, timeout_s=5)
    # "avd-01" is not serial-like: the verifier must run ``adb devices`` and
    # ``adb -s <serial> emu avd name`` to map it.
    result = verifier.verify_guest(
        "avd-01", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )
    assert result.ok is True
    details = verifier.guest_details("avd-01")
    assert details is not None and details.serial == "emulator-5554"


def test_verify_guest_missing_adb_executable_returns_failure(tmp_path: Path) -> None:
    verifier = GuestEgressVerifier(
        adb_path=str(tmp_path / "missing-adb"), timeout_s=5
    )
    result = verifier.verify_guest(
        "emulator-5554", make_http_proxy(5555), token_url="http://listener.invalid/probe"
    )
    assert result.ok is False
    assert "adb executable not found" in result.error


# ---------------------------------------------------------------------------
# Secret hygiene + parsing helpers
# ---------------------------------------------------------------------------


def test_no_secret_in_argv_results_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="avdfleet")
    proxy_password = "proxy-super-secret-99"
    token_value = "guest-token-77"
    runner = RecordingRunner()
    verifier = GuestEgressVerifier(runner=runner, timeout_s=5)

    result = verifier.verify_guest(
        "emulator-5554",
        make_http_proxy(5555, password=proxy_password),
        token_url=f"http://listener.invalid/probe?token={token_value}",
    )

    assert result.ok is True
    assert proxy_password not in result.error
    assert proxy_password not in caplog.text
    assert token_value not in caplog.text
    for _serial, command, _timeout in runner.calls:
        assert proxy_password not in " ".join(command)
        assert token_value in " ".join(command)  # the guest token stays in the URL
    details = verifier.guest_details("emulator-5554")
    assert details is not None
    assert token_value not in details.token_url_masked
    assert all(token_value not in part for part in details.attempts[0].command)
    assert token_value not in details.error


def test_verify_proxy_errors_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="avdfleet")
    secret = "proxy-password-xyz"
    with HttpProxyDouble() as upstream, OriginServer() as origin:
        verifier = GuestEgressVerifier(verify_url=origin.url("/ip"), timeout_s=5)
        result = verifier.verify_proxy(make_http_proxy(upstream.port, password=secret))
        assert result.ok is False
        assert secret not in result.error
        assert secret not in caplog.text


def test_extract_egress_ip_variants() -> None:
    assert extract_egress_ip('{"ip": "203.0.113.7"}') == "203.0.113.7"
    assert extract_egress_ip('{"origin": "198.51.100.2"}') == "198.51.100.2"
    assert extract_egress_ip('{"query": ["192.0.2.9"]}') == "192.0.2.9"
    assert extract_egress_ip("your address is 192.0.2.33\n") == "192.0.2.33"
    assert extract_egress_ip("no address here") is None
    assert extract_egress_ip('{"ip": "999.999.1.1"}') is None
    assert extract_egress_ip("") is None


def test_mask_token_url() -> None:
    masked = mask_token_url("http://alice:secret@127.0.0.1:8080/probe?token=abc&keep=1")
    assert "alice" not in masked
    assert "secret" not in masked
    assert "abc" not in masked
    assert "keep=1" in masked
    assert mask_token_url("not a url") == "not a url"
