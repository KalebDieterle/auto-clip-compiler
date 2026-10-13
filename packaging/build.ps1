# Build the Windows app and installer. Run from anywhere:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Needs: .venv with requirements-dev.txt, ffmpeg on PATH (winget install Gyan.FFmpeg), Inno Setup 6 for the installer.
param([switch]$SkipTests, [switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$version = (Select-String -Path clipcompiler\__init__.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Auto Clip Compiler $version" -ForegroundColor Cyan

if (-not $SkipTests) {
    Write-Host 'Running tests...' -ForegroundColor Cyan
    & $py -m unittest discover -s tests -t .
    if ($LASTEXITCODE) { throw 'Tests failed; not building.' }
}

Write-Host 'Staging ffmpeg...' -ForegroundColor Cyan
$ffmpegDir = if ($env:ACC_FFMPEG_DIR) { $env:ACC_FFMPEG_DIR } else { Split-Path (Get-Command ffmpeg).Source }
$stage = Join-Path $PSScriptRoot 'ffmpeg'
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item (Join-Path $ffmpegDir 'ffmpeg.exe'), (Join-Path $ffmpegDir 'ffprobe.exe') $stage -Force
$license = Get-ChildItem (Split-Path $ffmpegDir) -Filter LICENSE* -ErrorAction SilentlyContinue | Select-Object -First 1
if ($license) { Copy-Item $license.FullName (Join-Path $stage 'FFMPEG-LICENSE.txt') -Force }

Write-Host 'Building app (PyInstaller)...' -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\autoclip.spec
if ($LASTEXITCODE) { throw 'PyInstaller failed.' }

Write-Host 'Smoke test of the built app...' -ForegroundColor Cyan
$home_ = Join-Path $env:TEMP "acc-build-selftest-$(Get-Random)"
$env:ACC_HOME = $home_; $env:ACC_SELFTEST = '1'
$app = Start-Process dist\AutoClipCompiler\AutoClipCompiler.exe -PassThru
if (-not $app.WaitForExit(90000)) { $app.Kill(); throw 'Built app did not finish its self-test in 90 s.' }
Remove-Item Env:ACC_HOME, Env:ACC_SELFTEST
$log = Get-Content (Join-Path $home_ 'logs\app.log') -Raw
if ($log -notmatch 'SELFTEST OK') { Write-Host $log; throw 'Built app self-test failed.' }
Write-Host ($log -split "`n" | Select-String 'SELFTEST') -ForegroundColor Green
Remove-Item -Recurse -Force $home_

if ($SkipInstaller) { return }
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { Write-Host 'Inno Setup 6 not found (winget install JRSoftware.InnoSetup); skipping the installer.' -ForegroundColor Yellow; return }
Write-Host 'Building installer (Inno Setup)...' -ForegroundColor Cyan
& $iscc /Qp "/DAppVersion=$version" packaging\installer.iss
if ($LASTEXITCODE) { throw 'Inno Setup failed.' }
Get-ChildItem dist\*.exe | ForEach-Object { Write-Host ("{0}  {1:N0} MB" -f $_.FullName, ($_.Length / 1MB)) -ForegroundColor Green }
