# -*- coding: utf-8 -*-
"""字幕エディター (tkinter GUI)

字幕ファイル（SRT / VTT / ASS / Whisper 字幕作成ツールの JSON）を、音を聞きながら直して、
保存するか動画に焼き込む。

使い方:
    python editor_app.py [字幕ファイル または 動画・音声]
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "字幕エディター"
APP_VERSION = "1.0"

IS_FROZEN = getattr(sys, "frozen", False)

# exe 化した場合は exe のあるフォルダ、通常実行なら .py のあるフォルダ
APP_DIR = (os.path.dirname(sys.executable) if IS_FROZEN
           else os.path.dirname(os.path.abspath(__file__)))
if not IS_FROZEN and APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


def _fatal(title, message):
    """起動できないレベルのエラーをダイアログで知らせて終了する。"""
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        print(message)
    sys.exit(1)


try:
    import audio
    import binaries
    import burn_in
    import checks
    import media_info
    import subtitle_formats as fmt
    import subtitle_io as sio
except Exception as _e:
    _fatal(
        APP_TITLE,
        "必要なファイルが読み込めませんでした。\n\n"
        + ("・同梱ファイルが壊れている可能性があります。\n"
           "　配布元から入手し直してください。\n\n"
           if IS_FROZEN else
           "・editor_app.py と同じフォルダに audio.py / binaries.py / burn_in.py /\n"
           "　checks.py / media_info.py / subtitle_formats.py / subtitle_io.py が\n"
           "　あるか確認してください。\n\n")
        + "詳細: {}: {}".format(type(_e).__name__, _e),
    )


# ドラッグ＆ドロップ（tkinterdnd2 があれば有効化）
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    ROOT_CLASS = TkinterDnD.Tk
    HAS_DND = True
except Exception:
    ROOT_CLASS = tk.Tk
    DND_FILES = None
    HAS_DND = False


SUBTITLE_TYPES = [
    ("字幕ファイル", "*.srt *.vtt *.ass *.ssa *.json"),
    ("SRT", "*.srt"), ("WebVTT", "*.vtt"), ("ASS", "*.ass *.ssa"),
    ("Whisper 字幕作成ツールの JSON", "*.json"),
    ("すべてのファイル", "*.*"),
]
MEDIA_TYPES = [
    ("動画・音声", " ".join("*" + e for e in sio.MEDIA_EXTS)),
    ("すべてのファイル", "*.*"),
]
SAVE_TYPES = [("SRT", "*.srt"), ("WebVTT", "*.vtt"), ("ASS", "*.ass")]

PREVIEW_WIDTH = 480
DEFAULT_SPAN = 30.0          # 波形の表示幅（秒）
MIN_SPAN = 2.0
FRAME_INTERVAL_MS = 1000     # 再生中にコマを作り直す間隔


def _settings_path():
    """設定ファイルの保存先。書き込めない場所に置かれても動くようにする。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "johukku", "subtitle-editor")
    try:
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "settings.json")
    except OSError:
        return os.path.join(APP_DIR, "settings.json")


SETTINGS_PATH = _settings_path()


def fmt_time(seconds, force_hours=False):
    """一覧・表示用の時刻。1 時間未満なら m:ss.mmm、以上なら h:mm:ss.mmm。"""
    if seconds is None or seconds < 0:
        seconds = 0.0
    total_ms = int(round(float(seconds) * 1000))
    h, rest = divmod(total_ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    if h or force_hours:
        return "{}:{:02d}:{:02d}.{:03d}".format(h, m, s, ms)
    return "{}:{:02d}.{:03d}".format(m, s, ms)


def first_line(text, limit=60):
    line = (text or "").replace("\n", " ⏎ ")
    return line if len(line) <= limit else line[:limit - 1] + "…"


# ------------------------------------------------------------------ 文書

class Document:
    """開いている字幕。cues は常に開始時刻順。編集の前に push_undo() を呼ぶ。"""

    UNDO_LIMIT = 100

    def __init__(self):
        self.cues = []
        self.path = None
        self.kind = "srt"
        self.dirty = False
        self.undo_stack = []
        self.redo_stack = []
        self.backed_up = False

    def load(self, cues, path, kind):
        self.cues = [dict(c) for c in cues]
        self.path = path
        self.kind = kind
        self.dirty = False
        self.undo_stack = []
        self.redo_stack = []
        self.backed_up = False

    def snapshot(self):
        return tuple((c["start"], c["end"], c["text"]) for c in self.cues)

    def restore(self, snap):
        self.cues = [{"start": s, "end": e, "text": t} for s, e, t in snap]

    def push_undo(self):
        self.undo_stack.append(self.snapshot())
        if len(self.undo_stack) > self.UNDO_LIMIT:
            del self.undo_stack[0]
        self.redo_stack = []
        self.dirty = True

    def undo(self):
        if not self.undo_stack:
            return False
        self.redo_stack.append(self.snapshot())
        self.restore(self.undo_stack.pop())
        self.dirty = True
        return True

    def redo(self):
        if not self.redo_stack:
            return False
        self.undo_stack.append(self.snapshot())
        self.restore(self.redo_stack.pop())
        self.dirty = True
        return True

    def sort(self):
        self.cues.sort(key=lambda c: (c["start"], c["end"]))

    def cue_at(self, seconds):
        """その時刻に表示される字幕の番号（無ければ None）。"""
        for i, c in enumerate(self.cues):
            if c["start"] <= seconds < c["end"]:
                return i
        return None

    def title(self):
        name = os.path.basename(self.path) if self.path else "（未保存の字幕）"
        return name + ("（未保存）" if self.dirty else "")


# ------------------------------------------------------------------ 画面

class App:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.settings = self._load_settings()
        self.doc = Document()
        self.check_settings = checks.settings_from(self.settings.get("checks", {}))
        self.results = []

        # 動画・音声
        self.media_path = None
        self.media = None            # media_info.probe() の結果
        self.wav_path = None
        self.peaks = None
        self.player = audio.Player()
        self.media_gen = 0           # 開き直したときに古い連絡を捨てるための番号
        self.media_loading = False
        self.media_cancel = threading.Event()

        # 再生
        self.playhead = 0.0
        self.play_range = None
        self.was_playing = False
        self.last_frame_at = -1.0
        self.frame_timer = 0

        # 波形
        self.view_start = 0.0
        self.view_span = float(self.settings.get("span", DEFAULT_SPAN))
        self.drag = None

        # 編集欄
        self.current = None          # 編集欄に出している字幕の番号
        self.text_session = False    # 本文の編集が始まっていて、まだ確定していない
        self.text_timer = None
        self.loading_panel = False

        # 裏の仕事
        self.frame_lock = threading.Lock()
        self.frame_request = None
        self.frame_count = 0
        self.burn_cancel = threading.Event()
        self.burning = False
        self.busy_dialog = None

        self.var_status = tk.StringVar(value="")
        self.var_files = tk.StringVar(value="字幕: なし　　動画: なし")
        self.var_warn = tk.StringVar(value="")
        self.var_parts = tk.StringVar(value="")
        self.var_pos = tk.StringVar(value="0:00.000 / 0:00.000")
        self.var_start = tk.StringVar()
        self.var_end = tk.StringVar()
        self.var_info = tk.StringVar(value="")
        self.var_cue_warn = tk.StringVar(value="")
        self.var_loop = tk.BooleanVar(value=False)
        self.var_overlay = tk.BooleanVar(value=bool(self.settings.get("overlay", True)))
        self.var_preset = tk.StringVar(value=self.settings.get("preset", burn_in.DEFAULT_PRESET))
        if self.var_preset.get() not in burn_in.PRESET_LABELS:
            self.var_preset.set(burn_in.DEFAULT_PRESET)

        self.root.title("{} v{}".format(APP_TITLE, APP_VERSION))
        self.root.geometry("{}x{}".format(*self._initial_size()))
        self.root.minsize(1000, 660)

        self._build_menu()
        self._build_ui()
        self._bind_keys()
        self._update_title()
        self._refresh_parts()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self._pump)
        self.root.after(200, self._first_run)

    def _initial_size(self):
        try:
            width = int(self.settings.get("width", 1180))
            height = int(self.settings.get("height", 760))
        except (TypeError, ValueError):
            width, height = 1180, 760
        width = min(width, self.root.winfo_screenwidth() - 40)
        height = min(height, self.root.winfo_screenheight() - 80)
        return max(width, 1000), max(height, 660)

    # ------------------------------------------------------------ メニュー

    def _build_menu(self):
        bar = tk.Menu(self.root)

        m_file = tk.Menu(bar, tearoff=False)
        m_file.add_command(label="字幕を開く...", accelerator="Ctrl+O", command=self.on_open_subtitle)
        m_file.add_command(label="動画・音声を開く...", accelerator="Ctrl+Shift+O",
                           command=self.on_open_media)
        self.m_recent = tk.Menu(m_file, tearoff=False)
        m_file.add_cascade(label="最近開いたもの", menu=self.m_recent)
        m_file.add_separator()
        m_file.add_command(label="保存", accelerator="Ctrl+S", command=self.on_save)
        m_file.add_command(label="別名で保存...", accelerator="Ctrl+Shift+S", command=self.on_save_as)
        m_file.add_separator()
        m_file.add_command(label="動画に焼き込む...", accelerator="Ctrl+B", command=self.on_burn)
        m_file.add_separator()
        m_file.add_command(label="終了", command=self.on_close)
        bar.add_cascade(label="ファイル", menu=m_file)

        m_edit = tk.Menu(bar, tearoff=False)
        m_edit.add_command(label="元に戻す", accelerator="Ctrl+Z", command=self.on_undo)
        m_edit.add_command(label="やり直す", accelerator="Ctrl+Y", command=self.on_redo)
        m_edit.add_separator()
        m_edit.add_command(label="検索・置換...", accelerator="Ctrl+H", command=self.on_find)
        m_edit.add_command(label="次の警告へ", accelerator="F3", command=self.on_next_warning)
        m_edit.add_separator()
        m_edit.add_command(label="設定...", command=self.on_settings)
        bar.add_cascade(label="編集", menu=m_edit)

        m_cue = tk.Menu(bar, tearoff=False)
        m_cue.add_command(label="ここで分割", accelerator="Ctrl+D", command=self.on_split)
        m_cue.add_command(label="次と結合", accelerator="Ctrl+J", command=self.on_merge)
        m_cue.add_command(label="後ろに挿入", accelerator="Ctrl+Insert", command=self.on_insert)
        m_cue.add_command(label="削除", accelerator="Ctrl+Delete", command=self.on_delete)
        m_cue.add_separator()
        m_cue.add_command(label="開始を今の位置に", accelerator="[", command=lambda: self.on_set_time("start"))
        m_cue.add_command(label="終了を今の位置に", accelerator="]", command=lambda: self.on_set_time("end"))
        m_cue.add_separator()
        m_cue.add_command(label="全体をずらす...", command=self.on_shift)
        m_cue.add_command(label="自動で折り返す...", command=self.on_wrap)
        bar.add_cascade(label="字幕", menu=m_cue)

        m_play = tk.Menu(bar, tearoff=False)
        m_play.add_command(label="再生 / 停止", accelerator="Space", command=self.on_play_toggle)
        m_play.add_command(label="この字幕を再生", accelerator="Ctrl+Enter", command=self.on_play_cue)
        m_play.add_checkbutton(label="繰り返す", variable=self.var_loop)
        m_play.add_separator()
        m_play.add_command(label="3 秒戻る", accelerator="Alt+←", command=lambda: self.on_skip(-3))
        m_play.add_command(label="3 秒進む", accelerator="Alt+→", command=lambda: self.on_skip(3))
        m_play.add_command(label="前の字幕", accelerator="Ctrl+↑", command=lambda: self.on_step(-1))
        m_play.add_command(label="次の字幕", accelerator="Ctrl+↓", command=lambda: self.on_step(1))
        bar.add_cascade(label="再生", menu=m_play)

        m_help = tk.Menu(bar, tearoff=False)
        m_help.add_command(label="使い方（サイト）", command=lambda: self._open_url(
            "https://johukku.pages.dev/editor/usage/"))
        m_help.add_command(label="部品を入れ直す（FFmpeg）", command=self.on_reinstall)
        m_help.add_separator()
        m_help.add_command(label="バージョン情報", command=self.on_about)
        bar.add_cascade(label="ヘルプ", menu=m_help)

        self.root.config(menu=bar)
        self._refresh_recent()

    def _refresh_recent(self):
        self.m_recent.delete(0, "end")
        recent = [p for p in self.settings.get("recent", []) if isinstance(p, str)]
        if not recent:
            self.m_recent.add_command(label="（なし）", state="disabled")
            return
        for path in recent[:8]:
            self.m_recent.add_command(label=path, command=lambda p=path: self.open_any(p))

    def _remember(self, path):
        recent = [p for p in self.settings.get("recent", []) if isinstance(p, str) and p != path]
        recent.insert(0, path)
        self.settings["recent"] = recent[:8]
        self.settings["last_dir"] = os.path.dirname(path)
        self._refresh_recent()

    # ---------------------------------------------------------------- 画面

    def _build_ui(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        # --- 状態バー（いちばん最初に置く。窓が低くても消えないように）---
        status = ttk.Frame(outer)
        status.pack(side="bottom", fill="x", padx=8, pady=(2, 4))
        ttk.Label(status, textvariable=self.var_files).pack(side="left")
        ttk.Label(status, textvariable=self.var_warn, foreground="#8a5a00").pack(side="left", padx=(16, 0))
        ttk.Label(status, textvariable=self.var_parts, foreground="#666").pack(side="right")
        self.progress = ttk.Progressbar(status, mode="determinate", maximum=100, length=160)
        self.progress.pack(side="right", padx=(0, 10))
        self.progress.pack_forget()
        ttk.Label(status, textvariable=self.var_status, foreground="#1a5c1a").pack(
            side="right", padx=(0, 10))

        # --- 波形 ---
        wave_frame = ttk.Frame(outer)
        wave_frame.pack(side="bottom", fill="x", padx=8, pady=(0, 2))
        self.canvas = tk.Canvas(wave_frame, height=150, bg="#f4f4f6", highlightthickness=1,
                                highlightbackground="#c8c8cc", cursor="arrow")
        self.canvas.pack(fill="x")
        self.hscroll = ttk.Scrollbar(wave_frame, orient="horizontal", command=self._on_hscroll)
        self.hscroll.pack(fill="x")
        self.canvas.bind("<Configure>", lambda _e: self._redraw_wave())
        self.canvas.bind("<Button-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double)
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas.bind("<MouseWheel>", self._on_canvas_wheel)
        self.canvas.bind("<Control-MouseWheel>", self._on_canvas_zoom)

        # --- 再生 ---
        transport = ttk.Frame(outer)
        transport.pack(side="bottom", fill="x", padx=8, pady=(4, 4))
        ttk.Button(transport, text="◀ 3秒", width=7,
                   command=lambda: self.on_skip(-3)).pack(side="left")
        self.btn_play = ttk.Button(transport, text="▶ 再生", width=9, command=self.on_play_toggle)
        self.btn_play.pack(side="left", padx=(4, 0))
        ttk.Button(transport, text="3秒 ▶", width=7,
                   command=lambda: self.on_skip(3)).pack(side="left", padx=(4, 0))
        self.btn_play_cue = ttk.Button(transport, text="この字幕を再生", width=14,
                                       command=self.on_play_cue)
        self.btn_play_cue.pack(side="left", padx=(10, 0))
        ttk.Checkbutton(transport, text="繰り返す", variable=self.var_loop).pack(side="left", padx=(8, 0))
        ttk.Label(transport, textvariable=self.var_pos, font=("Consolas", 11)).pack(side="left", padx=(16, 0))
        ttk.Label(transport, text="波形: ホイールで移動、Ctrl+ホイールで拡大縮小、"
                                  "上の帯をドラッグで時刻の調整", foreground="#666").pack(side="right")

        # --- ツールバー ---
        bar = ttk.Frame(outer)
        bar.pack(side="top", fill="x", padx=8, pady=(6, 4))
        for text, cmd in (("字幕を開く...", self.on_open_subtitle),
                          ("動画・音声を開く...", self.on_open_media),
                          ("保存", self.on_save), ("別名で保存...", self.on_save_as)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 4))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        self.btn_undo = ttk.Button(bar, text="元に戻す", command=self.on_undo, width=9)
        self.btn_undo.pack(side="left", padx=(0, 4))
        self.btn_redo = ttk.Button(bar, text="やり直す", command=self.on_redo, width=9)
        self.btn_redo.pack(side="left", padx=(0, 4))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        for text, cmd in (("全体をずらす...", self.on_shift), ("検索・置換...", self.on_find),
                          ("自動で折り返す...", self.on_wrap)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 4))
        self.btn_burn = ttk.Button(bar, text="動画に焼き込む...", command=self.on_burn)
        self.btn_burn.pack(side="right")

        # --- 上段（一覧 | 編集欄）---
        self.paned = ttk.PanedWindow(outer, orient="horizontal")
        self.paned.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 4))

        left = ttk.Frame(self.paned)
        right = ttk.Frame(self.paned)
        self.paned.add(left, weight=3)
        self.paned.add(right, weight=2)

        columns = ("no", "start", "end", "dur", "cps", "mark", "text")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended")
        heads = {"no": ("#", 44, "e"), "start": ("開始", 92, "e"), "end": ("終了", 92, "e"),
                 "dur": ("秒", 48, "e"), "cps": ("字/秒", 52, "e"), "mark": ("", 26, "center"),
                 "text": ("本文", 300, "w")}
        for key in columns:
            label, width, anchor = heads[key]
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=width if key != "text" else 120,
                             anchor=anchor, stretch=(key == "text"))
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree.tag_configure("problem", foreground="#b00020")
        self.tree.tag_configure("warn", foreground="#8a5a00")
        self.tree.tag_configure("playing", background="#e4eefc")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-Button-1>", lambda _e: self.on_play_cue())
        self.hint = ttk.Label(left, text="「字幕を開く...」で字幕ファイルを、「動画・音声を開く...」で"
                                         "その動画を開いてください。ここにファイルを落としても開けます。"
                              if HAS_DND else
                              "「字幕を開く...」で字幕ファイルを、「動画・音声を開く...」でその動画を開いてください。",
                              foreground="#666", wraplength=520, justify="left")
        self.hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        if HAS_DND:
            for widget in (self.tree, self.canvas):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_drop)

        # 編集欄。コマの枠はピクセルで大きさを固定する
        # （tk.Label の width は文字数なので、枠を別に作って中に入れる）
        self.frame_box = tk.Frame(right, width=PREVIEW_WIDTH, height=int(PREVIEW_WIDTH * 9 / 16),
                                  bg="#2b2b30")
        self.frame_box.pack(anchor="w", pady=(0, 4))
        self.frame_box.pack_propagate(False)
        self.frame_label = tk.Label(self.frame_box, text="動画を開くと、ここにその時刻のコマが出ます",
                                    bg="#2b2b30", fg="#bbbbc0")
        self.frame_label.pack(fill="both", expand=True)

        row = ttk.Frame(right)
        row.pack(fill="x", pady=(2, 2))
        ttk.Label(row, text="開始").pack(side="left")
        self.entry_start = ttk.Entry(row, textvariable=self.var_start, width=12, font=("Consolas", 11))
        self.entry_start.pack(side="left", padx=(4, 0))
        ttk.Button(row, text="◀今", width=4, command=lambda: self.on_set_time("start")).pack(side="left", padx=(2, 0))
        ttk.Label(row, text="終了").pack(side="left", padx=(12, 0))
        self.entry_end = ttk.Entry(row, textvariable=self.var_end, width=12, font=("Consolas", 11))
        self.entry_end.pack(side="left", padx=(4, 0))
        ttk.Button(row, text="◀今", width=4, command=lambda: self.on_set_time("end")).pack(side="left", padx=(2, 0))
        ttk.Checkbutton(row, text="字幕を重ねて表示", variable=self.var_overlay,
                        command=self._overlay_changed).pack(side="right")
        for entry in (self.entry_start, self.entry_end):
            entry.bind("<Return>", lambda _e: self._commit_times())
            entry.bind("<FocusOut>", lambda _e: self._commit_times())

        ttk.Label(right, text="本文").pack(anchor="w", pady=(4, 0))
        text_wrap = ttk.Frame(right)
        text_wrap.pack(fill="both", expand=True)
        base_font = tkfont.nametofont("TkDefaultFont")
        self.text_font = tkfont.Font(family=base_font.cget("family"), size=base_font.cget("size") + 3)
        self.text = tk.Text(text_wrap, height=4, wrap="char", font=self.text_font, undo=False)
        tsb = ttk.Scrollbar(text_wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tsb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        tsb.grid(row=0, column=1, sticky="ns")
        text_wrap.rowconfigure(0, weight=1)
        text_wrap.columnconfigure(0, weight=1)
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.bind("<FocusOut>", lambda _e: self._commit_text())
        self.text.bind("<Control-space>", lambda _e: (self.on_play_toggle(), "break")[1])
        self.text.bind("<Control-Return>", lambda _e: (self.on_play_cue(), "break")[1])
        self.text.bind("<Control-a>", lambda _e: (self.text.tag_add("sel", "1.0", "end-1c"), "break")[1])

        ttk.Label(right, textvariable=self.var_info, foreground="#666").pack(anchor="w", pady=(2, 0))
        ttk.Label(right, textvariable=self.var_cue_warn, foreground="#b00020",
                  wraplength=460, justify="left").pack(anchor="w")

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=(6, 0))
        for text, cmd in (("ここで分割", self.on_split), ("次と結合", self.on_merge),
                          ("後ろに挿入", self.on_insert), ("削除", self.on_delete)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=(0, 4))

        self._set_panel_enabled(False)

    def _bind_keys(self):
        r = self.root
        r.bind_all("<Control-o>", lambda _e: self.on_open_subtitle())
        r.bind_all("<Control-O>", lambda _e: self.on_open_media())
        r.bind_all("<Control-s>", lambda _e: self.on_save())
        r.bind_all("<Control-S>", lambda _e: self.on_save_as())
        r.bind_all("<Control-z>", lambda _e: self._if_not_typing(self.on_undo))
        r.bind_all("<Control-y>", lambda _e: self._if_not_typing(self.on_redo))
        r.bind_all("<Control-h>", lambda _e: self.on_find())
        r.bind_all("<Control-f>", lambda _e: self.on_find())
        r.bind_all("<F3>", lambda _e: self.on_next_warning())
        r.bind_all("<Control-d>", lambda _e: self.on_split())
        r.bind_all("<Control-j>", lambda _e: self.on_merge())
        r.bind_all("<Control-Insert>", lambda _e: self.on_insert())
        r.bind_all("<Control-Delete>", lambda _e: self.on_delete())
        r.bind_all("<Control-b>", lambda _e: self.on_burn())
        r.bind_all("<Control-Return>", lambda _e: self.on_play_cue())
        r.bind_all("<Control-Up>", lambda _e: self.on_step(-1))
        r.bind_all("<Control-Down>", lambda _e: self.on_step(1))
        r.bind_all("<Alt-Left>", lambda _e: self.on_skip(-3))
        r.bind_all("<Alt-Right>", lambda _e: self.on_skip(3))
        r.bind_all("<space>", self._on_space)
        r.bind_all("<bracketleft>", lambda e: self._on_bracket(e, "start"))
        r.bind_all("<bracketright>", lambda e: self._on_bracket(e, "end"))

        # 本文欄・時刻欄では Tk 標準の Emacs 風のキー（Ctrl+D で 1 文字削除、Ctrl+O で改行など）が
        # 先に動いてしまうので、同じキーをこちらの操作で上書きする（"break" で標準の動きを止める）
        overrides = (
            ("<Control-o>", self.on_open_subtitle), ("<Control-s>", self.on_save),
            ("<Control-d>", self.on_split), ("<Control-j>", self.on_merge),
            ("<Control-h>", self.on_find), ("<Control-f>", self.on_find),
            ("<Control-b>", self.on_burn),
            ("<Control-Insert>", self.on_insert), ("<Control-Delete>", self.on_delete),
            ("<Control-Up>", lambda: self.on_step(-1)), ("<Control-Down>", lambda: self.on_step(1)),
        )
        for widget in (self.text, self.entry_start, self.entry_end):
            for sequence, func in overrides:
                widget.bind(sequence, lambda _e, f=func: (f(), "break")[1])

    def _typing(self):
        widget = self.root.focus_get()
        return isinstance(widget, (tk.Text, tk.Entry, ttk.Entry, ttk.Combobox, ttk.Spinbox))

    def _if_not_typing(self, func):
        # 本文欄の Ctrl+Z は「本文の入力を戻す」ではなく、文書全体の元に戻すにする。
        # 入力中でも確定してから戻すので、迷いがない。
        if isinstance(self.root.focus_get(), tk.Text):
            self._commit_text()
        func()
        return "break"

    def _on_space(self, _event):
        if self._typing():
            return None
        self.on_play_toggle()
        return "break"

    def _on_bracket(self, _event, which):
        if self._typing():
            return None
        self.on_set_time(which)
        return "break"

    # ------------------------------------------------------------ 起動時

    def _first_run(self):
        if binaries.missing():
            ok = messagebox.askyesno(
                APP_TITLE,
                "音声の再生・波形・コマの表示・焼き込みには FFmpeg が要ります。\n"
                "いま取得しますか？（初回だけです）\n\n"
                "　・FFmpeg（{}）\n\n"
                "保存先: {}\n\n"
                "※ 他の johukku 製ツールと共有するので、\n"
                "　 すでに入っていれば取得は省かれます。\n"
                "※ 「いいえ」でも、字幕の編集と保存はできます。".format(
                    binaries.APPROX_SIZE["FFmpeg"], binaries.bin_dir()))
            if ok:
                self._start_setup()
            else:
                self.var_status.set("FFmpeg が未取得です。動画を開くときに改めて確認します。")
        else:
            self.var_status.set("字幕ファイルか動画を開いてください。")

        args = [a for a in sys.argv[1:] if not a.startswith("-")]
        if args:
            self.root.after(300, lambda: self.open_any(args[0]))

    def _refresh_parts(self):
        version = binaries.ffmpeg_version()
        self.var_parts.set("FFmpeg {}".format(version or "未取得"))

    def _ensure_ffmpeg(self, then=None):
        """FFmpeg が無ければ取得を提案する。あれば True。"""
        if not binaries.missing():
            return True
        if messagebox.askyesno(APP_TITLE, "先に FFmpeg を取得します。よろしいですか？"):
            self._start_setup(then=then)
        return False

    def _start_setup(self, then=None):
        if self.media_loading or self.burning:
            return
        self.var_status.set("FFmpeg を取得しています...")
        self._show_progress(None)

        def work():
            def on_progress(done, total, label):
                if done < 0:
                    self.queue.put(("status", label))
                elif total > 0:
                    self.queue.put(("progress", done * 100.0 / total, "{} を取得中".format(label)))
                else:
                    self.queue.put(("progress", None, "{} を取得中".format(label)))
            try:
                binaries.ensure_ffmpeg(on_progress)
                self.queue.put(("setup_done", True, "FFmpeg の準備ができました。", then))
            except binaries.SetupError as e:
                self.queue.put(("setup_done", False, str(e), None))
            except Exception:
                self.queue.put(("setup_done", False, traceback.format_exc(), None))

        threading.Thread(target=work, daemon=True).start()

    def on_reinstall(self):
        if self.media_loading or self.burning:
            return
        if not messagebox.askyesno(
                APP_TITLE,
                "FFmpeg を取得し直します。\n（{}）\n\n"
                "※ 他の johukku 製ツールとも共有しているファイルです。\n\n"
                "よろしいですか？".format(binaries.bin_dir())):
            return
        for path in (binaries.ffmpeg_path(), binaries.ffprobe_path()):
            try:
                os.remove(path)
            except OSError:
                pass
        self._start_setup()

    # ------------------------------------------------------- ファイルを開く

    def open_any(self, path):
        path = os.path.normpath(str(path).strip('"'))
        if not os.path.isfile(path):
            messagebox.showwarning(APP_TITLE, "ファイルが見つかりません:\n{}".format(path))
            return
        if sio.is_subtitle_file(path):
            self.open_subtitle(path)
        elif sio.is_media_file(path):
            self.open_media(path)
        else:
            messagebox.showinfo(APP_TITLE,
                                "対応していないファイルです。\n"
                                "字幕（SRT / VTT / ASS / JSON）か動画・音声を指定してください。")

    def on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        subs = [p for p in paths if sio.is_subtitle_file(p)]
        media = [p for p in paths if sio.is_media_file(p)]
        if media:
            self.open_media(media[0])
        if subs:
            self.open_subtitle(subs[0])
        if not subs and not media and paths:
            self.open_any(paths[0])

    def on_open_subtitle(self):
        path = filedialog.askopenfilename(title="字幕ファイルを開く", filetypes=SUBTITLE_TYPES,
                                          initialdir=self._initial_dir())
        if path:
            self.open_subtitle(path)

    def on_open_media(self):
        path = filedialog.askopenfilename(title="動画・音声を開く", filetypes=MEDIA_TYPES,
                                          initialdir=self._initial_dir())
        if path:
            self.open_media(path)

    def _initial_dir(self):
        folder = self.settings.get("last_dir")
        if folder and os.path.isdir(folder):
            return folder
        home = os.path.expanduser("~")
        for name in ("Videos", "Downloads", "Desktop"):
            path = os.path.join(home, name)
            if os.path.isdir(path):
                return path
        return home

    def open_subtitle(self, path):
        if not self._confirm_discard():
            return
        try:
            result = sio.load(path)
        except sio.LoadError as e:
            messagebox.showerror(APP_TITLE, "字幕を開けませんでした。\n\n{}".format(e))
            return
        self._commit_text(force_close=True)
        kind = result["format"]
        self.doc.load(result["cues"], path if kind != "json" else None, kind if kind != "json" else "srt")
        self.doc_source = path
        self._remember(path)
        self._rebuild_list()
        self._select(0)
        notes = list(result["notes"])
        if kind == "json":
            notes.append("JSON は編集用の形式ではないので、保存は「別名で保存」（SRT など）になります。")
        self.var_status.set("　".join(["{} 件の字幕を読み込みました。".format(len(self.doc.cues))] + notes))
        if notes:
            messagebox.showinfo(APP_TITLE, "\n".join(notes))
        self._update_title()

        if self.media_path is None:
            candidates = sio.find_media_for(path)
            if candidates:
                name = os.path.basename(candidates[0])
                if messagebox.askyesno(APP_TITLE, "同じ場所に動画がありました。一緒に開きますか？\n\n{}".format(name)):
                    self.open_media(candidates[0])

    def open_media(self, path):
        if self.burning:
            messagebox.showinfo(APP_TITLE, "焼き込みが終わるまで待ってください。")
            return
        if not self._ensure_ffmpeg(then=lambda: self.open_media(path)):
            return
        # 前のものを片づける
        self.media_cancel.set()
        self.media_cancel = threading.Event()
        self.media_gen += 1
        gen = self.media_gen
        self.player.close()
        self.peaks = None
        self.media = None
        self.media_path = path
        self.wav_path = None
        self.playhead = 0.0
        self.play_range = None
        self.was_playing = False
        self.media_loading = True
        self.view_start = 0.0
        self._remember(path)
        self._update_files_label()
        self._show_frame_placeholder("読み込んでいます...")
        self._redraw_wave()
        self.var_status.set("動画を調べています...")
        self._show_progress(None)
        cancel = self.media_cancel

        def work():
            try:
                info = media_info.probe(path)
                self.queue.put(("media_info", gen, info))
                if not info.get("audio"):
                    self.queue.put(("media_ready", gen, None, None,
                                    "音声が入っていないので、コマの表示だけ使えます。"))
                    return
                wav = os.path.join(audio.workdir(), "audio_{}.wav".format(gen))
                self.queue.put(("status", "音声を取り出しています..."))
                audio.extract_wav(binaries.ffmpeg_path(), path, wav, duration=info.get("duration"),
                                  on_progress=lambda p: self.queue.put(("progress", p * 70.0, "音声を取り出し中")),
                                  should_cancel=cancel.is_set)
                self.queue.put(("status", "波形を作っています..."))
                peaks = audio.load_peaks(wav,
                                         on_progress=lambda p: self.queue.put(("progress", 70 + p * 30.0, "波形を作成中")),
                                         should_cancel=cancel.is_set)
                self.queue.put(("media_ready", gen, wav, peaks, None))
            except audio.Cancelled:
                pass
            except (audio.AudioError, media_info.ProbeError) as e:
                self.queue.put(("media_error", gen, str(e)))
            except Exception:
                self.queue.put(("media_error", gen, traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()

    def _media_loaded(self, wav, peaks, note):
        self.media_loading = False
        self._hide_progress()
        self.wav_path = wav
        self.peaks = peaks
        if wav:
            try:
                self.player.open(wav)
            except audio.AudioError as e:
                messagebox.showerror(APP_TITLE, str(e))
        duration = (self.media or {}).get("duration") or (peaks.duration if peaks else 0.0)
        self.view_span = min(self.view_span, max(MIN_SPAN, duration or DEFAULT_SPAN))
        self._redraw_wave()
        self._update_pos_label()
        self.var_status.set(note or "動画を開きました。字幕を選ぶとその区間を聞けます。")
        self._request_frame(self.playhead if self.current is None else self.doc.cues[self.current]["start"])

        if not self.doc.cues:
            candidates = sio.find_sidecars(self.media_path)
            if candidates:
                name = os.path.basename(candidates[0])
                if messagebox.askyesno(APP_TITLE, "同じ場所に字幕がありました。開きますか？\n\n{}".format(name)):
                    self.open_subtitle(candidates[0])

    def _update_files_label(self):
        sub = self.doc.title() if (self.doc.cues or self.doc.path) else "なし"
        media = os.path.basename(self.media_path) if self.media_path else "なし"
        self.var_files.set("字幕: {}　　動画: {}".format(sub, media))

    def _update_title(self):
        self._update_files_label()
        name = self.doc.title() if (self.doc.cues or self.doc.path) else ""
        self.root.title("{}{} v{}".format(name + " - " if name else "", APP_TITLE, APP_VERSION))
        self.btn_undo.configure(state="normal" if self.doc.undo_stack else "disabled")
        self.btn_redo.configure(state="normal" if self.doc.redo_stack else "disabled")

    # ---------------------------------------------------------------- 保存

    def _confirm_discard(self):
        if not self.doc.dirty:
            return True
        answer = messagebox.askyesnocancel(APP_TITLE, "字幕に未保存の変更があります。保存しますか？")
        if answer is None:
            return False
        if answer:
            return self.on_save()
        return True

    def on_save(self):
        self._commit_text()
        self._commit_times()
        if not self.doc.cues:
            return False
        if not self.doc.path:
            return self.on_save_as()
        return self._write(self.doc.path, self.doc.kind)

    def on_save_as(self):
        self._commit_text()
        self._commit_times()
        if not self.doc.cues:
            messagebox.showinfo(APP_TITLE, "保存する字幕がありません。")
            return False
        base = self.doc.path or getattr(self, "doc_source", None) or (
            os.path.splitext(self.media_path)[0] + ".ja.srt" if self.media_path else "字幕.srt")
        stem, ext = os.path.splitext(os.path.basename(base))
        if ext.lower() not in (".srt", ".vtt", ".ass"):
            ext = ".srt"
        path = filedialog.asksaveasfilename(
            title="別名で保存", initialdir=os.path.dirname(base) or self._initial_dir(),
            initialfile=stem + ext, defaultextension=ext, filetypes=SAVE_TYPES)
        if not path:
            return False
        kind = os.path.splitext(path)[1].lower().lstrip(".")
        if kind not in ("srt", "vtt", "ass"):
            kind = "srt"
            path += ".srt"
        if not self._write(path, kind):
            return False
        self.doc.path = path
        self.doc.kind = kind
        self.doc.backed_up = True     # 別名なので控えは要らない
        self._remember(path)
        self._update_title()
        return True

    def _write(self, path, kind):
        try:
            if os.path.exists(path) and not self.doc.backed_up:
                backup = sio.backup_path(path)
                if not os.path.exists(backup):
                    with open(path, "rb") as src, open(backup, "wb") as dst:
                        dst.write(src.read())
                self.doc.backed_up = True
            style = self._style_for_output() if kind == "ass" else None
            sio.save(path, self.doc.cues, kind, style)
        except OSError as e:
            messagebox.showerror(APP_TITLE, "保存できませんでした。\n\n{}".format(e))
            return False
        self.doc.dirty = False
        self.var_status.set("保存しました: {}".format(os.path.basename(path)))
        self._update_title()
        return True

    def _style_for_output(self):
        width, height = 1920, 1080
        if self.media and self.media.get("video"):
            width, height = media_info.display_size(self.media)
        return burn_in.build_style(burn_in.preset_by_label(self.var_preset.get()),
                                   width, height, burn_in.max_line_width(self.doc.cues))

    # ------------------------------------------------------------------ 一覧

    def _rebuild_list(self):
        self.results = checks.check_all(self.doc.cues, self.check_settings)
        self.tree.delete(*self.tree.get_children())
        for i, cue in enumerate(self.doc.cues):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(i, cue),
                             tags=self._row_tags(i))
        self._update_warn_count()
        self._update_title()
        self._redraw_cues()
        self.hint.grid_remove() if self.doc.cues else self.hint.grid()

    def _refresh_rows(self):
        """時刻や本文が変わったあと、行を作り直さずに中身だけ入れ替える。"""
        self.results = checks.check_all(self.doc.cues, self.check_settings)
        children = self.tree.get_children()
        if len(children) != len(self.doc.cues):
            self._rebuild_list()
            return
        for i, cue in enumerate(self.doc.cues):
            self.tree.item(str(i), values=self._row_values(i, cue), tags=self._row_tags(i))
        self._update_warn_count()
        self._update_title()
        self._redraw_cues()

    def _row_values(self, i, cue):
        duration = cue["end"] - cue["start"]
        rate = checks.cps(cue)
        return (i + 1, fmt_time(cue["start"]), fmt_time(cue["end"]),
                "{:.1f}".format(duration), "{:.1f}".format(rate) if rate is not None else "",
                checks.mark(self.results[i]) if i < len(self.results) else "",
                first_line(cue["text"]))

    def _row_tags(self, i):
        tags = []
        if i < len(self.results):
            mark = checks.mark(self.results[i])
            if mark == "！":
                tags.append("problem")
            elif mark == "△":
                tags.append("warn")
        return tuple(tags)

    def _update_warn_count(self):
        problems, warns = checks.count(self.results)
        if not self.doc.cues:
            self.var_warn.set("")
        elif problems or warns:
            self.var_warn.set("問題 {} 件・注意 {} 件（F3 で次へ）".format(problems, warns))
        else:
            self.var_warn.set("警告なし")

    def _select(self, index, see=True):
        if not self.doc.cues:
            self.current = None
            self._load_panel(None)
            return
        index = max(0, min(len(self.doc.cues) - 1, index))
        iid = str(index)
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        if see:
            self.tree.see(iid)
        # <<TreeviewSelect>> はイベントループを回さないと届かないので、ここで直接反映する
        self._on_tree_select()

    def _selected_indexes(self):
        out = []
        for iid in self.tree.selection():
            try:
                out.append(int(iid))
            except ValueError:
                pass
        return sorted(i for i in out if i < len(self.doc.cues))

    def _on_tree_select(self, _event=None):
        selected = self._selected_indexes()
        if not selected:
            return
        index = selected[0]
        if index == self.current and not self.loading_panel:
            return
        self._commit_text()
        self._commit_times()
        self.current = index
        self._load_panel(index)
        cue = self.doc.cues[index]
        self._ensure_visible(cue["start"], cue["end"])
        self._redraw_cues()
        if not self.player.is_playing():
            self.playhead = cue["start"]
            self._update_pos_label()
            self._draw_playhead()
            self._request_frame(cue["start"] + 0.05, [cue])

    # ---------------------------------------------------------------- 編集欄

    def _set_panel_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in (self.entry_start, self.entry_end):
            widget.configure(state=state)
        self.text.configure(state=state, bg="white" if enabled else "#f0f0f0")

    def _load_panel(self, index):
        self.loading_panel = True
        try:
            if index is None or index >= len(self.doc.cues):
                self.var_start.set("")
                self.var_end.set("")
                self.text.configure(state="normal")
                self.text.delete("1.0", "end")
                self.text.edit_modified(False)
                self.var_info.set("")
                self.var_cue_warn.set("")
                self._set_panel_enabled(False)
                return
            cue = self.doc.cues[index]
            self._set_panel_enabled(True)
            self.var_start.set(fmt_time(cue["start"]))
            self.var_end.set(fmt_time(cue["end"]))
            self.text.delete("1.0", "end")
            self.text.insert("1.0", cue["text"])
            self.text.edit_modified(False)
            self.text_session = False
            self._update_cue_info(cue, index)
        finally:
            self.loading_panel = False

    def _update_cue_info(self, cue, index):
        lines = [l for l in cue["text"].split("\n") if l.strip()]
        widest = max((fmt.display_width(l) for l in lines), default=0.0)
        rate = checks.cps(cue)
        parts = ["{} 行".format(len(lines)), "最長の行 {:.0f} 文字".format(widest),
                 "{:.1f} 秒".format(cue["end"] - cue["start"])]
        if rate is not None:
            parts.append("{:.1f} 文字/秒".format(rate))
        self.var_info.set(" / ".join(parts))
        result = self.results[index] if index < len(self.results) else []
        self.var_cue_warn.set("　".join("{}: {}".format(level, msg) for level, msg in result))

    def _on_text_modified(self, _event=None):
        if self.loading_panel or self.current is None:
            self.text.edit_modified(False)
            return
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if not self.text_session:
            self.doc.push_undo()
            self.text_session = True
            self._update_title()
        if self.text_timer:
            self.root.after_cancel(self.text_timer)
        self.text_timer = self.root.after(700, self._commit_text)

    def _commit_text(self, force_close=False):
        """本文欄の内容を字幕に反映する。編集の途中なら、ここで 1 段の変更として確定する。"""
        if self.text_timer:
            self.root.after_cancel(self.text_timer)
            self.text_timer = None
        if self.current is None or self.current >= len(self.doc.cues):
            self.text_session = False
            return
        if not self.text_session and not force_close:
            return
        new_text = self.text.get("1.0", "end-1c").replace("\r", "")
        cue = self.doc.cues[self.current]
        if new_text != cue["text"]:
            cue["text"] = new_text
            self.doc.dirty = True
            self._refresh_rows()
            self._update_cue_info(cue, self.current)
            if self.var_overlay.get():
                self._request_frame(self.playhead if self.player.is_playing() else cue["start"] + 0.05, [cue])
        self.text_session = False

    def _commit_times(self):
        if self.current is None or self.current >= len(self.doc.cues) or self.loading_panel:
            return
        cue = self.doc.cues[self.current]
        try:
            start = sio.parse_time_input(self.var_start.get())
            end = sio.parse_time_input(self.var_end.get())
        except ValueError:
            self.var_start.set(fmt_time(cue["start"]))
            self.var_end.set(fmt_time(cue["end"]))
            return
        if abs(start - cue["start"]) < 0.0005 and abs(end - cue["end"]) < 0.0005:
            return
        if end <= start:
            end = start + 0.2
        self._apply_times(self.current, start, end)

    def _apply_times(self, index, start, end):
        """時刻を変えて並べ直す。並びが変わったら、同じ字幕を選び直す。"""
        self.doc.push_undo()
        cue = self.doc.cues[index]
        cue["start"] = max(0.0, start)
        cue["end"] = max(cue["start"] + 0.1, end)
        self.doc.sort()
        new_index = self.doc.cues.index(cue)
        self._refresh_rows()
        if new_index != self.current:
            self.current = new_index
            self._select(new_index)
        self._load_panel(new_index)
        self._redraw_cues()

    # ----------------------------------------------------------- 字幕の操作

    def on_undo(self):
        self._commit_text()
        if self.doc.undo():
            self._after_structural_change()

    def on_redo(self):
        self._commit_text()
        if self.doc.redo():
            self._after_structural_change()

    def _after_structural_change(self, select=None):
        index = self.current if select is None else select
        self._rebuild_list()
        if self.doc.cues:
            index = max(0, min(len(self.doc.cues) - 1, index if index is not None else 0))
            self.current = None
            self._select(index)
        else:
            self.current = None
            self._load_panel(None)

    def on_split(self):
        if self.current is None:
            return
        self._commit_text()
        cue = self.doc.cues[self.current]
        text = cue["text"]
        cursor = None
        if self.root.focus_get() is self.text:
            line, col = map(int, self.text.index("insert").split("."))
            cursor = sum(len(l) + 1 for l in text.split("\n")[:line - 1]) + col
        if cursor is None or cursor <= 0 or cursor >= len(text):
            cursor = _natural_split_point(text)
        if cursor is None:
            messagebox.showinfo(APP_TITLE, "分割する位置に本文のカーソルを置いてから押してください。")
            return
        first, second = text[:cursor].strip(), text[cursor:].strip()
        if not first or not second:
            messagebox.showinfo(APP_TITLE, "分割すると片方が空になります。")
            return
        weight_a = max(1.0, fmt.display_width(first.replace("\n", "")))
        weight_b = max(1.0, fmt.display_width(second.replace("\n", "")))
        duration = cue["end"] - cue["start"]
        middle = cue["start"] + duration * weight_a / (weight_a + weight_b)
        self.doc.push_undo()
        index = self.current
        cue["text"] = first
        cue["end"] = middle
        self.doc.cues.insert(index + 1, {"start": middle, "end": cue["start"] + duration, "text": second})
        self._after_structural_change(index + 1)

    def on_merge(self):
        if self.current is None or self.current + 1 >= len(self.doc.cues):
            return
        self._commit_text()
        index = self.current
        a, b = self.doc.cues[index], self.doc.cues[index + 1]
        self.doc.push_undo()
        joiner = " " if (a["text"] and b["text"] and a["text"][-1].isascii() and a["text"][-1].isalnum()
                         and b["text"][0].isascii() and b["text"][0].isalnum()) else "\n"
        a["text"] = (a["text"] + joiner + b["text"]).strip()
        a["end"] = max(a["end"], b["end"])
        del self.doc.cues[index + 1]
        self._after_structural_change(index)

    def on_insert(self):
        self._commit_text()
        if not self.doc.cues:
            self.doc.push_undo()
            self.doc.cues.append({"start": self.playhead, "end": self.playhead + 2.0, "text": ""})
            self._after_structural_change(0)
            self.text.focus_set()
            return
        index = self.current if self.current is not None else len(self.doc.cues) - 1
        cue = self.doc.cues[index]
        start = cue["end"]
        end = start + 2.0
        if index + 1 < len(self.doc.cues):
            end = min(end, max(start + 0.3, self.doc.cues[index + 1]["start"]))
        self.doc.push_undo()
        self.doc.cues.insert(index + 1, {"start": start, "end": end, "text": ""})
        self._after_structural_change(index + 1)
        self.text.focus_set()

    def on_delete(self):
        selected = self._selected_indexes()
        if not selected:
            return
        self._commit_text(force_close=False)
        self.text_session = False
        if len(selected) > 1 and not messagebox.askyesno(
                APP_TITLE, "{} 件の字幕を削除しますか？".format(len(selected))):
            return
        self.doc.push_undo()
        for index in reversed(selected):
            del self.doc.cues[index]
        self._after_structural_change(selected[0])

    def on_set_time(self, which):
        if self.current is None:
            return
        cue = self.doc.cues[self.current]
        now = self.player.position() if self.player.is_playing() else self.playhead
        if which == "start":
            start, end = now, cue["end"]
            if end <= start:
                end = start + max(0.5, cue["end"] - cue["start"])
        else:
            start, end = cue["start"], now
            if end <= start:
                start = max(0.0, end - max(0.5, cue["end"] - cue["start"]))
        self._apply_times(self.current, start, end)

    def on_shift(self):
        if not self.doc.cues:
            return
        self._commit_text()
        win = tk.Toplevel(self.root)
        win.title("全体をずらす")
        win.transient(self.root)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="字幕の時刻をまとめて動かします。遅らせるなら正、早めるなら負の秒数。").grid(
            row=0, column=0, columnspan=3, sticky="w")
        var_amount = tk.StringVar(value="0.5")
        ttk.Label(body, text="ずらす量:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        entry = ttk.Entry(body, textvariable=var_amount, width=10)
        entry.grid(row=1, column=1, sticky="w", pady=(10, 0))
        ttk.Label(body, text="秒（例: 0.5、-1.2）").grid(row=1, column=2, sticky="w", pady=(10, 0), padx=(4, 0))
        var_scope = tk.StringVar(value="all")
        ttk.Radiobutton(body, text="すべての字幕", value="all", variable=var_scope).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Radiobutton(body, text="選択している字幕から後ろ", value="after", variable=var_scope).grid(
            row=3, column=0, columnspan=3, sticky="w")
        btns = ttk.Frame(body)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))

        def apply():
            try:
                amount = float(var_amount.get().replace("，", ".").replace("．", ".").strip())
            except ValueError:
                messagebox.showwarning(APP_TITLE, "秒数は 0.5 や -1.2 のように入れてください。", parent=win)
                return
            first = 0 if var_scope.get() == "all" or self.current is None else self.current
            self.doc.push_undo()
            for cue in self.doc.cues[first:]:
                cue["start"] = max(0.0, cue["start"] + amount)
                cue["end"] = max(cue["start"] + 0.1, cue["end"] + amount)
            self.doc.sort()
            win.destroy()
            self._after_structural_change(self.current)
            self.var_status.set("{} 件の字幕を {:+.3f} 秒ずらしました。".format(
                len(self.doc.cues) - first, amount))

        ttk.Button(btns, text="ずらす", command=apply).pack(side="right")
        ttk.Button(btns, text="キャンセル", command=win.destroy).pack(side="right", padx=(0, 8))
        entry.focus_set()
        entry.select_range(0, "end")
        win.bind("<Return>", lambda _e: apply())
        win.bind("<Escape>", lambda _e: win.destroy())
        win.grab_set()

    def on_wrap(self):
        if not self.doc.cues:
            return
        self._commit_text()
        selected = self._selected_indexes()
        chars = int(self.settings.get("wrap_chars", 20))
        lines = int(self.settings.get("wrap_lines", 2))
        scope = "選択している {} 件".format(len(selected)) if len(selected) > 1 else "すべての字幕"
        if not messagebox.askyesno(
                APP_TITLE,
                "{} を、1 行 {} 文字・{} 行までに折り返し直します。\n"
                "行数を超える分は複数の字幕に分けます（時間は文字数で按分）。\n\n"
                "文字数と行数は「編集 → 設定」で変えられます。よろしいですか？".format(scope, chars, lines)):
            return
        targets = set(selected) if len(selected) > 1 else set(range(len(self.doc.cues)))
        self.doc.push_undo()
        out = []
        for i, cue in enumerate(self.doc.cues):
            if i in targets:
                text = cue["text"].replace("\n", "")
                out.extend(fmt.split_segments([{"start": cue["start"], "end": cue["end"], "text": text}],
                                              max_chars=chars, max_lines=lines))
            else:
                out.append(cue)
        self.doc.cues = out
        self.doc.sort()
        self._after_structural_change(self.current)

    # ----------------------------------------------------------- 検索・置換

    def on_find(self):
        if getattr(self, "find_win", None) and self.find_win.winfo_exists():
            self.find_win.lift()
            self.find_entry.focus_set()
            return
        win = tk.Toplevel(self.root)
        self.find_win = win
        win.title("検索・置換")
        win.transient(self.root)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)
        var_find = tk.StringVar(value=self.settings.get("find", ""))
        var_repl = tk.StringVar(value=self.settings.get("replace", ""))
        ttk.Label(body, text="検索:").grid(row=0, column=0, sticky="w")
        self.find_entry = ttk.Entry(body, textvariable=var_find, width=36)
        self.find_entry.grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(body, text="置換:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(body, textvariable=var_repl, width=36).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        ttk.Label(body, text="本文だけを探します。大文字と小文字は区別しません。",
                  foreground="#666").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        btns = ttk.Frame(body)
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def find_next():
            needle = var_find.get()
            if not needle or not self.doc.cues:
                return None
            self._commit_text()
            start = (self.current + 1) if self.current is not None else 0
            order = list(range(start, len(self.doc.cues))) + list(range(0, start))
            for i in order:
                if needle.lower() in self.doc.cues[i]["text"].lower():
                    self._select(i)
                    self._highlight_in_text(needle)
                    return i
            self.var_status.set("「{}」は見つかりませんでした。".format(needle))
            return None

        def replace_here():
            needle = var_find.get()
            if not needle or self.current is None:
                return
            self._commit_text()
            cue = self.doc.cues[self.current]
            if needle.lower() not in cue["text"].lower():
                find_next()
                return
            self.doc.push_undo()
            cue["text"] = _replace_ci(cue["text"], needle, var_repl.get())
            self._refresh_rows()
            self._load_panel(self.current)
            find_next()

        def replace_all():
            needle = var_find.get()
            if not needle:
                return
            self._commit_text()
            hits = [c for c in self.doc.cues if needle.lower() in c["text"].lower()]
            if not hits:
                self.var_status.set("「{}」は見つかりませんでした。".format(needle))
                return
            self.doc.push_undo()
            for cue in hits:
                cue["text"] = _replace_ci(cue["text"], needle, var_repl.get())
            self._refresh_rows()
            if self.current is not None:
                self._load_panel(self.current)
            self.var_status.set("{} 件の字幕で置換しました。".format(len(hits)))

        ttk.Button(btns, text="次を検索", command=find_next).pack(side="left")
        ttk.Button(btns, text="置換して次へ", command=replace_here).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="すべて置換", command=replace_all).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="閉じる", command=win.destroy).pack(side="left", padx=(12, 0))
        win.bind("<Return>", lambda _e: find_next())
        win.bind("<Escape>", lambda _e: win.destroy())

        def on_close():
            self.settings["find"] = var_find.get()
            self.settings["replace"] = var_repl.get()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")

    def _highlight_in_text(self, needle):
        self.text.tag_remove("sel", "1.0", "end")
        pos = self.text.search(needle, "1.0", nocase=True, stopindex="end")
        if pos:
            end = "{}+{}c".format(pos, len(needle))
            self.text.tag_add("sel", pos, end)
            self.text.mark_set("insert", end)
            self.text.see(pos)

    def on_next_warning(self):
        if not self.doc.cues:
            return
        start = (self.current + 1) if self.current is not None else 0
        order = list(range(start, len(self.doc.cues))) + list(range(0, start))
        for i in order:
            if i < len(self.results) and self.results[i]:
                self._select(i)
                return
        self.var_status.set("警告のある字幕はありません。")

    # ---------------------------------------------------------------- 設定

    def on_settings(self):
        win = tk.Toplevel(self.root)
        win.title("設定")
        win.transient(self.root)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="点検のしきい値（これを超えると一覧に印が付きます）").grid(
            row=0, column=0, columnspan=3, sticky="w")
        fields = [
            ("max_chars", "1 行の文字数（全角換算）", "文字まで"),
            ("max_lines", "行数", "行まで"),
            ("max_cps", "1 秒あたりの文字数", "文字/秒まで"),
            ("min_duration", "表示の短さ", "秒以上"),
            ("max_duration", "表示の長さ", "秒まで"),
        ]
        vars_ = {}
        for row, (key, label, unit) in enumerate(fields, start=1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=(6, 0))
            var = tk.StringVar(value=str(self.check_settings[key]))
            vars_[key] = var
            ttk.Entry(body, textvariable=var, width=8).grid(row=row, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
            ttk.Label(body, text=unit).grid(row=row, column=2, sticky="w", padx=(4, 0), pady=(6, 0))

        row = len(fields) + 1
        ttk.Separator(body, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(body, text="「自動で折り返す」の設定").grid(row=row + 1, column=0, columnspan=3, sticky="w")
        var_wc = tk.StringVar(value=str(self.settings.get("wrap_chars", 20)))
        var_wl = tk.StringVar(value=str(self.settings.get("wrap_lines", 2)))
        ttk.Label(body, text="1 行の文字数").grid(row=row + 2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(body, textvariable=var_wc, width=8).grid(row=row + 2, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(body, text="行数").grid(row=row + 3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(body, textvariable=var_wl, width=8).grid(row=row + 3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Separator(body, orient="horizontal").grid(row=row + 4, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(body, text="焼き込みと ASS 保存のスタイル").grid(row=row + 5, column=0, sticky="w")
        combo = ttk.Combobox(body, textvariable=self.var_preset, values=burn_in.PRESET_LABELS,
                             state="readonly", width=26)
        combo.grid(row=row + 5, column=1, columnspan=2, sticky="w", padx=(8, 0))

        btns = ttk.Frame(body)
        btns.grid(row=row + 6, column=0, columnspan=3, sticky="e", pady=(14, 0))

        def apply():
            data = {}
            for key, var in vars_.items():
                data[key] = var.get()
            self.check_settings = checks.settings_from(data)
            self.settings["checks"] = dict(self.check_settings)
            try:
                self.settings["wrap_chars"] = max(8, min(60, int(var_wc.get())))
                self.settings["wrap_lines"] = max(1, min(4, int(var_wl.get())))
            except ValueError:
                pass
            self.settings["preset"] = self.var_preset.get()
            win.destroy()
            self._refresh_rows()
            if self.current is not None:
                self._update_cue_info(self.doc.cues[self.current], self.current)
            self._overlay_changed()

        ttk.Button(btns, text="OK", command=apply, width=10).pack(side="right")
        ttk.Button(btns, text="キャンセル", command=win.destroy, width=10).pack(side="right", padx=(0, 8))
        win.bind("<Escape>", lambda _e: win.destroy())
        win.grab_set()

    def on_about(self):
        messagebox.showinfo(
            "バージョン情報",
            "{} v{}\n\n字幕ファイルを、音を聞きながら直して、保存するか動画に焼き込むツールです。\n\n"
            "作者: johukku\nサイト: https://johukku.pages.dev/\n"
            "ソース: https://github.com/johukku/subtitle-editor\n\nMIT License".format(APP_TITLE, APP_VERSION))

    def _open_url(self, url):
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    # ---------------------------------------------------------------- 再生

    def on_play_toggle(self):
        if not self.player.opened:
            if self.media_path is None:
                self.var_status.set("先に動画・音声を開いてください。")
            return
        mode = self.player.mode()
        if mode == "playing":
            self.player.pause()
            self.playhead = self.player.position()
            self._playback_stopped(paused=True)
        elif mode == "paused":
            self.player.resume()
            self.was_playing = True
            self.btn_play.configure(text="■ 停止")
        else:
            self.play_range = None
            self._start_play(self.playhead)

    def _start_play(self, start, end=None):
        try:
            self.player.play(start, end)
        except audio.AudioError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.play_range = (start, end) if end is not None else None
        self.was_playing = True
        self.last_frame_at = -1.0
        self.btn_play.configure(text="■ 停止")

    def on_play_cue(self):
        if self.current is None or not self.player.opened:
            return
        cue = self.doc.cues[self.current]
        self._commit_times()
        self.playhead = cue["start"]
        self._start_play(cue["start"], cue["end"])

    def on_skip(self, seconds):
        if not self.player.opened:
            return
        now = self.player.position() if self.player.is_playing() else self.playhead
        target = max(0.0, min(self.player.length, now + seconds))
        self._seek(target)

    def _seek(self, seconds, play=None):
        """再生位置を動かす。再生中ならそこから続ける。"""
        playing = self.player.is_playing() if play is None else play
        self.playhead = seconds
        if playing:
            self.play_range = None
            self._start_play(seconds)
        else:
            if self.player.opened:
                self.player.seek(seconds)
            self._update_pos_label()
            self._draw_playhead()
            self._request_frame(seconds, self._cues_at(seconds))
        self._ensure_visible(seconds, seconds)

    def on_step(self, delta):
        if not self.doc.cues:
            return
        index = 0 if self.current is None else max(0, min(len(self.doc.cues) - 1, self.current + delta))
        self._select(index)

    def _playback_stopped(self, paused=False):
        self.was_playing = False
        self.btn_play.configure(text="▶ 再生")
        self._update_pos_label()
        self._draw_playhead()
        self._request_frame(self.playhead, self._cues_at(self.playhead))
        self._mark_playing(None)

    def _cues_at(self, seconds):
        return [c for c in self.doc.cues if c["start"] <= seconds < c["end"]]

    def _update_pos_label(self):
        total = self.player.length if self.player.opened else ((self.media or {}).get("duration") or 0.0)
        hours = total >= 3600
        self.var_pos.set("{} / {}".format(fmt_time(self.playhead, hours), fmt_time(total, hours)))

    def _mark_playing(self, index):
        for iid in self.tree.tag_has("playing"):
            tags = tuple(t for t in self.tree.item(iid, "tags") if t != "playing")
            self.tree.item(iid, tags=tags)
        if index is not None:
            iid = str(index)
            if self.tree.exists(iid):
                self.tree.item(iid, tags=tuple(self.tree.item(iid, "tags")) + ("playing",))

    # ---------------------------------------------------------------- コマ

    def _show_frame_placeholder(self, text):
        self.frame_label.configure(image="", text=text)
        self.frame_label.image = None

    def _request_frame(self, at, cues=None):
        """at 秒のコマを裏で作る。次々に頼まれたら最後のものだけ作る。"""
        if not self.media_path or not (self.media or {}).get("video") or self.media_loading:
            return
        duration = self.media.get("duration") or 0.0
        if duration:
            at = max(0.0, min(at, max(0.0, duration - 0.15)))
        style = None
        if self.var_overlay.get() and cues:
            width, height = media_info.display_size(self.media)
            style = burn_in.build_style(burn_in.preset_by_label(self.var_preset.get()),
                                        width, height, burn_in.max_line_width(self.doc.cues))
        segments = [dict(c) for c in cues] if (cues and style) else None
        with self.frame_lock:
            first = self.frame_request is None
            self.frame_request = (self.media_gen, self.media_path, at, style, segments)
        if first:
            threading.Thread(target=self._frame_worker, daemon=True).start()

    def _frame_worker(self):
        while True:
            with self.frame_lock:
                request = self.frame_request
                if request is None:
                    return
                self.frame_request = None
            gen, path, at, style, segments = request
            self.frame_count += 1
            png = os.path.join(audio.workdir(), "frame_{}.png".format(self.frame_count % 2))
            try:
                audio.grab_frame(binaries.ffmpeg_path(), path, at, png, PREVIEW_WIDTH,
                                 style=style, segments=segments)
                self.queue.put(("frame", gen, png))
            except Exception as e:
                self.queue.put(("frame_error", gen, str(e).splitlines()[0] if str(e) else ""))
            with self.frame_lock:
                if self.frame_request is None:
                    return

    def _show_frame(self, png):
        try:
            image = tk.PhotoImage(file=png)
        except tk.TclError:
            return
        self.frame_label.configure(image=image, text="")
        self.frame_label.image = image
        self.frame_box.configure(width=max(PREVIEW_WIDTH, image.width()), height=image.height())

    def _overlay_changed(self):
        self.settings["overlay"] = bool(self.var_overlay.get())
        if self.current is not None and not self.player.is_playing():
            cue = self.doc.cues[self.current]
            self._request_frame(cue["start"] + 0.05, [cue])

    # ---------------------------------------------------------------- 波形

    def _duration(self):
        if self.peaks:
            return max(self.peaks.duration, 0.1)
        if self.media and self.media.get("duration"):
            return float(self.media["duration"])
        if self.doc.cues:
            return max(c["end"] for c in self.doc.cues) + 5.0
        return 60.0

    def _x_of(self, seconds):
        width = max(1, self.canvas.winfo_width())
        return (seconds - self.view_start) / self.view_span * width

    def _t_of(self, x):
        width = max(1, self.canvas.winfo_width())
        return self.view_start + x / width * self.view_span

    def _clamp_view(self):
        duration = self._duration()
        self.view_span = max(MIN_SPAN, min(self.view_span, max(duration, MIN_SPAN)))
        self.view_start = max(0.0, min(self.view_start, max(0.0, duration - self.view_span)))

    def _ensure_visible(self, t0, t1):
        if t0 < self.view_start or t1 > self.view_start + self.view_span:
            self.view_start = max(0.0, t0 - self.view_span * 0.2)
            self._redraw_wave()

    def _redraw_wave(self):
        self._clamp_view()
        c = self.canvas
        c.delete("wave")
        width = max(1, c.winfo_width())
        height = max(1, c.winfo_height())
        top = 34
        bottom = height - 16
        mid = (top + bottom) / 2.0
        amp = (bottom - top) / 2.0 - 2
        c.create_rectangle(0, top, width, bottom, fill="#ffffff", outline="", tags="wave")
        c.create_line(0, mid, width, mid, fill="#d0d0d4", tags="wave")
        if self.peaks:
            cols = self.peaks.columns(self.view_start, self.view_start + self.view_span, width)
            for x, (lo, hi) in enumerate(cols):
                y0 = mid - hi * amp
                y1 = mid - lo * amp
                if y1 - y0 < 1:
                    y1 = y0 + 1
                c.create_line(x, y0, x, y1, fill="#4a7fc1", tags="wave")
        elif self.media_path:
            c.create_text(width / 2, mid, text="波形を作っています..." if self.media_loading else "（音声なし）",
                          fill="#888", tags="wave")
        else:
            c.create_text(width / 2, mid, text="動画・音声を開くと、ここに波形が出ます", fill="#888", tags="wave")
        # 目盛り
        step = _tick_step(self.view_span)
        t = (int(self.view_start / step)) * step
        while t <= self.view_start + self.view_span:
            x = self._x_of(t)
            c.create_line(x, bottom, x, bottom + 4, fill="#888", tags="wave")
            c.create_text(x + 2, bottom + 5, text=fmt_time(t)[:-4], anchor="nw", fill="#666",
                          font=("Consolas", 8), tags="wave")
            t += step
        self._redraw_cues()
        self._draw_playhead()
        duration = self._duration()
        lo = self.view_start / duration if duration else 0.0
        hi = min(1.0, (self.view_start + self.view_span) / duration) if duration else 1.0
        self.hscroll.set(lo, hi)

    def _redraw_cues(self):
        c = self.canvas
        c.delete("cue")
        width = max(1, c.winfo_width())
        y0, y1 = 4, 30
        view_end = self.view_start + self.view_span
        for i, cue in enumerate(self.doc.cues):
            if cue["end"] < self.view_start or cue["start"] > view_end:
                continue
            x0 = max(-2, self._x_of(cue["start"]))
            x1 = min(width + 2, self._x_of(cue["end"]))
            selected = i == self.current
            mark = checks.mark(self.results[i]) if i < len(self.results) else ""
            fill = "#ffd27a" if selected else ("#f6d5d5" if mark == "！" else "#dbe7f7")
            outline = "#d19a2a" if selected else ("#c66" if mark == "！" else "#7a9cc9")
            c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, tags=("cue", "cue{}".format(i)))
            if x1 - x0 > 24:
                label = self._fit_label("{} {}".format(i + 1, first_line(cue["text"], 60)), x1 - x0 - 8)
                if label:
                    c.create_text(x0 + 4, (y0 + y1) / 2, text=label, anchor="w", fill="#222",
                                  font=self.band_font, tags=("cue", "cue{}".format(i)))
        c.tag_raise("playhead")

    def _fit_label(self, text, max_px):
        """帯の幅に収まるところまで文字を削る（折り返さず、はみ出させない）。"""
        if not hasattr(self, "band_font"):
            self.band_font = tkfont.Font(family="Yu Gothic UI", size=9)
        if self.band_font.measure(text) <= max_px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.band_font.measure(text[:mid] + "…") <= max_px:
                lo = mid
            else:
                hi = mid - 1
        return (text[:lo] + "…") if lo > 0 else ""

    def _draw_playhead(self):
        c = self.canvas
        c.delete("playhead")
        x = self._x_of(self.playhead)
        if 0 <= x <= c.winfo_width():
            c.create_line(x, 0, x, c.winfo_height(), fill="#e0403a", width=2, tags="playhead")

    def _on_hscroll(self, *args):
        duration = self._duration()
        if args[0] == "moveto":
            self.view_start = float(args[1]) * duration
        elif args[0] == "scroll":
            amount = int(args[1])
            self.view_start += amount * (self.view_span * (0.1 if args[2] == "units" else 0.9))
        self._redraw_wave()

    def _on_canvas_wheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self.view_start += direction * self.view_span * 0.15
        self._redraw_wave()

    def _on_canvas_zoom(self, event):
        anchor = self._t_of(event.x)
        factor = 0.8 if event.delta > 0 else 1.25
        new_span = max(MIN_SPAN, min(self._duration(), self.view_span * factor))
        self.view_start = anchor - (anchor - self.view_start) * new_span / self.view_span
        self.view_span = new_span
        self.settings["span"] = self.view_span
        self._redraw_wave()

    def _cue_edge_at(self, x, y):
        """帯の上での位置から (字幕の番号, "start" / "end" / "body") を返す。無ければ None。"""
        if not (2 <= y <= 32):
            return None
        t = self._t_of(x)
        tol = self.view_span / max(1, self.canvas.winfo_width()) * 6
        best = None
        for i, cue in enumerate(self.doc.cues):
            if cue["start"] - tol <= t <= cue["end"] + tol:
                if abs(t - cue["start"]) <= tol:
                    return (i, "start")
                if abs(t - cue["end"]) <= tol:
                    return (i, "end")
                best = (i, "body")
        return best

    def _on_canvas_motion(self, event):
        hit = self._cue_edge_at(event.x, event.y)
        if hit and hit[1] in ("start", "end"):
            self.canvas.configure(cursor="sb_h_double_arrow")
        elif hit:
            self.canvas.configure(cursor="fleur")
        else:
            self.canvas.configure(cursor="arrow")

    def _on_canvas_press(self, event):
        self.canvas.focus_set()
        hit = self._cue_edge_at(event.x, event.y)
        if hit:
            index, part = hit
            cue = self.doc.cues[index]
            if index != self.current:
                self._select(index)
            self.drag = {"index": index, "part": part, "t0": self._t_of(event.x),
                         "start": cue["start"], "end": cue["end"], "moved": False}
            return
        self.drag = None
        self._seek(max(0.0, self._t_of(event.x)))

    def _on_canvas_drag(self, event):
        if not self.drag:
            return
        d = self.drag
        cue = self.doc.cues[d["index"]]
        delta = self._t_of(event.x) - d["t0"]
        if abs(delta) < 0.005 and not d["moved"]:
            return
        d["moved"] = True
        if d["part"] == "start":
            cue["start"] = max(0.0, min(d["start"] + delta, d["end"] - 0.1))
        elif d["part"] == "end":
            cue["end"] = max(d["start"] + 0.1, d["end"] + delta)
        else:
            start = max(0.0, d["start"] + delta)
            cue["start"] = start
            cue["end"] = start + (d["end"] - d["start"])
        self.var_start.set(fmt_time(cue["start"]))
        self.var_end.set(fmt_time(cue["end"]))
        self._redraw_cues()

    def _on_canvas_release(self, _event):
        if not self.drag:
            return
        d = self.drag
        self.drag = None
        if not d["moved"]:
            return
        cue = self.doc.cues[d["index"]]
        new_start, new_end = cue["start"], cue["end"]
        cue["start"], cue["end"] = d["start"], d["end"]     # いったん戻して undo に積む
        self._apply_times(d["index"], new_start, new_end)

    def _on_canvas_double(self, event):
        hit = self._cue_edge_at(event.x, event.y)
        if hit:
            self._select(hit[0])
            self.on_play_cue()

    # ----------------------------------------------------------- 焼き込み

    def on_burn(self):
        if self.burning:
            return
        self._commit_text()
        self._commit_times()
        if not self.doc.cues:
            messagebox.showinfo(APP_TITLE, "焼き込む字幕がありません。先に字幕を開いてください。")
            return
        if not self.media_path or not (self.media or {}).get("video"):
            messagebox.showinfo(APP_TITLE, "焼き込む動画を先に「動画・音声を開く...」で開いてください。")
            return
        if self.media_loading:
            messagebox.showinfo(APP_TITLE, "動画の読み込みが終わるまで待ってください。")
            return
        if not self._ensure_ffmpeg():
            return
        if self.doc.dirty:
            answer = messagebox.askyesnocancel(APP_TITLE, "字幕に未保存の変更があります。先に保存しますか？\n"
                                                          "（「いいえ」でも、画面の内容で焼き込みます）")
            if answer is None:
                return
            if answer and not self.on_save():
                return

        win = tk.Toplevel(self.root)
        win.title("動画に焼き込む")
        win.transient(self.root)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)

        width, height = media_info.display_size(self.media)
        ttk.Label(body, text="{}　（{}×{}・{}）".format(
            os.path.basename(self.media_path), width, height,
            media_info.human_duration(self.media.get("duration")))).grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(body, text="スタイル:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        combo = ttk.Combobox(body, textvariable=self.var_preset, values=burn_in.PRESET_LABELS,
                             state="readonly", width=26)
        combo.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(10, 0))
        ttk.Button(body, text="見本を見る", command=lambda: self._burn_preview(win)).grid(
            row=1, column=2, sticky="w", padx=(6, 0), pady=(10, 0))

        var_gpu = tk.BooleanVar(value=bool(self.settings.get("use_gpu", True)))
        ttk.Checkbutton(body, text="GPU が使えれば使う（速い。駄目なら自動で CPU に切り替え）",
                        variable=var_gpu).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        stem, _ = os.path.splitext(self.media_path)
        ext = burn_in.output_extension(self.media_path)
        default_out = "{}_字幕入り{}".format(stem, ext)
        n = 2
        while os.path.exists(default_out):
            default_out = "{}_字幕入り_{}{}".format(stem, n, ext)
            n += 1
        var_out = tk.StringVar(value=default_out)
        ttk.Label(body, text="保存先:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(body, textvariable=var_out, width=52).grid(row=3, column=1, columnspan=2, sticky="we",
                                                           padx=(6, 0), pady=(8, 0))

        def browse():
            path = filedialog.asksaveasfilename(parent=win, title="焼き込んだ動画の保存先",
                                                initialfile=os.path.basename(var_out.get()),
                                                initialdir=os.path.dirname(var_out.get()),
                                                defaultextension=ext,
                                                filetypes=[("動画", "*" + ext)])
            if path:
                var_out.set(path)

        ttk.Button(body, text="参照...", command=browse).grid(row=4, column=1, sticky="w", padx=(6, 0), pady=(4, 0))
        ttk.Label(body, text="元の動画は書き換えません。時間は動画の長さの数分の 1〜数倍（GPU の有無で変わります）。",
                  foreground="#666", wraplength=460, justify="left").grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        var_state = tk.StringVar(value="")
        ttk.Label(body, textvariable=var_state).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        bar = ttk.Progressbar(body, mode="determinate", maximum=100)
        bar.grid(row=7, column=0, columnspan=3, sticky="we", pady=(4, 0))

        btns = ttk.Frame(body)
        btns.grid(row=8, column=0, columnspan=3, sticky="e", pady=(12, 0))
        btn_start = ttk.Button(btns, text="焼き込む", width=12)
        btn_cancel = ttk.Button(btns, text="中止", width=10, state="disabled")
        btn_close = ttk.Button(btns, text="閉じる", width=10, command=lambda: close())
        btn_start.pack(side="right")
        btn_cancel.pack(side="right", padx=(0, 8))
        btn_close.pack(side="right", padx=(0, 8))

        def close():
            if self.burning:
                return
            win.destroy()

        def start():
            out = var_out.get().strip()
            if not out:
                messagebox.showwarning(APP_TITLE, "保存先を指定してください。", parent=win)
                return
            if os.path.abspath(out) == os.path.abspath(self.media_path):
                messagebox.showwarning(APP_TITLE, "元の動画と同じ名前には保存できません。", parent=win)
                return
            if os.path.exists(out) and not messagebox.askyesno(
                    APP_TITLE, "同じ名前のファイルがあります。上書きしますか？", parent=win):
                return
            self.settings["use_gpu"] = bool(var_gpu.get())
            self.settings["preset"] = self.var_preset.get()
            self.burning = True
            self.burn_cancel.clear()
            btn_start.configure(state="disabled")
            btn_close.configure(state="disabled")
            btn_cancel.configure(state="normal")
            self.btn_burn.configure(state="disabled")
            var_state.set("焼き込んでいます...")
            self.var_status.set("字幕を焼き込んでいます...")
            style = self._style_for_output()
            cues = [dict(c) for c in self.doc.cues]
            info = burn_in.probe_video(self.media_path)
            use_gpu = bool(var_gpu.get())

            def work():
                try:
                    burn_in.burn(binaries.ffmpeg_path(), self.media_path, cues, out, style,
                                 duration=info.get("duration"), audio_codec=info.get("audio_codec"),
                                 use_gpu=use_gpu,
                                 on_progress=lambda p: self.queue.put(("burn_progress", p)),
                                 should_cancel=self.burn_cancel.is_set,
                                 log=lambda t: self.queue.put(("status", t)))
                    self.queue.put(("burn_done", True, out))
                except burn_in.Cancelled:
                    self.queue.put(("burn_done", False, "中止しました。"))
                except burn_in.BurnError as e:
                    self.queue.put(("burn_done", False, str(e)))
                except Exception:
                    self.queue.put(("burn_done", False, traceback.format_exc()))

            threading.Thread(target=work, daemon=True).start()

        def cancel():
            self.burn_cancel.set()
            var_state.set("中止しています...")

        btn_start.configure(command=start)
        btn_cancel.configure(command=cancel)
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _e: close())
        self.burn_dialog = {"win": win, "bar": bar, "state": var_state,
                            "buttons": (btn_start, btn_cancel, btn_close)}

    def _burn_preview(self, parent):
        cue = self.doc.cues[self.current] if self.current is not None else None
        style = self._style_for_output()
        png = os.path.join(audio.workdir(), "sample.png")
        at = (cue["start"] + 0.05) if cue else (self.media.get("duration") or 0.0) / 3.0
        try:
            burn_in.make_preview(binaries.ffmpeg_path(), self.media_path, style, png, at=at,
                                 preview_width=880, segments=[cue] if cue else None)
            image = tk.PhotoImage(file=png)
        except Exception as e:
            messagebox.showerror(APP_TITLE, "見本を作れませんでした:\n{}".format(e), parent=parent)
            return
        win = tk.Toplevel(parent)
        win.title("焼き込みの見本")
        win.transient(parent)
        label = ttk.Label(win, image=image)
        label.image = image
        label.pack(padx=12, pady=12)
        ttk.Label(win, text="文字の大きさ {} px（一番長い行が横幅に収まる大きさで決めています）".format(
            style["size"]), foreground="#666").pack(padx=12, pady=(0, 6))
        ttk.Button(win, text="閉じる", command=win.destroy).pack(pady=(0, 12))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _burn_finished(self, ok, message):
        self.burning = False
        self.btn_burn.configure(state="normal")
        dialog = getattr(self, "burn_dialog", None)
        if dialog and dialog["win"].winfo_exists():
            btn_start, btn_cancel, btn_close = dialog["buttons"]
            btn_start.configure(state="normal")
            btn_cancel.configure(state="disabled")
            btn_close.configure(state="normal")
            dialog["bar"]["value"] = 100 if ok else 0
            dialog["state"].set("完了しました。" if ok else message.splitlines()[0])
        if ok:
            self.var_status.set("焼き込みが完了しました: {}".format(os.path.basename(message)))
            if messagebox.askyesno(APP_TITLE, "焼き込みが完了しました。\n\n{}\n\n保存先のフォルダを開きますか？".format(message)):
                try:
                    os.startfile(os.path.dirname(message))
                except OSError:
                    pass
        else:
            self.var_status.set(message.splitlines()[0])
            if "中止" not in message:
                messagebox.showerror(APP_TITLE, message)

    # ------------------------------------------------------------ 表示の更新

    def _show_progress(self, percent):
        self.progress.pack(side="right", padx=(0, 10))
        if percent is None:
            self.progress.configure(mode="indeterminate")
            self.progress.start(30)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress["value"] = percent

    def _hide_progress(self):
        self.progress.stop()
        self.progress.pack_forget()

    def _pump(self):
        """裏の仕事からの連絡を画面に反映し、再生位置を追いかける。"""
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.var_status.set(msg[1])
                elif kind == "progress":
                    self._show_progress(msg[1])
                    if msg[1] is not None:
                        self.var_status.set("{}  {:.0f}%".format(msg[2], msg[1]))
                    else:
                        self.var_status.set(msg[2])
                elif kind == "setup_done":
                    self._hide_progress()
                    self._refresh_parts()
                    self.var_status.set(msg[2] if msg[1] else msg[2].splitlines()[0])
                    if not msg[1]:
                        messagebox.showerror(APP_TITLE, msg[2])
                    elif msg[3]:
                        msg[3]()
                elif kind == "media_info":
                    if msg[1] == self.media_gen:
                        self.media = msg[2]
                        self._update_pos_label()
                        self._redraw_wave()
                        self._request_frame(0.0)
                elif kind == "media_ready":
                    if msg[1] == self.media_gen:
                        self._media_loaded(msg[2], msg[3], msg[4])
                elif kind == "media_error":
                    if msg[1] == self.media_gen:
                        self.media_loading = False
                        self._hide_progress()
                        self.var_status.set("動画を開けませんでした。")
                        self._show_frame_placeholder("開けませんでした")
                        messagebox.showerror(APP_TITLE, "動画を開けませんでした。\n\n{}".format(msg[2]))
                        if self.media is None:
                            self.media_path = None
                            self._update_files_label()
                        self._redraw_wave()
                elif kind == "frame":
                    if msg[1] == self.media_gen:
                        self._show_frame(msg[2])
                elif kind == "frame_error":
                    pass
                elif kind == "burn_progress":
                    dialog = getattr(self, "burn_dialog", None)
                    if dialog and dialog["win"].winfo_exists():
                        dialog["bar"]["value"] = msg[1] * 100
                        dialog["state"].set("焼き込んでいます... {:.0f}%".format(msg[1] * 100))
                elif kind == "burn_done":
                    self._burn_finished(msg[1], msg[2])
        except queue.Empty:
            pass
        self._tick()
        self.root.after(50, self._pump)

    def _tick(self):
        if not self.was_playing:
            return
        mode = self.player.mode()
        if mode == "playing":
            pos = self.player.position()
            self.playhead = pos
            self._update_pos_label()
            self._draw_playhead()
            if pos > self.view_start + self.view_span * 0.95 or pos < self.view_start:
                self.view_start = pos - self.view_span * 0.1
                self._redraw_wave()
            self._mark_playing(self.doc.cue_at(pos))
            self.frame_timer += 50
            if self.frame_timer >= FRAME_INTERVAL_MS:
                self.frame_timer = 0
                self._request_frame(pos, self._cues_at(pos))
        elif mode == "paused":
            return
        else:
            # 終端か「ここまで」で止まった
            if self.play_range and self.var_loop.get():
                self._start_play(*self.play_range)
                return
            if self.play_range:
                self.playhead = self.play_range[0]
            self._playback_stopped()

    # ---------------------------------------------------------------- 終了

    def on_close(self):
        if self.burning:
            if not messagebox.askyesno(APP_TITLE, "焼き込みの途中です。中止して終了しますか？"):
                return
            self.burn_cancel.set()
        self._commit_text()
        if not self._confirm_discard():
            return
        self.media_cancel.set()
        try:
            self.player.close()
        except Exception:
            pass
        self._save_settings()
        self.root.destroy()
        audio.cleanup()

    # ---------------------------------------------------------------- 設定

    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        self.settings.update({
            "width": self.root.winfo_width(),
            "height": self.root.winfo_height(),
            "span": self.view_span,
            "overlay": bool(self.var_overlay.get()),
            "preset": self.var_preset.get(),
            "checks": dict(self.check_settings),
        })
        try:
            self.settings["sash"] = self.paned.sashpos(0)
        except tk.TclError:
            pass
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


# ------------------------------------------------------------------ 小物

def _natural_split_point(text):
    """カーソルが無いときの分割位置。改行 → 句読点 → 真ん中の順に探す。"""
    if "\n" in text:
        return text.index("\n") + 1
    best = None
    center = len(text) / 2.0
    for i, ch in enumerate(text):
        if ch in "。、！？!?,." and 0 < i + 1 < len(text):
            if best is None or abs(i + 1 - center) < abs(best - center):
                best = i + 1
    if best is not None:
        return best
    if len(text) >= 2:
        return len(text) // 2
    return None


def _replace_ci(text, needle, replacement):
    """大文字小文字を区別せずに置換する。"""
    out = []
    low = text.lower()
    key = needle.lower()
    i = 0
    while True:
        j = low.find(key, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append(replacement)
        i = j + len(needle)
    return "".join(out)


def _tick_step(span):
    for step in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if span / step <= 12:
            return step
    return 7200


def main():
    root = ROOT_CLASS()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    app = App(root)
    try:
        sash = int(app.settings.get("sash", 0))
        if sash > 200:
            root.after(150, lambda: app.paned.sashpos(0, sash))
    except (TypeError, ValueError):
        pass
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
