#!/bin/bash
# DEB を使わずに、このユーザーだけにインストールする
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.local/share/arcade-tana"
mkdir -p "$DIR" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$SRC/arcade_tana.py" "$SRC/jp_names.tsv" "$SRC/arcade-tana.svg" "$DIR/"
cp "$SRC/arcade-tana.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/"
sed "s|^Exec=.*|Exec=python3 $DIR/arcade_tana.py|" "$SRC/arcade-tana.desktop" \
    > "$HOME/.local/share/applications/arcade-tana.desktop"
echo "インストールしました。アプリ一覧から「アーケード棚」で起動できます。"
