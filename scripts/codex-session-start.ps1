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
    } | ConvertTo-Json -Compress
}
