<#
Builds dist\RDR (embedded Python + app) and, if ISCC is available, the
setup exe and the rdr-app-<version>.zip used by the in-app updater.

Run on Windows from the repo root:
    powershell -ExecutionPolicy Bypass -File installer\build_windows.ps1

Requires: a normal Python 3 on PATH (only to run pip), internet access.
#>
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$PyVersion = if ($env:RDR_PYTHON_VERSION) { $env:RDR_PYTHON_VERSION } else { "3.12.8" }
$PyShort = ($PyVersion -split "\.")[0..1] -join ""          # 312
$Version = (Select-String -Path "app\__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "== RDR $Version  (embedded Python $PyVersion)"

$Dist = Join-Path $Root "dist"
$Out = Join-Path $Dist "RDR"
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $Out "python") | Out-Null

# 1. Embedded Python
$Zip = Join-Path $Dist "python-$PyVersion-embed-amd64.zip"
if (-not (Test-Path $Zip)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip" -OutFile $Zip
}
Expand-Archive $Zip -DestinationPath (Join-Path $Out "python") -Force

# Let the embedded interpreter see site-packages and the app folder.
$Pth = Join-Path $Out "python\python$PyShort._pth"
@(
    "python$PyShort.zip",
    ".",
    "Lib\site-packages",
    "..",
    "import site"
) | Set-Content -Path $Pth -Encoding ASCII

# 2. pip + requirements inside the embedded runtime
$EmbPy = Join-Path $Out "python\python.exe"
$GetPip = Join-Path $Dist "get-pip.py"
if (-not (Test-Path $GetPip)) { Invoke-WebRequest "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip }
& $EmbPy $GetPip --no-warn-script-location -q
& $EmbPy -m pip install -q --disable-pip-version-check -r requirements.txt
& $EmbPy -c "import fastapi, uvicorn, jinja2, openpyxl, multipart, itsdangerous, cryptography, pystray, PIL; print('runtime ok')"

# 3. Application files
Copy-Item -Recurse "app" (Join-Path $Out "app")
Get-ChildItem -Recurse (Join-Path $Out "app") -Include "__pycache__" -Directory | Remove-Item -Recurse -Force
Copy-Item "run.py", "tray.py", "requirements.txt", "README.md" $Out
Copy-Item (Join-Path $PSScriptRoot "RDR.cmd") $Out
Set-Content -Path (Join-Path $Out "VERSION") -Value $Version -NoNewline

# 3b. ffmpeg for the mirror's RTSP programme feed (GPL build from BtbN/FFmpeg-Builds on GitHub)
if ($env:RDR_SKIP_FFMPEG -ne "1") {
    $FfZip = Join-Path $Dist "ffmpeg-win64.zip"
    if (-not (Test-Path $FfZip)) {
        Invoke-WebRequest "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $FfZip
    }
    $FfTmp = Join-Path $Dist "ffmpeg-tmp"
    if (Test-Path $FfTmp) { Remove-Item $FfTmp -Recurse -Force }
    Expand-Archive $FfZip -DestinationPath $FfTmp -Force
    $FfExe = Get-ChildItem -Recurse $FfTmp -Filter "ffmpeg.exe" | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path (Join-Path $Out "tools") | Out-Null
    Copy-Item $FfExe.FullName (Join-Path $Out "tools\ffmpeg.exe")
    $FfLicense = Get-ChildItem -Recurse $FfTmp -Filter "LICENSE*" | Select-Object -First 1
    if ($FfLicense) { Copy-Item $FfLicense.FullName (Join-Path $Out "tools\ffmpeg-LICENSE.txt") }
    Remove-Item $FfTmp -Recurse -Force
    Write-Host "== bundled ffmpeg"
}

# 4. App-only zip for the in-app updater
$AppZip = Join-Path $Dist "rdr-app-$Version.zip"
if (Test-Path $AppZip) { Remove-Item $AppZip }
Compress-Archive -Path (Join-Path $Out "app"), (Join-Path $Out "run.py"), (Join-Path $Out "tray.py"), (Join-Path $Out "requirements.txt"), (Join-Path $Out "README.md"), (Join-Path $Out "VERSION") -DestinationPath $AppZip

# 5. Installer
$Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($Iscc) {
    & $Iscc "/DAppVersion=$Version" "/DDistDir=$Out" (Join-Path $PSScriptRoot "rdr.iss")
    Write-Host "== Built dist\RDR-Setup-$Version.exe"
} else {
    Write-Warning "Inno Setup (ISCC.exe) not found; skipped the setup exe. dist\RDR is still usable: run RDR.cmd inside it."
}
Write-Host "== Done"
