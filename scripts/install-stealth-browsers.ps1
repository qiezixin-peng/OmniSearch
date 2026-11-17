param(
    [switch]$SkipCamoufoxDownload
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$MainPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$CamoufoxRoot = Join-Path $ProjectRoot ".venv-camoufox312"
$CamoufoxPython = Join-Path $CamoufoxRoot "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $MainPython)) {
    throw "OmniSearch virtual environment is missing: $MainPython"
}

& $MainPython -m pip install "patchright==1.64.4" "playwright==1.65.2"
if ($LASTEXITCODE -ne 0) {
    throw "Patchright installation failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $CamoufoxPython)) {
    $UvCommand = (Get-Command uv.exe -ErrorAction SilentlyContinue).Source
    if ([string]::IsNullOrWhiteSpace($UvCommand)) {
        throw "uv is required to provision the isolated Python 3.12 Camoufox runtime"
    }
    & $UvCommand python install 3.12
    $CamoufoxBasePython = (& $UvCommand python find 3.12).Trim()
    & $CamoufoxBasePython -m venv $CamoufoxRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Camoufox virtual environment creation failed with exit code $LASTEXITCODE"
    }
}

& $CamoufoxPython -m pip install --upgrade pip
& $CamoufoxPython -m pip install "camoufox==0.5.9"
if ($LASTEXITCODE -ne 0) {
    throw "Camoufox installation failed with exit code $LASTEXITCODE"
}

if (-not $SkipCamoufoxDownload) {
    & $CamoufoxPython -m camoufox fetch
    if ($LASTEXITCODE -ne 0) {
        throw "Camoufox browser download failed with exit code $LASTEXITCODE"
    }
}

& $CamoufoxPython (Join-Path $ProjectRoot "scripts\prepare_camoufox_model.py")
if ($LASTEXITCODE -ne 0) {
    throw "Camoufox fingerprint model preparation failed with exit code $LASTEXITCODE"
}

& $MainPython -m pip check
& $CamoufoxPython -m pip check
Write-Output "Patchright and the isolated Camoufox runtime are ready."
