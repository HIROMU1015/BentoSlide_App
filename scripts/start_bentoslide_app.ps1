[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 4180,
    [ValidateRange(1, 300)][int]$StartupTimeoutSeconds = 30,
    [switch]$NoBrowser,
    [switch]$NoClipboard,
    [switch]$RebuildFrontend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'bento_editor_launcher.common.ps1')
. (Join-Path $PSScriptRoot 'resolve_bentoslide_app_node.ps1')

$repository = Get-BentoRepositoryRoot -ScriptsDirectory $PSScriptRoot
Set-Location -LiteralPath $repository
$stateDirectory = Join-Path $repository 'output'
$pidPath = Join-Path $stateDirectory 'bentoslide-app.pid'
$sessionPath = Join-Path $stateDirectory 'bentoslide-app-session.json'
$logPath = Join-Path $stateDirectory 'bentoslide-app.log'
$stdoutLogPath = Join-Path $stateDirectory 'bentoslide-app.stdout.log'
$stderrLogPath = Join-Path $stateDirectory 'bentoslide-app.error.log'
$lockHandle = $null
$startedProcessId = 0
$createdSession = $false
$managedEngine = $null
$hostAddress = '127.0.0.1'
$url = "http://${hostAddress}:$Port/"

function Get-BentoSlideAppHealth {
    try {
        $health = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/health" -f $Port) -TimeoutSec 2
        if ([string]$health.format -ne 'bento/application-api-health/v1' -or
            -not [string]::Equals([string]$health.repository, $repository, [System.StringComparison]::OrdinalIgnoreCase)) { return $null }
        return $health
    }
    catch { return $null }
}

function Test-BentoSlideAppIdentity {
    param($Session)
    $snapshot = Get-BentoProcessSnapshot -ProcessId ([int]$Session.pid)
    if (-not $snapshot.Exists) { return [pscustomobject]@{ Valid = $false; Exists = $false; Reason = 'process does not exist'; Snapshot = $snapshot } }
    try {
        $expected = [System.DateTimeOffset]::Parse([string]$Session.processStartTimeUtc).UtcDateTime
        $actual = [System.DateTimeOffset]::Parse([string]$snapshot.StartTimeUtc).UtcDateTime
    }
    catch { return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time is invalid'; Snapshot = $snapshot } }
    if ([Math]::Abs(($expected - $actual).TotalMilliseconds) -gt 100) { return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time does not match'; Snapshot = $snapshot } }
    if ([string]::IsNullOrWhiteSpace([string]$snapshot.CommandLine) -or $snapshot.CommandLine -notmatch '(?i)(?:^|\s)-m\s+app\.backend\.main(?:\s|$)') {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line is not the BentoSlide App'; Snapshot = $snapshot }
    }
    if ($snapshot.CommandLine.IndexOf($repository, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process repository does not match'; Snapshot = $snapshot }
    }
    return [pscustomobject]@{ Valid = $true; Exists = $true; Reason = 'match'; Snapshot = $snapshot }
}

try {
    [System.IO.Directory]::CreateDirectory($stateDirectory) | Out-Null
    $lockHandle = Enter-BentoFileLock -Repository $repository -Name 'bentoslide-app-launcher.lock'
    if (-not $lockHandle.Acquired) { throw 'Another BentoSlide App launcher is already working.' }
    $python = Find-BentoLauncherPython -Repository $repository -RequiredImports @('bento_converter', 'fastapi', 'uvicorn', 'app.backend.main')

    $session = $null
    if (Test-Path -LiteralPath $sessionPath -PathType Leaf) {
        $session = Get-Content -LiteralPath $sessionPath -Raw -Encoding utf8 | ConvertFrom-Json
        if ([string]$session.format -ne 'bento/application-session/v1') { throw 'Unknown BentoSlide App session format.' }
    }
    $health = Get-BentoSlideAppHealth
    if ($null -ne $health) {
        if ($null -eq $session) { throw "An untracked BentoSlide App is already using port $Port." }
        $identity = Test-BentoSlideAppIdentity -Session $session
        if (-not $identity.Valid) { throw "The existing app PID cannot be verified safely: $($identity.Reason)" }
        if (-not $NoClipboard) { Copy-BentoUrlToClipboard -Url $url | Out-Null }
        if (-not $NoBrowser) { Start-Process -FilePath $url }
        Write-Host "BentoSlide App is already running.`n$url"
        exit 0
    }
    if (Test-BentoPortOpen -HostAddress $hostAddress -Port $Port) { throw "Port $Port is used by another service. It will not be stopped." }
    if ($null -ne $session) {
        $identity = Test-BentoSlideAppIdentity -Session $session
        if ($identity.Exists) { throw "The recorded app process exists but health is unavailable: $($identity.Reason)" }
        Remove-Item -LiteralPath $pidPath,$sessionPath -Force -ErrorAction SilentlyContinue
    }

    $frontend = Join-Path $repository 'app\frontend'
    $distIndex = Join-Path $frontend 'dist\index.html'
    $sourceFiles = Get-ChildItem -LiteralPath (Join-Path $frontend 'src') -Recurse -File
    $sourceFiles += Get-Item -LiteralPath (Join-Path $frontend 'package.json'),(Join-Path $frontend 'vite.config.ts')
    $newestSource = ($sourceFiles | Measure-Object -Property LastWriteTimeUtc -Maximum).Maximum
    $needsBuild = $RebuildFrontend -or -not (Test-Path -LiteralPath $distIndex -PathType Leaf) -or (Get-Item -LiteralPath $distIndex).LastWriteTimeUtc -lt $newestSource
    if ($needsBuild) {
        $node = Resolve-BentoSlideAppNode -Repository $repository
        $npm = [string]$node.Npm
        if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules') -PathType Container)) {
            & $npm ci --prefix $frontend
            if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        }
        & $npm run build --prefix $frontend
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $distIndex -PathType Leaf)) { throw 'Frontend build failed.' }
    }

    $routeJson = & $python.Executable -m scripts.deck_workflow --root $repository route --json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve the current BentoSlide workspace: $($routeJson -join ' ')" }
    $workspaceRoute = ($routeJson -join "`n" | ConvertFrom-Json).route
    if ($workspaceRoute -in @('authoring-editor', 'final-editor')) {
        $editorExisted = Test-Path -LiteralPath (Join-Path $stateDirectory 'work-editor-session.json') -PathType Leaf
        $workspaceLauncher = Join-Path $PSScriptRoot 'start_deck_workspace.ps1'
        $workspaceProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',(ConvertTo-BentoProcessArgument -Argument $workspaceLauncher),'-NoClipboard') -Wait -PassThru -WindowStyle Hidden
        if ($workspaceProcess.ExitCode -ne 0) { throw "The existing BentoSlide workspace could not start (exit $($workspaceProcess.ExitCode))." }
        if (-not $editorExisted -and (Test-Path -LiteralPath (Join-Path $stateDirectory 'work-editor-session.json') -PathType Leaf)) { $managedEngine = 'work-editor' }
    }

    Set-Content -LiteralPath $logPath -Value @(
        "startedAt=$([System.DateTimeOffset]::Now.ToString('o'))", "repository=$repository",
        "python=$($python.Executable)", "host=$hostAddress", "port=$Port", "url=$url", 'status=starting'
    ) -Encoding utf8
    $detachedPython = Join-Path (Split-Path -Parent $python.Executable) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $detachedPython -PathType Leaf)) { $detachedPython = $python.Executable }
    $arguments = @(
        '-X','utf8','-m','app.backend.main','--root',$repository,'--host',$hostAddress,'--port',[string]$Port,
        '--stdout-log',$stdoutLogPath,'--stderr-log',$stderrLogPath
    )
    $detached = Start-BentoDetachedProcess -FilePath $detachedPython -Arguments $arguments -WorkingDirectory $repository
    $startedProcessId = [int]$detached.Id
    $deadline = [System.DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $startedHealth = $null
    do {
        Start-Sleep -Milliseconds 200
        if (-not (Get-BentoProcessSnapshot -ProcessId $startedProcessId).Exists) { break }
        $startedHealth = Get-BentoSlideAppHealth
        if ($null -ne $startedHealth) { break }
    } while ([System.DateTime]::UtcNow -lt $deadline)
    if ($null -eq $startedHealth) { throw "BentoSlide App did not become ready within $StartupTimeoutSeconds seconds." }
    $snapshot = Get-BentoProcessSnapshot -ProcessId $startedProcessId
    if (-not $snapshot.Exists) { throw 'Cannot capture the BentoSlide App process identity.' }
    $record = [ordered]@{
        format = 'bento/application-session/v1'; pid = $startedProcessId; startedAt = [System.DateTimeOffset]::Now.ToString('o')
        processStartTimeUtc = $snapshot.StartTimeUtc; repository = $repository; python = $detachedPython
        host = $hostAddress; port = $Port; url = $url; launchMode = [string]$detached.LaunchMode; managedEngine = $managedEngine
    }
    $temporary = $sessionPath + '.tmp'
    $record | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $sessionPath -Force
    $createdSession = $true
    Set-Content -LiteralPath $pidPath -Value ([string]$startedProcessId) -Encoding ascii
    Add-Content -LiteralPath $logPath -Value @("pid=$startedProcessId", "managedEngine=$managedEngine", 'status=started') -Encoding utf8
    if (-not $NoClipboard) { Copy-BentoUrlToClipboard -Url $url | Out-Null }
    if (-not $NoBrowser) { Start-Process -FilePath $url }
    Write-Host "BentoSlide App started.`n$url"
    exit 0
}
catch {
    if ($startedProcessId -gt 0) { Stop-Process -Id $startedProcessId -Force -ErrorAction SilentlyContinue }
    if ($createdSession) { Remove-Item -LiteralPath $pidPath,$sessionPath -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $logPath) { Add-Content -LiteralPath $logPath -Value @("failedAt=$([System.DateTimeOffset]::Now.ToString('o'))", "error=$($_.Exception.Message)", 'status=failed') -Encoding utf8 }
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (Test-Path -LiteralPath $stderrLogPath) { Get-Content -LiteralPath $stderrLogPath -Tail 20 -ErrorAction SilentlyContinue }
    exit 1
}
finally { Exit-BentoLauncherLock -Handle $lockHandle }
