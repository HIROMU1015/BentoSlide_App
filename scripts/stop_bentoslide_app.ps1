[CmdletBinding()]
param([ValidateRange(1, 120)][int]$ShutdownTimeoutSeconds = 10)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'bento_editor_launcher.common.ps1')

$repository = Get-BentoRepositoryRoot -ScriptsDirectory $PSScriptRoot
Set-Location -LiteralPath $repository
$stateDirectory = Join-Path $repository 'output'
$pidPath = Join-Path $stateDirectory 'bentoslide-app.pid'
$sessionPath = Join-Path $stateDirectory 'bentoslide-app-session.json'
$logPath = Join-Path $stateDirectory 'bentoslide-app.log'
$lockHandle = $null

try {
    $lockHandle = Enter-BentoFileLock -Repository $repository -Name 'bentoslide-app-launcher.lock'
    if (-not $lockHandle.Acquired) { throw 'Another BentoSlide App launcher is already working.' }
    if (-not (Test-Path -LiteralPath $sessionPath -PathType Leaf)) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        Write-Host 'BentoSlide App is already stopped.'
        exit 0
    }
    $session = Get-Content -LiteralPath $sessionPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ([string]$session.format -ne 'bento/application-session/v1' -or [string]$session.host -ne '127.0.0.1') { throw 'Unsafe or unknown BentoSlide App session; no process was stopped.' }
    if (-not [string]::Equals([string]$session.repository, $repository, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Session repository mismatch; no process was stopped.' }
    $recordedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw -Encoding ascii).Trim(), [ref]$recordedPid) -or $recordedPid -ne [int]$session.pid) { throw 'PID and session mismatch; no process was stopped.' }
    $snapshot = Get-BentoProcessSnapshot -ProcessId $recordedPid
    if (-not $snapshot.Exists) {
        Remove-Item -LiteralPath $pidPath,$sessionPath -Force
        Write-Host 'BentoSlide App was already stopped; stale state was removed.'
        exit 0
    }
    if ([string]::IsNullOrWhiteSpace([string]$snapshot.CommandLine) -or $snapshot.CommandLine -notmatch '(?i)(?:^|\s)-m\s+app\.backend\.main(?:\s|$)' -or $snapshot.CommandLine.IndexOf($repository, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'App process identity does not match; no process was stopped.'
    }
    $expected = [System.DateTimeOffset]::Parse([string]$session.processStartTimeUtc).UtcDateTime
    $actual = [System.DateTimeOffset]::Parse([string]$snapshot.StartTimeUtc).UtcDateTime
    if ([Math]::Abs(($expected - $actual).TotalMilliseconds) -gt 100) { throw 'App process start time does not match; no process was stopped.' }
    $health = Invoke-RestMethod -Uri ([string]$session.url + 'api/health') -TimeoutSec 2
    if ([string]$health.format -ne 'bento/application-api-health/v1' -or -not [string]::Equals([string]$health.repository, $repository, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'App health identity does not match; no process was stopped.' }
    Stop-Process -Id $recordedPid -Force -ErrorAction Stop
    $deadline = [System.DateTime]::UtcNow.AddSeconds($ShutdownTimeoutSeconds)
    do { Start-Sleep -Milliseconds 200 } while ((Get-Process -Id $recordedPid -ErrorAction SilentlyContinue) -and [System.DateTime]::UtcNow -lt $deadline)
    if (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue) { throw 'BentoSlide App process did not stop; session state was retained.' }
    Remove-Item -LiteralPath $pidPath,$sessionPath -Force
    if ([string]$session.managedEngine -eq 'html-preview') {
        $stopScript = Join-Path $PSScriptRoot 'stop_html_preview.ps1'
        $engine = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',(ConvertTo-BentoProcessArgument -Argument $stopScript)) -Wait -PassThru -WindowStyle Hidden
        if ($engine.ExitCode -ne 0) { Write-Host 'The app stopped, but its HTML preview needs manual inspection.' -ForegroundColor Yellow }
    }
    elseif ([string]$session.managedEngine -eq 'work-editor') {
        $stopScript = Join-Path $PSScriptRoot 'stop_bento_editor.ps1'
        $engine = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',(ConvertTo-BentoProcessArgument -Argument $stopScript)) -Wait -PassThru -WindowStyle Hidden
        if ($engine.ExitCode -ne 0) { Write-Host 'The app stopped, but its Bento editor needs manual inspection.' -ForegroundColor Yellow }
    }
    if (Test-Path -LiteralPath $logPath) { Add-Content -LiteralPath $logPath -Value @("stoppedAt=$([System.DateTimeOffset]::Now.ToString('o'))", "pid=$recordedPid", 'status=stopped') -Encoding utf8 }
    Write-Host 'BentoSlide App stopped. Project files and logs were retained.'
    exit 0
}
catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
finally { Exit-BentoLauncherLock -Handle $lockHandle }
