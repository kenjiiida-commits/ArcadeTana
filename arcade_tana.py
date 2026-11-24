#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
アーケード棚 (Arcade Tana)
MAME を日本語で使いやすくするためのランチャー（起動用ソフト）。
MAME 本体には手を加えず、外から mame コマンドを呼び出して使う。

ライセンス: MIT
作者: 飯田顕治
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QByteArray, QModelIndex, QObject, QProcess, QSortFilterProxyModel,
    QStandardPaths, Qt, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSizePolicy, QSplitter, QTableView, QVBoxLayout, QWidget,
)

APP_NAME = "アーケード棚"
APP_ID = "arcade-tana"
VERSION = "0.8.0"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_ID
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"

ROM_EXTS = {".zip", ".7z"}
IMG_EXTS = (".png", ".jpg", ".jpeg")


# ─────────────────────────────────────────────
# 設定の読み書き
# ─────────────────────────────────────────────

def guess_mame_path() -> str:
    found = shutil.which("mame")
    if found:
        return found
    for p in ("/usr/games/mame", "/usr/bin/mame", "/snap/bin/mame"):
        if Path(p).exists():
            return p
    return "mame"


def guess_rom_dirs() -> list[str]:
    """~/.mame/mame.ini の rompath を読んで ROM フォルダを推測する。"""
    dirs: list[str] = []
    ini = Path.home() / ".mame" / "mame.ini"
    if ini.exists():
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*rompath\s+(.+)$", line)
            if m:
                for part in m.group(1).strip().strip('"').split(";"):
                    part = os.path.expandvars(os.path.expanduser(part.strip()))
                    if part:
                        p = Path(part)
                        if not p.is_absolute():
                            p = Path.home() / ".mame" / p
                        dirs.append(str(p))
    for cand in (Path.home() / "mame" / "roms", Path.home() / ".mame" / "roms"):
        if cand.is_dir() and str(cand) not in dirs:
            dirs.append(str(cand))
    return [d for d in dirs if Path(d).is_dir()]


def guess_snap_dirs() -> list[str]:
    return [str(Path.home() / ".mame" / "snap")]


DEFAULT_CONFIG = {
    "mame_path": "",
    "rom_dirs": [],
    "snap_dirs": [],            # ゲーム画面の画像フォルダ
    "hide_clones": False,       # クローン（別バージョン）を隠す（日本版があれば日本版を残す）
    "window_mode": False,       # True ならウィンドウで起動
    "mame_japanese": True,      # MAME 内のメニューも日本語に
    "skip_gameinfo": True,      # 起動時の英語の注意画面を飛ばす
    "extra_args": "",
    "gamepad": True,            # ゲームパッドで操作する
    "record": False,            # 録画しながら遊ぶ
    "record_dir": "",           # 録画の保存先（空なら「ビデオ/アーケード棚」）
    "record_crf": 20,           # 画質（小さいほど高画質・大きなファイル）
    "record_43": True,          # 4:3（縦画面は 3:4）に整えて拡大する
    "record_keep_avi": False,   # 変換前の AVI を残す
    "favorites": [],
    "jp_names": {},             # セット名 → 日本語名
    "jp_yomi": {},              # セット名 → 読み（並べ替え用）
    "header_state": "",         # 列の幅（前回のまま）
    "splitter_state": "",
    "history": {},              # セット名 → {"count": 回数, "last": UNIX時刻}
    "filter_mode": 0,
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    if not cfg["mame_path"]:
        cfg["mame_path"] = guess_mame_path()
    if not cfg["rom_dirs"]:
        cfg["rom_dirs"] = guess_rom_dirs()
    if not cfg["snap_dirs"]:
        cfg["snap_dirs"] = guess_snap_dirs()
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def read_name_list(path) -> tuple[dict[str, str], dict[str, str]]:
    """「セット名<TAB>日本語名<TAB>読み」（またはカンマ区切り）のリストを読む。読みは省略可。"""
    names: dict[str, str] = {}
    yomi: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t") if "\t" in line else line.split(",", 2)
                if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                    names[parts[0].strip()] = parts[1].strip()
                    if len(parts) >= 3 and parts[2].strip():
                        yomi[parts[0].strip()] = parts[2].strip()
    except OSError:
        pass
    return names, yomi


BUILTIN_NAMES_FILE = Path(__file__).resolve().parent / "jp_names.tsv"


def normalize(text: str) -> str:
    """検索用：全角/半角・大文字/小文字・カタカナ/ひらがなの違いをなくす。"""
    t = unicodedata.normalize("NFKC", text).lower()
    # カタカナ → ひらがな
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in t)


def yomi_key(text: str) -> str:
    """並べ替え用：ひらがなにそろえ、濁点・半濁点・長音・記号を外す（が→か、ぱ→は）。"""
    t = unicodedata.normalize("NFKD", normalize(text))
    t = "".join(c for c in t if c not in "\u3099\u309a")
    return re.sub(r"[\sー・'’!！?？.,、。:：\-~〜&＆*]", "", t)


# ─────────────────────────────────────────────
# MAME からゲーム一覧を取る（時間がかかるので別スレッド）
# ─────────────────────────────────────────────

class ListLoader(QThread):
    finished_ok = Signal(dict, str)   # {セット名: 英語名}, MAMEのバージョン
    failed = Signal(str)

    def __init__(self, mame_path: str):
        super().__init__()
        self.mame_path = mame_path

    def run(self):
        try:
            ver = subprocess.run([self.mame_path, "-version"], capture_output=True,
                                 text=True, timeout=30).stdout.strip() or "unknown"
        except FileNotFoundError:
            self.failed.emit(f"MAME が見つかりません：{self.mame_path}\n"
                             "「設定」で MAME の場所を指定してください。\n"
                             "まだ入れていない場合は、端末で\n  sudo apt install mame\nを実行してください。")
            return
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"MAME を起動できませんでした：{e}")
            return

        safe_ver = re.sub(r"[^0-9A-Za-z._-]", "_", ver)[:40]
        cache = CACHE_DIR / f"listfull-{safe_ver}.json"
        if cache.exists():
            try:
                self.finished_ok.emit(json.loads(cache.read_text(encoding="utf-8")), ver)
                return
            except (OSError, json.JSONDecodeError):
                pass

        try:
            out = subprocess.run([self.mame_path, "-listfull"], capture_output=True,
                                 text=True, timeout=300, errors="replace").stdout
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"ゲーム一覧の取得に失敗しました：{e}")
            return

        games: dict[str, str] = {}
        for line in out.splitlines():
            m = re.match(r'^(\S+)\s+"(.*)"\s*$', line)
            if m:
                games[m.group(1)] = m.group(2)
        if not games:
            self.failed.emit("MAME からゲーム一覧を読み取れませんでした。")
            return
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(games, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        self.finished_ok.emit(games, ver)


class InfoLoader(QThread):
    """mame -listxml から年・メーカー・人数・動作状況などを読む（初回のみ。数十秒かかる）"""
    progress = Signal(int)
    finished_ok = Signal(dict)

    def __init__(self, mame_path: str, version: str):
        super().__init__()
        self.mame_path = mame_path
        self.version = version

    def run(self):
        safe_ver = re.sub(r"[^0-9A-Za-z._-]", "_", self.version)[:40]
        cache = CACHE_DIR / f"info2-{safe_ver}.json"
        if cache.exists():
            try:
                self.finished_ok.emit(json.loads(cache.read_text(encoding="utf-8")))
                return
            except (OSError, json.JSONDecodeError):
                pass
        info: dict[str, dict] = {}
        try:
            proc = subprocess.Popen([self.mame_path, "-listxml"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            ctx = ET.iterparse(proc.stdout, events=("start", "end"))
            _, root = next(ctx)
            for ev, el in ctx:
                if ev != "end" or el.tag != "machine":
                    continue
                name = el.get("name")
                if name and el.get("isdevice") != "yes" and el.get("runnable") != "no":
                    drv = el.find("driver")
                    inp = el.find("input")
                    info[name] = {
                        "year": (el.findtext("year") or "").strip(),
                        "maker": (el.findtext("manufacturer") or "").strip(),
                        "cloneof": el.get("cloneof", ""),
                        "bios": el.get("isbios") == "yes",
                        "mech": el.get("ismechanical") == "yes",
                        "players": inp.get("players", "") if inp is not None else "",
                        "status": drv.get("status", "") if drv is not None else "",
                    }
                    if el.get("cloneof"):
                        # 別バージョンだけが持つ ROM（親と共有しないもの）の CRC。
                        # 親の zip にこれが全部入っていれば、マージ形式で「持っている」と判定する。
                        info[name]["crcs"] = [
                            r.get("crc").lower() for r in el.findall("rom")
                            if r.get("crc") and not r.get("merge") and r.get("status") != "nodump"]
                    if len(info) % 2000 == 0:
                        self.progress.emit(len(info))
                root.clear()
            proc.wait()
        except Exception:  # noqa: BLE001  詳細情報は無くても動くので静かに諦める
            return
        if not info:
            return
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        self.finished_ok.emit(info)


def maker_key(maker: str) -> list[str]:
    """「Namco (Midway license)」「Capcom / Sega」→ ["Namco"], ["Capcom", "Sega"]"""
    out = []
    for part in maker.split("/"):
        part = re.sub(r"\(.*?\)|\[.*?\]", "", part).strip(" ?")
        if part and part.lower() not in ("<unknown>", "unknown", "bootleg"):
            out.append(part)
    return out


def decade_of(year: str) -> str:
    m = re.match(r"^(\d{3})", year)
    return f"{m.group(1)}0年代" if m else ""


REGION_JA = [("Hong Kong", "香港"), ("Japan", "日本"), ("World", "海外"), ("Europe", "欧州"),
             ("USA", "北米"), ("US", "北米"), ("Asia", "アジア"), ("Korea", "韓国"),
             ("Taiwan", "台湾"), ("Hispanic", "中南米"), ("Brazil", "ブラジル"),
             ("China", "中国"), ("bootleg", "海賊版"), ("prototype", "試作版")]


def paren_of(desc: str) -> str:
    """英語名の最後の（ ）の中身。例：Rygar (US set 1) → US set 1"""
    m = re.search(r"\(([^()]*)\)\s*$", desc)
    return m.group(1) if m else ""


def has_region(p: str) -> bool:
    return any(re.search(rf"\b{re.escape(en)}\b", p) for en, _ in REGION_JA)


def translate_paren(p: str) -> str:
    """US set 1 → 北米 set 1、Japan, newer → 日本, newer"""
    for en, ja in REGION_JA:
        p = re.sub(rf"\b{re.escape(en)}\b", ja, p)
    return p


def is_japan_desc(desc: str) -> bool:
    return bool(re.search(r"\bJapan\b", paren_of(desc)))


STATUS_JA = {"good": "◎ よく動く", "imperfect": "△ 一部不完全", "preliminary": "× 未完成（遊べないことが多い）"}


def find_snapshot(name: str, snap_dirs: list[str]) -> Path | None:
    for d in snap_dirs:
        base = Path(d)
        for ext in IMG_EXTS:
            f = base / f"{name}{ext}"
            if f.is_file():
                return f
        sub = base / name
        if sub.is_dir():
            try:
                imgs = [f for f in sub.iterdir() if f.suffix.lower() in IMG_EXTS]
            except OSError:
                imgs = []
            if imgs:
                return max(imgs, key=lambda f: f.stat().st_mtime)   # いちばん新しい画像
    return None


def scan_owned(rom_dirs: list[str]) -> dict[str, Path]:
    """ROM フォルダにある zip / 7z / フォルダを集める。{セット名: 場所}"""
    found: dict[str, Path] = {}
    for d in rom_dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        try:
            for entry in p.iterdir():
                if entry.is_dir():
                    found.setdefault(entry.name.lower(), entry)
                elif entry.suffix.lower() in ROM_EXTS:
                    found.setdefault(entry.stem.lower(), entry)
        except OSError:
            continue
    return found


def expand_owned(found: dict[str, Path], info: dict) -> set[str]:
    """マージ形式の ROM セット対応：
    別バージョン（例 rygarj）の zip が無くても、親の zip（rygar.zip）の中に
    別バージョン用のファイルが全部入っていれば「持っている」とみなす。"""
    owned = set(found)
    zip_crcs: dict[Path, set[str]] = {}
    for name, inf in info.items():
        parent = inf.get("cloneof")
        if not parent or name in owned or parent not in found:
            continue
        crcs = inf.get("crcs", [])
        path = found[parent]
        if not crcs or path.is_dir() or path.suffix.lower() != ".zip":
            # 共有ファイルだけで動くもの／中身を調べられない形式（7z・フォルダ）は、親があれば OK とする
            owned.add(name)
            continue
        if path not in zip_crcs:
            try:
                with zipfile.ZipFile(path) as z:
                    zip_crcs[path] = {f"{zi.CRC:08x}" for zi in z.infolist()}
            except (OSError, zipfile.BadZipFile):
                zip_crcs[path] = set()
        if all(c in zip_crcs[path] for c in crcs):
            owned.add(name)
    return owned


# ─────────────────────────────────────────────
# 録画（MAME が書き出した AVI を ffmpeg で MP4 に変換する）
# ─────────────────────────────────────────────

def default_record_dir() -> str:
    base = QStandardPaths.writableLocation(QStandardPaths.MoviesLocation) or str(Path.home() / "Videos")
    return str(Path(base) / APP_NAME)


def safe_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\n\r\t]', "_", text).strip() or "game"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
    return path


class Recorder(QObject):
    """AVI → MP4 の変換を1本ずつ順番に行う。"""
    progress = Signal(str)
    done = Signal(str)            # 保存した MP4（または AVI）の場所
    failed = Signal(str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.queue: list[tuple[Path, Path]] = []
        self.proc: QProcess | None = None
        self.current: tuple[Path, Path] | None = None
        self.duration = 0.0

    def busy(self) -> bool:
        return self.proc is not None or bool(self.queue)

    def add(self, avi: Path, mp4: Path):
        self.queue.append((avi, mp4))
        if self.proc is None:
            self.start_next()

    def start_next(self):
        if not self.queue:
            return
        avi, mp4 = self.queue.pop(0)
        mp4.parent.mkdir(parents=True, exist_ok=True)
        if not shutil.which("ffmpeg"):
            dest = unique_path(mp4.with_suffix(".avi"))
            try:
                shutil.move(str(avi), dest)
                self.failed.emit(f"ffmpeg が無いので、MP4 に変換せず AVI のまま保存しました：\n{dest}\n\n"
                                 "小さな MP4 にしたいときは、端末で\n  sudo apt install ffmpeg\nを実行してください。")
            except OSError as e:
                self.failed.emit(f"録画ファイルを移動できませんでした：{e}")
            self.start_next()
            return
        self.current = (avi, mp4)
        self.duration = 0.0
        if shutil.which("ffprobe"):
            try:
                out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                      "-of", "csv=p=0", str(avi)], capture_output=True, text=True,
                                     timeout=20).stdout.strip()
                self.duration = float(out or 0)
            except (ValueError, subprocess.SubprocessError, OSError):
                pass
        if self.cfg.get("record_43", True):
            # 横画面 → 1280x960、縦画面 → 720x960。ドットはぼかさずに拡大
            vf = "scale='if(gte(iw,ih),1280,720)':960:flags=neighbor,setsar=1"
        else:
            vf = "scale=trunc(iw*3/2)*2:trunc(ih*3/2)*2:flags=neighbor,setsar=1"
        part = mp4.with_name(mp4.name + ".part")
        self.proc = QProcess(self)
        self.proc.setProgram("ffmpeg")
        self.proc.setArguments([
            "-hide_banner", "-nostdin", "-y", "-i", str(avi), "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.cfg.get("record_crf", 20)),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", "-f", "mp4", str(part)])
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.finished.connect(lambda code, _st, part=part: self.on_finished(code, part))
        self.proc.start()
        self.progress.emit(f"録画を MP4 に変換中：{mp4.name}")

    def on_output(self):
        text = bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
        for m in re.finditer(r"out_time_us=(\d+)", text):
            sec = int(m.group(1)) / 1_000_000
            name = self.current[1].name if self.current else ""
            if self.duration > 0:
                pct = min(99, int(sec / self.duration * 100))
                self.progress.emit(f"録画を MP4 に変換中… {pct}%　{name}")
            else:
                self.progress.emit(f"録画を MP4 に変換中… {int(sec)} 秒分　{name}")

    def on_finished(self, code: int, part: Path):
        avi, mp4 = self.current
        err = bytes(self.proc.readAllStandardError()).decode(errors="replace")
        self.proc.deleteLater()
        self.proc = None
        self.current = None
        if code == 0 and part.exists() and part.stat().st_size > 0:
            final = unique_path(mp4)
            part.replace(final)
            if self.cfg.get("record_keep_avi"):
                try:
                    shutil.move(str(avi), unique_path(final.with_suffix(".avi")))
                except OSError:
                    pass
            else:
                avi.unlink(missing_ok=True)
            self.done.emit(str(final))
        else:
            part.unlink(missing_ok=True)
            self.failed.emit("MP4 への変換に失敗しました。元の AVI は残してあります：\n"
                             f"{avi}\n\n{err.strip()[-600:]}")
        self.start_next()

    def abort_all(self):
        """終了するとき：変換をやめ、まだの AVI は保存先へ移しておく。"""
        if self.proc:
            self.proc.kill()
            self.proc.waitForFinished(3000)
            if self.current:
                self.queue.insert(0, self.current)
        for avi, mp4 in self.queue:
            mp4.with_name(mp4.name + ".part").unlink(missing_ok=True)
            try:
                mp4.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(avi), unique_path(mp4.with_suffix(".avi")))
            except OSError:
                pass
        self.queue.clear()


# ─────────────────────────────────────────────
# ゲームパッド（pygame / SDL で読む。入っていなければ使わない）
# ─────────────────────────────────────────────

class Gamepad(QObject):
    """ゲームパッドのボタンを読んで action シグナルを出す。
    action の名前：up / down / left / right / a / b / x / y / lb / rb / back / start"""
    action = Signal(str)
    status = Signal(str)          # 接続状態の表示用

    REPEATABLE = {"up", "down", "left", "right", "lb", "rb"}
    REPEAT_DELAY = 0.40           # 押しっぱなしで連続移動が始まるまで（秒）
    REPEAT_RATE = 0.07            # 連続移動の間隔（秒）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ok = False
        self.devs: list = []
        self.held: dict[str, float] = {}
        self.next_repeat: dict[str, float] = {}
        self.paused = False
        try:
            os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")      # pygame の窓は作らない
            os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
            # SDL が終了の合図（Ctrl+C・ログアウト時の SIGTERM）を横取りしないようにする
            os.environ.setdefault("SDL_NO_SIGNAL_HANDLERS", "1")
            import pygame
            self.pg = pygame
            try:
                from pygame._sdl2 import controller as sdlc
                self.sdlc = sdlc
            except ImportError:
                self.sdlc = None
            pygame.display.init()
            pygame.joystick.init()
            if self.sdlc:
                self.sdlc.init()
            self.ok = True
        except Exception:  # noqa: BLE001
            self.pg = None
            return
        self.rescan()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(30)

    def rescan(self):
        pg = self.pg
        self.devs = []
        names = []
        for i in range(pg.joystick.get_count()):
            try:
                if self.sdlc and self.sdlc.is_controller(i):
                    dev = self.sdlc.Controller(i)
                    names.append(dev.name)
                else:
                    dev = pg.joystick.Joystick(i)
                    dev.init()
                    names.append(dev.get_name())
                self.devs.append(dev)
            except Exception:  # noqa: BLE001
                continue
        self.status.emit(f"🎮 {names[0]}" if names else "🎮 ゲームパッド未接続")

    def read(self) -> set[str]:
        pg = self.pg
        st: set[str] = set()
        for dev in self.devs:
            try:
                if self.sdlc and isinstance(dev, self.sdlc.Controller):
                    btn = {pg.CONTROLLER_BUTTON_DPAD_UP: "up", pg.CONTROLLER_BUTTON_DPAD_DOWN: "down",
                           pg.CONTROLLER_BUTTON_DPAD_LEFT: "left", pg.CONTROLLER_BUTTON_DPAD_RIGHT: "right",
                           pg.CONTROLLER_BUTTON_A: "a", pg.CONTROLLER_BUTTON_B: "b",
                           pg.CONTROLLER_BUTTON_X: "x", pg.CONTROLLER_BUTTON_Y: "y",
                           pg.CONTROLLER_BUTTON_LEFTSHOULDER: "lb", pg.CONTROLLER_BUTTON_RIGHTSHOULDER: "rb",
                           pg.CONTROLLER_BUTTON_BACK: "back", pg.CONTROLLER_BUTTON_START: "start"}
                    for b, nm in btn.items():
                        if dev.get_button(b):
                            st.add(nm)
                    y = dev.get_axis(pg.CONTROLLER_AXIS_LEFTY) / 32768
                    x = dev.get_axis(pg.CONTROLLER_AXIS_LEFTX) / 32768
                else:
                    # 汎用の扱い：ボタン 0〜7 を A B X Y LB RB BACK START とみなす
                    for b, nm in enumerate(("a", "b", "x", "y", "lb", "rb", "back", "start")):
                        if b < dev.get_numbuttons() and dev.get_button(b):
                            st.add(nm)
                    if dev.get_numhats():
                        hx, hy = dev.get_hat(0)
                        if hy > 0: st.add("up")
                        if hy < 0: st.add("down")
                        if hx < 0: st.add("left")
                        if hx > 0: st.add("right")
                    x = dev.get_axis(0) if dev.get_numaxes() > 0 else 0
                    y = dev.get_axis(1) if dev.get_numaxes() > 1 else 0
                if y < -0.6: st.add("up")
                if y > 0.6: st.add("down")
                if x < -0.6: st.add("left")
                if x > 0.6: st.add("right")
            except Exception:  # noqa: BLE001
                continue
        return st

    def poll(self):
        pg = self.pg
        try:
            for ev in pg.event.get():
                if ev.type in (pg.JOYDEVICEADDED, pg.JOYDEVICEREMOVED,
                               getattr(pg, "CONTROLLERDEVICEADDED", -1),
                               getattr(pg, "CONTROLLERDEVICEREMOVED", -1)):
                    self.rescan()
        except Exception:  # noqa: BLE001
            return
        now = time.monotonic()
        pressed = self.read()
        if self.paused:
            # ゲーム中は反応しない（離すまで押されたまま扱いにして、戻った瞬間の誤作動を防ぐ）
            self.held = {k: now for k in pressed}
            self.next_repeat = {k: now + 3600 for k in pressed}
            return
        for k in list(self.held):
            if k not in pressed:
                del self.held[k]
                self.next_repeat.pop(k, None)
        for k in pressed:
            if k not in self.held:
                self.held[k] = now
                self.next_repeat[k] = now + self.REPEAT_DELAY
                self.action.emit(k)
            elif k in self.REPEATABLE and now >= self.next_repeat[k]:
                self.next_repeat[k] = now + self.REPEAT_RATE
                self.action.emit(k)


# ─────────────────────────────────────────────
# 一覧表のデータ
# ─────────────────────────────────────────────

COLS = ["★", "ゲーム名", "年", "メーカー", "セット名", "回数", "最後に遊んだ日"]


class GameModel(QAbstractTableModel):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.rows: list[str] = []
        self.games: dict[str, str] = {}
        self.owned: set[str] = set()
        self.info: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}
        self._rep: dict[str, str] = {}
        self.builtin, self.builtin_yomi = read_name_list(BUILTIN_NAMES_FILE)

    def jp(self, name: str) -> str:
        """日本語名：自分で付けた名前 → 同梱リスト の順に探す。"""
        return self.cfg["jp_names"].get(name) or self.builtin.get(name, "")

    def yomi(self, name: str) -> str:
        """読み（無ければ日本語名そのもの）"""
        if name in self.cfg["jp_names"]:
            return self.cfg["jp_yomi"].get(name) or self.cfg["jp_names"][name]
        return self.builtin_yomi.get(name) or self.builtin.get(name, "")

    def sort_key(self, name: str) -> str:
        eng = self.games.get(name, name)
        parent = self.info.get(name, {}).get("cloneof", "")
        if self.jp(name):
            base = self.yomi(name)
        elif parent and self.jp(parent):
            base = re.sub(r"（[^（）]*）$", "", self.yomi(parent))
        else:
            return yomi_key(eng)
        # 同じゲームの中では日本版を先に
        rank = "0" if self.is_japan(name) else "1"
        return yomi_key(base) + "\uffff" + rank + paren_of(eng).lower()

    def set_info(self, info: dict):
        self.beginResetModel()
        self.info = info
        self.children: dict[str, list[str]] = {}
        for n, inf in info.items():
            if inf.get("cloneof"):
                self.children.setdefault(inf["cloneof"], []).append(n)
        self._rep = {}
        self.endResetModel()

    def is_japan(self, name: str) -> bool:
        return is_japan_desc(self.games.get(name, ""))

    def family(self, name: str) -> list[str]:
        """同じゲームのバージョン一覧（元のゲーム ＋ 別バージョン）"""
        root = self.info.get(name, {}).get("cloneof") or name
        return [root] + sorted(getattr(self, "children", {}).get(root, []))

    def representative(self, name: str) -> str:
        """別バージョンを隠すときに残す1本：持っている中で日本版 → 元のゲーム → ほか"""
        root = self.info.get(name, {}).get("cloneof") or name
        if root not in self._rep:
            fam = self.family(root)
            pool = [n for n in fam if n in self.owned] or fam
            jp = [n for n in pool if self.is_japan(n)]
            self._rep[root] = jp[0] if jp else (root if root in pool else pool[0])
        return self._rep[root]

    def set_data(self, games: dict[str, str], owned: set[str]):
        self.beginResetModel()
        self.games = games
        self.owned = owned
        self._rep = {}
        self.rows = sorted(games.keys())
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[section]
        return None

    def display_name(self, name: str) -> str:
        eng = self.games.get(name, name)
        p = paren_of(eng)
        own = self.jp(name)
        if own:
            # 地域（日本・北米など）が分かるときは書き足す
            if p and has_region(p) and not re.search(r"[（(]", own):
                return f"{own}（{translate_paren(p)}）"
            return own
        parent = self.info.get(name, {}).get("cloneof", "")
        if parent and self.jp(parent):
            # 別バージョン：元のゲームの日本語名 ＋ 英語名の（ ）の中身（地域は日本語に）
            base = re.sub(r"（[^（）]*）$", "", self.jp(parent))
            return f"{base}（{translate_paren(p)}）" if p else f"{base}（別バージョン）"
        return eng

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        name = self.rows[index.row()]
        col = index.column()
        hist = self.cfg["history"].get(name, {})
        if role in (Qt.DisplayRole, Qt.UserRole):
            if col == 0:
                fav = name in self.cfg["favorites"]
                return ("★" if fav else "") if role == Qt.DisplayRole else (0 if fav else 1)
            if col == 1:
                return self.display_name(name) if role == Qt.DisplayRole else self.sort_key(name)
            if col == 2:
                return self.info.get(name, {}).get("year", "")
            if col == 3:
                return self.info.get(name, {}).get("maker", "")
            if col == 4:
                return name
            if col == 5:
                c = hist.get("count", 0)
                return (str(c) if c else "") if role == Qt.DisplayRole else c
            if col == 6:
                last = hist.get("last", 0)
                if role == Qt.UserRole:
                    return last
                return time.strftime("%Y/%m/%d", time.localtime(last)) if last else ""
        if role == Qt.ForegroundRole and name not in self.owned:
            return QApplication.palette().placeholderText()
        if role == Qt.TextAlignmentRole and col in (0, 2, 5):
            return Qt.AlignCenter
        if role == Qt.ToolTipRole:
            tip = f"{self.display_name(name)}\n{self.games.get(name, '')}"
            return tip if name in self.owned else f"{tip}\n（この ROM は ROM フォルダに見つかりません）"
        return None

    def refresh_row(self, name: str):
        try:
            r = self.rows.index(name)
        except ValueError:
            return
        self.dataChanged.emit(self.index(r, 0), self.index(r, len(COLS) - 1))


class GameFilter(QSortFilterProxyModel):
    # 0=持っているゲーム 1=お気に入り 2=最近遊んだ 3=すべて
    def __init__(self, model: GameModel):
        super().__init__()
        self.m = model
        self.mode = 0
        self.words: list[str] = []
        self.decade = ""
        self.maker = ""
        self.hide_clones = False
        self.setSortRole(Qt.UserRole)

    def set_extra(self, decade: str, maker: str, hide_clones: bool):
        self.decade, self.maker, self.hide_clones = decade, maker, hide_clones
        self.invalidateFilter()

    def set_query(self, text: str):
        self.words = normalize(text).split()
        self.invalidateFilter()

    def set_mode(self, mode: int):
        self.mode = mode
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        name = self.m.rows[row]
        cfg = self.m.cfg
        if self.mode == 0 and name not in self.m.owned:
            return False
        if self.mode == 1 and name not in cfg["favorites"]:
            return False
        if self.mode == 2 and name not in cfg["history"]:
            return False
        inf = self.m.info.get(name)
        if inf is not None:
            if inf["bios"] or inf["mech"]:
                return False
            if self.hide_clones and self.m.representative(name) != name:
                return False
            if self.decade and decade_of(inf["year"]) != self.decade:
                return False
            if self.maker and self.maker not in maker_key(inf["maker"]):
                return False
        elif self.decade or self.maker:
            return False
        if not self.words:
            return True
        parent = self.m.info.get(name, {}).get("cloneof", "")
        hay = normalize(" ".join((name, self.m.games.get(name, ""), self.m.display_name(name),
                                  self.m.yomi(name), self.m.yomi(parent) if parent else "")))
        return all(w in hay for w in self.words)


# ─────────────────────────────────────────────
# 設定画面
# ─────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.resize(620, 700)
        self.cfg = cfg

        form = QFormLayout()
        self.mame_edit = QLineEdit(cfg["mame_path"])
        btn = QPushButton("選ぶ…")
        btn.clicked.connect(self.pick_mame)
        row = QHBoxLayout()
        row.addWidget(self.mame_edit)
        row.addWidget(btn)
        form.addRow("MAME の場所：", row)

        self.dir_list, box = self.dir_editor(cfg["rom_dirs"], "ROM フォルダを選ぶ")
        form.addRow("ROM フォルダ：", box)
        self.snap_list, box2 = self.dir_editor(cfg["snap_dirs"], "画像フォルダを選ぶ")
        form.addRow("画像フォルダ：", box2)
        hint = QLabel("※ ゲーム中に F12 キーで撮った画面は、1つ目の画像フォルダに保存され、一覧の右に表示されます。")
        hint.setWordWrap(True)
        form.addRow("", hint)

        self.window_chk = QCheckBox("ウィンドウで起動する（オフなら全画面）")
        self.window_chk.setChecked(cfg["window_mode"])
        self.jp_chk = QCheckBox("MAME の中のメニューも日本語にする")
        self.jp_chk.setChecked(cfg["mame_japanese"])
        self.skip_chk = QCheckBox("起動時の英語の注意画面を飛ばす")
        self.skip_chk.setChecked(cfg["skip_gameinfo"])
        form.addRow("", self.window_chk)
        form.addRow("", self.jp_chk)
        form.addRow("", self.skip_chk)
        self.pad_chk = QCheckBox("ゲームパッドで一覧を操作する（次回起動から反映）")
        self.pad_chk.setChecked(cfg.get("gamepad", True))
        form.addRow("", self.pad_chk)

        self.rec_edit = QLineEdit(cfg.get("record_dir") or default_record_dir())
        rec_btn = QPushButton("選ぶ…")
        rec_btn.clicked.connect(lambda: (lambda d: d and self.rec_edit.setText(d))(
            QFileDialog.getExistingDirectory(self, "録画の保存先を選ぶ", self.rec_edit.text())))
        rrow = QHBoxLayout()
        rrow.addWidget(self.rec_edit)
        rrow.addWidget(rec_btn)
        form.addRow("録画の保存先：", rrow)
        self.crf_box = QComboBox()
        for label, crf in (("高画質（ファイル大きめ）", 16), ("標準", 20), ("小さめ（画質ふつう）", 26)):
            self.crf_box.addItem(label, crf)
        self.crf_box.setCurrentIndex(max(0, self.crf_box.findData(cfg.get("record_crf", 20))))
        form.addRow("録画の画質：", self.crf_box)
        self.r43_chk = QCheckBox("録画を 4:3（縦画面は 3:4）に整えて大きくする")
        self.r43_chk.setChecked(cfg.get("record_43", True))
        self.keep_chk = QCheckBox("変換前の AVI ファイルも残す（とても大きいので通常はオフ）")
        self.keep_chk.setChecked(cfg.get("record_keep_avi", False))
        form.addRow("", self.r43_chk)
        form.addRow("", self.keep_chk)

        self.extra_edit = QLineEdit(cfg["extra_args"])
        self.extra_edit.setPlaceholderText("例：-video opengl（分からなければ空のままで OK）")
        form.addRow("追加オプション：", self.extra_edit)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("キャンセル")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)

    def pick_mame(self):
        path, _ = QFileDialog.getOpenFileName(self, "MAME の実行ファイルを選ぶ", "/usr/games")
        if path:
            self.mame_edit.setText(path)

    def dir_editor(self, dirs: list[str], title: str):
        lw = QListWidget()
        lw.addItems(dirs)
        lw.setMaximumHeight(90)
        add_btn = QPushButton("フォルダを追加…")
        del_btn = QPushButton("選んだものを外す")

        def add():
            d = QFileDialog.getExistingDirectory(self, title, str(Path.home()))
            if d:
                lw.addItem(d)
        add_btn.clicked.connect(add)
        del_btn.clicked.connect(lambda: [lw.takeItem(lw.row(i)) for i in lw.selectedItems()])
        btns = QHBoxLayout()
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addStretch()
        box = QVBoxLayout()
        box.addWidget(lw)
        box.addLayout(btns)
        return lw, box

    def apply(self):
        self.cfg["mame_path"] = self.mame_edit.text().strip() or "mame"
        self.cfg["rom_dirs"] = [self.dir_list.item(i).text() for i in range(self.dir_list.count())]
        self.cfg["snap_dirs"] = [self.snap_list.item(i).text() for i in range(self.snap_list.count())]
        self.cfg["window_mode"] = self.window_chk.isChecked()
        self.cfg["mame_japanese"] = self.jp_chk.isChecked()
        self.cfg["skip_gameinfo"] = self.skip_chk.isChecked()
        self.cfg["gamepad"] = self.pad_chk.isChecked()
        rd = self.rec_edit.text().strip()
        self.cfg["record_dir"] = "" if rd == default_record_dir() else rd
        self.cfg["record_crf"] = self.crf_box.currentData()
        self.cfg["record_43"] = self.r43_chk.isChecked()
        self.cfg["record_keep_avi"] = self.keep_chk.isChecked()
        self.cfg["extra_args"] = self.extra_edit.text().strip()


# ─────────────────────────────────────────────
# MAME のエラーを日本語に
# ─────────────────────────────────────────────

ERROR_HINTS = [
    (r"required files are missing|NOT FOUND", "ROM ファイルが足りません。\nROM フォルダに、このゲームの zip が入っているか確認してください。\n（親セットの zip が必要なゲームもあります）"),
    (r"Unknown system|Unknown option", "MAME がこのゲーム名（または指定したオプション）を知りません。\nMAME のバージョンが古い可能性があります。"),
    (r"Unable to (open|create) .*(window|display)|SDL.*video", "画面を開けませんでした。\n設定の「追加オプション」に -video opengl や -video soft を入れて試してください。"),
    (r"CHD|hard disk", "このゲームには CHD（ディスクイメージ）ファイルも必要です。\nROM フォルダの中に、ゲーム名のフォルダを作って CHD を入れてください。"),
]


def explain_error(text: str) -> str:
    for pat, msg in ERROR_HINTS:
        if re.search(pat, text, re.IGNORECASE):
            return msg
    return "MAME がエラーで終了しました。"


class SnapView(QLabel):
    """枠の大きさに合わせて画像を縮小表示するラベル。"""
    def __init__(self):
        super().__init__()
        self.pix: QPixmap | None = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(240, 180)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setStyleSheet("background: #111; color: #888; border-radius: 4px;")

    def set_image(self, pix: QPixmap | None, message: str = ""):
        self.pix = pix if pix and not pix.isNull() else None
        if self.pix:
            self.setText("")
            self.rescale()
        else:
            self.setPixmap(QPixmap())
            self.setText(message)

    def rescale(self):
        if self.pix:
            self.setPixmap(self.pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.rescale()


# ─────────────────────────────────────────────
# メイン画面
# ─────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.resize(1200, 720)
        self.info_loader = None
        self.found: dict[str, Path] = {}
        self.procs: list[QProcess] = []

        self.model = GameModel(self.cfg)
        self.proxy = GameFilter(self.model)
        self.proxy.setSourceModel(self.model)

        # 上の段：検索と絞り込み
        self.search = QLineEdit()
        self.search.setPlaceholderText("検索（日本語名・英語名・セット名。スペース区切りで複数語）")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.proxy.set_query)
        self.mode_box = QComboBox()
        self.mode_box.addItems(["持っているゲーム", "お気に入り", "最近遊んだ", "MAME 対応のすべて"])
        self.mode_box.currentIndexChanged.connect(self.on_mode)
        self.play_btn = QPushButton("▶ 遊ぶ")
        self.play_btn.setDefault(True)
        self.play_btn.clicked.connect(self.launch_selected)
        top = QHBoxLayout()
        top.addWidget(QLabel("表示："))
        top.addWidget(self.mode_box)
        top.addWidget(self.search, 1)
        self.rec_chk = QCheckBox("● 録画する")
        self.rec_chk.setToolTip("オンにすると、遊んだ内容を録画して MP4 で保存します（Ctrl+R）")
        self.rec_chk.setChecked(self.cfg.get("record", False))
        self.rec_chk.toggled.connect(self.on_rec_toggled)
        self.on_rec_toggled(self.rec_chk.isChecked())
        top.addWidget(self.rec_chk)
        top.addWidget(self.play_btn)

        # 2段目：年代・メーカー・クローン
        self.decade_box = QComboBox()
        self.decade_box.addItem("すべての年代", "")
        self.maker_box = QComboBox()
        self.maker_box.addItem("すべてのメーカー", "")
        self.maker_box.setMinimumWidth(220)
        self.maker_box.setMaxVisibleItems(25)
        self.clone_chk = QCheckBox("別バージョン（クローン）を隠す")
        self.clone_chk.setChecked(self.cfg["hide_clones"])
        self.clone_chk.setToolTip("同じゲームの地域違い・改訂版などをまとめて隠します")
        for w in (self.decade_box, self.maker_box):
            w.currentIndexChanged.connect(self.on_extra_filter)
        self.clone_chk.toggled.connect(self.on_extra_filter)
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: gray;")
        second = QHBoxLayout()
        second.addWidget(QLabel("絞り込み："))
        second.addWidget(self.decade_box)
        second.addWidget(self.maker_box)
        second.addWidget(self.clone_chk)
        second.addStretch()
        second.addWidget(self.info_label)

        # 一覧表
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setStretchLastSection(False)
        for col, w in ((0, 30), (2, 50), (3, 130), (4, 85), (5, 45), (6, 95)):
            self.table.setColumnWidth(col, w)
        self.table.doubleClicked.connect(lambda _: self.launch_selected())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)

        self.table.selectionModel().currentRowChanged.connect(lambda *_: self.update_detail())

        # 右側：ゲーム画面と詳しい情報
        self.snap_label = SnapView()
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.ver_box = QComboBox()
        self.ver_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.ver_box.setMinimumContentsLength(16)
        self.ver_row = QWidget()
        vr = QHBoxLayout(self.ver_row)
        vr.setContentsMargins(0, 0, 0, 0)
        vr.addWidget(QLabel("バージョン："))
        vr.addWidget(self.ver_box, 1)
        play2 = QPushButton("▶ このバージョンで遊ぶ")
        play2.setMinimumHeight(36)
        play2.clicked.connect(lambda: self.launch(self.ver_box.currentData() or self.selected_name()))
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.snap_label, 3)
        sl.addWidget(self.detail)
        sl.addWidget(self.ver_row)
        sl.addWidget(play2)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(side)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([800, 360])
        self.restore_layout()

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(top)
        lay.addLayout(second)
        lay.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

        self.build_menus()
        self.statusBar().showMessage("ゲーム一覧を読み込んでいます…")

        self.rec_label = QLabel("")
        self.rec_label.setStyleSheet("color: #d32f2f;")
        self.statusBar().addPermanentWidget(self.rec_label)
        self.recorder = Recorder(self.cfg, self)
        self.recorder.progress.connect(self.rec_label.setText)
        self.recorder.done.connect(self.on_rec_done)
        self.recorder.failed.connect(self.on_rec_failed)
        self.pad_label = QLabel("")
        self.statusBar().addPermanentWidget(self.pad_label)
        self.pad = None
        self.pad_grace = False     # ゲーム終了直後は、窓に焦点が戻らなくてもパッドを受け付ける
        QApplication.instance().applicationStateChanged.connect(
            lambda st: setattr(self, "pad_grace", False) if st == Qt.ApplicationActive else None)
        if self.cfg.get("gamepad", True):
            self.pad = Gamepad(self)
            if self.pad.ok:
                self.pad.status.connect(self.pad_label.setText)
                self.pad.action.connect(self.on_pad)
                self.pad.rescan()
            else:
                self.pad_label.setText("🎮 pygame が無いのでゲームパッドは使えません")

        self.mode_box.setCurrentIndex(self.cfg.get("filter_mode", 0))
        self.proxy.set_mode(self.mode_box.currentIndex())
        self.reload()

    def restore_layout(self):
        """前回の列幅・左右の幅を戻す。"""
        try:
            if self.cfg.get("header_state"):
                self.table.horizontalHeader().restoreState(
                    QByteArray.fromBase64(self.cfg["header_state"].encode()))
                self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            if self.cfg.get("splitter_state"):
                self.splitter.restoreState(QByteArray.fromBase64(self.cfg["splitter_state"].encode()))
        except Exception:  # noqa: BLE001
            pass

    # ── メニュー ──
    def build_menus(self):
        mb = self.menuBar()
        m_file = mb.addMenu("ファイル(&F)")
        self.add_action(m_file, "設定…", self.open_settings, "Ctrl+,")
        self.add_action(m_file, "録画フォルダを開く", self.open_record_dir)
        self.add_action(m_file, "一覧を読み込み直す", lambda: self.reload(force=True), "F5")
        m_file.addSeparator()
        self.add_action(m_file, "日本語名リストを読み込む…", self.import_names)
        self.add_action(m_file, "日本語名リストを書き出す…", self.export_names)
        m_file.addSeparator()
        self.add_action(m_file, "終了", self.close, "Ctrl+Q")

        m_game = mb.addMenu("ゲーム(&G)")
        self.add_action(m_game, "遊ぶ", self.launch_selected, "Return")
        self.add_action(m_game, "録画する／しない の切り替え", self.rec_chk.toggle, "Ctrl+R")
        self.add_action(m_game, "お気に入りに追加／外す", self.toggle_fav, "Ctrl+D")
        self.add_action(m_game, "日本語名を付ける…", self.rename_selected, "F2")
        self.add_action(m_game, "全画面表示の切り替え", self.toggle_fullscreen, "F11")
        self.add_action(m_game, "検索欄へ移動", lambda: (self.search.setFocus(), self.search.selectAll()), "Ctrl+F")

        m_help = mb.addMenu("ヘルプ(&H)")
        self.add_action(m_help, "使い方", self.show_help, "F1")
        self.add_action(m_help, f"{APP_NAME} について", self.show_about)

    def add_action(self, menu, text, slot, shortcut=None):
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        menu.addAction(act)
        self.addAction(act)
        return act

    # ── 読み込み ──
    def reload(self, force=False):
        if force:
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            self.model.set_info({})
        self.statusBar().showMessage("ゲーム一覧を読み込んでいます…（初回は少し時間がかかります）")
        self.loader = ListLoader(self.cfg["mame_path"])
        self.loader.finished_ok.connect(self.on_loaded)
        self.loader.failed.connect(self.on_load_failed)
        self.loader.start()

    def compute_owned(self) -> set[str]:
        if self.model.info:
            return expand_owned(self.found, self.model.info)
        return set(self.found)

    def update_status(self):
        games, owned = self.model.games, self.model.owned
        have = len(owned & games.keys())
        named = sum(1 for n in owned & games.keys() if self.model.jp(n))
        merged = len(owned - set(self.found))
        msg = (f"MAME {getattr(self, 'mame_version', '')}　対応ゲーム {len(games):,} 本　／　"
               f"持っているゲーム {have:,} 本（うち日本語名あり {named:,} 本"
               + (f"、親の zip に同梱 {merged:,} 本" if merged else "") + "）")
        if not self.cfg["rom_dirs"]:
            msg += "　※ ROM フォルダが未設定です（ファイル → 設定）"
        self.statusBar().showMessage(msg)

    def on_loaded(self, games: dict, ver: str):
        self.found = scan_owned(self.cfg["rom_dirs"])
        self.mame_version = ver
        self.model.set_data(games, self.compute_owned())
        have = len(self.model.owned & games.keys())
        self.update_status()
        self.fill_filter_boxes()
        if not self.model.info and self.info_loader is None:
            self.info_label.setText("年・メーカーの情報を読み込み中…（初回のみ・数十秒）")
            self.info_loader = InfoLoader(self.cfg["mame_path"], ver)
            self.info_loader.progress.connect(
                lambda n: self.info_label.setText(f"年・メーカーの情報を読み込み中… {n:,} 件"))
            self.info_loader.finished_ok.connect(self.on_info_loaded)
            self.info_loader.finished.connect(self.on_info_thread_done)
            self.info_loader.start()
        if have == 0 and self.mode_box.currentIndex() == 0:
            QMessageBox.information(
                self, APP_NAME,
                "ROM フォルダにゲームが見つかりませんでした。\n\n"
                "「ファイル → 設定」で、ROM の zip ファイルが入っているフォルダを追加してください。\n"
                "（表示を「MAME 対応のすべて」にすると、対応ゲームの一覧は見られます）")

    def on_info_loaded(self, info: dict):
        self.model.set_info(info)
        self.model.set_data(self.model.games, self.compute_owned())   # マージ形式の判定
        self.update_status()
        self.info_label.setText("")
        self.fill_filter_boxes()
        self.update_detail()

    def on_info_thread_done(self):
        if not self.model.info:
            self.info_label.setText("年・メーカーの情報は読み込めませんでした")
        self.info_loader = None

    def fill_filter_boxes(self):
        """年代・メーカーの選択肢を、持っているゲーム（無ければ全部）から作る。"""
        info = self.model.info
        names = [n for n in self.model.owned if n in info] or list(info)
        decades: dict[str, int] = {}
        makers: dict[str, int] = {}
        for n in names:
            inf = info[n]
            if inf["bios"] or inf["mech"]:
                continue
            d = decade_of(inf["year"])
            if d:
                decades[d] = decades.get(d, 0) + 1
            for mk in maker_key(inf["maker"]):
                makers[mk] = makers.get(mk, 0) + 1
        cur_d, cur_m = self.decade_box.currentData(), self.maker_box.currentData()
        for box, items, label in ((self.decade_box, sorted(decades.items()), "すべての年代"),
                                  (self.maker_box, sorted(makers.items(), key=lambda kv: (-kv[1], kv[0])), "すべてのメーカー")):
            box.blockSignals(True)
            box.clear()
            box.addItem(label, "")
            for key, cnt in items:
                box.addItem(f"{key}（{cnt}）", key)
            box.blockSignals(False)
        for box, cur in ((self.decade_box, cur_d), (self.maker_box, cur_m)):
            i = box.findData(cur)
            box.setCurrentIndex(max(i, 0))
        self.on_extra_filter()

    def on_extra_filter(self, *_):
        self.cfg["hide_clones"] = self.clone_chk.isChecked()
        self.proxy.set_extra(self.decade_box.currentData() or "",
                             self.maker_box.currentData() or "",
                             self.clone_chk.isChecked())

    # ── 右側の詳細表示 ──
    def update_detail(self):
        name = self.selected_name()
        self.ver_box.clear()
        if not name:
            self.snap_label.set_image(None, "（ゲームを選んでください）")
            self.detail.setText("")
            self.ver_row.hide()
            return
        # バージョン選択：持っているもの → 日本版 の順
        fam = self.model.family(name)
        fam.sort(key=lambda n: (n not in self.model.owned, not self.model.is_japan(n), n))
        for n in fam:
            label = self.model.display_name(n)
            if n not in self.model.owned:
                label += "　※ROMなし"
            self.ver_box.addItem(f"{label}　[{n}]", n)
        self.ver_box.setCurrentIndex(max(self.ver_box.findData(name), 0))
        self.ver_row.setVisible(len(fam) > 1)
        inf = self.model.info.get(name, {})
        parent = inf.get("cloneof", "")
        img = find_snapshot(name, self.cfg["snap_dirs"]) or (
            find_snapshot(parent, self.cfg["snap_dirs"]) if parent else None)
        self.snap_label.set_image(QPixmap(str(img)) if img else None,
                                  "（画像なし）\nゲーム中に F12 キーで\n画面を撮るとここに出ます")

        def esc(t):
            return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rows = [("英語名", self.model.games.get(name, "")),
                ("年", inf.get("year", "")),
                ("メーカー", inf.get("maker", "")),
                ("人数", f"{inf['players']} 人まで" if inf.get("players") else ""),
                ("動作状況", STATUS_JA.get(inf.get("status", ""), inf.get("status", ""))),
                ("セット名", name)]
        if parent:
            rows.append(("元のゲーム", f"{self.model.display_name(parent)}（{parent}）"))
        jp_vers = [n for n in self.model.family(name) if self.model.is_japan(n) and n != name]
        if not self.model.is_japan(name) and jp_vers:
            have = [n for n in jp_vers if n in self.model.owned]
            rows.append(("日本版", ("あり：下の「バージョン」で選べます" if have
                                    else f"あり（{jp_vers[0]}）／ROMなし")))
        if name not in self.model.owned:
            rows.append(("ROM", "<span style='color:#c33'>見つかりません</span>"))
        html = f"<h3 style='margin:4px 0'>{esc(self.model.display_name(name))}</h3><table>"
        for k, v in rows:
            if v:
                vv = v if k == "ROM" else esc(v)
                html += f"<tr><td style='color:gray; padding-right:10px'>{k}</td><td>{vv}</td></tr>"
        self.detail.setText(html + "</table>")

    def on_load_failed(self, msg: str):
        self.statusBar().showMessage("ゲーム一覧を読み込めませんでした")
        QMessageBox.warning(self, APP_NAME, msg)

    def on_mode(self, i: int):
        self.proxy.set_mode(i)
        self.cfg["filter_mode"] = i
        save_config(self.cfg)

    # ── 選択中のゲーム ──
    def selected_name(self) -> str | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        return self.model.rows[self.proxy.mapToSource(idx).row()]

    def context_menu(self, pos):
        if not self.table.indexAt(pos).isValid():
            return
        name = self.selected_name()
        menu = QMenu(self)
        menu.addAction("▶ 遊ぶ", self.launch_selected)
        fav = name in self.cfg["favorites"]
        menu.addAction("お気に入りから外す" if fav else "★ お気に入りに追加", self.toggle_fav)
        menu.addAction("日本語名を付ける…", self.rename_selected)
        menu.addSeparator()
        menu.addAction("セット名をコピー", lambda: QApplication.clipboard().setText(name))
        if self.cfg["rom_dirs"]:
            menu.addAction("ROM フォルダを開く",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.cfg["rom_dirs"][0])))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def toggle_fav(self):
        name = self.selected_name()
        if not name:
            return
        favs = self.cfg["favorites"]
        if name in favs:
            favs.remove(name)
        else:
            favs.append(name)
        save_config(self.cfg)
        self.model.refresh_row(name)

    def rename_selected(self):
        name = self.selected_name()
        if not name:
            return
        builtin = self.model.builtin.get(name, "")
        back = f"同梱の名前「{builtin}」" if builtin else "英語名"
        dlg = QDialog(self)
        dlg.setWindowTitle("日本語名を付ける")
        dlg.setMinimumWidth(460)
        name_edit = QLineEdit(self.model.jp(name))
        cur_yomi = self.model.yomi(name)
        yomi_edit = QLineEdit(cur_yomi if cur_yomi != self.model.jp(name) else "")
        yomi_edit.setPlaceholderText("漢字を含むときだけ（ひらがなで）")
        form = QFormLayout()
        form.addRow("英語名：", QLabel(self.model.games.get(name, name)))
        form.addRow("日本語名：", name_edit)
        form.addRow("読み：", yomi_edit)
        note = QLabel(f"日本語名を空にすると{back}に戻ります。読みは、あいうえお順の並べ替えに使います。")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("キャンセル")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay = QVBoxLayout(dlg)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(bb)
        if not dlg.exec():
            return
        text = name_edit.text().strip()
        yomi = yomi_edit.text().strip()
        same_as_builtin = (text == builtin and
                           (not yomi or yomi == self.model.builtin_yomi.get(name, "")))
        if text and not same_as_builtin:
            self.cfg["jp_names"][name] = text
            if yomi:
                self.cfg["jp_yomi"][name] = yomi
            else:
                self.cfg["jp_yomi"].pop(name, None)
        else:
            self.cfg["jp_names"].pop(name, None)
            self.cfg["jp_yomi"].pop(name, None)
        save_config(self.cfg)
        self.model.refresh_row(name)
        self.proxy.invalidate()       # 並び順を更新
        self.update_detail()

    # ── 起動 ──
    def build_args(self, name: str) -> list[str]:
        args = [name]
        if self.cfg["rom_dirs"]:
            args += ["-rompath", ";".join(self.cfg["rom_dirs"])]
        args.append("-window" if self.cfg["window_mode"] else "-nowindow")
        if self.cfg["skip_gameinfo"]:
            args.append("-skip_gameinfo")
        if self.cfg["snap_dirs"]:
            Path(self.cfg["snap_dirs"][0]).mkdir(parents=True, exist_ok=True)
            args += ["-snapshot_directory", self.cfg["snap_dirs"][0]]
        if self.cfg["mame_japanese"]:
            args += ["-language", "Japanese"]
        if self.cfg["extra_args"]:
            args += self.cfg["extra_args"].split()
        return args

    def launch_selected(self):
        self.launch(self.selected_name())

    def launch(self, name: str | None):
        if not name:
            QMessageBox.information(self, APP_NAME, "一覧から遊びたいゲームを選んでください。")
            return
        if name not in self.model.owned:
            r = QMessageBox.question(
                self, APP_NAME,
                f"「{self.model.display_name(name)}」の ROM が ROM フォルダに見つかりません。\nそれでも起動してみますか？")
            if r != QMessageBox.Yes:
                return

        args = self.build_args(name)
        rec_file = None
        if self.cfg.get("record"):
            rec_file = f"arcade-tana-rec-{name}-{time.strftime('%Y%m%d-%H%M%S')}.avi"
            args += ["-aviwrite", rec_file]
            snap = Path(self.cfg["snap_dirs"][0]) if self.cfg["snap_dirs"] else Path.home()
            try:
                if shutil.disk_usage(snap if snap.exists() else Path.home()).free < 3 * 1024**3:
                    self.statusBar().showMessage("⚠ ディスクの空きが少なめです（録画は 1 分で数百 MB 使います）", 8000)
            except OSError:
                pass

        proc = QProcess(self)
        proc.setProgram(self.cfg["mame_path"])
        proc.setArguments(args)
        proc.setProperty("rec_file", rec_file or "")
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.finished.connect(lambda code, _st, p=proc, n=name: self.on_finished(p, n, code))
        proc.errorOccurred.connect(lambda err, p=proc: self.on_proc_error(p, err))
        self.procs.append(proc)
        proc.start()
        if self.pad:
            self.pad.paused = True

        h = self.cfg["history"].setdefault(name, {"count": 0, "last": 0})
        h["count"] += 1
        h["last"] = int(time.time())
        save_config(self.cfg)
        self.model.refresh_row(name)
        self.statusBar().showMessage(
            f"{'● 録画しながら' if rec_file else ''}起動中：{self.model.display_name(name)}", 5000)

    def on_finished(self, proc: QProcess, name: str, code: int):
        out = bytes(proc.readAll()).decode("utf-8", errors="replace")
        if proc in self.procs:
            self.procs.remove(proc)
        proc.deleteLater()
        if self.pad and not self.procs:
            self.pad.paused = False
            self.pad_grace = True
            self.raise_()
            self.activateWindow()
        self.update_detail()          # F12 で撮った画像をすぐ反映
        rec_file = proc.property("rec_file")
        if rec_file:
            avi = self.find_avi(rec_file)
            if avi:
                title = safe_filename(self.model.display_name(name))
                mp4 = self.record_dir() / f"{title}_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
                self.recorder.add(avi, mp4)
            elif code == 0:
                self.statusBar().showMessage("録画ファイルが見つかりませんでした", 8000)
        if code != 0:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle(APP_NAME)
            box.setText(f"「{self.model.display_name(name)}」を起動できませんでした。\n\n{explain_error(out)}")
            box.setDetailedText(out.strip() or "（MAME からのメッセージはありません）")
            box.exec()

    def on_proc_error(self, proc: QProcess, err):
        if err == QProcess.FailedToStart:
            if proc in self.procs:
                self.procs.remove(proc)
            if self.pad and not self.procs:
                self.pad.paused = False
            QMessageBox.warning(self, APP_NAME,
                                f"MAME を起動できませんでした：{self.cfg['mame_path']}\n"
                                "「ファイル → 設定」で MAME の場所を確認してください。")

    # ── 録画 ──
    def on_rec_toggled(self, on: bool):
        self.cfg["record"] = on
        self.rec_chk.setStyleSheet("color: #d32f2f; font-weight: bold;" if on else "")
        save_config(self.cfg)

    def record_dir(self) -> Path:
        return Path(self.cfg.get("record_dir") or default_record_dir())

    def open_record_dir(self):
        d = self.record_dir()
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def find_avi(self, fname: str) -> Path | None:
        cands = [Path(d) / fname for d in self.cfg["snap_dirs"][:1]]
        cands += [Path.home() / ".mame" / "snap" / fname, Path.cwd() / fname,
                  Path.cwd() / "snap" / fname]
        for c in cands:
            if c.is_file() and c.stat().st_size > 0:
                return c
        return None

    def on_rec_done(self, path: str):
        self.rec_label.setText("")
        self.statusBar().showMessage(f"録画を保存しました：{Path(path).name}（ファイル → 録画フォルダを開く）", 15000)

    def on_rec_failed(self, msg: str):
        self.rec_label.setText("")
        QMessageBox.warning(self, APP_NAME, msg)

    # ── ゲームパッド ──
    def on_pad(self, act: str):
        # ほかのアプリを使っているとき、ダイアログが出ているときは反応しない
        if QApplication.activeModalWidget() or self.isMinimized():
            return
        if not (self.isActiveWindow() or self.pad_grace):
            return
        if act in ("up", "down", "left", "right", "lb", "rb"):
            step = {"up": -1, "down": 1, "left": -10, "right": 10, "lb": -10, "rb": 10}[act]
            n = self.proxy.rowCount()
            if n == 0:
                return
            cur = self.table.currentIndex().row()
            row = 0 if cur < 0 else max(0, min(n - 1, cur + step))
            self.table.selectRow(row)
            self.table.scrollTo(self.proxy.index(row, 1))
        elif act in ("a", "start"):
            name = self.selected_name()
            if name and name not in self.model.owned:
                # パッドでは確認ダイアログに答えられないので、知らせるだけにする
                self.statusBar().showMessage("この ROM は見つからないので起動できません", 3000)
                return
            self.launch_selected()
        elif act == "y":
            self.toggle_fav()
        elif act == "x":
            self.mode_box.setCurrentIndex((self.mode_box.currentIndex() + 1) % self.mode_box.count())
            self.statusBar().showMessage(f"表示：{self.mode_box.currentText()}", 2000)
        elif act == "back":
            self.clone_chk.toggle()
            self.statusBar().showMessage(
                "別バージョンを隠す：" + ("オン" if self.clone_chk.isChecked() else "オフ"), 2000)
        elif act == "b":
            if self.search.text():
                self.search.clear()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # ── 日本語名リスト ──
    def import_names(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "日本語名リストを読み込む", str(Path.home()),
            "テキスト (*.tsv *.txt *.csv);;すべて (*)")
        if not path:
            return
        names, yomi = read_name_list(path)
        self.cfg["jp_names"].update(names)
        self.cfg["jp_yomi"].update(yomi)
        added = len(names)
        save_config(self.cfg)
        self.model.layoutChanged.emit()
        QMessageBox.information(self, APP_NAME, f"{added:,} 件の日本語名を読み込みました。")

    def export_names(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "日本語名リストを書き出す", str(Path.home() / "日本語名リスト.tsv"),
            "TSV (*.tsv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write("# セット名<TAB>日本語名<TAB>読み\n")
            for k in sorted(self.cfg["jp_names"]):
                y = self.cfg["jp_yomi"].get(k, "")
                f.write(f"{k}\t{self.cfg['jp_names'][k]}" + (f"\t{y}" if y else "") + "\n")
        QMessageBox.information(self, APP_NAME, "書き出しました。")

    # ── その他 ──
    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            old = self.cfg["mame_path"]
            dlg.apply()
            save_config(self.cfg)
            if self.cfg["mame_path"] != old:
                self.reload()
            elif self.model.games:
                self.on_loaded(self.model.games, getattr(self, "mame_version", ""))
                self.proxy.invalidateFilter()
                self.update_detail()

    def show_help(self):
        QMessageBox.information(self, "使い方", (
            "■ 遊ぶ\n"
            "　一覧でゲームをダブルクリック、または選んで Enter キー。\n\n"
            "■ 探す\n"
            "　上の検索欄に入力（Ctrl+F）。ひらがな・カタカナ・全角半角の違いは気にしなくて OK。\n\n"
            "■ 日本語名を付ける\n"
            "　有名なゲーム約280本には、最初から日本語名が付いています。\n"
            "　別バージョンには、元のゲームの日本語名が自動で使われます。\n"
            "　自分で付けたいときはゲームを選んで F2（同梱の名前より優先されます）。\n\n"
            "■ 絞り込み\n"
            "　年代・メーカーで絞り込めます。「別バージョンを隠す」で地域違いなどをまとめられます。\n\n"
            "■ 日本版・海外版\n"
            "　名前の後ろの（日本）（北米）（海外）などが地域です。同じゲームの別の版は、\n"
            "　右下の「バージョン」で選んで遊べます。「別バージョンを隠す」にすると、\n"
            "　日本版を持っていれば日本版が残ります。\n\n"
            "■ ゲーム画面の画像\n"
            "　ゲーム中に F12 キーで撮った画面が、一覧の右に表示されます。\n"
            "　画像をまとめて持っている場合は「設定 → 画像フォルダ」に追加してください\n"
            "　（セット名.png、またはセット名のフォルダの中の画像を表示します）。\n\n"
            "■ お気に入り\n"
            "　ゲームを選んで Ctrl+D。表示を「お気に入り」にすると、それだけ出ます。\n\n"
            "■ 録画\n"
            "　上の「● 録画する」をオン（Ctrl+R）にして遊ぶと、遊んだ内容を録画します。\n"
            "　ゲームを終えると自動で MP4 に変換し、「ビデオ/アーケード棚」に保存します\n"
            "　（ffmpeg が必要。保存先・画質は設定で変えられます）。\n\n"
            "■ ゲームパッドで操作（pygame が必要）\n"
            "　十字キー・左スティック：上下で選ぶ　左右・LB/RB：10 本ずつ移動\n"
            "　A・START：遊ぶ　Y：お気に入り　X：表示の切り替え\n"
            "　BACK（SELECT）：別バージョンを隠す　B：検索をクリア\n"
            "　F11 キーでこの画面を全画面にすると、ソファーからでも見やすくなります。\n\n"
            "■ ゲーム中の操作（MAME の基本キー）\n"
            "　5：コイン投入　1：1P スタート　矢印キー：移動\n"
            "　左Ctrl / 左Alt / スペース：ボタン 1・2・3\n"
            "　Tab：MAME のメニュー（キー設定など）\n"
            "　P：一時停止　F12：画面を撮る　Esc：終了してこの画面に戻る\n\n"
            "■ ゲームパッドだけでゲームを終わらせたいとき\n"
            "　ゲーム中に Tab → 入力設定（全般）→ ユーザーインターフェース →\n"
            "　「UIキャンセル」にパッドのボタン（例：BACK + START）を割り当ててください。\n"
            "　（メニューの名前は MAME のバージョンによって少し違うことがあります）"))

    def show_about(self):
        QMessageBox.about(self, f"{APP_NAME} について", (
            f"{APP_NAME} {VERSION}\n\n"
            "MAME を日本語で使いやすくするためのランチャーです。\n"
            "MAME 本体とは別のソフトで、MAME 開発チームとは関係ありません。\n"
            "MAME® は Gregory Ember 氏の登録商標です。\n\n"
            "ライセンス：MIT"))

    def closeEvent(self, e):
        if self.recorder.busy():
            r = QMessageBox.question(
                self, APP_NAME,
                "録画を MP4 に変換している途中です。\n今終了すると、変換前の AVI ファイルのまま録画フォルダに残します。\n\n終了しますか？")
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.recorder.abort_all()
        self.cfg["header_state"] = bytes(self.table.horizontalHeader().saveState().toBase64()).decode()
        self.cfg["splitter_state"] = bytes(self.splitter.saveState().toBase64()).decode()
        save_config(self.cfg)
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    icon = QIcon.fromTheme(APP_ID)
    if icon.isNull():
        icon = QIcon(str(Path(__file__).resolve().parent / f"{APP_ID}.svg"))
    app.setWindowIcon(icon)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
