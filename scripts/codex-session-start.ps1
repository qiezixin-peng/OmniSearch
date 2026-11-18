$ErrorActionPreference = "Stop"
$Supervisor = Join-Path $PSScriptRoot "windows-native.ps1"

try {
    $Output = & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $Supervisor `
        Ensure `
        -Quiet 2>&1
    if ($LASTEXITCODE -ne 0) {
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
