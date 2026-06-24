$ErrorActionPreference = "Stop"

$Python = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" }
$BuildTemp = Join-Path $PWD ".build-tmp"
New-Item -ItemType Directory -Force -Path $BuildTemp | Out-Null
$env:TEMP = $BuildTemp
$env:TMP = $BuildTemp

& $Python -c "import PyInstaller, tkinterdnd2"
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install -e ".[build]"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$DistExe = Join-Path $PWD "dist\ScanAirDJIImporter.exe"
if (Test-Path -LiteralPath $DistExe) {
    try {
        Remove-Item -LiteralPath $DistExe -Force
    } catch {
        Write-Error "Could not replace $DistExe. Close any running ScanAir DJI Importer windows, wait a moment for Explorer/antivirus to release the file, then run build_exe.ps1 again."
        exit 1
    }
}
& $Python -m PyInstaller --clean --noconfirm ScanAirDJIImporter.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Built EXE:"
Write-Host "  $PWD\dist\ScanAirDJIImporter.exe"
