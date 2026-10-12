#!/bin/bash
# Construit "Rsync GUI.app" puis le DMG dans build/.
#
# Usage : scripts/build.sh [--rebuild-rsync] [--install]
#   --rebuild-rsync  recompile rsync même si une version est déjà en cache
#   --install        copie aussi l'app dans ~/Applications
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(tr -d '[:space:]' < VERSION)"
BUILD="$ROOT/build"
APP="$BUILD/Rsync GUI.app"
CACHE="$BUILD/cache"
DMG="$BUILD/Rsync-GUI-$VERSION.dmg"
REBUILD=0; INSTALL=0
for a in "$@"; do
  case "$a" in
    --rebuild-rsync) REBUILD=1 ;;
    --install) INSTALL=1 ;;
    *) echo "Option inconnue : $a" >&2; exit 2 ;;
  esac
done

echo "==> Logo"
[ -f assets/AppIcon.icns ] || python3 assets/make_icon.py

echo "==> rsync embarqué"
mkdir -p "$CACHE"
if [ "$REBUILD" = 1 ] || [ ! -x "$CACHE/rsync" ]; then
  python3 src/app.py --build-rsync "$CACHE/rsync"
fi
echo "    $("$CACHE/rsync" --version | head -1)"

echo "==> App"
mkdir -p "$BUILD"
# on garde le cache, on refait le reste
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/bin"
swiftc -O -o "$APP/Contents/MacOS/RsyncGUI" src/RsyncGUI.swift -framework Cocoa -framework WebKit
cp src/app.py "$APP/Contents/Resources/app.py"
rm -rf "$APP/Contents/Resources/web"
cp -R src/web "$APP/Contents/Resources/web"
cp "$CACHE/rsync" "$APP/Contents/Resources/bin/rsync"
cp assets/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
sed "s/__VERSION__/$VERSION/g" src/Info.plist > "$APP/Contents/Info.plist"
codesign --force --deep --sign - "$APP"
echo "    $APP"

echo "==> DMG"
STAGE="$BUILD/dmg-stage"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Rsync GUI" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
echo "    $DMG"

if [ "$INSTALL" = 1 ]; then
  echo "==> Installation dans ~/Applications"
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/Rsync GUI.app"
  cp -R "$APP" "$HOME/Applications/"
fi
echo "Terminé."
