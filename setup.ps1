# Setup for Auto Clip Compiler (run from this folder in PowerShell)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# TensorFlow on Windows supports Python 3.9-3.12 (NOT 3.13/3.14). Pick the best available.
$pyVer = $null
foreach ($v in '3.12', '3.11', '3.10') {
    & py "-$v" --version *> $null
    if ($LASTEXITCODE -eq 0) { $pyVer = $v; break }
}
if (-not $pyVer) {
    Write-Host "No TensorFlow-compatible Python (3.10-3.12) found via 'py'." -ForegroundColor Red
    Write-Host "Install one (e.g. winget install Python.Python.3.12) then re-run setup.ps1." -ForegroundColor Red
    exit 1
}
Write-Host "Creating venv (.venv) with Python $pyVer..." -ForegroundColor Cyan
py "-$pyVer" -m venv .venv

.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

Write-Host ""
Write-Host "Python deps installed." -ForegroundColor Green
Write-Host "External tools still required (one-time, separate PowerShell):" -ForegroundColor Yellow
Write-Host "    winget install Gyan.FFmpeg"
Write-Host "    winget install UB-Mannheim.TesseractOCR"
Write-Host ""
Write-Host "Then test detection (no copying) with:" -ForegroundColor Cyan
Write-Host "    python autoclip.py --preflight 8 --source <your clips folder>"
