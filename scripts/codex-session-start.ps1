$ErrorActionPreference = "Stop"
$Supervisor = Join-Path $PSScriptRoot "windows-native.ps1"

try {
    # Run in the existing Hook host. Starting a nested powershell.exe can flash
    # a console window even when the parent Agent application has no console.
    $Output = & $Supervisor Ensure -Quiet 2>&1
    # A directly invoked PowerShell script does not reliably populate
    # $LASTEXITCODE. $? is the authoritative success signal for this path.
    if (-not $?) {
        throw ($Output -join [Environment]::NewLine)
    }
}
catch {
    $Message = "OmniSearch SessionStart recovery failed: $($_.Exception.Message)"
    @{
        continue = $true
        systemMessage = $Message
        hookSpecificOutput = @{
            hookEventName = "SessionStart"
            additionalContext = (
                "OmniSearch automatic recovery failed. In this turn, the Agent must tell the user " +
                "that the service is unavailable, why recovery failed, how internet research coverage " +
                "is affected, and whether a fallback was used. Do not hide this failure."
            )
        }
    } | ConvertTo-Json -Compress
}
