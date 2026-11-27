"""Static/content tests for the AVD Fleet bootstrap installers.

These tests only read and parse the bootstrap scripts. They never run an
installation and never execute ``install.sh`` or ``install.ps1`` end to end.
If a POSIX shell or PowerShell host is present, the scripts are additionally
syntax-checked with ``bash -n`` and the PowerShell parser respectively; those
checks cannot install anything.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BOOTSTRAP_DIR = Path(__file__).resolve().parent
INSTALL_SH = BOOTSTRAP_DIR / "install.sh"
INSTALL_PS1 = BOOTSTRAP_DIR / "install.ps1"
README = BOOTSTRAP_DIR / "README.md"

SH_FLAGS = [
    "--accept-android-sdk-licenses",
    "--yes",
    "--dry-run",
    "--no-appium",
    "--sdk-root",
    "--verbose",
    "--help",
    "--version",
]

PS_PARAMETERS = [
    "$AcceptAndroidSdkLicenses",
    "$Yes",
    "$DryRun",
    "$SdkRoot",
    "$NoAppium",
    "$Verbose",
    "$Version",
    "$Help",
]

# Syntax that does not exist in Windows PowerShell 5.1 / bash 3.2.
PS51_FORBIDDEN = [
    r"\?\?",
    r"\?\.",
    r"\bForEach-Object\s+-Parallel\b",
    r"\$IsWindows\b",
    r"-AsHashtable\b",
    r"-AsByteStream\b",
    r"-AdditionalChildPath\b",
    r"\busing\s+namespace\b",
]

BASH4_FORBIDDEN = [
    r"\bmapfile\b",
    r"\breadarray\b",
    r"\bdeclare\s+-A\b",
    r"\bdeclare\s+-a\b",
    r"\blocal\s+-A\b",
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\^\^\}",
    r"\$\{[A-Za-z_][A-Za-z0-9_]*,,}",
]


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required file: {path}"
    assert path.stat().st_size > 0, f"empty required file: {path}"
    return path.read_text(encoding="utf-8")


def _strip_shell_noise(text: str) -> str:
    """Remove full-line comments and here-document bodies.

    Content tests for forbidden syntax must not trip over the prose that
    documents which syntax is avoided.
    """
    kept: list[str] = []
    heredoc_tag: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if heredoc_tag is not None:
            if stripped == heredoc_tag:
                heredoc_tag = None
            continue
        if stripped.startswith("#"):
            continue
        match = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?", line)
        if match:
            heredoc_tag = match.group(1)
        kept.append(line)
    return "\n".join(kept)


def _param_block(ps1_text: str) -> str:
    start = ps1_text.find("param(")
    assert start != -1, "install.ps1 has no param() block"
    end = ps1_text.find("\n)", start)
    assert end != -1, "install.ps1 param() block is not terminated"
    return ps1_text[start : end + 3]


def _strip_ps1_noise(text: str) -> str:
    """Remove comment lines and here-string bodies for syntax content checks."""
    kept: list[str] = []
    heredoc_end: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if heredoc_end is not None:
            if stripped == heredoc_end:
                heredoc_end = None
            continue
        if stripped.startswith("#"):
            continue
        if re.search(r"@['\"]\s*$", line):
            heredoc_end = stripped[-2:]
            continue
        kept.append(line)
    return "\n".join(kept)


def _powershell_host() -> str | None:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(scope="module")
def sh_text() -> str:
    return _read(INSTALL_SH)


@pytest.fixture(scope="module")
def ps1_text() -> str:
    return _read(INSTALL_PS1)


@pytest.fixture(scope="module")
def readme_text() -> str:
    return _read(README)


# ---------------------------------------------------------------------------
# Shared properties
# ---------------------------------------------------------------------------


def test_required_files_exist() -> None:
    for path in (INSTALL_SH, INSTALL_PS1, README):
        assert path.is_file(), f"missing {path}"
        assert path.stat().st_size > 500, f"{path} looks suspiciously small"


def test_bootstrap_files_are_ascii() -> None:
    for path in (INSTALL_SH, INSTALL_PS1, README):
        data = path.read_bytes()
        try:
            data.decode("ascii")
        except UnicodeDecodeError as exc:  # pragma: no cover - failure path
            pytest.fail(f"{path} must stay ASCII-only: {exc}")


def test_no_embedded_credentials_or_tokens() -> None:
    pattern = re.compile(
        r"(?i)\b(password|passwd|api[_-]?key|access[_-]?token|secret[_-]?key)\s*=\s*['\"][^'\"]+['\"]"
    )
    for path in (INSTALL_SH, INSTALL_PS1):
        assert not pattern.search(path.read_text(encoding="utf-8")), f"possible embedded credential in {path}"


# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------


def test_install_sh_shebang_and_strict_mode(sh_text: str) -> None:
    first_line = sh_text.splitlines()[0]
    assert first_line == "#!/usr/bin/env bash"
    assert re.search(r"^set -eu\s*$", sh_text, re.MULTILINE), "install.sh should start with 'set -eu'"


def test_install_sh_uses_lf_and_ends_with_newline() -> None:
    data = INSTALL_SH.read_bytes()
    assert b"\r" not in data, "install.sh must use LF line endings"
    assert data.endswith(b"\n"), "install.sh must end with a newline"


def test_install_sh_supports_required_flags(sh_text: str) -> None:
    for flag in SH_FLAGS:
        assert flag in sh_text, f"install.sh is missing the {flag} flag"
    assert "unknown option" in sh_text
    assert "unexpected argument" in sh_text


def test_install_sh_rejects_unknown_flags_with_usage_exit(sh_text: str) -> None:
    assert re.search(r"unknown option: \$?_?arg", sh_text), "unknown option should name the offending flag"
    assert re.search(r"exit \"\$EXIT_USAGE\"", sh_text), "usage errors should exit with EXIT_USAGE"


def test_install_sh_forwards_flags_to_installer(sh_text: str) -> None:
    expected = [
        'set -- "$@" --accept-android-sdk-licenses',
        'set -- "$@" --yes',
        'set -- "$@" --dry-run',
        'set -- "$@" --no-appium',
        'set -- "$@" --sdk-root "$SDK_ROOT"',
    ]
    for fragment in expected:
        assert fragment in sh_text, f"install.sh does not forward: {fragment}"


def test_install_sh_requires_https_downloader(sh_text: str) -> None:
    assert "curl" in sh_text and "wget" in sh_text
    assert "refusing to download a non-HTTPS URL" in sh_text
    assert "--proto '=https'" in sh_text or '--proto "=https"' in sh_text
    assert "--https-only" in sh_text
    assert "curl or wget is required" in sh_text


def test_install_sh_never_pipes_downloads_into_a_shell(sh_text: str) -> None:
    dangerous = re.compile(
        r"(?im)^\s*(?:\S+\s+)?(?:curl|wget)\b[^\n|]*\|\s*(?:(?:ba|da|z|k)?sh|python3?|perl|ruby)\b"
    )
    assert not dangerous.search(sh_text), "install.sh must never pipe curl/wget output into a shell"
    assert "uv-install.sh" in sh_text, "the uv installer should be downloaded to a temporary file"
    assert "mktemp" in sh_text


def test_install_sh_verifies_checksum_when_provided(sh_text: str) -> None:
    for fragment in ("UV_INSTALLER_SHA256", "AVDFLEET_UV_INSTALLER_CHECKSUM", "verify_checksum"):
        assert fragment in sh_text
    assert "shasum" in sh_text and "sha256sum" in sh_text
    assert "checksum mismatch" in sh_text
    assert "UV_INSTALLER_SHA256 is not set" in sh_text, "unverified installs must be warned about explicitly"
    assert "integrity is not pinned" in sh_text


def test_install_sh_installs_uv_and_tool(sh_text: str) -> None:
    assert "https://astral.sh/uv/install.sh" in sh_text
    assert "uv tool install" in sh_text
    assert "--force" in sh_text
    assert "AVDFLEET_PACKAGE_SPEC" in sh_text
    assert "AVDFLEET_VERSION" in sh_text
    assert "AVDFLEET_REINSTALL" in sh_text
    assert "python3 -m pip install --user" in sh_text


def test_install_sh_is_idempotent(sh_text: str) -> None:
    assert "find_avdfleet" in sh_text
    assert 'avdfleet_version_raw' in sh_text
    assert "--version" in sh_text
    assert "already installed" in sh_text
    assert "Skipping 'uv tool install'" in sh_text
    assert "_force" in sh_text


def test_install_sh_runs_engine_and_doctor(sh_text: str) -> None:
    assert "set -- install" in sh_text
    assert '"$AVDFLEET_BIN" "$@"' in sh_text
    assert 'run_doctor' in sh_text
    assert '"$AVDFLEET_BIN" doctor' in sh_text or "doctor" in sh_text
    assert "avdfleet doctor: all checks passed" in sh_text


def test_install_sh_handles_reboot(sh_text: str) -> None:
    for fragment in (
        "3010",
        "194",
        "EXIT_REBOOT=6",
        "Reboot required",
        "install-manifest.json",
        "reboot_required",
        "AVDFLEET_IGNORE_REBOOT_MARKER",
        "re-run the same command",
    ):
        assert fragment in sh_text, f"install.sh reboot handling is missing: {fragment}"


def test_install_sh_avoids_bash4_only_syntax(sh_text: str) -> None:
    code = _strip_shell_noise(sh_text)
    for pattern in BASH4_FORBIDDEN:
        assert not re.search(pattern, code), f"install.sh uses bash-4-only syntax: {pattern}"


def test_install_sh_path_setup_does_not_edit_profiles(sh_text: str) -> None:
    assert "UV_NO_MODIFY_PATH=1" in sh_text
    assert "LOCAL_BIN" in sh_text
    # No writes to shell startup files.
    assert not re.search(r"(?:>>|>)\s*[\"']?\$(?:HOME|profile)", sh_text)


def test_install_sh_dry_run_is_non_destructive(sh_text: str) -> None:
    assert "--dry-run" in sh_text
    assert "DRY_RUN=0" in sh_text
    assert "dry-run" in sh_text.lower()
    assert "no changes were made" in sh_text or "no software was installed" in sh_text


def test_install_sh_mentions_windows_helper(sh_text: str) -> None:
    assert "install.ps1" in sh_text, "install.sh should direct Windows shells to install.ps1"


# ---------------------------------------------------------------------------
# install.ps1
# ---------------------------------------------------------------------------


def test_install_ps1_has_required_header(ps1_text: str) -> None:
    assert ps1_text.splitlines()[0].strip() == "#requires -version 5.1"
    assert "param(" in ps1_text


def test_install_ps1_uses_crlf_and_no_bom() -> None:
    data = INSTALL_PS1.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "install.ps1 must not have a UTF-8 BOM"
    assert b"\r\n" in data
    without_crlf = data.replace(b"\r\n", b"")
    assert b"\n" not in without_crlf, "install.ps1 must use CRLF line endings"
    assert b"\r" not in without_crlf, "install.ps1 has a bare CR character"


def test_install_ps1_declares_all_parameters(ps1_text: str) -> None:
    block = _param_block(ps1_text)
    for declaration in PS_PARAMETERS:
        assert declaration in block, f"install.ps1 param() block is missing {declaration}"
    assert "[switch]$AcceptAndroidSdkLicenses" in block
    assert "[switch]$Yes" in block
    assert "[switch]$DryRun" in block
    assert "[switch]$NoAppium" in block
    assert "[switch]$Verbose" in block
    assert "[switch]$Version" in block
    assert "[switch]$Help" in block
    assert "[string]$SdkRoot" in block


def test_install_ps1_avoids_verbose_parameter_conflict(ps1_text: str) -> None:
    # A [CmdletBinding()] script already has a common -Verbose parameter and
    # would fail to start with an explicit [switch]$Verbose declaration.
    assert "[CmdletBinding()]" not in ps1_text
    assert "[CmdletBinding(" not in ps1_text
    assert "[switch]$Verbose" in ps1_text


def test_install_ps1_rejects_unknown_arguments(ps1_text: str) -> None:
    assert "$args.Count" in ps1_text
    assert "unknown argument(s)" in ps1_text
    assert "ExitUsage" in ps1_text


def test_install_ps1_is_powershell_51_compatible(ps1_text: str) -> None:
    code = _strip_ps1_noise(ps1_text)
    for pattern in PS51_FORBIDDEN:
        assert not re.search(pattern, code), f"install.ps1 uses syntax unavailable in PS 5.1: {pattern}"
    assert "$PSVersionTable.PSVersion.Major -lt 6" in ps1_text, "UseBasicParsing must be conditional for PS 5.1"
    assert "UseBasicParsing" in ps1_text


def test_install_ps1_handles_https_and_checksums(ps1_text: str) -> None:
    assert "Invoke-WebRequest" in ps1_text
    assert "Get-FileHash" in ps1_text
    assert "UV_INSTALLER_SHA256" in ps1_text
    assert "Test-Checksum" in ps1_text
    assert "refusing to download a non-HTTPS URL" in ps1_text
    assert "winget" in ps1_text
    assert "integrity is not pinned" in ps1_text
    assert "uv-install.ps1" in ps1_text


def test_install_ps1_never_downloads_and_iex_executes(ps1_text: str) -> None:
    dangerous = re.compile(
        r"(?i)\b(?:irm|invoke-restmethod|invoke-webrequest)\b[^\n]*\|\s*(?:iex|invoke-expression)\b"
    )
    assert not dangerous.search(ps1_text), "install.ps1 must never pipe a download into Invoke-Expression"
    assert "child PowerShell process" in ps1_text or "-File" in ps1_text


def test_install_ps1_installs_uv_and_tool(ps1_text: str) -> None:
    assert "https://astral.sh/uv/install.ps1" in ps1_text
    assert "'tool', 'install'" in ps1_text
    assert "'--force'" in ps1_text
    assert "AVDFLEET_PACKAGE_SPEC" in ps1_text
    assert "AVDFLEET_VERSION" in ps1_text
    assert "AVDFLEET_REINSTALL" in ps1_text
    assert "pip install --user" in ps1_text or "'--user'" in ps1_text


def test_install_ps1_forwards_flags_to_installer(ps1_text: str) -> None:
    for flag in (
        "--accept-android-sdk-licenses",
        "--yes",
        "--dry-run",
        "--no-appium",
        "--sdk-root",
    ):
        assert flag in ps1_text, f"install.ps1 does not forward {flag}"
    assert "Get-InstallerArguments" in ps1_text


def test_install_ps1_runs_engine_and_doctor(ps1_text: str) -> None:
    assert "Get-InstallerArguments" in ps1_text
    assert "@('doctor')" in ps1_text
    assert "avdfleet doctor: all checks passed" in ps1_text
    assert "Show-Success" in ps1_text


def test_install_ps1_handles_reboot(ps1_text: str) -> None:
    for fragment in (
        "3010",
        "ExitReboot",
        "install-manifest.json",
        "reboot_required",
        "Get-ResumeCommand",
        "AVDFLEET_IGNORE_REBOOT_MARKER",
    ):
        assert fragment in ps1_text, f"install.ps1 reboot handling is missing: {fragment}"
    assert "exit $script:ExitReboot" in ps1_text


def test_install_ps1_propagates_exit_codes(ps1_text: str) -> None:
    assert re.search(r"\$(?:global:)?LASTEXITCODE\b", ps1_text)
    assert "Get-SafeExitCode" in ps1_text
    assert "exit $code" in ps1_text
    assert "ExitUvInstall" in ps1_text


def test_install_ps1_refreshes_path(ps1_text: str) -> None:
    assert "Update-ProcessPath" in ps1_text
    assert "Get-LocalBin" in ps1_text


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def test_readme_documents_both_installers(readme_text: str) -> None:
    assert "bootstrap/install.sh" in readme_text or "install.sh" in readme_text
    assert "install.ps1" in readme_text
    for flag in SH_FLAGS:
        assert flag in readme_text, f"README does not document {flag}"
    for parameter in ("-AcceptAndroidSdkLicenses", "-Yes", "-DryRun", "-SdkRoot", "-NoAppium", "-Verbose", "-Version", "-Help"):
        assert parameter in readme_text, f"README does not document {parameter}"


def test_readme_documents_reboot_and_exit_codes(readme_text: str) -> None:
    assert "3010" in readme_text
    assert "reboot" in readme_text.lower()
    assert "Exit codes" in readme_text or "exit code" in readme_text.lower()
    assert "ExecutionPolicy" in readme_text
    assert "Unblock-File" in readme_text
    assert "UV_INSTALLER_SHA256" in readme_text
    assert "Troubleshooting" in readme_text
    assert "Prerequisites" in readme_text


def test_readme_documents_platform_refusals(readme_text: str) -> None:
    assert "WSL" in readme_text
    assert "ARM64" in readme_text
    assert "KVM" in readme_text


# ---------------------------------------------------------------------------
# Optional syntax checks (no installation is executed)
# ---------------------------------------------------------------------------


def test_install_sh_syntax_when_bash_available() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available on this host; install.sh syntax check skipped")
    result = subprocess.run(
        [bash, "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stdout}\n{result.stderr}"


def test_install_ps1_parses_when_powershell_available(tmp_path: Path) -> None:
    host = _powershell_host()
    if not host:
        pytest.skip("PowerShell is not available on this host; parser check skipped")

    checker = tmp_path / "parse-bootstrap.ps1"
    checker.write_text(
        "param([string]$Target)\n"
        "$tokens = $null\n"
        "$errors = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$tokens, [ref]$errors)\n"
        "if ($errors -and $errors.Count -gt 0) {\n"
        "    foreach ($err in $errors) {\n"
        "        Write-Error ($err.Extent.StartLineNumber.ToString() + ': ' + $err.Message)\n"
        "    }\n"
        "    exit 1\n"
        "}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    command = [host, "-NoProfile", "-NonInteractive"]
    if sys.platform == "win32":
        command += ["-ExecutionPolicy", "Bypass"]
    command += ["-File", str(checker), str(INSTALL_PS1)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90, env=os.environ.copy())
    assert result.returncode == 0, f"PowerShell parser reported errors:\n{result.stdout}\n{result.stderr}"
