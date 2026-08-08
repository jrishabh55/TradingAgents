# Build the Windows helper artifacts. PyInstaller cannot cross-compile, so
# this must run ON Windows (a VM or CI runner is fine). From the REPO ROOT:
#   powershell -ExecutionPolicy Bypass -File apps\helper\packaging\build.ps1
#
# Output:
#   dist\DrishtiHelper\             onedir app folder (DrishtiHelper.exe inside)
#   dist\DrishtiHelperSetup.exe     installer (only if Inno Setup's iscc
#                                   is on PATH: winget install JRSoftware.InnoSetup)
#   dist\DrishtiHelper-windows.zip  the app folder, zipped, for the
#                                   /api/helper/download endpoint
#
# --onedir, NOT --onefile: onefile re-extracts the Python runtime to %TEMP%
# on every launch (seconds of dead time before any window). The installer
# ships the folder pre-extracted, so the app opens near-instantly.
#
# Runtime note: pywebview uses the WebView2 runtime, preinstalled on Win 11
# and current Win 10. Signing (signtool + a code-signing cert) is a
# release-pipeline concern, same as macOS notarization.
$ErrorActionPreference = "Stop"

# Dedicated uv-managed venv (native-arch CPython), isolated from the project
# .venv — same rationale as build.sh.
$PkgVenv = "build\package-venv"
uv venv --clear --python 3.12 $PkgVenv
uv pip install --python "$PkgVenv\Scripts\python.exe" `
  pyinstaller -r apps/helper/requirements.txt

# --add-data uses ";" as the src;dest separator on Windows (":" on POSIX).
& "$PkgVenv\Scripts\pyinstaller.exe" `
  --onedir `
  --windowed `
  --name DrishtiHelper `
  --paths . `
  --add-data "$PWD\apps\helper\ui.html;apps/helper" `
  --hidden-import apps.helper.server `
  --hidden-import apps.helper.login_flow `
  --distpath dist `
  --workpath build\pyinstaller `
  --specpath build\pyinstaller `
  -y `
  apps\helper\packaging\entry.py

$version = (Select-String -Path apps\helper\version.py -Pattern '"([^"]+)"').Matches[0].Groups[1].Value

$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if ($iscc) {
  & $iscc /DAppVersion=$version /Odist apps\helper\packaging\helper.iss
  Write-Host "built: dist\DrishtiHelperSetup.exe (installer, v$version)"
} else {
  Write-Host "Inno Setup (iscc) not on PATH - skipped the installer, portable exe only."
}

Compress-Archive -Force -Path dist\DrishtiHelper `
  -DestinationPath dist\DrishtiHelper-windows.zip
Write-Host "built: dist\DrishtiHelper\ (+ -windows.zip for the download endpoint)"
