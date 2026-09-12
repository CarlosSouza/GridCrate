#!/bin/sh
# GridCrate installer - creates the launcher, icon and desktop entry for the
# current user. Nothing is copied: the app runs from this checkout.
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

mkdir -p "$BIN" "$APPS" "$ICONS"

printf '#!/bin/sh\nexec python3 "%s/gridcrate.py" "$@"\n' "$REPO" > "$BIN/gridcrate"
chmod +x "$BIN/gridcrate"

cp "$REPO/assets/gridcrate.png" "$ICONS/gridcrate.png"

cat > "$APPS/gridcrate.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=GridCrate
Comment=Artwork manager for non-Steam games
Exec=$BIN/gridcrate
Icon=gridcrate
Terminal=false
Categories=Utility;
Keywords=steam;artwork;grid;steamgriddb;faugus;playnite;
StartupNotify=false
StartupWMClass=gridcrate
DESKTOP

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 --noincremental >/dev/null 2>&1 || true

echo "Installed."
echo "  launcher: $BIN/gridcrate"
echo "  desktop:  $APPS/gridcrate.desktop"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  note: $BIN is not in your PATH" ;;
esac
echo
echo "Run 'gridcrate' to open the app, or find GridCrate in your application menu."
