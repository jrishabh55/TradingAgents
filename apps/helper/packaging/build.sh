#!/usr/bin/env bash
# Build the double-clickable helper app with PyInstaller.
# Run from the REPO ROOT: bash apps/helper/packaging/build.sh
# Output: dist/TradingAgentsHelper (single self-contained executable).
#
# Signing/notarization and per-OS installers are a release-pipeline concern;
# this produces the raw artifact TA_HELPER_DOWNLOAD_URL should point at.
set -euo pipefail

uv pip install pyinstaller -r apps/helper/requirements.txt

uv run pyinstaller \
  --onefile \
  --windowed \
  --name TradingAgentsHelper \
  --paths . \
  --add-data "$PWD/apps/helper/ui.html:apps/helper" \
  --hidden-import apps.helper.server \
  --hidden-import apps.helper.login_flow \
  --distpath dist \
  --workpath build/pyinstaller \
  --specpath build/pyinstaller \
  -y \
  apps/helper/packaging/entry.py

# macOS installer: a DMG with an /Applications shortcut — drag, drop, done.
# Unsigned builds still hit Gatekeeper on first open (right-click → Open);
# codesign + notarization is the release-pipeline fix.
if [ -d dist/TradingAgentsHelper.app ]; then
  rm -f dist/TradingAgentsHelper.dmg
  staging=$(mktemp -d)
  cp -R dist/TradingAgentsHelper.app "$staging/"
  ln -s /Applications "$staging/Applications"
  hdiutil create -quiet -volname "TradingAgents Helper" \
    -srcfolder "$staging" -format UDZO dist/TradingAgentsHelper.dmg
  rm -rf "$staging"
  echo "built: dist/TradingAgentsHelper.dmg (macOS installer)"
fi

# Zip for distribution: browsers strip the executable bit from raw binary
# downloads; unzipping restores it. On macOS --windowed also emits a proper
# .app bundle — ship that (double-click, no Terminal); elsewhere ship the
# onefile binary. -y preserves the symlinks inside the .app.
(cd dist && rm -f TradingAgentsHelper.zip
 if [ -d TradingAgentsHelper.app ]; then
   zip -qry TradingAgentsHelper.zip TradingAgentsHelper.app
 else
   zip -q TradingAgentsHelper.zip TradingAgentsHelper
 fi)

echo
echo "built: dist/TradingAgentsHelper (+ .zip for the download endpoint)"
echo "smoke test: open dist/TradingAgentsHelper.app  (native window, no browser)"
