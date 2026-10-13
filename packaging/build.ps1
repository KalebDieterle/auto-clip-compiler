# Build the Windows app folder and installer. Run:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Layout (no exe of our own, so Smart App Control never has to judge an unsigned binary):
#   {app}\python\         official Python embeddable package (every binary signed by the Python Software Foundation)
#   {app}\site-packages\  dependencies, the same PyPI wheels as everyone else's
#   {app}\app\            the app source (clipcompiler)
#   {app}\ffmpeg\         ffmpeg.exe + ffprobe.exe
# The Start menu shortcut runs  python\pythonw.exe -m clipcompiler.ui.main  with the app's icon.
# Needs: .venv (requirements.txt), ffmpeg on PATH (winget install Gyan.FFmpeg), Inno Setup 6 for the installer.
param([switch]$SkipTests, [switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$version = (Select-String -Path clipcompiler\__init__.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$pyVersion = (& $py -c "import platform; print(platform.python_version())").Trim()
Write-Host "Auto Clip Compiler $version (Python $pyVersion)" -ForegroundColor Cyan

if (-not $SkipTests) {
    Write-Host 'Running tests...' -ForegroundColor Cyan
    & $py -m unittest discover -s tests -t .
    if ($LASTEXITCODE) { throw 'Tests failed; not building.' }
}

$out = Join-Path $root 'dist\AutoClipCompiler'
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null

Write-Host 'Python runtime (embeddable, signed)...' -ForegroundColor Cyan
$cache = Join-Path $PSScriptRoot 'cache'; New-Item -ItemType Directory -Force $cache | Out-Null
$zip = Join-Path $cache "python-$pyVersion-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip" -OutFile $zip -UseBasicParsing
}
$pyDir = Join-Path $out 'python'
Expand-Archive $zip $pyDir
$unsigned = Get-ChildItem $pyDir -Include *.exe, *.dll, *.pyd -Recurse | Get-AuthenticodeSignature |
    Where-Object { $_.Status -ne 'Valid' -or $_.SignerCertificate.Subject -notmatch 'Python Software Foundation|Microsoft Corporation' }
if ($unsigned) { throw "Python runtime has binaries not signed by the PSF or Microsoft: $($unsigned.Path -join ', ')" }
$pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
Set-Content -Encoding ascii $pth.FullName ("{0}`r`n.`r`n..\app`r`n..\site-packages`r`n" -f ($pth.BaseName + '.zip'))

Write-Host 'Dependencies...' -ForegroundColor Cyan
$sp = Join-Path $out 'site-packages'
$pins = Join-Path $cache 'constraints.txt'
& $py -m pip freeze --exclude-editable | Set-Content -Encoding ascii $pins  # Same versions the tests ran against.
& $py -m pip install --quiet --disable-pip-version-check --no-compile --prefer-binary --target $sp -r requirements.txt -c $pins
if ($LASTEXITCODE) { throw 'pip install failed.' }
Get-ChildItem $sp -Directory -Filter 'bin' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force

Write-Host 'App source...' -ForegroundColor Cyan
$app = Join-Path $out 'app'
New-Item -ItemType Directory -Force $app | Out-Null
Copy-Item -Recurse clipcompiler (Join-Path $app 'clipcompiler')
Get-ChildItem $app -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Copy-Item packaging\app.ico, LICENSE $out

Write-Host 'ffmpeg...' -ForegroundColor Cyan
$ffmpegDir = if ($env:ACC_FFMPEG_DIR) { $env:ACC_FFMPEG_DIR } else { Split-Path (Get-Command ffmpeg).Source }
$ff = Join-Path $out 'ffmpeg'; New-Item -ItemType Directory -Force $ff | Out-Null
Copy-Item (Join-Path $ffmpegDir 'ffmpeg.exe'), (Join-Path $ffmpegDir 'ffprobe.exe') $ff
$license = Get-ChildItem (Split-Path $ffmpegDir) -Filter LICENSE* -ErrorAction SilentlyContinue | Select-Object -First 1
if ($license) { Copy-Item $license.FullName (Join-Path $ff 'FFMPEG-LICENSE.txt') }

Write-Host 'Precompiling (faster first start)...' -ForegroundColor Cyan
& (Join-Path $pyDir 'python.exe') -m compileall -q -j 0 $app $sp | Out-Null

Write-Host 'Smoke test of the built app...' -ForegroundColor Cyan
$home_ = Join-Path $env:TEMP "acc-build-selftest-$(Get-Random)"
$env:ACC_HOME = $home_; $env:ACC_SELFTEST = '1'
$proc = Start-Process (Join-Path $pyDir 'pythonw.exe') -ArgumentList '-m', 'clipcompiler.ui.main' -WorkingDirectory $out -PassThru
if (-not $proc.WaitForExit(90000)) { $proc.Kill(); throw 'Built app did not finish its self-test in 90 s.' }
Remove-Item Env:ACC_HOME, Env:ACC_SELFTEST
$log = Get-Content (Join-Path $home_ 'logs\app.log') -Raw
if ($log -notmatch 'SELFTEST OK') { Write-Host $log; throw 'Built app self-test failed.' }
Write-Host ($log -split "`n" | Select-String 'SELFTEST') -ForegroundColor Green
Remove-Item -Recurse -Force $home_
$mb = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("App folder: {0:N0} MB" -f $mb)

if ($SkipInstaller) { return }
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { Write-Host 'Inno Setup 6 not found (winget install JRSoftware.InnoSetup --scope user); skipping the installer.' -ForegroundColor Yellow; return }
Write-Host 'Building installer (Inno Setup)...' -ForegroundColor Cyan
& $iscc /Qp "/DAppVersion=$version" packaging\installer.iss
if ($LASTEXITCODE) { throw 'Inno Setup failed.' }
Get-ChildItem dist\*.exe | ForEach-Object { Write-Host ("{0}  {1:N0} MB" -f $_.FullName, ($_.Length / 1MB)) -ForegroundColor Green }
