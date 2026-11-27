#!/usr/bin/env bash
# =============================================================================
# AVD Fleet - one-command bootstrap installer for macOS and Linux.
#
# Responsibilities (deliberately thin, per BRIEF section 3.2):
#   1. detect OS/architecture and refuse hosts that cannot run the emulator
#   2. make sure an HTTPS downloader and sudo are available
#   3. install uv: use an existing uv, otherwise download the official uv
#      installer to a temporary file (never pipe a download into a shell),
#      verify it when UV_INSTALLER_SHA256 is provided, and fall back to
#      `python3 -m pip install --user uv` when the installer cannot run
#   4. `uv tool install avdfleet` (skipped when a usable version already exists)
#   5. run `avdfleet install ...`, forwarding the operator's flags
#   6. treat exit code 3010 (or an incomplete reboot checkpoint) as
#      "a reboot is required; re-run the same command to resume"
#   7. run `avdfleet doctor` as the final acceptance check
#
# The script targets bash 3.2 (the stock macOS shell): no arrays, no
# associative arrays, no `${var,,}`/`${var^^}`, no `readarray`/`mapfile`.
# It is idempotent: a second run repeats only the installer engine (which is
# itself idempotent) and the doctor checks.
#
# This script never reads or prints secrets. It does not edit shell profiles:
# it puts $HOME/.local/bin on PATH for the current process only and prints the
# exact line to add to your shell profile.
# =============================================================================
set -eu

BOOTSTRAP_VERSION="0.1.0"
PROGRAM="$(basename "$0" 2>/dev/null || printf 'install.sh')"
DEFAULT_PACKAGE_SPEC="avdfleet"
DEFAULT_UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

# Exit codes. Codes returned by `avdfleet install`/`avdfleet doctor` are
# propagated unchanged, except 3010 (reboot required) which POSIX shells cannot
# represent and which is reported here as 6.
EXIT_FAILURE=1
EXIT_USAGE=2
EXIT_UNSUPPORTED=3
EXIT_PREREQUISITE=4
EXIT_UV_INSTALL=5
EXIT_REBOOT=6

# ---------------------------------------------------------------------------
# Global state (populated by parse_args / detect_host / prepare_path)
# ---------------------------------------------------------------------------
ACCEPT_LICENSES=0
ASSUME_YES=0
DRY_RUN=0
NO_APPIUM=0
VERBOSE=0
SHOW_HELP=0
SHOW_VERSION=0
SDK_ROOT=""
ORIGINAL_ARGS_DISPLAY=""
OS_NAME=""
OS_ID=""
DISTRO_LABEL=""
ARCH=""
DOWNLOADER=""
LOCAL_BIN=""
UV_BIN=""
AVDFLEET_BIN=""
TMP_DIR=""
INSTALL_LOG=""
KEEP_TMP=0
STREAM_STATUS=0

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

say() {
    printf '%s\n' "$*"
}

info() {
    printf '==> %s\n' "$*"
}

warn() {
    printf 'WARNING: %s\n' "$*" >&2
}

error() {
    printf 'ERROR: %s\n' "$*" >&2
}

debug() {
    if [ "$VERBOSE" -eq 1 ]; then
        printf 'DEBUG: %s\n' "$*" >&2
    fi
}

die() {
    _die_code=$1
    shift
    error "$*"
    exit "$_die_code"
}

have() {
    command -v "$1" >/dev/null 2>&1
}

# Print a command line with each argument quoted where necessary.
show_cmd() {
    _sc_command=$1
    shift
    printf '%s' "$_sc_command"
    for _sc_arg in "$@"; do
        case "$_sc_arg" in
            *[!A-Za-z0-9_./:=@+-]*)
                printf ' %s' "$(shell_quote "$_sc_arg")"
                ;;
            *)
                printf ' %s' "$_sc_arg"
                ;;
        esac
    done
    printf '\n'
}

# POSIX single-quote a string for safe re-display in a shell command line.
shell_quote() {
    _sq_value=$1
    _sq_result=""
    while :; do
        case "$_sq_value" in
            *\'*)
                _sq_result="${_sq_result}${_sq_value%%\'*}'\\''"
                _sq_value=${_sq_value#*\'}
                ;;
            *)
                _sq_result="${_sq_result}${_sq_value}"
                break
                ;;
        esac
    done
    printf "'%s'" "$_sq_result"
}

append_original_arg() {
    ORIGINAL_ARGS_DISPLAY="${ORIGINAL_ARGS_DISPLAY:-} $(shell_quote "$1")"
}

# ---------------------------------------------------------------------------
# Help and version
# ---------------------------------------------------------------------------

usage() {
    cat <<'USAGE_EOF'
AVD Fleet bootstrap installer (macOS / Linux)

Usage:
  ./bootstrap/install.sh [options]

Installs uv and AVD Fleet, then runs the AVD Fleet installer engine:
  uv tool install avdfleet
  avdfleet install [flags]
  avdfleet doctor

Options:
  --accept-android-sdk-licenses
                        Accept the Android SDK licenses unattended. Required
                        for a fully non-interactive install (BRIEF section 3.2
                        and R-INSTALL). Passed through to avdfleet install.
  -y, --yes             Assume "yes" for installer questions. Passed through
                        to avdfleet install.
  --dry-run             Print the planned actions without installing anything.
                        If avdfleet is already installed, its own
                        `avdfleet install --dry-run` plan is shown too.
  --no-appium           Skip the Appium server and driver steps. Passed
                        through to avdfleet install.
  --sdk-root PATH       Install the Android SDK under PATH instead of the
                        default data directory. Must be an absolute path.
  --verbose             Verbose bootstrap diagnostics (does not change the
                        avdfleet log level).
  -h, --help            Show this help text and exit.
  --version             Show bootstrap, uv and avdfleet versions and exit.

Environment overrides:
  AVDFLEET_PACKAGE_SPEC     Package spec passed to `uv tool install`.
                            Default: avdfleet. Examples: a local wheel
                            (/tmp/avdfleet-0.1.0-py3-none-any.whl), a git URL
                            (git+https://example.invalid/avdfleet.git), or a
                            version-pinned spec (avdfleet==0.1.0).
  AVDFLEET_VERSION          Desired avdfleet version. Used to decide whether a
                            reinstall with --force is needed and to pin the
                            default package spec.
  AVDFLEET_REINSTALL=1      Force `uv tool install --force` even when a
                            version is already installed.
  AVDFLEET_PYTHON           Optional Python interpreter passed to uv via
                            `--python` for the tool environment.
  AVDFLEET_DATA_DIR         Data directory used to look for the installer
                            checkpoint (reboot detection).
  AVDFLEET_IGNORE_REBOOT_MARKER=1
                            Do not treat an incomplete install manifest as a
                            pending reboot.
  AVDFLEET_MIN_FREE_GB      Minimum free disk space to warn about (default 10).
  AVDFLEET_ALLOW_UNSUPPORTED_HOST=1
                            Continue on a host/architecture the emulator does
                            not support (installs the CLI only).
  UV_VERSION                Pin the uv installer URL to a specific version:
                            https://astral.sh/uv/<UV_VERSION>/install.sh
  UV_INSTALLER_URL          Override the uv installer URL (must be https://).
  UV_INSTALLER_SHA256       Expected SHA-256 of the downloaded uv installer.
                            Strongly recommended: without it the installer is
                            run with an unverified-integrity warning.
  AVDFLEET_UV_INSTALLER_CHECKSUM
                            Alias for UV_INSTALLER_SHA256 (for automation).
  NO_COLOR                  Accepted for compatibility; output is plain ASCII.

Exit codes:
  0  Success (AVD Fleet installed and `avdfleet doctor` passed)
  1  Unexpected bootstrap failure
  2  Usage error (unknown option, missing value, bad path)
  3  Unsupported operating system or architecture
  4  Prerequisite failure (downloader, checksum, sudo, network)
  5  Could not install uv (or the avdfleet tool entry point)
  6  Reboot required: reboot, then re-run the same command to resume
  N  Exit code propagated from `avdfleet install` or `avdfleet doctor`

Examples:
  ./bootstrap/install.sh --accept-android-sdk-licenses --yes
  ./bootstrap/install.sh --accept-android-sdk-licenses --yes --no-appium
  ./bootstrap/install.sh --dry-run --sdk-root "$HOME/Android/Sdk"
  UV_INSTALLER_SHA256=<sha256> ./bootstrap/install.sh --accept-android-sdk-licenses --yes

See bootstrap/README.md for prerequisites, reboot/resume and troubleshooting.
USAGE_EOF
}

show_version() {
    printf 'avdfleet bootstrap %s (%s)\n' "$BOOTSTRAP_VERSION" "$PROGRAM"
    _found_uv="$(find_uv 2>/dev/null || true)"
    if [ -n "$_found_uv" ]; then
        printf 'uv: %s\n' "$("$_found_uv" --version 2>/dev/null | head -n 1 || true)"
    else
        printf 'uv: not installed (or not on PATH)\n'
    fi
    _found_avd="$(find_avdfleet 2>/dev/null || true)"
    if [ -n "$_found_avd" ]; then
        printf 'avdfleet: %s\n' "$("$_found_avd" --version 2>/dev/null | head -n 1 || true)"
    else
        printf 'avdfleet: not installed (or not on PATH)\n'
    fi
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

cleanup() {
    _cleanup_rc=$?
    trap - EXIT INT TERM HUP
    if [ "$KEEP_TMP" -eq 1 ] && [ -n "${INSTALL_LOG:-}" ] && [ -f "$INSTALL_LOG" ]; then
        printf 'Full log kept at: %s\n' "$INSTALL_LOG" >&2
    elif [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    exit "$_cleanup_rc"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

parse_args() {
    while [ $# -gt 0 ]; do
        _arg=$1
        case "$_arg" in
            --accept-android-sdk-licenses)
                ACCEPT_LICENSES=1
                append_original_arg "$_arg"
                shift
                ;;
            --yes|-y)
                ASSUME_YES=1
                append_original_arg "$_arg"
                shift
                ;;
            --dry-run)
                DRY_RUN=1
                append_original_arg "$_arg"
                shift
                ;;
            --no-appium)
                NO_APPIUM=1
                append_original_arg "$_arg"
                shift
                ;;
            --verbose)
                VERBOSE=1
                append_original_arg "$_arg"
                shift
                ;;
            -h|--help)
                SHOW_HELP=1
                shift
                ;;
            --version)
                SHOW_VERSION=1
                shift
                ;;
            --sdk-root)
                if [ $# -lt 2 ]; then
                    error "option --sdk-root requires a PATH value"
                    printf 'Run %s --help for usage.\n' "$PROGRAM" >&2
                    exit "$EXIT_USAGE"
                fi
                case "$2" in
                    -|--*)
                        error "option --sdk-root requires a PATH value, got option: $2"
                        printf 'Run %s --help for usage.\n' "$PROGRAM" >&2
                        exit "$EXIT_USAGE"
                    ;;
                esac
                SDK_ROOT=$2
                append_original_arg "$_arg"
                append_original_arg "$2"
                shift 2
                ;;
            --sdk-root=*)
                SDK_ROOT=${_arg#*=}
                if [ -z "$SDK_ROOT" ]; then
                    error "option --sdk-root requires a non-empty PATH value"
                    printf 'Run %s --help for usage.\n' "$PROGRAM" >&2
                    exit "$EXIT_USAGE"
                fi
                append_original_arg "$_arg"
                shift
                ;;
            --)
                shift
                if [ $# -gt 0 ]; then
                    error "unexpected argument: $1"
                    printf 'Run %s --help for usage.\n' "$PROGRAM" >&2
                    exit "$EXIT_USAGE"
                fi
                ;;
            -*)
                error "unknown option: $_arg"
                printf 'Run %s --help for usage.\n' "$PROGRAM" >&2
                exit "$EXIT_USAGE"
                ;;
            *)
                error "unexpected argument: $_arg"
                printf 'Run %s --help for usage.\n' "$PROGRAM" >&2
                exit "$EXIT_USAGE"
                ;;
        esac
    done
}

validate_options() {
    if [ -n "${UV_INSTALLER_URL:-}" ]; then
        case "$UV_INSTALLER_URL" in
            https://*) ;;
            *)
                die "$EXIT_USAGE" "UV_INSTALLER_URL must use https:// (refusing: $UV_INSTALLER_URL)"
                ;;
        esac
    fi
    if [ -n "${UV_INSTALLER_SHA256:-}" ] && [ -n "${AVDFLEET_UV_INSTALLER_CHECKSUM:-}" ]; then
        if [ "$UV_INSTALLER_SHA256" != "$AVDFLEET_UV_INSTALLER_CHECKSUM" ]; then
            die "$EXIT_USAGE" "UV_INSTALLER_SHA256 and AVDFLEET_UV_INSTALLER_CHECKSUM disagree"
        fi
    fi
    if [ -n "$SDK_ROOT" ]; then
        case "$SDK_ROOT" in
            "~")
                SDK_ROOT=$HOME
                ;;
            "~/"*)
                SDK_ROOT="$HOME/${SDK_ROOT#\~/}"
                ;;
        esac
        case "$SDK_ROOT" in
            /*) ;;
            *)
                die "$EXIT_USAGE" "--sdk-root must be an absolute path: $SDK_ROOT"
                ;;
        esac
        if [ -e "$SDK_ROOT" ] && [ ! -d "$SDK_ROOT" ]; then
            die "$EXIT_USAGE" "--sdk-root exists but is not a directory: $SDK_ROOT"
        fi
        ANDROID_SDK_ROOT="$SDK_ROOT"
        ANDROID_HOME="$SDK_ROOT"
        export ANDROID_SDK_ROOT ANDROID_HOME
    fi
}

# ---------------------------------------------------------------------------
# Host detection
# ---------------------------------------------------------------------------

detect_host() {
    _uname_s="$(uname -s 2>/dev/null || printf 'unknown')"
    case "$_uname_s" in
        Linux)
            OS_NAME="linux"
            ;;
        Darwin)
            OS_NAME="macos"
            ;;
        MINGW*|MSYS*|CYGWIN*|Windows_NT)
            die "$EXIT_UNSUPPORTED" "this is a Windows shell. Run bootstrap/install.ps1 from PowerShell instead: powershell -ExecutionPolicy Bypass -File .\\bootstrap\\install.ps1"
            ;;
        *)
            die "$EXIT_UNSUPPORTED" "unsupported operating system: $_uname_s (supported: Linux, macOS)"
            ;;
    esac

    _uname_m="$(uname -m 2>/dev/null || printf 'unknown')"
    case "$_uname_m" in
        x86_64|amd64)
            ARCH="x86_64"
            ;;
        arm64|aarch64)
            ARCH="arm64"
            ;;
        *)
            die "$EXIT_UNSUPPORTED" "unsupported CPU architecture: $_uname_m (supported: x86_64, arm64)"
            ;;
    esac

    if [ "$OS_NAME" = "linux" ]; then
        if [ -n "${WSL_DISTRO_NAME:-}" ] || { [ -r /proc/version ] && grep -qi 'microsoft' /proc/version 2>/dev/null; }; then
            die "$EXIT_UNSUPPORTED" "WSL detected. The Android emulator needs Windows hardware virtualization; run bootstrap/install.ps1 on the Windows host instead."
        fi
        OS_ID="unknown"
        DISTRO_LABEL="Linux"
        if [ -r /etc/os-release ]; then
            # shellcheck disable=SC1091
            . /etc/os-release 2>/dev/null || true
            OS_ID=${ID:-unknown}
            DISTRO_LABEL=${PRETTY_NAME:-${NAME:-Linux}}
        elif [ -r /etc/redhat-release ]; then
            OS_ID=$(tr '[:upper:]' '[:lower:]' < /etc/redhat-release 2>/dev/null | sed -n 's/^\([a-z]*\).*/\1/p')
            DISTRO_LABEL=$(cat /etc/redhat-release 2>/dev/null)
        fi
        OS_ID=$(printf '%s' "$OS_ID" | tr '[:upper:]' '[:lower:]')
        case "$OS_ID" in
            ubuntu|debian|fedora|rocky|rhel|centos|almalinux|arch|manjaro)
                ;;
            unknown)
                warn "could not identify the Linux distribution; the installer will probe package managers itself."
                ;;
            *)
                warn "distribution '$OS_ID' is not one of the tested platforms (Ubuntu LTS, Debian, Fedora, Rocky/RHEL, Arch). The installer may not find a package provider."
                ;;
        esac
        if [ "$ARCH" = "arm64" ]; then
            if [ "${AVDFLEET_ALLOW_UNSUPPORTED_HOST:-0}" = "1" ]; then
                warn "Linux arm64 is not a supported Android emulator host; continuing because AVDFLEET_ALLOW_UNSUPPORTED_HOST=1 (CLI-only install)."
            else
                die "$EXIT_UNSUPPORTED" "Linux arm64 is not supported by the Android emulator. Use an x86_64 host (or the portable edition on a supported machine). Set AVDFLEET_ALLOW_UNSUPPORTED_HOST=1 to install the CLI only."
            fi
        fi
    elif [ "$OS_NAME" = "macos" ]; then
        OS_ID="macos"
        DISTRO_LABEL="macOS $(sw_vers -productVersion 2>/dev/null || printf 'unknown')"
        if [ "$ARCH" = "x86_64" ]; then
            warn "Intel Macs are supported but Apple has deprecated x86_64 emulator acceleration; expect slower boots. Apple silicon is the recommended macOS host."
        fi
    fi
    debug "host: os=$OS_NAME id=$OS_ID arch=$ARCH label=$DISTRO_LABEL"
}

# ---------------------------------------------------------------------------
# Prerequisites and host hints
# ---------------------------------------------------------------------------

check_prerequisites() {
    if [ -z "${HOME:-}" ]; then
        die "$EXIT_PREREQUISITE" "HOME is not set; cannot locate user install directories"
    fi
    if [ ! -d "$HOME" ]; then
        die "$EXIT_PREREQUISITE" "HOME does not point to a directory: $HOME"
    fi

    DOWNLOADER=""
    if have curl; then
        DOWNLOADER="curl"
    elif have wget; then
        DOWNLOADER="wget"
    elif [ "$DRY_RUN" -eq 1 ]; then
        warn "neither curl nor wget was found; a real run needs one of them to download uv."
    else
        die "$EXIT_PREREQUISITE" "curl or wget is required to download uv. Install one of them and re-run."
    fi
    debug "downloader: ${DOWNLOADER:-none}"

    if ! have sh && ! have bash; then
        die "$EXIT_PREREQUISITE" "no POSIX shell (sh) is available to run the uv installer"
    fi

    if [ "$OS_NAME" = "linux" ] && [ "$(id -u 2>/dev/null || printf '1')" -ne 0 ]; then
        if ! have sudo; then
            if [ "$DRY_RUN" -eq 1 ]; then
                warn "sudo was not found; the Android SDK/toolchain system steps need root."
            else
                die "$EXIT_PREREQUISITE" "sudo is required for system packages (run as root or install sudo). See bootstrap/README.md."
            fi
        fi
    fi

    if [ "$DOWNLOADER" = "curl" ]; then
        case "$(curl --version 2>/dev/null | head -n 1 || true)" in
            *TLS*|*Schannel*|*OpenSSL*|*LibreSSL*|*GnuTLS*|*SecureTransport*)
                ;;
            *)
                debug "could not confirm TLS support from 'curl --version'; HTTPS downloads may still work"
                ;;
        esac
    fi
}

check_free_space() {
    [ "$DRY_RUN" -eq 1 ] && return 0
    have df || return 0
    have awk || return 0
    _min_gb=${AVDFLEET_MIN_FREE_GB:-10}
    case "$_min_gb" in
        ''|*[!0-9]*)
            _min_gb=10
            ;;
    esac
    _min_gb=$(printf '%s' "$_min_gb" | sed 's/^0*//')
    [ -n "$_min_gb" ] || _min_gb=0
    _probe="$(default_data_dir)"
    while [ ! -d "$_probe" ] && [ "$_probe" != "/" ] && [ "$_probe" != "." ]; do
        _probe=$(dirname "$_probe" 2>/dev/null || printf '/')
    done
    [ -d "$_probe" ] || return 0
    _free_kb=$(df -Pk "$_probe" 2>/dev/null | awk 'NR==2 {print $4}')
    case "$_free_kb" in
        ''|*[!0-9]*)
            return 0
            ;;
    esac
    if [ "$_free_kb" -lt "$((_min_gb * 1024 * 1024))" ]; then
        warn "only $((_free_kb / 1024 / 1024)) GB free under $_probe; the Android SDK, emulator and system images can need ${_min_gb}+ GB. Set AVDFLEET_MIN_FREE_GB to change this warning threshold."
    fi
}

check_acceleration_hint() {
    if [ "$OS_NAME" = "linux" ]; then
        if [ -c /dev/kvm ]; then
            info "/dev/kvm is present (KVM acceleration available)"
            if [ "$(id -u 2>/dev/null || printf '1')" -ne 0 ] && have id && have grep; then
                if ! id -Gn 2>/dev/null | grep -qw kvm; then
                    warn "your user is not in the 'kvm' group; the installer adds you but a logout/login (or reboot) is needed."
                fi
            fi
        else
            warn "no /dev/kvm device found. The Android emulator needs KVM; the installer will report whether acceleration can be enabled (BIOS/UEFI virtualization and nested-virtualisation support)."
        fi
    elif [ "$OS_NAME" = "macos" ]; then
        if have sysctl && [ "$(sysctl -n kern.hv_support 2>/dev/null || printf '0')" = "1" ]; then
            info "Hypervisor framework is available (HVF acceleration)"
        else
            warn "the Hypervisor framework does not report support. Enable virtualization in firmware; the installer will report details."
        fi
    fi
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

prepare_path() {
    for _path_dir in "$LOCAL_BIN" "$HOME/.local/bin" "$HOME/.cargo/bin" "$HOME/bin"; do
        [ -n "$_path_dir" ] || continue
        case ":${PATH:-}:" in
            *":$_path_dir:"*)
                ;;
            *)
                PATH="$_path_dir:${PATH:-}"
                ;;
        esac
    done
    export PATH
    debug "PATH prepared with user tool directories"
}

default_data_dir() {
    if [ -n "${AVDFLEET_DATA_DIR:-}" ]; then
        printf '%s\n' "$AVDFLEET_DATA_DIR"
        return 0
    fi
    case "$OS_NAME" in
        macos)
            printf '%s\n' "$HOME/Library/Application Support/avdfleet"
            ;;
        *)
            printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/avdfleet"
            ;;
    esac
}

find_uv() {
    if have uv; then
        command -v uv
        return 0
    fi
    for _uv_candidate in "$LOCAL_BIN/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
        if [ -n "$_uv_candidate" ] && [ -f "$_uv_candidate" ] && [ -x "$_uv_candidate" ]; then
            printf '%s\n' "$_uv_candidate"
            return 0
        fi
    done
    return 1
}

find_avdfleet() {
    if have avdfleet; then
        command -v avdfleet
        return 0
    fi
    for _avd_candidate in "$LOCAL_BIN/avdfleet" "$HOME/.local/bin/avdfleet"; do
        if [ -n "$_avd_candidate" ] && [ -f "$_avd_candidate" ] && [ -x "$_avd_candidate" ]; then
            printf '%s\n' "$_avd_candidate"
            return 0
        fi
    done
    return 1
}

avdfleet_version_raw() {
    "$1" --version 2>/dev/null | head -n 1 || true
}

extract_version() {
    printf '%s\n' "$1" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*' 2>/dev/null | head -n 1 || true
}

# ---------------------------------------------------------------------------
# Downloads and checksums
# ---------------------------------------------------------------------------

download_file() {
    _dl_url=$1
    _dl_dest=$2
    case "$_dl_url" in
        https://*)
            ;;
        *)
            error "refusing to download a non-HTTPS URL: $_dl_url"
            return "$EXIT_PREREQUISITE"
            ;;
    esac
    debug "downloading $_dl_url"
    if [ "$DOWNLOADER" = "curl" ]; then
        if ! curl --fail --silent --show-error --location --retry 2 --retry-delay 2 --connect-timeout 30 --proto '=https' --tlsv1.2 --output "$_dl_dest" "$_dl_url"; then
            error "download failed: $_dl_url"
            return "$EXIT_PREREQUISITE"
        fi
    elif [ "$DOWNLOADER" = "wget" ]; then
        if ! wget --quiet --https-only --secure-protocol=TLSv1_2 --tries=3 --timeout=60 --output-document="$_dl_dest" "$_dl_url"; then
            error "download failed: $_dl_url"
            return "$EXIT_PREREQUISITE"
        fi
    else
        error "no HTTPS downloader is available (need curl or wget)"
        return "$EXIT_PREREQUISITE"
    fi
    if [ ! -s "$_dl_dest" ]; then
        error "downloaded file is empty: $_dl_url"
        return "$EXIT_PREREQUISITE"
    fi
    return 0
}

sha256_file() {
    _sha_file=$1
    if have shasum; then
        shasum -a 256 "$_sha_file" 2>/dev/null | awk '{print $1}'
        return 0
    fi
    if have sha256sum; then
        sha256sum "$_sha_file" 2>/dev/null | awk '{print $1}'
        return 0
    fi
    if have openssl; then
        openssl dgst -sha256 "$_sha_file" 2>/dev/null | awk '{print $NF}'
        return 0
    fi
    if have python3; then
        python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$_sha_file" 2>/dev/null
        return 0
    fi
    return 1
}

verify_checksum() {
    _vc_file=$1
    _vc_expected=$2
    _vc_expected=$(printf '%s' "$_vc_expected" | tr '[:upper:]' '[:lower:]' | sed 's/^sha256://' | tr -d '[:space:]')
    case "$_vc_expected" in
        *[!0-9a-f]*)
            error "UV_INSTALLER_SHA256 must be hexadecimal (got: $2)"
            return "$EXIT_PREREQUISITE"
            ;;
    esac
    if [ "${#_vc_expected}" -ne 64 ]; then
        error "UV_INSTALLER_SHA256 must be exactly 64 hexadecimal characters (got ${#_vc_expected})"
        return "$EXIT_PREREQUISITE"
    fi
    _vc_actual=$(sha256_file "$_vc_file" || true)
    if [ -z "$_vc_actual" ]; then
        error "no SHA-256 tool available (need shasum, sha256sum, openssl or python3) but UV_INSTALLER_SHA256 was set"
        return "$EXIT_PREREQUISITE"
    fi
    _vc_actual=$(printf '%s' "$_vc_actual" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    if [ "$_vc_actual" != "$_vc_expected" ]; then
        error "uv installer checksum mismatch"
        error "  expected: $_vc_expected"
        error "  actual:   $_vc_actual"
        return "$EXIT_PREREQUISITE"
    fi
    info "uv installer SHA-256 verified"
    return 0
}

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------

install_uv_official() {
    if [ -z "$DOWNLOADER" ]; then
        error "cannot download the uv installer: no curl/wget available"
        return "$EXIT_PREREQUISITE"
    fi
    _uv_url=${UV_INSTALLER_URL:-$DEFAULT_UV_INSTALLER_URL}
    if [ -n "${UV_VERSION:-}" ] && [ -z "${UV_INSTALLER_URL:-}" ]; then
        _uv_url="https://astral.sh/uv/${UV_VERSION}/install.sh"
    fi
    _uv_script="$TMP_DIR/uv-install.sh"
    info "Downloading uv installer from $_uv_url"
    if ! download_file "$_uv_url" "$_uv_script"; then
        return "$EXIT_PREREQUISITE"
    fi

    _expected_sha=${UV_INSTALLER_SHA256:-${AVDFLEET_UV_INSTALLER_CHECKSUM:-}}
    if [ -n "$_expected_sha" ]; then
        if ! verify_checksum "$_uv_script" "$_expected_sha"; then
            return "$EXIT_PREREQUISITE"
        fi
    else
        _actual_sha=$(sha256_file "$_uv_script" 2>/dev/null || true)
        if [ -n "$_actual_sha" ]; then
            info "uv installer SHA-256: $_actual_sha"
            info "Set UV_INSTALLER_SHA256=$_actual_sha to verify this download on future runs."
        fi
        warn "UV_INSTALLER_SHA256 is not set; the uv installer was downloaded over HTTPS but its integrity is not pinned. Astral publishes checksums only for versioned installer URLs - for a verified install set UV_VERSION and UV_INSTALLER_SHA256, or install uv yourself."
    fi

    _uv_shell=""
    if have sh; then
        _uv_shell="sh"
    elif have bash; then
        _uv_shell="bash"
    else
        error "no sh or bash available to run the uv installer"
        return "$EXIT_PREREQUISITE"
    fi

    info "Running the uv installer (UV_NO_MODIFY_PATH=1; shell profiles are not edited)"
    if ! UV_NO_MODIFY_PATH=1 "$_uv_shell" "$_uv_script"; then
        error "the uv installer failed"
        return "$EXIT_PREREQUISITE"
    fi
    return 0
}

install_uv_pip() {
    if ! have python3; then
        error "the uv installer failed and python3 is not available for the pip fallback"
        return "$EXIT_PREREQUISITE"
    fi
    if ! python3 -m pip --version >/dev/null 2>&1; then
        error "the uv installer failed and python3 has no pip module for the fallback"
        error "Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and re-run."
        return "$EXIT_PREREQUISITE"
    fi
    info "Trying fallback: python3 -m pip install --user uv"
    if ! python3 -m pip install --user --disable-pip-version-check uv; then
        error "python3 -m pip install --user uv failed (PEP 668 'externally managed' environments reject --user installs)"
        error "Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and re-run."
        return "$EXIT_PREREQUISITE"
    fi
    _py_user_bin=$(python3 -m site --user-base 2>/dev/null || true)
    if [ -n "$_py_user_bin" ] && [ -d "$_py_user_bin/bin" ]; then
        case ":${PATH:-}:" in
            *":$_py_user_bin/bin:"*) ;;
            *) PATH="$_py_user_bin/bin:${PATH:-}" ;;
        esac
        export PATH
    fi
    return 0
}

setup_uv() {
    info "Checking for uv"
    _uv_found="$(find_uv 2>/dev/null || true)"
    if [ -n "$_uv_found" ]; then
        UV_BIN=$_uv_found
        info "Using existing uv: $("$UV_BIN" --version 2>/dev/null | head -n 1 || true)"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] uv is not installed; would download the official uv installer (checksum-verified when UV_INSTALLER_SHA256 is set) and run it."
        return 0
    fi
    info "uv was not found; installing uv"
    _uv_install_status="$EXIT_PREREQUISITE"
    if install_uv_official; then
        _uv_install_status=0
    fi
    if [ "$_uv_install_status" -ne 0 ]; then
        warn "official uv installer did not succeed; trying the pip fallback"
        if ! install_uv_pip; then
            die "$EXIT_UV_INSTALL" "could not install uv. Install it manually from https://docs.astral.sh/uv/getting-started/installation/ and re-run."
        fi
    fi
    prepare_path
    _uv_found="$(find_uv 2>/dev/null || true)"
    if [ -z "$_uv_found" ]; then
        die "$EXIT_UV_INSTALL" "uv was installed but is not on PATH ($LOCAL_BIN). Open a new shell or add this directory to PATH and re-run."
    fi
    UV_BIN=$_uv_found
    info "uv installed: $("$UV_BIN" --version 2>/dev/null | head -n 1 || true)"
}

# ---------------------------------------------------------------------------
# avdfleet tool installation
# ---------------------------------------------------------------------------

is_local_package_spec() {
    case "$1" in
        /*|./*|../*|"~/"*|*.whl|*.tar.gz|*.tgz)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

resolve_package_spec() {
    _pkg_spec=${AVDFLEET_PACKAGE_SPEC:-$DEFAULT_PACKAGE_SPEC}
    _pkg_desired=${AVDFLEET_VERSION:-}
    if [ -n "$_pkg_desired" ] && [ "$_pkg_spec" = "$DEFAULT_PACKAGE_SPEC" ]; then
        _pkg_spec="${_pkg_spec}==${_pkg_desired}"
    fi
    if is_local_package_spec "$_pkg_spec"; then
        _pkg_check=$_pkg_spec
        case "$_pkg_check" in
            "~/"*)
                _pkg_check="$HOME/${_pkg_check#\~/}"
                ;;
        esac
        if [ ! -e "$_pkg_check" ]; then
            die "$EXIT_USAGE" "AVDFLEET_PACKAGE_SPEC points to a path that does not exist: $_pkg_spec"
        fi
    fi
    printf '%s\n' "$_pkg_spec"
}

install_tool() {
    _pkg_spec=$(resolve_package_spec)
    _pkg_desired=${AVDFLEET_VERSION:-}
    _avd_existing="$(find_avdfleet 2>/dev/null || true)"
    _avd_installed_version=""
    _force=0
    _force_reason=""

    if [ -n "$_avd_existing" ]; then
        _avd_raw=$(avdfleet_version_raw "$_avd_existing")
        _avd_installed_version=$(extract_version "$_avd_raw")
        if is_local_package_spec "$_pkg_spec"; then
            _force=1
            _force_reason="local package spec may have changed"
        elif [ -n "$_pkg_desired" ]; then
            if [ "$_avd_installed_version" != "$_pkg_desired" ]; then
                _force=1
                _force_reason="installed version '${_avd_installed_version:-unknown}' does not match requested '$_pkg_desired'"
            fi
        elif [ "${AVDFLEET_REINSTALL:-0}" = "1" ]; then
            _force=1
            _force_reason="AVDFLEET_REINSTALL=1"
        fi
    fi

    if [ -n "$_avd_existing" ] && [ "$_force" -eq 0 ]; then
        info "avdfleet is already installed: $_avd_existing (${_avd_installed_version:-version unknown})"
        info "Skipping 'uv tool install' (set AVDFLEET_REINSTALL=1 to reinstall, or AVDFLEET_VERSION=x.y.z to pin a version)."
        AVDFLEET_BIN=$_avd_existing
        return 0
    fi

    if [ -z "$UV_BIN" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            info "[dry-run] would run: uv tool install --force $_pkg_spec"
            return 0
        fi
        die "$EXIT_UV_INSTALL" "uv is not available, cannot install the avdfleet tool"
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        info "[dry-run] would run: $(show_cmd "$UV_BIN" tool install --force "$_pkg_spec")"
        return 0
    fi

    if [ -n "$_avd_existing" ]; then
        info "Reinstalling avdfleet tool ($_force_reason)"
    else
        info "Installing avdfleet tool: $_pkg_spec"
    fi

    if [ -n "${AVDFLEET_PYTHON:-}" ]; then
        set -- tool install --force --python "$AVDFLEET_PYTHON" "$_pkg_spec"
    else
        set -- tool install --force "$_pkg_spec"
    fi
    info "Running: $(show_cmd "$UV_BIN" "$@")"
    if ! "$UV_BIN" "$@"; then
        die "$EXIT_UV_INSTALL" "uv tool install failed for '$_pkg_spec'. Check the network/proxy settings and the package spec, then re-run."
    fi

    prepare_path
    _avd_existing="$(find_avdfleet 2>/dev/null || true)"
    if [ -z "$_avd_existing" ]; then
        die "$EXIT_UV_INSTALL" "avdfleet was installed by uv but no executable was found in $LOCAL_BIN. Add that directory to PATH and re-run."
    fi
    AVDFLEET_BIN=$_avd_existing
    info "avdfleet installed: $(avdfleet_version_raw "$AVDFLEET_BIN")"
}

# ---------------------------------------------------------------------------
# Reboot detection (contract with the installer engine: exit code 3010 and an
# incomplete checkpoint at <data_dir>/install-manifest.json)
# ---------------------------------------------------------------------------

log_mentions_reboot() {
    [ -f "$1" ] || return 1
    grep -Eiq 'reboot (is |was )?(required|needed)|requires (a )?reboot|restart (is |was )?(required|needed)|please reboot' "$1" 2>/dev/null
}

reboot_checkpoint_pending() {
    if [ "${AVDFLEET_IGNORE_REBOOT_MARKER:-0}" = "1" ]; then
        return 1
    fi
    _data_dir="$(default_data_dir)"
    for _legacy_marker in "$_data_dir/reboot-required" "$_data_dir/reboot.required"; do
        if [ -e "$_legacy_marker" ]; then
            return 0
        fi
    done
    _manifest="$_data_dir/install-manifest.json"
    if [ ! -f "$_manifest" ]; then
        return 1
    fi
    if have grep; then
        if grep -Eq '"completed_at"[[:space:]]*:[[:space:]]*null' "$_manifest" 2>/dev/null \
            && grep -Eq '"status"[[:space:]]*:[[:space:]]*"reboot_required"' "$_manifest" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Running the installer engine and doctor
# ---------------------------------------------------------------------------

# Stream a command's combined output to the terminal and to a log file.
# Sets STREAM_STATUS to the command's exit code.
run_streamed() {
    _rs_log=$1
    _rs_status_file=$2
    shift 2
    STREAM_STATUS=0
    rm -f "$_rs_status_file"
    : > "$_rs_log"
    ( set +e; "$@" 2>&1; printf '%s\n' "$?" > "$_rs_status_file" ) | tee "$_rs_log"
    if [ -f "$_rs_status_file" ]; then
        STREAM_STATUS=$(cat "$_rs_status_file" 2>/dev/null || printf '1')
    else
        STREAM_STATUS=1
    fi
    case "$STREAM_STATUS" in
        ''|*[!0-9]*)
            STREAM_STATUS=1
            ;;
    esac
}

run_installer() {
    if [ -z "$AVDFLEET_BIN" ] || [ ! -x "$AVDFLEET_BIN" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            info "[dry-run] avdfleet is not installed yet; after installation this command would run:"
            say "  $(show_cmd avdfleet "$@")"
            return 0
        fi
        die "$EXIT_UV_INSTALL" "avdfleet executable not found after installation; cannot run the installer engine"
    fi

    info "Running AVD Fleet installer engine:"
    say "  $(show_cmd "$AVDFLEET_BIN" "$@")"
    _status_file="$TMP_DIR/avdfleet-install.status"
    run_streamed "$INSTALL_LOG" "$_status_file" "$AVDFLEET_BIN" "$@"

    if [ "$DRY_RUN" -eq 1 ]; then
        if [ "$STREAM_STATUS" -ne 0 ]; then
            error "avdfleet install --dry-run failed with exit code $STREAM_STATUS"
            KEEP_TMP=1
            exit "$STREAM_STATUS"
        fi
        info "avdfleet install --dry-run completed; no changes were made."
        return 0
    fi

    _reboot=0
    _reboot_reason=""
    if [ "$STREAM_STATUS" -eq 3010 ]; then
        _reboot=1
        _reboot_reason="avdfleet install returned exit code 3010"
    elif [ "$STREAM_STATUS" -eq 194 ] && log_mentions_reboot "$INSTALL_LOG"; then
        # POSIX shells truncate exit statuses to 0-255, so 3010 is observed as 194.
        _reboot=1
        _reboot_reason="avdfleet install requested a reboot (exit code 3010/194)"
    fi
    if [ "$_reboot" -eq 0 ] && reboot_checkpoint_pending; then
        _reboot=1
        _reboot_reason="the installer checkpoint records a step in 'reboot_required' state"
    fi
    if [ "$_reboot" -eq 0 ] && [ "$STREAM_STATUS" -ne 0 ] && log_mentions_reboot "$INSTALL_LOG"; then
        _reboot=1
        _reboot_reason="the installer output requested a reboot"
    fi

    if [ "$_reboot" -eq 1 ]; then
        _resume_self=$(shell_quote "$0")
        _resume_cmd="${_resume_self}${ORIGINAL_ARGS_DISPLAY:-}"
        printf '\n'
        info "Reboot required to finish the installation."
        say "The installer stopped at a checkpoint (this is not a failure)."
        say "After rebooting, re-run the same command to resume:"
        say "  $_resume_cmd"
        say ""
        say "The installer resumes from <data_dir>/install-manifest.json; it does not start over."
        if [ "$OS_NAME" = "linux" ]; then
            say "On Linux a logout/login (or reboot) is also needed for 'kvm' group membership."
        fi
        say "If a stale checkpoint is wrongly reported, set AVDFLEET_IGNORE_REBOOT_MARKER=1."
        debug "reboot signal: $_reboot_reason"
        exit "$EXIT_REBOOT"
    fi

    if [ "$STREAM_STATUS" -ne 0 ]; then
        KEEP_TMP=1
        error "avdfleet install failed with exit code $STREAM_STATUS"
        say "Common causes: missing sudo/admin rights, no network or proxy needed, SDK licenses not accepted (pass --accept-android-sdk-licenses), or hypervisor (WHPX/KVM/HVF) not enabled."
        say "The full output is above and in: $INSTALL_LOG"
        say "After fixing the cause, re-run the same command; the installer is idempotent."
        exit "$STREAM_STATUS"
    fi
    info "avdfleet install completed successfully"
}

run_doctor() {
    if [ -z "$AVDFLEET_BIN" ] || [ ! -x "$AVDFLEET_BIN" ]; then
        die "$EXIT_UV_INSTALL" "avdfleet executable not found; cannot run 'avdfleet doctor'"
    fi
    info "Running final environment check: avdfleet doctor"
    _status_file="$TMP_DIR/avdfleet-doctor.status"
    _doctor_log="$TMP_DIR/avdfleet-doctor.log"
    run_streamed "$_doctor_log" "$_status_file" "$AVDFLEET_BIN" doctor
    if [ "$STREAM_STATUS" -ne 0 ]; then
        KEEP_TMP=1
        error "avdfleet doctor reported problems (exit code $STREAM_STATUS)"
        say "Review the checks above, then run 'avdfleet doctor --fix' or re-run this bootstrap."
        say "Doctor output is also in: $_doctor_log"
        exit "$STREAM_STATUS"
    fi
    info "avdfleet doctor: all checks passed"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

print_banner() {
    say "============================================================"
    say " AVD Fleet bootstrap installer ${BOOTSTRAP_VERSION}"
    say "============================================================"
    info "Host: ${DISTRO_LABEL} (${OS_NAME}/${ARCH})"
    info "Install mode: $([ "$DRY_RUN" -eq 1 ] && printf 'dry-run (no changes)' || printf 'real installation')"
}

print_success() {
    _avd_found="$(find_avdfleet 2>/dev/null || true)"
    printf '\n'
    info "AVD Fleet is ready."
    if [ -n "$_avd_found" ]; then
        say "  avdfleet: $_avd_found ($(avdfleet_version_raw "$_avd_found"))"
    fi
    if [ -n "$UV_BIN" ]; then
        say "  uv:       $UV_BIN ($("$UV_BIN" --version 2>/dev/null | head -n 1 || true))"
    fi
    if [ -n "$SDK_ROOT" ]; then
        say "  SDK root: $SDK_ROOT"
    fi
    say ""
    say "Next steps:"
    say "  avdfleet doctor         # re-run environment checks"
    say "  avdfleet serve          # start the API/GUI (default http://127.0.0.1:8765)"
    say "  avdfleet selftest --e2e # full OOBE/proxy/script self-test (needs acceleration)"
    if ! have avdfleet; then
        say ""
        say "Your shell does not know 'avdfleet' yet; add this directory to PATH:"
        say "  $LOCAL_BIN"
    fi
}

make_tmp_dir() {
    if have mktemp; then
        TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/avdfleet-bootstrap.XXXXXX" 2>/dev/null || true)
    fi
    if [ -z "$TMP_DIR" ] || [ ! -d "$TMP_DIR" ]; then
        TMP_DIR="${TMPDIR:-/tmp}/avdfleet-bootstrap.$$"
        if ! mkdir -p "$TMP_DIR"; then
            die "$EXIT_FAILURE" "cannot create a temporary directory under ${TMPDIR:-/tmp}"
        fi
    fi
    chmod 700 "$TMP_DIR" 2>/dev/null || true
    INSTALL_LOG="$TMP_DIR/avdfleet-install.log"
}

main() {
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP

    parse_args "$@"

    if [ "$SHOW_HELP" -eq 1 ]; then
        usage
        exit 0
    fi

    if [ -z "${HOME:-}" ]; then
        error "HOME is not set; cannot locate user install directories"
        exit "$EXIT_PREREQUISITE"
    fi

    LOCAL_BIN="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
    prepare_path

    if [ "$SHOW_VERSION" -eq 1 ]; then
        show_version
        exit 0
    fi

    make_tmp_dir
    validate_options

    detect_host
    print_banner
    check_prerequisites
    check_free_space
    check_acceleration_hint

    if [ "$ACCEPT_LICENSES" -eq 0 ]; then
        warn "Android SDK licenses are not pre-accepted. The SDK steps will stop and ask; pass --accept-android-sdk-licenses for an unattended install."
    fi
    if [ "$ASSUME_YES" -eq 0 ]; then
        warn "Interactive prompts may appear; pass --yes for an unattended install."
    fi

    setup_uv
    install_tool

    AVDFLEET_BIN="$(find_avdfleet 2>/dev/null || true)"
    if [ -z "$AVDFLEET_BIN" ] && [ "$DRY_RUN" -eq 0 ]; then
        die "$EXIT_UV_INSTALL" "avdfleet was not found after tool installation. Check that $LOCAL_BIN is on PATH."
    fi

    set -- install
    if [ "$ACCEPT_LICENSES" -eq 1 ]; then
        set -- "$@" --accept-android-sdk-licenses
    fi
    if [ "$ASSUME_YES" -eq 1 ]; then
        set -- "$@" --yes
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        set -- "$@" --dry-run
    fi
    if [ "$NO_APPIUM" -eq 1 ]; then
        set -- "$@" --no-appium
    fi
    if [ -n "$SDK_ROOT" ]; then
        set -- "$@" --sdk-root "$SDK_ROOT"
    fi
    run_installer "$@"

    if [ "$DRY_RUN" -eq 1 ]; then
        printf '\n'
        info "Dry run complete: no software was installed and no files were changed."
        exit 0
    fi

    run_doctor
    print_success
    exit 0
}

main "$@"
