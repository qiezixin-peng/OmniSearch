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

function Get-ServiceDefinitions {
    @(
        @{ Name = "api"; Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-api.exe"); Arguments = @() },
        @{ Name = "worker"; Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-worker.exe"); Arguments = @("--poll-interval", "1") },
        @{ Name = "mcp"; Command = (Join-Path $ProjectRoot ".venv\Scripts\omnisearch-mcp.exe"); Arguments = @("--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8010") }
    )
}

function Get-PidPath([string]$Name) {
    Join-Path $ProcessDir "$Name.pid"
}

function Get-ManagedProcess([string]$Name) {
    $PidPath = Get-PidPath $Name
    if (-not (Test-Path -LiteralPath $PidPath)) {
        return $null
    }
    $ManagedPid = [int](Get-Content -LiteralPath $PidPath -Raw)
    Get-Process -Id $ManagedPid -ErrorAction SilentlyContinue
}

function Show-Status {
    foreach ($Service in Get-ServiceDefinitions) {
        $Process = Get-ManagedProcess $Service.Name
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
    New-Item -ItemType Directory -Force -Path $ProcessDir, $LogDir | Out-Null

    Push-Location $ProjectRoot
    try {
        & $Python -m omnisearch.storage.migrate
        if ($LASTEXITCODE -ne 0) {
            throw "Database migration failed with exit code $LASTEXITCODE"
        }

        foreach ($Service in Get-ServiceDefinitions) {
            if ($null -ne (Get-ManagedProcess $Service.Name)) {
                continue
            }
            $Stdout = Join-Path $LogDir "$($Service.Name).stdout.log"
            $Stderr = Join-Path $LogDir "$($Service.Name).stderr.log"
            $StartParameters = @{
                FilePath = $Service.Command
                WorkingDirectory = $ProjectRoot
                WindowStyle = "Hidden"
                RedirectStandardOutput = $Stdout
                RedirectStandardError = $Stderr
                PassThru = $true
            }
            if ($Service.Arguments.Count -gt 0) {
                $StartParameters.ArgumentList = $Service.Arguments
            }
            $Process = Start-Process @StartParameters
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
            if ($Health.status -eq "ok") {
                Write-Output "OmniSearch Windows native runtime is ready."
                Write-Output "REST: http://127.0.0.1:8000/docs"
                Write-Output "MCP:  http://127.0.0.1:8010/mcp"
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
        $Process = Get-ManagedProcess $Service.Name
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
    $TaskAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" Start"
    $TaskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask `
        -TaskName $ScheduledTaskName `
        -Action $TaskAction `
        -Trigger $TaskTrigger `
        -Settings $TaskSettings `
        -Description "Start the OmniSearch REST, worker and Agent MCP runtime at user logon." `
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
