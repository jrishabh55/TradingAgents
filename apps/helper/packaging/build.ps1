# Build the Windows helper artifacts. PyInstaller cannot cross-compile, so
# this must run ON Windows (a VM or CI runner is fine). From the REPO ROOT:
#   powershell -ExecutionPolicy Bypass -File apps\helper\packaging\build.ps1
#
# Output:
#   dist\DrishtiHelper.exe          portable, double-clickable
#   dist\DrishtiHelperSetup.exe     installer (only if Inno Setup's iscc
#                                         is on PATH: winget install JRSoftware.InnoSetup)
#   dist\DrishtiHelper-windows.zip  the portable exe, zipped for the
#                                         /api/helper/download endpoint
#
# Runtime note: pywebview uses the WebView2 runtime, preinstalled on Win 11
# and current Win 10. Signing (signtool + a code-signing cert) is a
# release-pipeline concern, same as macOS notarization.
$ErrorActionPreference = "Stop"

uv pip install pyinstaller -r apps/helper/requirements.txt

# --add-data uses ";" as the src;dest separator on Windows (":" on POSIX).
uv run pyinstaller `
  --onefile `
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

Compress-Archive -Force -Path dist\DrishtiHelper.exe `
  -DestinationPath dist\DrishtiHelper-windows.zip
Write-Host "built: dist\DrishtiHelper.exe (+ -windows.zip for the download endpoint)"
