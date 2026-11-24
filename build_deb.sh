#!/bin/bash
# アーケード棚の DEB パッケージを作る（Ubuntu 25.04 以降向け）
#   使い方： ./build_deb.sh   → dist/arcade-tana_<版>_all.deb ができる
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' arcade_tana.py)
PKG=arcade-tana
ROOT=build/${PKG}_${VERSION}_all

rm -rf build && mkdir -p "$ROOT/DEBIAN" dist
install -Dm755 /dev/stdin "$ROOT/usr/bin/arcade-tana" <<'SH'
#!/bin/sh
exec python3 /usr/share/arcade-tana/arcade_tana.py "$@"
SH
install -Dm644 arcade_tana.py   "$ROOT/usr/share/arcade-tana/arcade_tana.py"
install -Dm644 jp_names.tsv     "$ROOT/usr/share/arcade-tana/jp_names.tsv"
install -Dm644 arcade-tana.svg  "$ROOT/usr/share/arcade-tana/arcade-tana.svg"
install -Dm644 arcade-tana.svg  "$ROOT/usr/share/icons/hicolor/scalable/apps/arcade-tana.svg"
install -Dm644 arcade-tana.desktop "$ROOT/usr/share/applications/arcade-tana.desktop"
install -Dm644 README.md        "$ROOT/usr/share/doc/$PKG/README.md"
install -Dm644 LICENSE          "$ROOT/usr/share/doc/$PKG/copyright"

SIZE=$(du -sk "$ROOT/usr" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<CTRL
Package: $PKG
Version: $VERSION
Section: games
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pyside6.qtcore, python3-pyside6.qtgui, python3-pyside6.qtwidgets
Recommends: mame, python3-pygame, ffmpeg
Installed-Size: $SIZE
Maintainer: 飯田顕治 <kenjiiida-commits@users.noreply.github.com>
Homepage: https://github.com/kenjiiida-commits/ArcadeTana
Description: Japanese-friendly launcher for MAME (アーケード棚)
 アーケード棚は、MAME を日本語で使いやすくするランチャーです。
 持っている ROM の一覧表示、日本語名での表示・検索、日本版／海外版の
 切り替え、ゲーム画面の画像表示、ゲームパッド操作、録画に対応しています。
 MAME 本体は含みません（mame パッケージを別に入れてください）。
CTRL

cat > "$ROOT/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
POST
chmod 755 "$ROOT/DEBIAN/postinst"
cp "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "$ROOT" "dist/${PKG}_${VERSION}_all.deb"
echo "できました： dist/${PKG}_${VERSION}_all.deb"
