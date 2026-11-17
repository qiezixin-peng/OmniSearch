param(
    [ValidateSet("Install", "Uninstall", "Start", "Stop", "Status")]
    [string]$Action = "Start"
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
$ScheduledTaskName = "OmniSearch-Agent-Runtime"
$RSSHubRoot = Join-Path (Split-Path -Parent $ProjectRoot) "RSSHub"
$NodeCommand = (Get-Command node.exe -ErrorAction SilentlyContinue).Source

function Get-ServiceDefinitions {
    @(
        @{ Name = "api"; Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-api.exe"); Arguments = @(); WorkingDirectory = $ProjectRoot },
        @{ Name = "worker"; Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-worker.exe"); Arguments = @("--poll-interval", "1"); WorkingDirectory = $ProjectRoot },
        @{ Name = "mcp"; Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-mcp.exe"); Arguments = @("--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8010"); WorkingDirectory = $ProjectRoot },
        @{ Name = "rsshub"; Command = $NodeCommand; Arguments = @("dist/index.mjs"); WorkingDirectory = $RSSHubRoot; Environment = @{ NODE_ENV = "production"; PORT = "1200"; CACHE_TYPE = "memory" } }
    )
}

function Get-PidPath([string]$Name) {
    Join-Path $ProcessDir "$Name.pid"
}

function Get-ManagedProcess([string]$Name, [string]$ExpectedCommand = "") {
    $PidPath = Get-PidPath $Name
    if (-not (Test-Path -LiteralPath $PidPath)) {
        return $null
    }
    $ManagedPid = [int](Get-Content -LiteralPath $PidPath -Raw)
    $Process = Get-Process -Id $ManagedPid -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        return $null
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedCommand)) {
        $ExpectedPath = [IO.Path]::GetFullPath($ExpectedCommand)
        if (-not [string]::Equals($Process.Path, $ExpectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
    }
    $Process
}

function Show-Status {
    foreach ($Service in Get-ServiceDefinitions) {
        $Process = Get-ManagedProcess $Service.Name $Service.Command
        if ($null -eq $Process) {
            Write-Output "$($Service.Name): stopped"
        }
        else {
            Write-Output "$($Service.Name): running (PID $($Process.Id))"
        }
    }
}

function Start-Services {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Python virtual environment is missing: $Python"
    }
    if ([string]::IsNullOrWhiteSpace($NodeCommand) -or -not (Test-Path -LiteralPath (Join-Path $RSSHubRoot "dist\index.mjs"))) {
        throw "RSSHub Windows build is missing. Expected Node.js and $RSSHubRoot\dist\index.mjs"
    }
    New-Item -ItemType Directory -Force -Path $ProcessDir, $LogDir | Out-Null

    Push-Location $ProjectRoot
    try {
        & $Python -m omnisearch.storage.migrate
        if ($LASTEXITCODE -ne 0) {
            throw "Database migration failed with exit code $LASTEXITCODE"
        }

        foreach ($Service in Get-ServiceDefinitions) {
            if ($null -ne (Get-ManagedProcess $Service.Name $Service.Command)) {
                continue
            }
            $Stdout = Join-Path $LogDir "$($Service.Name).stdout.log"
            $Stderr = Join-Path $LogDir "$($Service.Name).stderr.log"
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
    }
    finally {
        Pop-Location
    }

    $Deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
            $RSSHubHealth = Invoke-WebRequest -Uri "http://127.0.0.1:1200/healthz" -UseBasicParsing -TimeoutSec 2
            if ($Health.status -eq "ok" -and $RSSHubHealth.StatusCode -eq 200) {
                Write-Output "OmniSearch Windows native runtime is ready."
                Write-Output "REST: http://127.0.0.1:8000/docs"
                Write-Output "MCP:  http://127.0.0.1:8010/mcp"
                Write-Output "RSSHub: http://127.0.0.1:1200"
                Show-Status
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while ([DateTime]::UtcNow -lt $Deadline)

    Show-Status
    throw "API did not become healthy within 30 seconds. See $LogDir"
}

function Stop-Services {
    foreach ($Service in Get-ServiceDefinitions) {
        $PidPath = Get-PidPath $Service.Name
        $Process = Get-ManagedProcess $Service.Name $Service.Command
        if ($null -ne $Process) {
            Stop-Process -Id $Process.Id
            Wait-Process -Id $Process.Id -Timeout 10 -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $PidPath) {
            Remove-Item -LiteralPath $PidPath -Force
        }
    }
    Write-Output "OmniSearch Windows native runtime stopped."
}

function Install-Runtime {
    & (Join-Path $ProjectRoot "scripts\install-stealth-browsers.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Stealth browser installation failed with exit code $LASTEXITCODE"
    }
    $TaskAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" Start"
    $TaskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $RecoveryTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At ([DateTime]::Now.AddMinutes(1)) `
        -RepetitionInterval (New-TimeSpan -Minutes 2) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask `
        -TaskName $ScheduledTaskName `
        -Action $TaskAction `
        -Trigger @($TaskTrigger, $RecoveryTrigger) `
        -Settings $TaskSettings `
        -Description "Start and continuously recover the OmniSearch REST, worker, RSSHub and Agent MCP runtime." `
        -Force | Out-Null
    Write-Output "Installed current-user auto-start task: $ScheduledTaskName"
    Start-Services
}

function Uninstall-Runtime {
    Stop-Services
    if (Get-ScheduledTask -TaskName $ScheduledTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $ScheduledTaskName -Confirm:$false
    }
    Write-Output "Removed current-user auto-start task: $ScheduledTaskName"
}

switch ($Action) {
    "Install" { Install-Runtime }
    "Uninstall" { Uninstall-Runtime }
    "Start" { Start-Services }
    "Stop" { Stop-Services }
    "Status" { Show-Status }
}
