#requires -version 5.1
# =============================================================================
# AVD Fleet - one-command bootstrap installer for Windows.
#
# Responsibilities (deliberately thin, per BRIEF section 3.2):
#   1. detect Windows version and architecture and refuse unsupported hosts
#   2. install uv: use an existing uv, otherwise try winget, then download the
#      official uv installer to a temporary file (never run a network
#      response directly through iex), verify it when UV_INSTALLER_SHA256 is
#      provided, and finally fall back to "python -m pip install --user uv"
#   3. "uv tool install avdfleet" (skipped when a usable version already
#      exists; --force is only used for outdated or explicitly requested
#      reinstalls)
#   4. run "avdfleet install ...", forwarding the operator's flags
#   5. treat exit code 3010 (or an incomplete reboot checkpoint in
#      install-manifest.json) as "reboot Windows, then re-run the same
#      command to resume"
#   6. run "avdfleet doctor" as the final acceptance check
#
# Compatible with Windows PowerShell 5.1 and PowerShell 7.x. The file is
# ASCII-only and avoids syntax that only exists in PowerShell 6+: no null
# coalescing, no null conditional operators, no ternary operator, no
# ForEach-Object -Parallel, no $IsWindows. Line endings are CRLF.
#
# This script never reads or prints secrets. It does not edit the user's
# shell profile: it refreshes PATH for the current process and prints the
# directory to add to PATH permanently.
# =============================================================================

param(
    [switch]$AcceptAndroidSdkLicenses,
    [switch]$Yes,
    [switch]$DryRun,
    [string]$SdkRoot = "",
    [switch]$NoAppium,
    [switch]$Verbose,
    [switch]$Version,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$global:LASTEXITCODE = 0

# Exit codes. Codes returned by avdfleet install/doctor are propagated
# unchanged, except 3010 (reboot required) which is reported here as 3010
# and which the caller should pass to the reboot scheduler (3010 is the
# standard ERROR_SUCCESS_REBOOT_REQUIRED code).
$script:ExitFailure = 1
$script:ExitUsage = 2
$script:ExitUnsupported = 3
$script:ExitPrerequisite = 4
$script:ExitUvInstall = 5
$script:ExitReboot = 3010

$script:BootstrapVersion = '0.1.0'
$script:ProgramName = 'install.ps1'
$script:DefaultPackageSpec = 'avdfleet'
$script:DefaultUvInstallerUrl = 'https://astral.sh/uv/install.ps1'

# Snapshot switches so every function sees the same values.
$script:AcceptLicenses = [bool]$AcceptAndroidSdkLicenses
$script:AssumeYes = [bool]$Yes
$script:NoAppium = [bool]$NoAppium
$script:DryRun = [bool]$DryRun
$script:Verbose = [bool]$Verbose
$script:SdkRoot = [string]$SdkRoot

# Paths and state filled in by Main / helper functions.
$script:HomeDir = $env:USERPROFILE
if (-not $script:HomeDir) { $script:HomeDir = $HOME }
$script:WorkDir = ''
$script:InstallOutLog = ''
$script:InstallErrLog = ''
$script:UvExe = ''
$script:AvdFleetExe = ''
$script:KeepWorkDir = $false
$script:ScriptPath = $PSCommandPath
if (-not $script:ScriptPath) {
    try { $script:ScriptPath = $MyInvocation.MyCommand.Path } catch { $script:ScriptPath = 'install.ps1' }
}
if (-not $script:ScriptPath) { $script:ScriptPath = 'install.ps1' }

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

function Write-Info {
    param($Message)
    Write-Host ('==> ' + [string]$Message)
}

function Write-Warn {
    param($Message)
    Write-Host ('WARNING: ' + [string]$Message) -ForegroundColor Yellow
}

function Write-Err {
    param($Message)
    Write-Host ('ERROR: ' + [string]$Message) -ForegroundColor Red
}

function Write-DebugMsg {
    param($Message)
    if ($script:Verbose) {
        Write-Host ('DEBUG: ' + [string]$Message) -ForegroundColor DarkGray
    }
}

function Stop-WithError {
    param($Message, $Code = 1)
    Write-Err $Message
    exit ([int]$Code)
}

function Show-Cmd {
    param($FilePath, $Arguments)
    if ($null -eq $Arguments) { $Arguments = @() }
    $parts = @([string]$FilePath)
    foreach ($a in $Arguments) {
        $text = [string]$a
        if ($text -match '[\s"]') {
            $parts += ('"' + ($text -replace '"', '\"') + '"')
        }
        else {
            $parts += $text
        }
    }
    return ($parts -join ' ')
}

function Get-SafeExitCode {
    param($Code)
    if ($null -eq $Code) { return 1 }
    $value = 0
    if (-not [int]::TryParse([string]$Code, [ref]$value)) { return 1 }
    if ($value -lt 0) { return 1 }
    return $value
}

# ---------------------------------------------------------------------------
# Help and version
# ---------------------------------------------------------------------------

function Show-Help {
    Write-Host @'
AVD Fleet bootstrap installer (Windows, PowerShell 5.1 and 7)

Usage:
  powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 [options]

Or from an existing PowerShell session with the execution policy already
permitting local scripts:
  .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes

Installs uv and AVD Fleet, then runs the AVD Fleet installer engine:
  uv tool install avdfleet
  avdfleet install [flags]
  avdfleet doctor

Options:
  -AcceptAndroidSdkLicenses   Accept the Android SDK licenses unattended.
                              Required for a fully non-interactive install
                              (BRIEF section 3.2 and R-INSTALL). Passed
                              through to avdfleet install.
  -Yes                        Assume "yes" for installer questions. Passed
                              through to avdfleet install.
  -DryRun                     Print the planned actions without installing
                              anything. If avdfleet is already installed,
                              its own "avdfleet install --dry-run" plan is
                              shown too.
  -SdkRoot PATH               Install the Android SDK under PATH instead of
                              the default data directory. Must be absolute.
  -NoAppium                   Skip the Appium server and driver steps.
                              Passed through to avdfleet install.
  -Verbose                    Verbose bootstrap diagnostics (does not change
                              the avdfleet log level).
  -Version                    Show bootstrap, uv and avdfleet versions; exit.
  -Help                       Show this help text and exit.

Execution policy:
  The recommended invocation is:
    powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 ...
  This bypasses the policy for this single process only. If you prefer a
  permanent per-user setting:
    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  If the downloaded repository was blocked by Mark-of-the-Web, run:
    Unblock-File -LiteralPath .\bootstrap\install.ps1

Environment overrides:
  AVDFLEET_PACKAGE_SPEC     Package spec passed to "uv tool install".
                            Default: avdfleet. Examples: a local wheel
                            (C:\build\avdfleet-0.1.0-py3-none-any.whl), a git
                            URL (git+https://example.invalid/avdfleet.git),
                            or a pinned version (avdfleet==0.1.0).
  AVDFLEET_VERSION          Desired avdfleet version. Used to decide whether
                            a reinstall with --force is needed and to pin the
                            default package spec.
  AVDFLEET_REINSTALL=1      Force "uv tool install --force" even when a
                            version is already installed.
  AVDFLEET_PYTHON           Optional Python interpreter passed to uv via
                            "--python" for the tool environment.
  AVDFLEET_DATA_DIR         Data directory used to look for the installer
                            checkpoint (reboot detection).
  AVDFLEET_IGNORE_REBOOT_MARKER=1
                            Do not treat an incomplete install manifest as a
                            pending reboot.
  AVDFLEET_MIN_FREE_GB      Minimum free disk space to warn about
                            (default 10).
  AVDFLEET_ALLOW_UNSUPPORTED_HOST=1
                            Continue on a host/architecture the emulator does
                            not support (installs the CLI only).
  UV_VERSION                Pin the uv installer URL to a specific version:
                            https://astral.sh/uv/<UV_VERSION>/install.ps1
  UV_INSTALLER_URL          Override the uv installer URL (must be https).
  UV_INSTALLER_SHA256       Expected SHA-256 of the downloaded uv installer.
                            Strongly recommended: without it the installer is
                            run with an unverified-integrity warning.
  AVDFLEET_UV_INSTALLER_CHECKSUM
                            Alias for UV_INSTALLER_SHA256.

Exit codes:
  0     Success (AVD Fleet installed and avdfleet doctor passed)
  1     Unexpected bootstrap failure
  2     Usage error (unknown argument, missing value, bad path)
  3     Unsupported operating system or architecture
  4     Prerequisite failure (download, checksum, network)
  5     Could not install uv or the avdfleet tool
  3010  Reboot required: reboot Windows, then re-run the same command
  N     Exit code propagated from avdfleet install or avdfleet doctor

Examples:
  powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes
  powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes -NoAppium
  powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -DryRun
  $env:UV_INSTALLER_SHA256='<sha256>'; powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes

See bootstrap/README.md for prerequisites, reboot/resume and troubleshooting.
'@
}

function Get-CommandVersionText {
    param($FilePath)
    if (-not $FilePath) { return '' }
    try {
        $out = & $FilePath --version 2>$null | Out-String
        return ([string]$out).Trim()
    }
    catch {
        return ''
    }
}

function Get-ParsedVersion {
    param($Text)
    if (-not $Text) { return '' }
    $match = [regex]::Match([string]$Text, '[0-9]+\.[0-9]+\.[0-9]+[^\s]*')
    if ($match.Success) { return $match.Value }
    return ''
}

function Show-Version {
    Write-Info ('avdfleet bootstrap ' + $script:BootstrapVersion + ' (' + $script:ProgramName + ')')
    $uv = Find-Uv
    if ($uv) {
        Write-Info ('uv: ' + (Get-CommandVersionText $uv))
    }
    else {
        Write-Info 'uv: not installed (or not on PATH)'
    }
    $avd = Find-AvdFleet
    if ($avd) {
        Write-Info ('avdfleet: ' + (Get-CommandVersionText $avd))
    }
    else {
        Write-Info 'avdfleet: not installed (or not on PATH)'
    }
}

# ---------------------------------------------------------------------------
# Host and prerequisite checks
# ---------------------------------------------------------------------------

function Get-HostInfo {
    if ($env:OS -ne 'Windows_NT') {
        Stop-WithError 'install.ps1 supports Windows only. On macOS/Linux run bootstrap/install.sh instead.' $script:ExitUnsupported
    }

    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    if (-not $arch) { $arch = 'unknown' }
    switch ($arch.ToUpperInvariant()) {
        'AMD64' { $script:Arch = 'x86_64' }
        'ARM64' { $script:Arch = 'arm64' }
        default { Stop-WithError ('Unsupported CPU architecture: ' + $arch + ' (supported: AMD64/x86_64).') $script:ExitUnsupported }
    }
    if ($script:Arch -eq 'arm64') {
        if ($env:AVDFLEET_ALLOW_UNSUPPORTED_HOST -eq '1') {
            Write-Warn 'Windows on ARM64 is not a supported Android emulator host; continuing because AVDFLEET_ALLOW_UNSUPPORTED_HOST=1 (CLI-only install).'
        }
        else {
            Stop-WithError 'Windows on ARM64 is not supported by the Android emulator. Use an x86_64 Windows host, or set AVDFLEET_ALLOW_UNSUPPORTED_HOST=1 to install the CLI only.' $script:ExitUnsupported
        }
    }

    $version = [Environment]::OSVersion.Version
    if ($version.Major -lt 10) {
        Stop-WithError ('Windows 10 or Windows 11 is required; detected ' + $version.ToString() + '.') $script:ExitUnsupported
    }
    if ($version.Major -eq 10 -and $version.Build -lt 19041) {
        Write-Warn ('Windows build ' + $version.Build + ' is older than 19041; Windows 11 or a current Windows 10 is recommended for WHPX and the Android emulator.')
    }
    $script:WindowsLabel = 'Windows ' + $version.Major + '.' + $version.Minor + ' build ' + $version.Build
    Write-DebugMsg ('host: windows=' + $script:WindowsLabel + ' arch=' + $script:Arch)
}

function Test-IsElevated {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    catch {
        return $false
    }
}

function Test-HypervisorHint {
    try {
        $computer = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
        if ($computer.HypervisorPresent) {
            Write-Info 'A hypervisor is present (WHPX/Hyper-V virtualization is available to the emulator)'
        }
        else {
            Write-Warn 'No hypervisor is reported by Windows. The Android emulator needs WHPX or AEHD; the installer engine will detect and enable it (this may require a reboot).'
        }
    }
    catch {
        Write-DebugMsg ('hypervisor probe failed: ' + $_.Exception.Message)
    }
}

function Test-DiskSpace {
    $minGb = 10
    if ($env:AVDFLEET_MIN_FREE_GB) {
        $parsed = 0
        if ([int]::TryParse([string]$env:AVDFLEET_MIN_FREE_GB, [ref]$parsed) -and $parsed -ge 0) {
            $minGb = $parsed
        }
    }
    try {
        $driveLetter = $env:SystemDrive
        if (-not $driveLetter) { $driveLetter = 'C:' }
        $drive = Get-PSDrive -Name $driveLetter.TrimEnd(':') -ErrorAction Stop
        if ($drive.Free -lt ([int64]$minGb * [int64]1073741824)) {
            Write-Warn ('only ' + [math]::Round($drive.Free / 1073741824, 1) + ' GB free on ' + $driveLetter + '; the Android SDK, emulator and system images can need ' + $minGb + '+ GB. Set AVDFLEET_MIN_FREE_GB to change this warning threshold.')
        }
    }
    catch {
        Write-DebugMsg ('disk space probe failed: ' + $_.Exception.Message)
    }
}

function Test-Prerequisites {
    if (-not (Test-IsElevated)) {
        Write-Warn 'this PowerShell session is not elevated. Enabling WHPX/AEHD and installing machine-wide tools may show a UAC prompt or fail; an elevated shell is recommended.'
    }
    try {
        $policy = Get-ExecutionPolicy
        if ($policy -eq 'Restricted' -or $policy -eq 'AllSigned') {
            Write-Warn ('the effective execution policy is ' + $policy + '. Run with: powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 ...')
        }
    }
    catch {
        Write-DebugMsg ('execution policy probe failed: ' + $_.Exception.Message)
    }
    try {
        if ($script:ScriptPath -and (Test-Path -LiteralPath $script:ScriptPath)) {
            $zone = Get-Item -LiteralPath $script:ScriptPath -Stream Zone.Identifier -ErrorAction Stop
            if ($zone) {
                Write-Warn 'this script has a Mark-of-the-Web. If execution is blocked, run: Unblock-File -LiteralPath .\bootstrap\install.ps1'
            }
        }
    }
    catch {
        Write-DebugMsg 'no Mark-of-the-Web detected'
    }
    Test-DiskSpace
    Test-HypervisorHint
}

function Validate-Options {
    if ($env:UV_INSTALLER_URL) {
        if (-not $env:UV_INSTALLER_URL.StartsWith('https://', [StringComparison]::OrdinalIgnoreCase)) {
            Stop-WithError ('UV_INSTALLER_URL must use https (refusing: ' + $env:UV_INSTALLER_URL + ')') $script:ExitUsage
        }
    }
    if ($env:UV_INSTALLER_SHA256 -and $env:AVDFLEET_UV_INSTALLER_CHECKSUM) {
        $a = $env:UV_INSTALLER_SHA256.Trim().ToLowerInvariant()
        $b = $env:AVDFLEET_UV_INSTALLER_CHECKSUM.Trim().ToLowerInvariant()
        if ($a -ne $b) {
            Stop-WithError 'UV_INSTALLER_SHA256 and AVDFLEET_UV_INSTALLER_CHECKSUM disagree' $script:ExitUsage
        }
    }

    if ($script:SdkRoot) {
        $expanded = Expand-UserPath $script:SdkRoot
        if (-not [System.IO.Path]::IsPathRooted($expanded)) {
            Stop-WithError ('-SdkRoot must be an absolute path: ' + $script:SdkRoot) $script:ExitUsage
        }
        if (Test-Path -LiteralPath $expanded) {
            if (-not (Test-Path -LiteralPath $expanded -PathType Container)) {
                Stop-WithError ('-SdkRoot exists but is not a directory: ' + $expanded) $script:ExitUsage
            }
        }
        $script:SdkRoot = $expanded
        $env:ANDROID_SDK_ROOT = $expanded
        $env:ANDROID_HOME = $expanded
    }
}

function Expand-UserPath {
    param($Path)
    $text = [string]$Path
    if ($text -eq '~') { return $script:HomeDir }
    if ($text.StartsWith('~\')) { return (Join-Path $script:HomeDir $text.Substring(2)) }
    return $text
}

# ---------------------------------------------------------------------------
# PATH and executable discovery
# ---------------------------------------------------------------------------

function Get-LocalBin {
    if ($env:UV_TOOL_BIN_DIR) { return $env:UV_TOOL_BIN_DIR }
    if ($script:HomeDir) { return (Join-Path $script:HomeDir '.local\bin') }
    return '.local\bin'
}

function Get-PythonUserScriptDirs {
    $dirs = @()
    if ($env:APPDATA) {
        $root = Join-Path $env:APPDATA 'Python'
        if (Test-Path -LiteralPath $root) {
            try {
                $children = Get-ChildItem -LiteralPath $root -Directory -ErrorAction Stop
                foreach ($child in $children) {
                    $candidate = Join-Path $child.FullName 'Scripts'
                    if (Test-Path -LiteralPath $candidate) { $dirs += $candidate }
                }
            }
            catch {
                Write-DebugMsg 'could not inspect APPDATA\Python'
            }
        }
    }
    return $dirs
}

function Update-ProcessPath {
    $parts = @()
    $seen = @{}
    foreach ($source in @($env:Path, [Environment]::GetEnvironmentVariable('Path', 'User'), [Environment]::GetEnvironmentVariable('Path', 'Machine'))) {
        if (-not $source) { continue }
        foreach ($piece in ([string]$source).Split(';')) {
            if (-not $piece) { continue }
            if (-not $seen.ContainsKey($piece)) {
                $seen[$piece] = $true
                $parts += $piece
            }
        }
    }
    $prepend = @(
        (Get-LocalBin),
        (Join-Path $script:HomeDir '.local\bin'),
        (Join-Path $script:HomeDir '.cargo\bin'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv')
    )
    foreach ($dir in (Get-PythonUserScriptDirs)) { $prepend += $dir }
    $ordered = @()
    foreach ($dir in $prepend) {
        if (-not $dir) { continue }
        if (-not $seen.ContainsKey($dir)) {
            $seen[$dir] = $true
            $ordered += $dir
        }
    }
    $env:Path = (($ordered + $parts) -join ';')
    Write-DebugMsg 'PATH refreshed for this process'
}

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path (Get-LocalBin) 'uv.exe'),
        (Join-Path $script:HomeDir '.local\bin\uv.exe'),
        (Join-Path $script:HomeDir '.cargo\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe')
    )
    foreach ($dir in (Get-PythonUserScriptDirs)) { $candidates += (Join-Path $dir 'uv.exe') }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Find-AvdFleet {
    $command = Get-Command avdfleet -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path (Get-LocalBin) 'avdfleet.exe'),
        (Join-Path (Get-LocalBin) 'avdfleet.cmd'),
        (Join-Path $script:HomeDir '.local\bin\avdfleet.exe'),
        (Join-Path $script:HomeDir '.local\bin\avdfleet.cmd')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Downloads and checksums
# ---------------------------------------------------------------------------

function Download-File {
    param($Url, $Destination)
    if (-not $Url) {
        Write-Err 'no download URL provided'
        return $false
    }
    if (-not ([string]$Url).StartsWith('https://', [StringComparison]::OrdinalIgnoreCase)) {
        Write-Err ('refusing to download a non-HTTPS URL: ' + $Url)
        return $false
    }
    try {
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        }
        catch {
            Write-DebugMsg 'could not set TLS 1.2 explicitly; using the platform default'
        }
        Write-DebugMsg ('downloading ' + $Url)
        $params = @{
            Uri = [string]$Url
            OutFile = [string]$Destination
            TimeoutSec = 120
            ErrorAction = 'Stop'
        }
        if ($PSVersionTable.PSVersion.Major -lt 6) {
            $params['UseBasicParsing'] = $true
        }
        Invoke-WebRequest @params
    }
    catch {
        Write-Err ('download failed: ' + $Url + ' (' + $_.Exception.Message + ')')
        return $false
    }
    if (-not (Test-Path -LiteralPath $Destination)) {
        Write-Err ('download did not produce a file: ' + $Destination)
        return $false
    }
    if ((Get-Item -LiteralPath $Destination).Length -le 0) {
        Write-Err ('downloaded file is empty: ' + $Url)
        return $false
    }
    return $true
}

function Get-Sha256 {
    param($Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-Checksum {
    param($Path, $Expected)
    $expectedText = ([string]$Expected).Trim().ToLowerInvariant()
    if ($expectedText.StartsWith('sha256:')) { $expectedText = $expectedText.Substring(7) }
    if ($expectedText -notmatch '^[0-9a-f]{64}$') {
        Write-Err 'UV_INSTALLER_SHA256 must be exactly 64 hexadecimal characters'
        return $false
    }
    $actualText = Get-Sha256 $Path
    if ($actualText -ne $expectedText) {
        Write-Err 'uv installer checksum mismatch'
        Write-Err ('  expected: ' + $expectedText)
        Write-Err ('  actual:   ' + $actualText)
        return $false
    }
    Write-Info 'uv installer SHA-256 verified'
    return $true
}

# ---------------------------------------------------------------------------
# Native command execution
# ---------------------------------------------------------------------------

function Invoke-StreamedCommand {
    param($FilePath, $Arguments, $OutLog, $ErrLog)
    if ($null -eq $Arguments) { $Arguments = @() }
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @Arguments 2> $ErrLog | Tee-Object -FilePath $OutLog | Out-Host
        $code = $global:LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
    return [int](Get-SafeExitCode $code)
}

# ---------------------------------------------------------------------------
# uv installation
# ---------------------------------------------------------------------------

function Install-UvWinget {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-DebugMsg 'winget is not available'
        return 1
    }
    Write-Info 'Installing uv with winget (package id astral-sh.uv; winget verifies package hashes)'
    $arguments = @(
        'install',
        '--id', 'astral-sh.uv',
        '--exact',
        '--silent',
        '--accept-package-agreements',
        '--accept-source-agreements'
    )
    $code = Invoke-StreamedCommand -FilePath $winget.Source -Arguments $arguments -OutLog (Join-Path $script:WorkDir 'winget.out.log') -ErrLog (Join-Path $script:WorkDir 'winget.err.log')
    if ($code -ne 0) {
        Write-Warn ('winget could not install uv (exit code ' + $code + '); falling back to the official uv installer')
        return 1
    }
    Update-ProcessPath
    return 0
}

function Install-UvOfficial {
    $url = $script:DefaultUvInstallerUrl
    if ($env:UV_INSTALLER_URL) {
        $url = $env:UV_INSTALLER_URL
    }
    elseif ($env:UV_VERSION) {
        $url = 'https://astral.sh/uv/' + $env:UV_VERSION + '/install.ps1'
    }
    $installerPath = Join-Path $script:WorkDir 'uv-install.ps1'
    Write-Info ('Downloading uv installer from ' + $url)
    if (-not (Download-File -Url $url -Destination $installerPath)) {
        return $script:ExitPrerequisite
    }

    $expected = $env:UV_INSTALLER_SHA256
    if (-not $expected) { $expected = $env:AVDFLEET_UV_INSTALLER_CHECKSUM }
    if ($expected) {
        if (-not (Test-Checksum -Path $installerPath -Expected $expected)) {
            return $script:ExitPrerequisite
        }
    }
    else {
        try {
            $actual = Get-Sha256 $installerPath
            Write-Info ('uv installer SHA-256: ' + $actual)
            Write-Info ('Set UV_INSTALLER_SHA256=' + $actual + ' to verify this download on future runs.')
        }
        catch {
            Write-DebugMsg 'could not compute the uv installer SHA-256'
        }
        Write-Warn 'UV_INSTALLER_SHA256 is not set: the uv installer was downloaded over HTTPS but its integrity is not pinned. For a verified install set UV_VERSION and UV_INSTALLER_SHA256, or install uv yourself.'
    }

    $hostExe = Join-Path $PSHOME 'powershell.exe'
    if ($PSVersionTable.PSEdition -eq 'Core') {
        $hostExe = Join-Path $PSHOME 'pwsh.exe'
    }
    Write-Info 'Running the uv installer in a child PowerShell process'
    $code = Invoke-StreamedCommand -FilePath $hostExe -Arguments @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $installerPath) -OutLog (Join-Path $script:WorkDir 'uv-installer.out.log') -ErrLog (Join-Path $script:WorkDir 'uv-installer.err.log')
    if ($code -ne 0) {
        Write-Err ('the uv installer failed with exit code ' + $code)
        return $script:ExitPrerequisite
    }
    Update-ProcessPath
    return 0
}

function Install-UvPip {
    $pythonExe = $null
    $prefix = @()
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $pythonExe = $python.Source
    }
    else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            $pythonExe = $launcher.Source
            $prefix = @('-3')
        }
    }
    if (-not $pythonExe) {
        Write-Err 'the uv installer failed and no Python interpreter is available for the pip fallback'
        return $script:ExitPrerequisite
    }
    Write-Info 'Trying fallback: python -m pip install --user uv'
    $arguments = $prefix + @('-m', 'pip', 'install', '--user', '--disable-pip-version-check', 'uv')
    $code = Invoke-StreamedCommand -FilePath $pythonExe -Arguments $arguments -OutLog (Join-Path $script:WorkDir 'pip-uv.out.log') -ErrLog (Join-Path $script:WorkDir 'pip-uv.err.log')
    if ($code -ne 0) {
        Write-Err ('python -m pip install --user uv failed with exit code ' + $code)
        Write-Err 'Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and re-run.'
        return $script:ExitPrerequisite
    }
    Update-ProcessPath
    return 0
}

function Install-Uv {
    $existing = Find-Uv
    if ($existing) {
        $script:UvExe = $existing
        Write-Info ('Using existing uv: ' + (Get-CommandVersionText $existing))
        return
    }
    if ($script:DryRun) {
        Write-Info '[dry-run] uv is not installed; would install it with winget or the official uv installer (checksum-verified when UV_INSTALLER_SHA256 is set).'
        return
    }
    Write-Info 'uv was not found; installing uv'
    $status = Install-UvWinget
    if ($status -ne 0) {
        Write-DebugMsg 'winget path unavailable; trying the official installer'
        $status = Install-UvOfficial
    }
    if ($status -ne 0) {
        Write-Warn 'official uv installer did not succeed; trying the pip fallback'
        $status = Install-UvPip
    }
    if ($status -ne 0) {
        Stop-WithError 'could not install uv. Install it manually from https://docs.astral.sh/uv/getting-started/installation/ and re-run.' $script:ExitUvInstall
    }
    Update-ProcessPath
    $existing = Find-Uv
    if (-not $existing) {
        Stop-WithError ('uv was installed but is not on PATH (' + (Get-LocalBin) + '). Open a new shell or add that directory to PATH and re-run.') $script:ExitUvInstall
    }
    $script:UvExe = $existing
    Write-Info ('uv installed: ' + (Get-CommandVersionText $existing))
}

# ---------------------------------------------------------------------------
# avdfleet tool installation
# ---------------------------------------------------------------------------

function Test-LocalPackageSpec {
    param($Spec)
    $text = [string]$Spec
    if (-not $text) { return $false }
    if ($text -match '^(\.{1,2}[\\/]|[A-Za-z]:[\\/]|\\\\|/)') { return $true }
    if ($text -match '\.(whl|tar\.gz|tgz)$') { return $true }
    return $false
}

function Resolve-PackageSpec {
    $spec = $env:AVDFLEET_PACKAGE_SPEC
    if (-not $spec) { $spec = $script:DefaultPackageSpec }
    if ($env:AVDFLEET_VERSION -and $spec -eq $script:DefaultPackageSpec) {
        $spec = $spec + '==' + $env:AVDFLEET_VERSION
    }
    if (Test-LocalPackageSpec $spec) {
        $check = Expand-UserPath $spec
        if (-not (Test-Path -LiteralPath $check)) {
            Stop-WithError ('AVDFLEET_PACKAGE_SPEC points to a path that does not exist: ' + $spec) $script:ExitUsage
        }
    }
    return $spec
}

function Install-Tool {
    $packageSpec = Resolve-PackageSpec
    $desired = $env:AVDFLEET_VERSION
    $existing = Find-AvdFleet
    $force = $false
    $reason = ''
    if ($existing) {
        $installedVersion = Get-ParsedVersion (Get-CommandVersionText $existing)
        if (Test-LocalPackageSpec $packageSpec) {
            $force = $true
            $reason = 'local package spec may have changed'
        }
        elseif ($desired) {
            if ($installedVersion -ne $desired) {
                $force = $true
                $reason = "installed version '$installedVersion' does not match requested '$desired'"
            }
        }
        elseif ($env:AVDFLEET_REINSTALL -eq '1') {
            $force = $true
            $reason = 'AVDFLEET_REINSTALL=1'
        }
    }

    if ($existing -and -not $force) {
        Write-Info ('avdfleet is already installed: ' + $existing + ' (' + (Get-ParsedVersion (Get-CommandVersionText $existing)) + ')')
        Write-Info "Skipping 'uv tool install' (set AVDFLEET_REINSTALL=1 to reinstall, or AVDFLEET_VERSION=x.y.z to pin a version)."
        $script:AvdFleetExe = $existing
        return
    }

    if (-not $script:UvExe) {
        if ($script:DryRun) {
            Write-Info ('[dry-run] would run: uv tool install --force ' + $packageSpec)
            return
        }
        Stop-WithError 'uv is not available; cannot install the avdfleet tool' $script:ExitUvInstall
    }

    if ($script:DryRun) {
        Write-Info ('[dry-run] would run: ' + (Show-Cmd -FilePath $script:UvExe -Arguments @('tool', 'install', '--force', $packageSpec)))
        return
    }

    if ($existing) {
        Write-Info ('Reinstalling avdfleet tool (' + $reason + ')')
    }
    else {
        Write-Info ('Installing avdfleet tool: ' + $packageSpec)
    }

    $arguments = @('tool', 'install', '--force')
    if ($env:AVDFLEET_PYTHON) {
        $arguments += '--python'
        $arguments += $env:AVDFLEET_PYTHON
    }
    $arguments += $packageSpec
    Write-Info ('Running: ' + (Show-Cmd $script:UvExe $arguments))
    $code = Invoke-StreamedCommand -FilePath $script:UvExe -Arguments $arguments -OutLog (Join-Path $script:WorkDir 'uv-tool-install.out.log') -ErrLog (Join-Path $script:WorkDir 'uv-tool-install.err.log')
    if ($code -ne 0) {
        Stop-WithError ("uv tool install failed for '" + $packageSpec + "' (exit code " + $code + '). Check the network/proxy settings and the package spec, then re-run.') $script:ExitUvInstall
    }
    Update-ProcessPath
    $existing = Find-AvdFleet
    if (-not $existing) {
        Stop-WithError ('avdfleet was installed by uv but no executable was found in ' + (Get-LocalBin) + '. Add that directory to PATH and re-run.') $script:ExitUvInstall
    }
    $script:AvdFleetExe = $existing
    Write-Info ('avdfleet installed: ' + (Get-CommandVersionText $existing))
}

# ---------------------------------------------------------------------------
# Reboot detection (contract with the installer engine: exit code 3010 and an
# incomplete checkpoint at <data_dir>\install-manifest.json)
# ---------------------------------------------------------------------------

function Get-DataDir {
    if ($env:AVDFLEET_DATA_DIR) { return $env:AVDFLEET_DATA_DIR }
    $base = $env:LOCALAPPDATA
    if (-not $base) {
        if ($script:HomeDir) { $base = Join-Path $script:HomeDir 'AppData\Local' }
    }
    if (-not $base) { return 'avdfleet' }
    return (Join-Path $base 'avdfleet')
}

function Test-RebootCheckpointPending {
    if ($env:AVDFLEET_IGNORE_REBOOT_MARKER -eq '1') { return $false }
    $dataDir = Get-DataDir
    foreach ($legacy in @('reboot-required', 'reboot.required')) {
        if (Test-Path -LiteralPath (Join-Path $dataDir $legacy)) { return $true }
    }
    $manifest = Join-Path $dataDir 'install-manifest.json'
    if (-not (Test-Path -LiteralPath $manifest)) { return $false }
    try {
        $text = Get-Content -LiteralPath $manifest -Raw -ErrorAction Stop
        if (($text -match '"completed_at"\s*:\s*null') -and ($text -match '"status"\s*:\s*"reboot_required"')) {
            return $true
        }
    }
    catch {
        Write-DebugMsg ('could not read the install manifest: ' + $_.Exception.Message)
    }
    return $false
}

function Test-LogMentionsReboot {
    param($Path)
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $text = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        return [bool]($text -match '(?i)reboot (is |was )?(required|needed)|requires (a )?reboot|restart (is |was )?(required|needed)|please reboot')
    }
    catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# Installer engine and doctor
# ---------------------------------------------------------------------------

function Get-InstallerArguments {
    $arguments = @('install')
    if ($script:AcceptLicenses) { $arguments += '--accept-android-sdk-licenses' }
    if ($script:AssumeYes) { $arguments += '--yes' }
    if ($script:DryRun) { $arguments += '--dry-run' }
    if ($script:NoAppium) { $arguments += '--no-appium' }
    if ($script:SdkRoot) {
        $arguments += '--sdk-root'
        $arguments += $script:SdkRoot
    }
    return $arguments
}

function Get-ResumeCommand {
    $arguments = @()
    if ($script:AcceptLicenses) { $arguments += '-AcceptAndroidSdkLicenses' }
    if ($script:AssumeYes) { $arguments += '-Yes' }
    if ($script:NoAppium) { $arguments += '-NoAppium' }
    if ($script:SdkRoot) { $arguments += ('-SdkRoot "' + $script:SdkRoot + '"') }
    $text = 'powershell -ExecutionPolicy Bypass -File "' + $script:ScriptPath + '"'
    if ($arguments.Count -gt 0) { $text = $text + ' ' + ($arguments -join ' ') }
    return $text
}

function Invoke-InstallerEngine {
    if (-not $script:AvdFleetExe -or -not (Test-Path -LiteralPath $script:AvdFleetExe)) {
        if ($script:DryRun) {
            Write-Info '[dry-run] avdfleet is not installed yet; after installation this command would run:'
            Write-Host ('  ' + (Show-Cmd 'avdfleet' (Get-InstallerArguments)))
            return
        }
        Stop-WithError 'avdfleet executable not found after installation; cannot run the installer engine' $script:ExitUvInstall
    }

    $arguments = Get-InstallerArguments
    Write-Info 'Running AVD Fleet installer engine:'
    Write-Host ('  ' + (Show-Cmd $script:AvdFleetExe $arguments))
    $code = Invoke-StreamedCommand -FilePath $script:AvdFleetExe -Arguments $arguments -OutLog $script:InstallOutLog -ErrLog $script:InstallErrLog

    if ($script:DryRun) {
        if ($code -ne 0) {
            $script:KeepWorkDir = $true
            Stop-WithError ('avdfleet install --dry-run failed with exit code ' + $code + '. Logs: ' + $script:WorkDir) $code
        }
        Write-Info 'avdfleet install --dry-run completed; no changes were made.'
        return
    }

    $reboot = $false
    $rebootReason = ''
    if ($code -eq 3010) {
        $reboot = $true
        $rebootReason = 'avdfleet install returned exit code 3010'
    }
    if (-not $reboot -and (Test-RebootCheckpointPending)) {
        $reboot = $true
        $rebootReason = "the installer checkpoint records a step in 'reboot_required' state"
    }
    if (-not $reboot -and $code -ne 0 -and ((Test-LogMentionsReboot $script:InstallOutLog) -or (Test-LogMentionsReboot $script:InstallErrLog))) {
        $reboot = $true
        $rebootReason = 'the installer output requested a reboot'
    }

    if ($reboot) {
        $script:KeepWorkDir = $true
        Write-Host ''
        Write-Info 'Reboot required to finish the installation.'
        Write-Host 'The installer stopped at a checkpoint (this is not a failure).'
        Write-Host 'After rebooting Windows, re-run the same command to resume:'
        Write-Host ('  ' + (Get-ResumeCommand))
        Write-Host ''
        Write-Host 'The installer resumes from <data_dir>\install-manifest.json; it does not start over.'
        Write-Host 'If a stale checkpoint is wrongly reported, set AVDFLEET_IGNORE_REBOOT_MARKER=1.'
        Write-DebugMsg ('reboot signal: ' + $rebootReason)
        exit $script:ExitReboot
    }

    if ($code -ne 0) {
        $script:KeepWorkDir = $true
        Write-Err ('avdfleet install failed with exit code ' + $code)
        Write-Host 'Common causes: not running elevated (WHPX/AEHD changes need admin), no network or a proxy is required, SDK licenses not accepted (pass -AcceptAndroidSdkLicenses), or virtualization is disabled in firmware.'
        Write-Host ('Full output: ' + $script:InstallOutLog)
        Write-Host ('Error output: ' + $script:InstallErrLog)
        Write-Host 'After fixing the cause, re-run the same command; the installer is idempotent.'
        exit $code
    }
    Write-Info 'avdfleet install completed successfully'
}

function Invoke-Doctor {
    if (-not $script:AvdFleetExe -or -not (Test-Path -LiteralPath $script:AvdFleetExe)) {
        Stop-WithError 'avdfleet executable not found; cannot run avdfleet doctor' $script:ExitUvInstall
    }
    Write-Info 'Running final environment check: avdfleet doctor'
    $code = Invoke-StreamedCommand -FilePath $script:AvdFleetExe -Arguments @('doctor') -OutLog (Join-Path $script:WorkDir 'doctor.out.log') -ErrLog (Join-Path $script:WorkDir 'doctor.err.log')
    if ($code -ne 0) {
        $script:KeepWorkDir = $true
        Write-Err ('avdfleet doctor reported problems (exit code ' + $code + ')')
        Write-Host "Review the checks above, then run 'avdfleet doctor --fix' or re-run this bootstrap."
        Write-Host ('Doctor output: ' + (Join-Path $script:WorkDir 'doctor.out.log'))
        exit $code
    }
    Write-Info 'avdfleet doctor: all checks passed'
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

function Show-Banner {
    Write-Host '============================================================'
    Write-Host (' AVD Fleet bootstrap installer ' + $script:BootstrapVersion)
    Write-Host '============================================================'
    Write-Info ('Host: ' + $script:WindowsLabel + ' (' + $script:Arch + ')')
    if ($script:DryRun) {
        Write-Info 'Install mode: dry-run (no changes)'
    }
    else {
        Write-Info 'Install mode: real installation'
    }
}

function Show-Success {
    Write-Host ''
    Write-Info 'AVD Fleet is ready.'
    $avd = Find-AvdFleet
    if ($avd) {
        Write-Host ('  avdfleet: ' + $avd + ' (' + (Get-CommandVersionText $avd) + ')')
    }
    if ($script:UvExe) {
        Write-Host ('  uv:       ' + $script:UvExe + ' (' + (Get-CommandVersionText $script:UvExe) + ')')
    }
    if ($script:SdkRoot) {
        Write-Host ('  SDK root: ' + $script:SdkRoot)
    }
    Write-Host ''
    Write-Host 'Next steps:'
    Write-Host '  avdfleet doctor         # re-run environment checks'
    Write-Host '  avdfleet serve          # start the API/GUI (default http://127.0.0.1:8765)'
    Write-Host '  avdfleet selftest --e2e # full OOBE/proxy/script self-test (needs acceleration)'
    $command = Get-Command avdfleet -ErrorAction SilentlyContinue
    if (-not $command) {
        Write-Host ''
        Write-Host "This shell does not know 'avdfleet' yet; add this directory to PATH:"
        Write-Host ('  ' + (Get-LocalBin))
    }
}

function New-WorkDir {
    $base = $env:TEMP
    if (-not $base) { $base = '.' }
    $dir = Join-Path $base ('avdfleet-bootstrap-' + $PID)
    try {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    catch {
        Stop-WithError ('cannot create a temporary directory under ' + $base + ': ' + $_.Exception.Message) $script:ExitFailure
    }
    $script:WorkDir = $dir
    $script:InstallOutLog = Join-Path $dir 'avdfleet-install.out.log'
    $script:InstallErrLog = Join-Path $dir 'avdfleet-install.err.log'
}

function Remove-WorkDir {
    if ($script:KeepWorkDir) { return }
    if ($script:WorkDir -and (Test-Path -LiteralPath $script:WorkDir)) {
        try {
            Remove-Item -LiteralPath $script:WorkDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        catch {
            Write-DebugMsg 'could not remove the temporary directory'
        }
    }
}

function Main {
    if ($Help) {
        Show-Help
        exit 0
    }
    if ($Version) {
        Update-ProcessPath
        Show-Version
        exit 0
    }

    Update-ProcessPath
    Validate-Options
    Get-HostInfo
    Show-Banner
    Test-Prerequisites
    New-WorkDir

    Install-Uv
    Install-Tool
    if (-not $script:AvdFleetExe) {
        $foundAvdFleet = Find-AvdFleet
        if ($foundAvdFleet) { $script:AvdFleetExe = $foundAvdFleet }
    }
    Invoke-InstallerEngine

    if ($script:DryRun) {
        Write-Host ''
        Write-Info 'Dry run complete: no software was installed and no files were changed.'
        Remove-WorkDir
        exit 0
    }

    Invoke-Doctor
    Show-Success
    Remove-WorkDir
    exit 0
}

# Unknown named or extra positional arguments are not rejected by the
# PowerShell parameter binder for simple scripts, so check them explicitly.
if ($args.Count -gt 0) {
    Write-Err ('unknown argument(s): ' + ($args -join ' '))
    Write-Host 'Run bootstrap\install.ps1 -Help for usage.'
    exit $script:ExitUsage
}

Main
