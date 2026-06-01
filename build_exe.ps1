$ErrorActionPreference = "Stop"

$Python = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" }

& $Python -m pip install -e ".[build]"
& $Python -m PyInstaller --clean --noconfirm ScanAirDJIImporter.spec

Write-Host ""
Write-Host "Built EXE:"
Write-Host "  $PWD\dist\ScanAirDJIImporter.exe"
