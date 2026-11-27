<#
.SYNOPSIS
    Prove (or disprove) root / writable /system / SELinux capability on a throwaway AVD.

.DESCRIPTION
    Creates a disposable AVD named ``rootprobe-*`` from a chosen system image, boots it
    headless on a private port, optionally with ``-writable-system`` and
    ``-selinux permissive``, then reports a PASS/FAIL summary for:

      * ``adb root``            - does adbd restart as uid=0?
      * ``getenforce``          - SELinux state (and whether it is Permissive)
      * ``/system`` writable    - ``adb remount`` + write/read-back of a probe file
      * caller ``-Command``     - arbitrary shell command executed as root

    On API 25 x86 ``adb remount`` succeeds immediately.  On API 30+ images (verified on
    API 34 ``google_apis``) it only disables verity, switches the partitions to
    overlayfs and asks for a reboot, so the script reboots, re-roots and retries the
    write automatically before judging the check.

    The AVD and all of its files are removed again unless ``-KeepAvd`` is given, so
    nothing outside ``rootprobe*`` is ever created, booted, or deleted.

    Background: on emulator system images the ``/system`` block device is a qcow2
    overlay.  ``-writable-system`` keeps that overlay inside the AVD directory
    (``system.img.qcow2``) so writes persist across boots *of that AVD as long as it
    is booted with the flag again*; without the flag the emulator uses a temporary
    overlay that is discarded on exit, and a normal boot sees the pristine image.
    This script always uses a fresh AVD, so it never inherits someone else's overlay.

    See docs/rooted-images.md for the measured results and their limits.

.PARAMETER Image
    SDK system image package, e.g. ``system-images;android-25;google_apis;x86``.
    Only ``google_apis`` / ``default`` (userdebug) images support ``adb root``;
    ``google_apis_playstore`` images are user builds and will fail that check.

.PARAMETER DeviceProfile
    avdmanager device profile used for the throwaway AVD (default ``Nexus 5``).

.PARAMETER AvdName
    Explicit AVD name. Must start with ``rootprobe``. Default: ``rootprobe-<6 hex>``.

.PARAMETER Command
    Shell command to run as root on the device, e.g. ``'pm list packages -3'``.
    Passed to ``adb shell`` unchanged (do not rely on PowerShell interpolation;
    use single quotes if the command contains ``$``).

.PARAMETER Port
    First console port to try (default 5570). Even console ports 5570-5584 are
    scanned; adb uses console+1.  Upper bound stays below adb's default
    ``ADB_LOCAL_TRANSPORT_MAX_PORT`` (5585) so no environment tweak is needed.
    A port pair is only used when both ports are free and no ``emulator-<port>``
    is already attached.

.PARAMETER MemoryMB
    Guest RAM in MiB (default 2048).

.PARAMETER BootTimeoutSec
    Seconds to wait for ``sys.boot_completed=1`` and a responsive package manager.

.PARAMETER NoWritableSystem
    Boot without ``-writable-system``. ``adb remount`` then writes into a temporary
    overlay that disappears with the emulator, so the writable check is expected to
    fail - useful for A/B comparison, not for real work.

.PARAMETER SelinuxPermissive
    Add ``-selinux permissive`` so the guest boots with ``getenforce = Permissive``.
    Without it the image boots Enforcing and only a runtime ``setenforce 0`` (which
    does not survive a reboot) changes that.

.PARAMETER KeepAvd
    Keep the AVD and log files after the probe for manual inspection (the emulator is
    still shut down). Remember to delete it with ``avdmanager delete avd -n <name>``.

.PARAMETER LogDir
    Where to write emulator stdout/stderr logs. Default:
    ``%TEMP%\avdfleet-rootprobe``.

.EXAMPLE
    pwsh -File scripts/emulator-root-probe.ps1

.EXAMPLE
    pwsh -File scripts/emulator-root-probe.ps1 -SelinuxPermissive -Command 'settings put global http_proxy 10.0.2.2:8080; settings get global http_proxy'

.EXAMPLE
    pwsh -File scripts/emulator-root-probe.ps1 -Image 'system-images;android-34;google_apis;x86_64' -Port 5572

.NOTES
    Every adb call is scoped with ``-s emulator-<port>``; the shared adb server is
    never restarted or killed.  Protected AVD name prefixes are refused outright.
#>
[CmdletBinding()]
param(
    [string]$Image = 'system-images;android-25;google_apis;x86',
    [string]$DeviceProfile = 'Nexus 5',
    [string]$AvdName,
    [string]$Command,
    [int]$Port = 5570,
    [int]$MemoryMB = 2048,
    [int]$BootTimeoutSec = 300,
    [switch]$NoWritableSystem,
    [switch]$SelinuxPermissive,
    [switch]$KeepAvd,
    [string]$LogDir
)

Set-StrictMode -Version Latest
# Native tools (adb, emulator, avdmanager.bat) legitimately write to stderr; keeping
# 'Continue' and checking $LASTEXITCODE explicitly is far more predictable than
# letting NativeCommandError abort the run half way through a boot.
$ErrorActionPreference = 'Continue'

# AVDs owned by other work streams.  Never created, booted, or deleted here.
$ProtectedAvdPattern = '^(CloneForge|Device|selftest-|proxytest|oobe-reset)'
$OwnAvdPattern = '^rootprobe'

$script:Failures = 0

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Write-Detail {
    param([string]$Text)
    Write-Host "   $Text" -ForegroundColor DarkGray
}

function Format-Detail {
    # adb/remount output is multi-line and can be very long (API 34's remount prints
    # one "Using overlayfs for ..." line per partition); keep the summary table readable.
    param([string]$Text, [int]$MaxLength = 240)
    $flat = ($Text -replace '\s+', ' ').Trim()
    if ($flat.Length -gt $MaxLength) { return $flat.Substring(0, $MaxLength - 3) + '...' }
    return $flat
}

function Add-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail,
        [switch]$Informational
    )
    if (-not $Informational -and -not $Ok) {
        $script:Failures++
    }
    $status = if ($Informational) { 'INFO' } elseif ($Ok) { 'PASS' } else { 'FAIL' }
    $color = if ($Informational) { 'Gray' } elseif ($Ok) { 'Green' } else { 'Red' }
    $Detail = Format-Detail -Text $Detail
    Write-Host ("  {0}  {1,-22} {2}" -f $status, $Name, $Detail) -ForegroundColor $color
    return [pscustomobject]@{ Status = $status; Check = $Name; Detail = $Detail }
}

function Invoke-Native {
    <#
        Run a native command, capture merged stdout/stderr and the exit code without
        letting stderr turn into a terminating error.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $output = & $FilePath @Arguments 2>&1 | ForEach-Object { $_.ToString() }
    $code = $LASTEXITCODE
    return [pscustomobject]@{
        Output   = ($output -join "`n")
        Lines    = @($output)
        ExitCode = $code
        Ok       = ($code -eq 0)
    }
}

# ---------------------------------------------------------------------------
# Environment discovery
# ---------------------------------------------------------------------------

function Resolve-Sdk {
    $candidates = @()
    if ($env:ANDROID_SDK_ROOT) { $candidates += $env:ANDROID_SDK_ROOT }
    if ($env:ANDROID_HOME) { $candidates += $env:ANDROID_HOME }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Android\Sdk') }
    foreach ($root in $candidates) {
        if (-not $root) { continue }
        $emulator = Join-Path $root 'emulator\emulator.exe'
        $adb = Join-Path $root 'platform-tools\adb.exe'
        if ((Test-Path $emulator) -and (Test-Path $adb)) {
            $avdmanager = Get-ChildItem -Path (Join-Path $root 'cmdline-tools') -Filter 'avdmanager.bat' -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 1 -ExpandProperty FullName
            if (-not $avdmanager) {
                $avdmanager = Get-ChildItem -Path (Join-Path $root 'tools\bin') -Filter 'avdmanager.bat' -ErrorAction SilentlyContinue |
                    Select-Object -First 1 -ExpandProperty FullName
            }
            return [pscustomobject]@{
                Root       = $root
                Emulator   = $emulator
                Adb        = $adb
                AvdManager = $avdmanager
            }
        }
    }
    throw "Android SDK with emulator.exe + platform-tools\adb.exe not found. Set ANDROID_SDK_ROOT or ANDROID_HOME."
}

function Test-PortPairFree {
    param([int]$ConsolePort)
    foreach ($p in @($ConsolePort, $ConsolePort + 1)) {
        $listener = $null
        try {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $p)
            $listener.Start()
        } catch {
            return $false
        } finally {
            if ($listener) { $listener.Stop() }
        }
    }
    return $true
}

function Resolve-ConsolePort {
    param([int]$Start, [string]$Adb)
    $attached = (Invoke-Native -FilePath $Adb -Arguments @('devices')).Output
    for ($p = $Start; $p -le 5584; $p += 2) {
        if ($attached -match ("emulator-{0}\b" -f $p)) { continue }
        if (Test-PortPairFree -ConsolePort $p) { return $p }
    }
    throw "No free console/adb port pair in $Start..5584. Stop another emulator or pass -Port."
}

function Assert-SafeAvdName {
    param([string]$Name)
    if ($Name -match $ProtectedAvdPattern) {
        throw "Refusing to touch AVD '$Name': it matches a protected prefix (CloneForge*, Device*, selftest-*, proxytest*, oobe-reset*)."
    }
    if ($Name -notmatch $OwnAvdPattern) {
        throw "Refusing to touch AVD '$Name': this script only manages AVDs whose name starts with 'rootprobe'."
    }
}

# ---------------------------------------------------------------------------
# adb helpers (always device-scoped)
# ---------------------------------------------------------------------------

function Invoke-Adb {
    param(
        [Parameter(Mandatory)][string]$Adb,
        [Parameter(Mandatory)][string]$Serial,
        [string[]]$Arguments = @()
    )
    $full = @('-s', $Serial) + $Arguments
    return Invoke-Native -FilePath $Adb -Arguments $full
}

function Wait-ForBoot {
    param(
        [string]$Adb,
        [string]$Serial,
        [int]$TimeoutSec,
        [System.Diagnostics.Process]$Process
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $state = 'starting'
    while ((Get-Date) -lt $deadline) {
        if ($Process -and $Process.HasExited) {
            $code = 'unknown'
            try { $code = $Process.ExitCode } catch { }
            throw "Emulator exited during boot (exit code $code). Check the emulator log files."
        }
        $state = (Invoke-Adb -Adb $Adb -Serial $Serial -Arguments @('get-state')).Output.Trim()
        if ($state -eq 'device') {
            $booted = (Invoke-Adb -Adb $Adb -Serial $Serial -Arguments @('shell', 'getprop', 'sys.boot_completed')).Output.Trim()
            if ($booted -eq '1') {
                # Require a responsive package manager too: the framework can still be
                # restarting right after sys.boot_completed flips to 1.
                $pm = (Invoke-Adb -Adb $Adb -Serial $Serial -Arguments @('shell', 'pm', 'path', 'android')).Output.Trim()
                if ($pm -match 'package:') { return $true }
                $state = 'package-manager-not-ready'
            } else {
                $state = 'boot_completed=' + $booted
            }
        }
        Start-Sleep -Seconds 3
    }
    throw "Emulator did not finish booting within ${TimeoutSec}s (last state: $state)."
}

function Get-MountSummary {
    <#
        Compact view of the interesting mounts.  Parsed on the host: quoting a grep
        pattern through `adb shell` is unreliable (the quotes can be stripped, which on
        API 34 matches hundreds of /system/bin bind-mounts), and `mount` output for the
        whole table is only a few KB.
    #>
    param([string]$Adb, [string]$Serial)
    $out = (Invoke-Adb -Adb $Adb -Serial $Serial -Arguments @('shell', 'mount')).Output
    $interesting = @('/system', '/vendor', '/product', '/system_ext')
    $parts = @()
    foreach ($line in ($out -split "`n")) {
        $m = [regex]::Match($line, '^\S+ on (/\S*) type (\S+) \(([^,)]+)')
        if (-not $m.Success) { continue }
        $mountPoint = $m.Groups[1].Value
        if ($interesting -notcontains $mountPoint) { continue }
        $parts += ('{0}={1}({2})' -f $mountPoint, $m.Groups[3].Value, $m.Groups[2].Value)
    }
    if ($parts.Count -eq 0) { return '(no /system-style mount line)' }
    return ($parts -join ' ')
}

function Restart-Device {
    <#
        Reboot the guest and wait for a *complete* second boot.  On API 30+ images
        `adb remount` only writes the verity/overlayfs configuration and then asks for a
        reboot ("Now reboot your device for settings to take effect"); the write test
        only succeeds after that reboot.
    #>
    param(
        [string]$Adb,
        [string]$Serial,
        [int]$TimeoutSec,
        [System.Diagnostics.Process]$Process
    )
    Invoke-Adb -Adb $Adb -Serial $Serial -Arguments @('reboot') | Out-Null
    # Let the guest actually go down before polluting the boot check with the old value.
    Start-Sleep -Seconds 12
    Wait-ForBoot -Adb $Adb -Serial $Serial -TimeoutSec $TimeoutSec -Process $Process | Out-Null
}

function Become-Root {
    param([string]$Adb, [string]$Serial)
    Invoke-Adb -Adb $Adb -Serial $Serial -Arguments @('root') | Out-Null
    Start-Sleep -Seconds 3
    Invoke-Adb -Adb $Adb -Serial $Serial -Arguments @('wait-for-device') | Out-Null
    return (Invoke-Adb -Adb $Adb -Serial $Serial -Arguments @('shell', 'id')).Output.Trim()
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$started = Get-Date
$sdk = Resolve-Sdk
$created = $false
$emulatorProcess = $null
# NOTE: PowerShell variable names are case-insensitive, so the local below must not
# be called $port - that is the same variable as the -Port parameter.
$consolePort = 0
$serial = $null
$checks = @()
$logDir = if ($LogDir) { $LogDir } else { Join-Path $env:TEMP 'avdfleet-rootprobe' }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (-not $AvdName) {
    $AvdName = 'rootprobe-' + ([guid]::NewGuid().ToString('N').Substring(0, 6))
}
try {
    Assert-SafeAvdName -Name $AvdName
} catch {
    # Fail fast and quietly: this check runs before anything is created or booted.
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$writable = -not $NoWritableSystem

try {
    Write-Host 'AVD Fleet - emulator root probe' -ForegroundColor White
    Write-Detail "sdk        : $($sdk.Root)"
    Write-Detail "image      : $Image"
    Write-Detail "avd        : $AvdName (throwaway)"
    Write-Detail "flags      : -writable-system=$writable  -selinux permissive=$([bool]$SelinuxPermissive)"

    # -- 1. create the throwaway AVD ------------------------------------------------
    Write-Step 'Create throwaway AVD'
    if (Test-Path (Join-Path $env:USERPROFILE ".android\avd\$AvdName.avd")) {
        throw "AVD '$AvdName' already exists on disk; refusing to reuse or overwrite it. Pick another -AvdName."
    }
    if (-not $sdk.AvdManager) {
        throw "avdmanager.bat not found under $($sdk.Root)\cmdline-tools; cannot create the throwaway AVD."
    }
    $createArgs = @('create', 'avd', '-n', $AvdName, '-k', $Image, '--force')
    if ($DeviceProfile) { $createArgs += @('-d', $DeviceProfile) }
    # avdmanager asks "Do you wish to create a custom hardware profile?" - answer no.
    'no' | & $sdk.AvdManager @createArgs 2>&1 | Out-Null
    if (-not (Test-Path (Join-Path $env:USERPROFILE ".android\avd\$AvdName.avd"))) {
        throw "avdmanager did not create '$AvdName'. Does the image '$Image' exist? (sdkmanager --list_installed)"
    }
    $created = $true
    Write-Detail "created $AvdName"

    # -- 2. boot headless on a private port -----------------------------------------
    Write-Step 'Boot headless'
    $consolePort = Resolve-ConsolePort -Start $Port -Adb $sdk.Adb
    $serial = "emulator-$consolePort"
    $emuArgs = @(
        '-avd', $AvdName,
        '-port', "$consolePort",
        '-no-window',
        '-no-snapshot',
        '-no-audio',
        '-no-boot-anim',
        '-gpu', 'swiftshader_indirect',
        '-memory', "$MemoryMB"
    )
    if ($writable) { $emuArgs += '-writable-system' }
    if ($SelinuxPermissive) { $emuArgs += @('-selinux', 'permissive') }

    $outLog = Join-Path $logDir "$AvdName-out.log"
    $errLog = Join-Path $logDir "$AvdName-err.log"
    Write-Detail "console=$consolePort adb=$($consolePort + 1)"
    Write-Detail "log=$outLog"
    $emulatorProcess = Start-Process -FilePath $sdk.Emulator -ArgumentList $emuArgs -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog

    $bootStart = Get-Date
    Wait-ForBoot -Adb $sdk.Adb -Serial $serial -TimeoutSec $BootTimeoutSec -Process $emulatorProcess | Out-Null
    $bootSeconds = [int]((Get-Date) - $bootStart).TotalSeconds
    Write-Detail "booted in ${bootSeconds}s (pid $($emulatorProcess.Id))"

    # -- 3. identify the image ------------------------------------------------------
    Write-Step 'Image identity'
    $buildType = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', 'getprop', 'ro.build.type')).Output.Trim()
    $debuggable = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', 'getprop', 'ro.debuggable')).Output.Trim()
    $sdkLevel = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', 'getprop', 'ro.build.version.sdk')).Output.Trim()
    $abis = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', 'getprop', 'ro.product.cpu.abi')).Output.Trim()
    Write-Host ("  INFO  {0,-22} api={1} abi={2}" -f 'build', $sdkLevel, $abis) -ForegroundColor Gray
    $checks += Add-Check -Name 'userdebug build' -Ok ($buildType -eq 'userdebug' -and $debuggable -eq '1') `
        -Detail "ro.build.type=$buildType ro.debuggable=$debuggable" -Informational

    # -- 4. adb root ----------------------------------------------------------------
    Write-Step 'adb root'
    $idBefore = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', 'id')).Output.Trim()
    Write-Detail "before: $idBefore"
    $rootResult = Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('root')
    Write-Detail "adb root: $($rootResult.Output.Trim())"
    # adbd restarts; give it a moment and re-sync before asking for the uid.
    Start-Sleep -Seconds 3
    Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('wait-for-device') | Out-Null
    $idAfter = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', 'id')).Output.Trim()
    $isRoot = $idAfter -match 'uid=0'
    $rootDetail = if ($isRoot) { $idAfter } else { "still not root: $idAfter" }
    if ($rootResult.Output -match 'cannot run as root') {
        $rootDetail = 'adbd refused: production (user) build'
    }
    $checks += Add-Check -Name 'adb root' -Ok $isRoot -Detail $rootDetail

    # -- 5. SELinux ----------------------------------------------------------------
    Write-Step 'SELinux'
    $enforce = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', 'getenforce')).Output.Trim()
    $selinuxOk = if ($SelinuxPermissive) { $enforce -eq 'Permissive' } else { $true }
    $selinuxDetail = if ($SelinuxPermissive) {
        "getenforce=$enforce (expected Permissive via -selinux permissive)"
    } else {
        "getenforce=$enforce (runtime 'setenforce 0' works but does not survive reboot)"
    }
    $checks += Add-Check -Name 'selinux' -Ok $selinuxOk -Detail $selinuxDetail

    # -- 6. writable /system -------------------------------------------------------
    Write-Step 'Writable /system'
    Write-Detail ('before: ' + (Get-MountSummary -Adb $sdk.Adb -Serial $serial))

    if ($writable) {
        $rebooted = $false
        $remount = Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('remount')
        $remountText = $remount.Output.Trim()
        Write-Detail ('adb remount: ' + (Format-Detail -Text $remountText))

        # API 30+ images: remount disables verity, switches the partitions to overlayfs
        # and then asks for a reboot before /system really accepts writes.
        if ($remountText -match '(?i)reboot your device') {
            Write-Step 'Reboot required by adb remount (verity/overlayfs change)'
            Restart-Device -Adb $sdk.Adb -Serial $serial -TimeoutSec $BootTimeoutSec -Process $emulatorProcess
            $rebooted = $true
            $null = Become-Root -Adb $sdk.Adb -Serial $serial
            $remount = Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('remount')
            $remountText = $remount.Output.Trim()
            Write-Detail ('after reboot: ' + (Format-Detail -Text $remountText))
        }

        $remountOk = ($remount.ExitCode -eq 0) -and ($remountText -notmatch '(?i)\berror\b|cannot|denied|not permitted')
        $checks += Add-Check -Name 'adb remount' -Ok $remountOk -Detail $remountText

        $probePath = "/system/etc/avdfleet-probe-$(Get-Random -Minimum 1000 -Maximum 9999).txt"
        $probeToken = "avdfleet-writable-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
        $probeWrite = "echo $probeToken > $probePath"
        $writeOut = Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', $probeWrite)
        $readBack = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', "cat $probePath")).Output.Trim()

        # Second chance: a read-only failure usually means the remount's reboot was still
        # pending (or the image wants verity disabled before the overlay can mount).
        if (($readBack -ne $probeToken) -and (-not $rebooted) -and ($writeOut.Output -match '(?i)read-only')) {
            Write-Step 'Write hit a read-only /system - retrying after a reboot'
            Restart-Device -Adb $sdk.Adb -Serial $serial -TimeoutSec $BootTimeoutSec -Process $emulatorProcess
            $rebooted = $true
            $null = Become-Root -Adb $sdk.Adb -Serial $serial
            $remount = Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('remount')
            $remountText = $remount.Output.Trim()
            Write-Detail ('remount after reboot: ' + (Format-Detail -Text $remountText))
            $writeOut = Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', $probeWrite)
            $readBack = (Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', "cat $probePath")).Output.Trim()
        }

        $writableOk = ($readBack -eq $probeToken)
        $checks += Add-Check -Name '/system writable' -Ok $writableOk `
            -Detail "$probePath -> '$readBack'"
        Write-Detail ('after: ' + (Get-MountSummary -Adb $sdk.Adb -Serial $serial))
        if (-not $writableOk) {
            Write-Detail ('write error: ' + (Format-Detail -Text $writeOut.Output))
        }
        Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', "rm -f $probePath") | Out-Null
    } else {
        $checks += Add-Check -Name '/system writable' -Ok $false `
            -Detail 'skipped: -NoWritableSystem (temp overlay only, discarded on exit)'
    }

    # -- 7. caller command ---------------------------------------------------------
    if ($Command) {
        Write-Step 'Caller command (as root)'
        $cmdResult = Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('shell', $Command)
        foreach ($line in $cmdResult.Lines) { Write-Host "   $line" }
        $checks += Add-Check -Name 'caller command' -Ok ($cmdResult.ExitCode -eq 0) `
            -Detail "exit=$($cmdResult.ExitCode)"
    }

    # -- 8. summary ----------------------------------------------------------------
    Write-Step 'Summary'
    Write-Host ("  {0,-6} {1,-22} {2}" -f 'STATE', 'CHECK', 'DETAIL') -ForegroundColor White
    foreach ($c in $checks) {
        $color = switch ($c.Status) {
            'PASS' { 'Green' }
            'FAIL' { 'Red' }
            default { 'Gray' }
        }
        Write-Host ("  {0,-6} {1,-22} {2}" -f $c.Status, $c.Check, $c.Detail) -ForegroundColor $color
    }
    $elapsed = [int]((Get-Date) - $started).TotalSeconds
    Write-Host ''
    if ($script:Failures -eq 0) {
        Write-Host "OVERALL: PASS  (${elapsed}s, $serial, image $Image)" -ForegroundColor Green
    } else {
        Write-Host "OVERALL: FAIL  ($($script:Failures) failing check(s), ${elapsed}s, $serial)" -ForegroundColor Red
    }
}
catch {
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    $script:Failures++
}
finally {
    # -- shutdown + delete ---------------------------------------------------------
    if ($serial) {
        Write-Step 'Cleanup'
        Invoke-Adb -Adb $sdk.Adb -Serial $serial -Arguments @('emu', 'kill') | Out-Null
        # Wait for the console port to drain before deleting files underneath it.
        for ($i = 0; $i -lt 20; $i++) {
            if (-not (Get-NetTCPConnection -LocalPort $consolePort -State Listen -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 500
        }
        if ($emulatorProcess -and -not $emulatorProcess.HasExited) {
            try { $emulatorProcess.WaitForExit(15000) | Out-Null } catch { }
            if (-not $emulatorProcess.HasExited) {
                try { Stop-Process -Id $emulatorProcess.Id -Force -ErrorAction Stop } catch { }
            }
        }
        Write-Detail "emulator stopped ($serial)"
    }
    if ($created -and -not $KeepAvd) {
        $delete = Invoke-Native -FilePath $sdk.AvdManager -Arguments @('delete', 'avd', '-n', $AvdName)
        $avdPath = Join-Path $env:USERPROFILE ".android\avd\$AvdName.avd"
        if (Test-Path $avdPath) {
            Write-Detail "avdmanager delete failed, removing $avdPath"
            Remove-Item -LiteralPath $avdPath -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath (Join-Path $env:USERPROFILE ".android\avd\$AvdName.ini") -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path $avdPath) {
            Write-Host "WARNING: could not remove AVD '$AvdName' - delete it manually." -ForegroundColor Yellow
        } else {
            Write-Detail "AVD '$AvdName' deleted"
        }
    } elseif ($created -and $KeepAvd) {
        Write-Host "Kept AVD '$AvdName' (-KeepAvd). Remove it with: avdmanager delete avd -n $AvdName" -ForegroundColor Yellow
    }
    Write-Detail "logs: $logDir"
}

if ($script:Failures -gt 0) { exit 1 }
exit 0
