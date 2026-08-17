function Get-BentoRepositoryRoot {
    param([Parameter(Mandatory = $true)][string]$ScriptsDirectory)

    return [System.IO.Path]::GetFullPath((Join-Path $ScriptsDirectory ".."))
}

function Resolve-BentoLauncherPath {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Value
    )

    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Repository $Value))
}

function Get-BentoDisplayPath {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $prefix = $Repository.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($Value.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $Value.Substring($prefix.Length).Replace('\', '/')
    }
    return [System.IO.Path]::GetFileName($Value)
}

function Invoke-BentoUtf8JsonRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [ValidateRange(1, 300)][int]$TimeoutSeconds = 2
    )

    $response = Invoke-WebRequest -Method Get -Uri $Uri -TimeoutSec $TimeoutSeconds -UseBasicParsing
    $responseStream = $response.RawContentStream
    if ($null -eq $responseStream) {
        throw 'The HTTP response did not include a readable body.'
    }
    if ($responseStream.CanSeek) {
        $responseStream.Position = 0
    }
    $bodyStream = New-Object System.IO.MemoryStream
    try {
        $responseStream.CopyTo($bodyStream)
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $body = $strictUtf8.GetString($bodyStream.ToArray())
    }
    finally {
        $bodyStream.Dispose()
    }
    return $body | ConvertFrom-Json
}

function Get-BentoEditorStatus {
    param(
        [Parameter(Mandatory = $true)][string]$HostAddress,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 2
    )

    try {
        $payload = Invoke-BentoUtf8JsonRequest -Uri ("http://{0}:{1}/api/status" -f $HostAddress, $Port) -TimeoutSeconds $TimeoutSeconds
        $properties = @($payload.PSObject.Properties.Name)
        foreach ($required in @('target', 'revision', 'validation', 'runtimeFingerprint')) {
            if ($properties -notcontains $required) {
                return $null
            }
        }
        if ([string]::IsNullOrWhiteSpace([string]$payload.target) -or
            [string]::IsNullOrWhiteSpace([string]$payload.revision) -or
            [string]::IsNullOrWhiteSpace([string]$payload.validation)) {
            return $null
        }
        return $payload
    }
    catch {
        return $null
    }
}

function Test-BentoStatusTarget {
    param(
        [Parameter(Mandatory = $true)]$Status,
        [Parameter(Mandatory = $true)][string]$ExpectedTarget
    )

    return [string]::Equals([string]$Status.target, $ExpectedTarget, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-BentoPortOpen {
    param(
        [Parameter(Mandatory = $true)][string]$HostAddress,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 500
    )

    $client = New-Object System.Net.Sockets.TcpClient
    $waitHandle = $null
    try {
        $pending = $client.BeginConnect($HostAddress, $Port, $null, $null)
        $waitHandle = $pending.AsyncWaitHandle
        if (-not $waitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $client.EndConnect($pending)
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $waitHandle) {
            $waitHandle.Close()
        }
        $client.Close()
    }
}

function Get-BentoProcessSnapshot {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [pscustomobject]@{ Exists = $false; ProcessId = $ProcessId; StartTimeUtc = $null; CommandLine = $null }
    }

    $commandLine = $null
    try {
        $record = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop
        $commandLine = [string]$record.CommandLine
    }
    catch {
        $commandLine = $null
    }

    return [pscustomobject]@{
        Exists = $true
        ProcessId = $ProcessId
        StartTimeUtc = $process.StartTime.ToUniversalTime().ToString('o')
        CommandLine = $commandLine
    }
}

function Test-BentoSessionProcessIdentity {
    param(
        [Parameter(Mandatory = $true)]$Session,
        [Parameter(Mandatory = $true)][string]$Repository
    )

    $snapshot = Get-BentoProcessSnapshot -ProcessId ([int]$Session.pid)
    if (-not $snapshot.Exists) {
        return [pscustomobject]@{ Valid = $false; Exists = $false; Reason = 'process does not exist'; Snapshot = $snapshot }
    }

    try {
        $expectedStart = [System.DateTimeOffset]::Parse([string]$Session.processStartTimeUtc).UtcDateTime
        $actualStart = [System.DateTimeOffset]::Parse([string]$snapshot.StartTimeUtc).UtcDateTime
    }
    catch {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time is invalid'; Snapshot = $snapshot }
    }
    if ([Math]::Abs(($expectedStart - $actualStart).TotalMilliseconds) -gt 100) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time does not match'; Snapshot = $snapshot }
    }

    if ([string]::IsNullOrWhiteSpace([string]$snapshot.CommandLine)) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line is unavailable'; Snapshot = $snapshot }
    }
    if ($snapshot.CommandLine -notmatch '(?i)(?:^|\s)-m\s+scripts\.run_bento_work_editor(?:\s|$)') {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line is not the Bento Work editor'; Snapshot = $snapshot }
    }

    $targetMatches = $snapshot.CommandLine.IndexOf([string]$Session.target, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    $repositoryMatches = $snapshot.CommandLine.IndexOf($Repository, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not $targetMatches -and -not $repositoryMatches) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line does not match target or repository'; Snapshot = $snapshot }
    }

    return [pscustomobject]@{ Valid = $true; Exists = $true; Reason = 'match'; Snapshot = $snapshot }
}

function Enter-BentoLauncherLock {
    param([Parameter(Mandatory = $true)][string]$Repository)

    $stateDirectory = Join-Path $Repository 'output'
    [System.IO.Directory]::CreateDirectory($stateDirectory) | Out-Null
    $lockPath = Join-Path $stateDirectory 'work-editor-launcher.lock'
    try {
        $stream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        return [pscustomobject]@{ Stream = $stream; Acquired = $true; Path = $lockPath }
    }
    catch [System.IO.IOException] {
        return [pscustomobject]@{ Stream = $null; Acquired = $false; Path = $lockPath }
    }
}

function Enter-BentoFileLock {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($Name -notmatch '^[A-Za-z0-9._-]+$') {
        throw 'Invalid launcher lock file name.'
    }
    $stateDirectory = Join-Path $Repository 'output'
    [System.IO.Directory]::CreateDirectory($stateDirectory) | Out-Null
    $lockPath = Join-Path $stateDirectory $Name
    try {
        $stream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        return [pscustomobject]@{ Stream = $stream; Acquired = $true; Path = $lockPath }
    }
    catch [System.IO.IOException] {
        return [pscustomobject]@{ Stream = $null; Acquired = $false; Path = $lockPath }
    }
}

function Exit-BentoLauncherLock {
    param($Handle)

    if ($null -eq $Handle) {
        return
    }
    if ($Handle.Acquired) {
        try { $Handle.Stream.Dispose() } catch { }
        try { Remove-Item -LiteralPath $Handle.Path -Force -ErrorAction SilentlyContinue } catch { }
    }
}

function ConvertTo-BentoProcessArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Argument)

    if ($Argument.Contains('"')) {
        throw 'A process argument unexpectedly contains a double quote.'
    }
    if ($Argument.Length -eq 0 -or $Argument -match '\s') {
        return '"' + $Argument + '"'
    }
    return $Argument
}

function Start-BentoDetachedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $resolvedFile = [System.IO.Path]::GetFullPath($FilePath)
    $resolvedWorkingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory)
    if (-not (Test-Path -LiteralPath $resolvedFile -PathType Leaf)) {
        throw "Detached process executable does not exist: $resolvedFile"
    }
    if (-not (Test-Path -LiteralPath $resolvedWorkingDirectory -PathType Container)) {
        throw "Detached process working directory does not exist: $resolvedWorkingDirectory"
    }

    $commandLine = ConvertTo-BentoProcessArgument -Argument $resolvedFile
    if ($Arguments.Count -gt 0) {
        $commandLine += ' ' + (($Arguments | ForEach-Object {
            ConvertTo-BentoProcessArgument -Argument ([string]$_)
        }) -join ' ')
    }
    try {
        $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = $commandLine
            CurrentDirectory = $resolvedWorkingDirectory
        } -ErrorAction Stop
    }
    catch {
        throw "Cannot start a detached Windows process: $($_.Exception.Message)"
    }
    if ([int]$created.ReturnValue -ne 0 -or [int]$created.ProcessId -lt 1) {
        throw "Detached Windows process creation failed with Win32 code $($created.ReturnValue)."
    }
    return [pscustomobject]@{
        Id = [int]$created.ProcessId
        CommandLine = $commandLine
        LaunchMode = 'wmi-detached'
    }
}

function Copy-BentoUrlToClipboard {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        Set-Clipboard -Value $Url -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
}

function Find-BentoLauncherPython {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [string[]]$RequiredImports = @('bento_converter')
    )

    if (-not [string]::IsNullOrWhiteSpace([string]$env:BENTO_PYTHON)) {
        $configured = [Environment]::ExpandEnvironmentVariables([string]$env:BENTO_PYTHON)
        if (-not [System.IO.Path]::IsPathRooted($configured)) {
            $configured = Join-Path $Repository $configured
        }
        $configured = [System.IO.Path]::GetFullPath($configured)
        if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) {
            throw "BENTO_PYTHON does not point to a Python executable file: $configured"
        }
        return [pscustomobject]@{ Executable = $configured; DetectedBy = 'BENTO_PYTHON' }
    }

    $candidates = New-Object System.Collections.Generic.List[object]
    foreach ($relative in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe', 'env\Scripts\python.exe')) {
        $path = Join-Path $Repository $relative
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $candidates.Add([pscustomobject]@{ Command = $path; Prefix = @(); Label = $path })
        }
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        $candidates.Add([pscustomobject]@{ Command = $py.Source; Prefix = @('-3'); Label = ($py.Source + ' -3') })
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $candidates.Add([pscustomobject]@{ Command = $python.Source; Prefix = @(); Label = $python.Source })
    }

    $importStatement = 'import sys'
    foreach ($module in $RequiredImports) {
        if ($module -notmatch '^[A-Za-z_][A-Za-z0-9_.]*$') {
            throw 'Invalid required Python import name.'
        }
        $importStatement += '; import ' + $module
    }
    $importStatement += '; print(sys.executable)'
    $attempts = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $candidates) {
        Push-Location $Repository
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = & $candidate.Command @($candidate.Prefix) -c $importStatement 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
            Pop-Location
        }
        if ($exitCode -eq 0) {
            $executable = [string]($output | Select-Object -Last 1)
            if (Test-Path -LiteralPath $executable -PathType Leaf) {
                return [pscustomobject]@{ Executable = [System.IO.Path]::GetFullPath($executable); DetectedBy = $candidate.Label }
            }
        }
        $attempts.Add(("{0}: {1}" -f $candidate.Label, (($output | ForEach-Object { [string]$_ }) -join ' ')))
    }
    $details = if ($attempts.Count -gt 0) { $attempts -join "`n" } else { 'No Python candidate was found.' }
    throw "No compatible Python 3 environment was found.`n$details"
}
