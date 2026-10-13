# Setup for Auto Clip Compiler (run from this folder in PowerShell)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# TensorFlow (optional YAMNet laughter evidence) supports Python 3.10-3.12 on Windows.
$pyVer = $null
foreach ($v in '3.12', '3.11', '3.10') {
    & py "-$v" --version *> $null
    if ($LASTEXITCODE -eq 0) { $pyVer = $v; break }
}
if (-not $pyVer) {
    Write-Host "No compatible Python (3.10-3.12) found via 'py'." -ForegroundColor Red
    Write-Host "Install one (e.g. winget install Python.Python.3.12) then re-run setup.ps1." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path .venv)) {
    Write-Host "Creating venv (.venv) with Python $pyVer..." -ForegroundColor Cyan
    py "-$pyVer" -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Python deps installed." -ForegroundColor Green
Write-Host "Still required (one-time):" -ForegroundColor Yellow
Write-Host "    winget install Gyan.FFmpeg"
Write-Host "    winget install Ollama.Ollama"
Write-Host "    ollama pull qwen3-vl:8b"
Write-Host "    .\.venv\Scripts\python.exe setup_models.py --yamnet"
Write-Host ""
Write-Host "Then analyze a few clips (no render) with:" -ForegroundColor Cyan
Write-Host "    .\.venv\Scripts\python.exe autoclip.py --preflight 5 --source <clips folder> --output <output folder>"
