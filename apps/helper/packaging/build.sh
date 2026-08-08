#!/usr/bin/env bash
# Build the double-clickable helper app with PyInstaller.
# Run from the REPO ROOT: bash apps/helper/packaging/build.sh
# Output: dist/DrishtiHelper.app (+ dist/DrishtiHelper/ onedir folder).
#
# --onedir, NOT --onefile: onefile unpacks the whole Python runtime to a temp
# dir on EVERY launch (~10s before the window can exist). The .app ships
# pre-extracted, so it opens near-instantly.
#
# Signing/notarization and per-OS installers are a release-pipeline concern;
# this produces the raw artifact TA_HELPER_DOWNLOAD_URL should point at.
set -euo pipefail

uv pip install pyinstaller -r apps/helper/requirements.txt

uv run pyinstaller \
  --onedir \
  --windowed \
  --name DrishtiHelper \
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
if [ -d dist/DrishtiHelper.app ]; then
  rm -f dist/DrishtiHelper.dmg
  staging=$(mktemp -d)
  cp -R dist/DrishtiHelper.app "$staging/"
  ln -s /Applications "$staging/Applications"
  hdiutil create -quiet -volname "Drishti Helper" \
    -srcfolder "$staging" -format UDZO dist/DrishtiHelper.dmg
  rm -rf "$staging"
  echo "built: dist/DrishtiHelper.dmg (macOS installer)"
fi

# Zip for distribution: browsers strip the executable bit from raw binary
# downloads; unzipping restores it. Ship the .app; -y preserves its symlinks.
(cd dist && rm -f DrishtiHelper.zip
 if [ -d DrishtiHelper.app ]; then
   zip -qry DrishtiHelper.zip DrishtiHelper.app
 else
   zip -qry DrishtiHelper.zip DrishtiHelper
 fi)

echo
echo "built: dist/DrishtiHelper.app (+ .zip for the download endpoint)"
echo "smoke test: open dist/DrishtiHelper.app  (native window, no browser)"
