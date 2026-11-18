param(
    [ValidateSet("Install", "Register", "Uninstall", "Start", "Ensure", "Stop", "Status")]
    [string]$Action = "Ensure",
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$EnvFile = Join-Path $ProjectRoot ".env"
$RuntimeRoot = $null
if (Test-Path -LiteralPath $EnvFile) {
    $RuntimeSetting = Get-Content -LiteralPath $EnvFile -Encoding utf8 |
        Where-Object { $_ -match '^OMNISEARCH_RUNTIME_DIR=' } |
        Select-Object -First 1
    if ($RuntimeSetting) {
        $RuntimeRoot = ($RuntimeSetting -split '=', 2)[1].Trim().Trim('"')
    }
}
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path $ProjectRoot ".runtime"
}
$ProcessDir = Join-Path $RuntimeRoot "processes"
$LogDir = Join-Path $RuntimeRoot "logs"
$WorkerHeartbeat = Join-Path $ProcessDir "worker.heartbeat.json"
$ScheduledTaskName = "OmniSearch-Agent-Runtime"
$RSSHubRoot = Join-Path (Split-Path -Parent $ProjectRoot) "RSSHub"
$NodeCommand = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
$RuntimeMutexName = "Local\OmniSearch-Agent-Runtime"

function Get-ServiceDefinitions {
    @(
        @{
            Name = "api"
            Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-api.exe")
            Arguments = @()
            WorkingDirectory = $ProjectRoot
            Port = 8000
        },
        @{
            Name = "worker"
            Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-worker.exe")
            Arguments = @("--poll-interval", "1", "--heartbeat-file", $WorkerHeartbeat)
            WorkingDirectory = $ProjectRoot
        },
        @{
            Name = "mcp"
            Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-mcp.exe")
            Arguments = @("--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8010")
            WorkingDirectory = $ProjectRoot
            Port = 8010
        },
        @{
            Name = "rsshub"
            Command = $NodeCommand
            Arguments = @("dist/index.mjs")
            WorkingDirectory = $RSSHubRoot
            Environment = @{ NODE_ENV = "production"; PORT = "1200"; CACHE_TYPE = "memory" }
            Port = 1200
            Marker = "dist/index.mjs"
        }
    )
}

function Get-PidPath([string]$Name) {
    Join-Path $ProcessDir "$Name.pid"
}

function Get-ManagedProcess([hashtable]$Service) {
    $PidPath = Get-PidPath $Service.Name
    if (-not (Test-Path -LiteralPath $PidPath)) {
        return $null
    }
    try {
        $ManagedPid = [int](Get-Content -LiteralPath $PidPath -Raw)
    }
    catch {
        return $null
    }
    $Process = Get-Process -Id $ManagedPid -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        return $null
    }
    if (-not [string]::IsNullOrWhiteSpace($Service.Command)) {
        $ExpectedPath = [IO.Path]::GetFullPath($Service.Command)
        if (-not [string]::Equals($Process.Path, $ExpectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
    }
    $Process
}

function Test-ApiHealth {
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        return $Health.status -eq "ok"
    }
    catch {
        return $false
    }
}

function Test-McpHealth {
    try {
        $Headers = @{ Accept = "application/json, text/event-stream" }
        $Body = @{
            jsonrpc = "2.0"
            id = 1
            method = "initialize"
            params = @{
                protocolVersion = "2025-06-18"
                capabilities = @{}
                clientInfo = @{ name = "omnisearch-supervisor"; version = "1.0" }
            }
        } | ConvertTo-Json -Depth 8 -Compress
        $Response = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8010/mcp" `
            -Method Post `
            -ContentType "application/json" `
            -Headers $Headers `
            -Body $Body `
            -TimeoutSec 4
        return $Response.result.serverInfo.name -eq "OmniSearch"
    }
    catch {
        return $false
    }
}

function Test-RSSHubHealth {
    try {
        $Response = Invoke-WebRequest `
            -Uri "http://127.0.0.1:1200/healthz" `
            -UseBasicParsing `
            -TimeoutSec 2
        return $Response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Test-WorkerHealth {
    if (-not (Test-Path -LiteralPath $WorkerHeartbeat)) {
        return $false
    }
    try {
        $Heartbeat = Get-Content -LiteralPath $WorkerHeartbeat -Raw | ConvertFrom-Json
        $Age = [DateTime]::UtcNow - (Get-Item -LiteralPath $WorkerHeartbeat).LastWriteTimeUtc
        if ($Age.TotalSeconds -gt 90) {
            return $false
        }
        return $null -ne (Get-Process -Id ([int]$Heartbeat.pid) -ErrorAction SilentlyContinue)
    }
    catch {
        return $false
    }
}

function Test-ServiceHealth([hashtable]$Service) {
    switch ($Service.Name) {
        "api" { return Test-ApiHealth }
        "worker" { return Test-WorkerHealth }
        "mcp" { return Test-McpHealth }
        "rsshub" { return Test-RSSHubHealth }
        default { return $false }
    }
}

function Test-ServiceNeedsRecovery([hashtable]$Service) {
    if (Test-ServiceHealth $Service) {
        return $false
    }
    # A health probe can overlap a short database commit or browser setup.
    # Confirm once before killing a process that may still be serving work.
    Start-Sleep -Seconds 1
    return -not (Test-ServiceHealth $Service)
}

function Get-ProcessTreeIds([int]$RootPid) {
    try {
        $AllProcesses = @(Get-CimInstance Win32_Process)
    }
    catch {
        return @($RootPid)
    }
    $Pending = New-Object 'System.Collections.Generic.Queue[int]'
    $Found = New-Object 'System.Collections.Generic.HashSet[int]'
    $Pending.Enqueue($RootPid)
    while ($Pending.Count -gt 0) {
        $Current = $Pending.Dequeue()
        if (-not $Found.Add($Current)) {
            continue
        }
        foreach ($Child in $AllProcesses | Where-Object { $_.ParentProcessId -eq $Current }) {
            $Pending.Enqueue([int]$Child.ProcessId)
        }
    }
    @($Found)
}

function Get-ServiceProcessIds([hashtable]$Service) {
    $Ids = New-Object 'System.Collections.Generic.HashSet[int]'
    $Managed = Get-ManagedProcess $Service
    if ($null -ne $Managed) {
        foreach ($ProcessId in Get-ProcessTreeIds $Managed.Id) {
            [void]$Ids.Add($ProcessId)
        }
    }

    $Marker = $Service.Marker
    if ([string]::IsNullOrWhiteSpace($Marker)) {
        $Marker = $Service.Command
    }
    if (-not [string]::IsNullOrWhiteSpace($Marker)) {
        try {
            foreach ($Process in Get-CimInstance Win32_Process) {
                if (-not [string]::IsNullOrWhiteSpace($Process.CommandLine) -and
                    $Process.CommandLine.IndexOf(
                        $Marker,
                        [StringComparison]::OrdinalIgnoreCase
                    ) -ge 0) {
                    [void]$Ids.Add([int]$Process.ProcessId)
                }
            }
        }
        catch {
            # Restricted Agent sandboxes can deny CIM. PID and protocol health remain usable;
            # the scheduled supervisor performs the full process-tree cleanup outside the sandbox.
        }
    }

    if ($Service.Port) {
        foreach ($Connection in Get-NetTCPConnection `
            -State Listen `
            -LocalPort $Service.Port `
            -ErrorAction SilentlyContinue) {
            [void]$Ids.Add([int]$Connection.OwningProcess)
        }
    }
    @($Ids)
}

function Stop-ServiceProcess([hashtable]$Service) {
    $ProcessIds = @(Get-ServiceProcessIds $Service | Sort-Object -Descending -Unique)
    foreach ($ProcessId in $ProcessIds) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($ProcessId in $ProcessIds) {
        Wait-Process -Id $ProcessId -Timeout 5 -ErrorAction SilentlyContinue
    }
    $PidPath = Get-PidPath $Service.Name
    if (Test-Path -LiteralPath $PidPath) {
        Remove-Item -LiteralPath $PidPath -Force
    }
    if ($Service.Name -eq "worker" -and (Test-Path -LiteralPath $WorkerHeartbeat)) {
        Remove-Item -LiteralPath $WorkerHeartbeat -Force
    }
}

function Start-ServiceProcess([hashtable]$Service) {
    if ([string]::IsNullOrWhiteSpace($Service.Command) -or
        -not (Test-Path -LiteralPath $Service.Command)) {
        throw "Runtime command is missing for $($Service.Name): $($Service.Command)"
    }
    $Stdout = Join-Path $LogDir "$($Service.Name).stdout.log"
    $Stderr = Join-Path $LogDir "$($Service.Name).stderr.log"
    $ArchiveDir = Join-Path $LogDir "archive"
    New-Item -ItemType Directory -Force -Path $ArchiveDir | Out-Null
    $Timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    foreach ($LogPath in @($Stdout, $Stderr)) {
        if ((Test-Path -LiteralPath $LogPath) -and
            (Get-Item -LiteralPath $LogPath).Length -gt 0) {
            $Leaf = [IO.Path]::GetFileNameWithoutExtension($LogPath)
            $Extension = [IO.Path]::GetExtension($LogPath)
            $ArchivePath = Join-Path $ArchiveDir "$Leaf.$Timestamp$Extension"
            Move-Item -LiteralPath $LogPath -Destination $ArchivePath
        }
    }
    $StartParameters = @{
        FilePath = $Service.Command
        WorkingDirectory = $Service.WorkingDirectory
        WindowStyle = "Hidden"
        RedirectStandardOutput = $Stdout
        RedirectStandardError = $Stderr
        PassThru = $true
    }
    if ($Service.Arguments.Count -gt 0) {
        $StartParameters.ArgumentList = $Service.Arguments
    }
    $PreviousEnvironment = @{}
    if ($Service.Environment) {
        foreach ($Name in $Service.Environment.Keys) {
            $PreviousEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
            [Environment]::SetEnvironmentVariable($Name, $Service.Environment[$Name], "Process")
        }
    }
    try {
        $Process = Start-Process @StartParameters
    }
    finally {
        foreach ($Name in $PreviousEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($Name, $PreviousEnvironment[$Name], "Process")
        }
    }
    Set-Content -LiteralPath (Get-PidPath $Service.Name) -Value $Process.Id -Encoding ascii
}

function Wait-ServiceHealthy([hashtable]$Service, [int]$TimeoutSeconds = 45) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (Test-ServiceHealth $Service) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $Deadline)
    return $false
}

function Ensure-Services {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Python virtual environment is missing: $Python"
    }
    if ([string]::IsNullOrWhiteSpace($NodeCommand) -or
        -not (Test-Path -LiteralPath (Join-Path $RSSHubRoot "dist\index.mjs"))) {
        throw "RSSHub Windows build is missing. Expected Node.js and $RSSHubRoot\dist\index.mjs"
    }
    New-Item -ItemType Directory -Force -Path $ProcessDir, $LogDir | Out-Null

    $Services = @(Get-ServiceDefinitions)
    $Unhealthy = @($Services | Where-Object { Test-ServiceNeedsRecovery $_ })
    if ($Unhealthy.Count -eq 0) {
        if (-not $Quiet) {
            Write-Output "OmniSearch runtime is healthy."
        }
        return
    }

    Push-Location $ProjectRoot
    try {
        & $Python -m omnisearch.storage.migrate
        if ($LASTEXITCODE -ne 0) {
            throw "Database migration failed with exit code $LASTEXITCODE"
        }
        foreach ($Service in $Unhealthy) {
            Stop-ServiceProcess $Service
            Start-ServiceProcess $Service
        }
    }
    finally {
        Pop-Location
    }

    $Failed = @()
    foreach ($Service in $Unhealthy) {
        if (-not (Wait-ServiceHealthy $Service)) {
            $Failed += $Service.Name
        }
    }
    if ($Failed.Count -gt 0) {
        Show-Status
        throw "Runtime components failed health checks: $($Failed -join ', '). See $LogDir"
    }

    if (-not $Quiet) {
        Write-Output "OmniSearch Windows native runtime is ready."
        Write-Output "REST: http://127.0.0.1:8000/docs"
        Write-Output "MCP:  http://127.0.0.1:8010/mcp"
        Write-Output "RSSHub: http://127.0.0.1:1200"
        Show-Status
    }
}

function Show-Status {
    foreach ($Service in Get-ServiceDefinitions) {
        $Managed = Get-ManagedProcess $Service
        $Healthy = Test-ServiceHealth $Service
        $State = if ($Healthy) { "healthy" } else { "unhealthy" }
        if ($null -ne $Managed) {
            Write-Output "$($Service.Name): $State (supervisor PID $($Managed.Id))"
        }
        else {
            Write-Output "$($Service.Name): $State (no managed PID)"
        }
    }
}

function Stop-Services {
    foreach ($Service in Get-ServiceDefinitions) {
        Stop-ServiceProcess $Service
    }
    $StickyPidPath = Join-Path $RuntimeRoot "sticky-proxy\mihomo.pid"
    if (Test-Path -LiteralPath $StickyPidPath) {
        try {
            $StickyPid = [int](Get-Content -LiteralPath $StickyPidPath -Raw)
            $StickyProcess = Get-Process -Id $StickyPid -ErrorAction SilentlyContinue
            if ($null -ne $StickyProcess -and
                $StickyProcess.ProcessName -like "verge-mihomo*") {
                Stop-Process -Id $StickyPid -Force -ErrorAction SilentlyContinue
                Wait-Process -Id $StickyPid -Timeout 5 -ErrorAction SilentlyContinue
            }
        }
        finally {
            Remove-Item -LiteralPath $StickyPidPath -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $Quiet) {
        Write-Output "OmniSearch Windows native runtime stopped."
    }
}

function Register-RuntimeTask {
    $ConhostPath = Join-Path $env:SystemRoot "System32\conhost.exe"
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $TaskAction = New-ScheduledTaskAction `
        -Execute $ConhostPath `
        -Argument "--headless `"$PowerShellPath`" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$PSCommandPath`" Ensure -Quiet"
    $TaskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $RecoveryTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At ([DateTime]::Now.AddMinutes(1)) `
        -RepetitionInterval (New-TimeSpan -Minutes 2) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask `
        -TaskName $ScheduledTaskName `
        -Action $TaskAction `
        -Trigger @($TaskTrigger, $RecoveryTrigger) `
        -Settings $TaskSettings `
        -Description "Supervise and recover OmniSearch REST, worker, RSSHub and Agent MCP runtime." `
        -Force | Out-Null
    if (-not $Quiet) {
        Write-Output "Registered current-user supervisor task: $ScheduledTaskName"
    }
}

function Invoke-WithRuntimeLock([scriptblock]$Operation) {
    $Mutex = New-Object System.Threading.Mutex($false, $RuntimeMutexName)
    $Acquired = $false
    try {
        try {
            $Acquired = $Mutex.WaitOne([TimeSpan]::FromSeconds(60))
        }
        catch [Threading.AbandonedMutexException] {
            $Acquired = $true
        }
        if (-not $Acquired) {
            throw "Timed out waiting for the OmniSearch runtime supervisor lock."
        }
        & $Operation
    }
    finally {
        if ($Acquired) {
            $Mutex.ReleaseMutex()
        }
        $Mutex.Dispose()
    }
}

function Install-Runtime {
    & (Join-Path $ProjectRoot "scripts\install-stealth-browsers.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Stealth browser installation failed with exit code $LASTEXITCODE"
    }
    Register-RuntimeTask
    Invoke-WithRuntimeLock { Ensure-Services }
}

function Register-Runtime {
    Register-RuntimeTask
    Invoke-WithRuntimeLock { Ensure-Services }
}

function Uninstall-Runtime {
    Invoke-WithRuntimeLock { Stop-Services }
    if (Get-ScheduledTask -TaskName $ScheduledTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $ScheduledTaskName -Confirm:$false
    }
    if (-not $Quiet) {
        Write-Output "Removed current-user supervisor task: $ScheduledTaskName"
    }
}

switch ($Action) {
    "Install" { Install-Runtime }
    "Register" { Register-Runtime }
    "Uninstall" { Uninstall-Runtime }
    "Start" { Invoke-WithRuntimeLock { Ensure-Services } }
    "Ensure" { Invoke-WithRuntimeLock { Ensure-Services } }
    "Stop" { Invoke-WithRuntimeLock { Stop-Services } }
    "Status" { Show-Status }
}
