# -*- coding: utf-8 -*-
"""字幕ファイルの読み込み（SRT / VTT / ASS / Whisper 字幕作成ツールの JSON）

書き出しは subtitle_formats.py（字幕ツールの複製）に任せ、こちらは読む側を持つ。
返す形は字幕ツールの segments と同じ:

    [{"start": 秒, "end": 秒, "text": "本文（改行は \\n）"}, ...]

読めない行やブロックは飛ばして件数だけ数え、例外では止めない。
壊れた字幕を「開けません」で突き返すより、読めた分を直せるほうが役に立つ。

単体でも動かせる:

    python subtitle_io.py <字幕ファイル>
"""

from __future__ import annotations

import html
import json
import os
import re
import sys

import subtitle_formats as fmt


class LoadError(Exception):
    """読めなかった。メッセージはそのまま利用者に見せる。"""


SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".json")
MEDIA_EXTS = (
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".wmv", ".flv", ".ts", ".m2ts",
    ".mpg", ".mpeg", ".m4v", ".3gp", ".ogv",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff",
)

FORMAT_NAMES = {"srt": "SRT", "vtt": "WebVTT", "ass": "ASS", "json": "JSON"}


# ------------------------------------------------------------------ 時刻

# 1:02:03,456 / 01:02:03.456 / 02:03.456 / 1:02:03.45（ASS は 1/100 秒）
_TS_RE = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?")
_ARROW_RE = re.compile(
    r"((?:\d{1,2}:)?\d{1,2}:\d{1,2}(?:[.,]\d{1,3})?)\s*-->\s*"
    r"((?:\d{1,2}:)?\d{1,2}:\d{1,2}(?:[.,]\d{1,3})?)")


def parse_timestamp(text):
    """時刻の文字列を秒にする。形が合わなければ None。

    小数部は桁数で単位を決める（3 桁ならミリ秒、2 桁なら 1/100 秒、1 桁なら 1/10 秒）。
    """
    m = _TS_RE.fullmatch((text or "").strip())
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    frac = m.group(4) or ""
    sub = int(frac) / (10 ** len(frac)) if frac else 0.0
    return hours * 3600 + minutes * 60 + seconds + sub


def parse_time_input(text):
    """入力欄の時刻を秒にする。"83.5" / "1:23.5" / "0:01:23,500" のどれでも。

    合わなければ ValueError。空欄も ValueError（入力欄では必ず値が要る）。
    """
    text = (text or "").strip().replace("，", ",").replace("．", ".").replace("：", ":")
    if not text:
        raise ValueError(text)
    text = text.replace(",", ".")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(text)
    total = 0.0
    for part in parts:
        part = part.strip()
        if not part:
            part = "0"
        if not re.fullmatch(r"\d+(?:\.\d*)?|\.\d+", part):
            raise ValueError(text)
        total = total * 60 + float(part)
    return total


# ------------------------------------------------------------ 文字コード

def read_text(path):
    """(本文, 文字コード名) を返す。

    UTF-8（BOM の有無を問わず）→ UTF-16（BOM あり）→ CP932（Shift_JIS）の順に試し、
    どれでも読めなければ UTF-8 で置換読みする。古い SRT は Shift_JIS のことが多い。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise LoadError("ファイルを開けませんでした。\n\n詳細: {}".format(e))

    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "UTF-16"
        except UnicodeDecodeError:
            pass
    for name, label in (("utf-8-sig", "UTF-8"), ("cp932", "Shift_JIS")):
        try:
            return raw.decode(name), label
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "UTF-8（一部読めない文字あり）"


# ------------------------------------------------------------------ 判定

def detect_format(path, text=""):
    """拡張子で決め、迷うときは中身を見る。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".srt":
        return "srt"
    if ext == ".vtt":
        return "vtt"
    if ext in (".ass", ".ssa"):
        return "ass"
    if ext == ".json":
        return "json"
    head = (text or "")[:4000].lstrip("﻿")
    if head.startswith("WEBVTT"):
        return "vtt"
    if "[Events]" in head or "[Script Info]" in head:
        return "ass"
    if head.lstrip().startswith(("{", "[")):
        return "json"
    return "srt"


def is_subtitle_file(path):
    return os.path.splitext(path)[1].lower() in SUBTITLE_EXTS


def is_media_file(path):
    return os.path.splitext(path)[1].lower() in MEDIA_EXTS


# --------------------------------------------------------------- 読み込み

def load(path):
    """字幕ファイルを読んで dict を返す。

        {
          "cues":     [{"start","end","text"}, ...]   時刻順
          "format":   "srt" | "vtt" | "ass" | "json"
          "encoding": 文字コード名
          "skipped":  読めずに飛ばしたブロックの数
          "cleaned":  自動生成字幕の整理をかけたか
          "notes":    利用者に見せる補足（list of str）
        }
    """
    text, encoding = read_text(path)
    kind = detect_format(path, text)
    notes = []
    cleaned = False

    if kind == "srt":
        cues, skipped = parse_srt(text)
    elif kind == "vtt":
        cues, skipped, auto = parse_vtt(text)
        if auto:
            cues = tidy_auto_generated(cues)
            cleaned = True
            notes.append("自動生成字幕（重なりと時刻タグ入り）として整理しました。")
    elif kind == "ass":
        cues, skipped = parse_ass(text)
    else:
        cues, skipped = parse_json(text)

    if not cues:
        raise LoadError("字幕が 1 つも読み取れませんでした。\n"
                        "SRT / VTT / ASS / 字幕ツールの JSON のどれかを指定してください。")
    if skipped:
        notes.append("読めないブロック {} 件を飛ばしました。".format(skipped))
    if encoding != "UTF-8":
        notes.append("文字コード {} として読みました（保存は UTF-8 になります）。".format(encoding))

    return {
        "cues": normalize(cues),
        "format": kind,
        "encoding": encoding,
        "skipped": skipped,
        "cleaned": cleaned,
        "notes": notes,
    }


def normalize(cues):
    """時刻順に並べ、終了が開始以前のものを直し、空のものを落とす。"""
    out = []
    for cue in cues:
        text = _tidy_text(cue.get("text", ""))
        if not text:
            continue
        try:
            start = max(0.0, float(cue.get("start") or 0.0))
            end = float(cue.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = start + 0.2
        out.append({"start": start, "end": end, "text": text})
    out.sort(key=lambda c: (c["start"], c["end"]))
    return out


def _tidy_text(text):
    """本文の改行をそろえ、行末の空白と前後の空行を落とす。中身はいじらない。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# ------------------------------------------------------------------- SRT

_TAG_RE = re.compile(r"<[^<>]*>")             # <i> </b> <font ...> <c.xxx> <00:00:01.000>
_ASS_TAG_RE = re.compile(r"\{[^{}]*\}")        # {\an8} など SRT に紛れ込む ASS の指定


def strip_tags(text):
    text = _TAG_RE.sub("", text)
    text = _ASS_TAG_RE.sub("", text)
    return html.unescape(text)


def parse_srt(text):
    """(cues, 飛ばした数)。番号行は無くてもよく、ブロックの区切りが崩れていても拾う。"""
    return _scan_blocks(text, strip_header=False)


def _scan_blocks(text, strip_header):
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues = []
    skipped = 0
    current = None
    in_note = False
    in_header = strip_header         # VTT は先頭のヘッダ（最初の空行まで）を読み飛ばす
    in_garbage = False

    def is_blank(line):
        # VTT では「空白だけの行」は本文の一部（YouTube の自動生成字幕がそう書く）。
        # SRT では区切りと見なす（雑に編集されたファイルに空白が混じることがある）。
        return line == "" if strip_header else line.strip() == ""

    def next_is_timing(index):
        return index + 1 < len(lines) and _ARROW_RE.search(lines[index + 1]) is not None

    def close():
        nonlocal current, skipped
        if current is None:
            return
        body = _tidy_text("\n".join(strip_tags(l) for l in current["lines"]))
        if body:
            cues.append({"start": current["start"], "end": current["end"], "text": body})
        else:
            skipped += 1
        current = None

    for index, line in enumerate(lines):
        stripped = line.strip()
        blank = is_blank(line)

        if in_header:
            if blank:
                in_header = False
            continue
        if in_note:
            if blank:
                in_note = False
            continue
        if strip_header and current is None and stripped.split(" ")[0] in ("NOTE", "STYLE", "REGION"):
            in_note = True
            continue

        m = _ARROW_RE.search(line)
        if m:
            start = parse_timestamp(m.group(1))
            end = parse_timestamp(m.group(2))
            close()
            in_garbage = False
            if start is None or end is None:
                skipped += 1
                continue
            current = {"start": start, "end": end, "lines": []}
            continue

        if current is None:
            if blank:
                in_garbage = False
                continue
            if next_is_timing(index):
                continue                 # 番号行やキュー識別子
            if not in_garbage:
                skipped += 1             # 時刻の無い塊。まとめて 1 件と数える
                in_garbage = True
            continue

        if blank:
            close()
            continue
        # 空行が抜けていても、次の番号行 + 時刻行で区切る
        if stripped.isdigit() and next_is_timing(index):
            close()
            continue
        current["lines"].append(line)

    close()
    return cues, skipped


# ------------------------------------------------------------------- VTT

_INLINE_TS_RE = re.compile(r"<\d{1,2}:\d{2}(?::\d{2})?\.\d{3}>")


def parse_vtt(text):
    """(cues, 飛ばした数, 自動生成字幕らしいか)。"""
    body = text.lstrip("﻿")
    auto = len(_INLINE_TS_RE.findall(body)) >= 3 or body.count("<c>") >= 3
    cues, skipped = _scan_blocks(body, strip_header=True)
    return cues, skipped, auto


def tidy_auto_generated(cues):
    """YouTube などの自動生成字幕を、ふつうの字幕の並びに直す。

    自動生成の VTT は「前の行 + 今の行」の 2 行を重ねて出し、間に 10 ms の
    つなぎのキューを挟む。タグを外したあと、直前と重なる部分を落として空を消す。
    """
    out = []
    prev_lines = []
    for cue in cues:
        lines = [fmt._strip_fullwidth_spaces(re.sub(r"[ \t]+", " ", l)).strip()
                 for l in cue["text"].split("\n")]
        lines = [l for l in lines if l]
        # 直前の字幕と同じ行が先頭に来ていれば、それは「前の行の再掲」
        while lines and prev_lines and lines[0] == prev_lines[-1]:
            lines.pop(0)
        if lines and prev_lines and lines == prev_lines:
            lines = []
        if not lines:
            continue
        if cue["end"] - cue["start"] < 0.05:
            # つなぎのキュー。本文が新しいなら次の字幕に譲る
            continue
        out.append({"start": cue["start"], "end": cue["end"], "text": "\n".join(lines)})
        prev_lines = lines
    return out


# ------------------------------------------------------------------- ASS

_ASS_DEFAULT_FORMAT = ["Layer", "Start", "End", "Style", "Name",
                       "MarginL", "MarginR", "MarginV", "Effect", "Text"]


def parse_ass(text):
    """[Events] の Dialogue 行だけを読む。スタイルは読み捨てる。"""
    cues = []
    skipped = 0
    fields = list(_ASS_DEFAULT_FORMAT)
    in_events = False
    for raw in text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_events = line.lower() == "[events]"
            continue
        if not in_events or not line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip().lower()
        if key == "format":
            fields = [f.strip() for f in rest.split(",")]
            continue
        if key != "dialogue":
            continue
        values = rest.strip().split(",", len(fields) - 1)
        if len(values) < len(fields):
            skipped += 1
            continue
        row = dict(zip(fields, values))
        start = parse_timestamp(row.get("Start", ""))
        end = parse_timestamp(row.get("End", ""))
        if start is None or end is None:
            skipped += 1
            continue
        body = _ass_plain(row.get("Text", ""))
        if not body.strip():
            skipped += 1
            continue
        cues.append({"start": start, "end": end, "text": body})
    return cues, skipped


def _ass_plain(text):
    text = _ASS_TAG_RE.sub("", text)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return text


# ------------------------------------------------------------------ JSON

def parse_json(text):
    try:
        data = json.loads(text.lstrip("﻿"))
    except ValueError:
        raise LoadError("JSON として読み取れませんでした。")
    if isinstance(data, dict):
        segments = data.get("segments")
    else:
        segments = data
    if not isinstance(segments, list):
        raise LoadError("対応していない JSON です。\n"
                        "Whisper 字幕作成ツールが書き出した JSON を指定してください。")
    cues = []
    skipped = 0
    for seg in segments:
        if not isinstance(seg, dict) or "start" not in seg or "end" not in seg:
            skipped += 1
            continue
        cues.append({"start": seg.get("start"), "end": seg.get("end"),
                     "text": str(seg.get("text") or "")})
    return cues, skipped


# ------------------------------------------------------------------ 保存

def save(path, cues, kind, style=None):
    """kind（"srt" / "vtt" / "ass"）で書き出す。ASS は style を渡す（None なら既定）。"""
    if kind == "srt":
        return fmt.write_srt(cues, path)
    if kind == "vtt":
        return fmt.write_vtt(cues, path)
    if kind == "ass":
        return fmt.write_ass(cues, path, style)
    raise ValueError(kind)


def backup_path(path):
    """上書きの前に残す控えの名前。foo.srt → foo（編集前）.srt"""
    stem, ext = os.path.splitext(path)
    return "{}（編集前）{}".format(stem, ext)


# --------------------------------------------------------- 相棒のファイル

def find_sidecars(media_path):
    """動画と同じ場所にある、同じ名前で始まる字幕ファイルを、ふさわしい順に返す。"""
    folder = os.path.dirname(os.path.abspath(media_path))
    stem = os.path.splitext(os.path.basename(media_path))[0]
    found = []
    try:
        names = os.listdir(folder)
    except OSError:
        return found
    for name in names:
        base, ext = os.path.splitext(name)
        if ext.lower() not in SUBTITLE_EXTS:
            continue
        if base == stem or base.startswith(stem + "."):
            found.append(os.path.join(folder, name))

    def rank(p):
        """foo.srt → foo.ja.* → それ以外。同じ並びなら SRT を先に。JSON は常に最後。"""
        base, ext = os.path.splitext(os.path.basename(p))
        exact = 0 if base == stem else 1
        ja = 0 if base.endswith(".ja") else 1
        order = {".srt": 0, ".vtt": 1, ".ass": 2, ".ssa": 3, ".json": 4}.get(ext.lower(), 9)
        return (1 if ext.lower() == ".json" else 0, exact, ja, order, base)

    found.sort(key=rank)
    return found


def find_media_for(subtitle_path):
    """字幕と同じ場所にある、名前の合う動画・音声を探す。foo.ja.srt → foo.mp4 など。"""
    folder = os.path.dirname(os.path.abspath(subtitle_path))
    stem = os.path.splitext(os.path.basename(subtitle_path))[0]
    stems = [stem]
    while "." in stem:
        stem = stem.rsplit(".", 1)[0]
        stems.append(stem)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for candidate in stems:
        for name in names:
            base, ext = os.path.splitext(name)
            if base == candidate and ext.lower() in MEDIA_EXTS:
                found.append(os.path.join(folder, name))
        if found:
            break
    # 動画を先に（音声だけのファイルより、絵が出るほうが役に立つ）
    audio_only = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff")
    found.sort(key=lambda p: (os.path.splitext(p)[1].lower() in audio_only, p))
    return found


# ------------------------------------------------------------------ 単体

def main(argv):
    if len(argv) < 2:
        print("使い方: python subtitle_io.py <字幕ファイル>")
        return 2
    try:
        result = load(argv[1])
    except LoadError as e:
        print("失敗: {}".format(e))
        return 1
    print("形式: {}  文字コード: {}  字幕: {} 件  飛ばした: {}".format(
        FORMAT_NAMES[result["format"]], result["encoding"], len(result["cues"]),
        result["skipped"]))
    for note in result["notes"]:
        print("  " + note)
    for i, cue in enumerate(result["cues"][:10], start=1):
        print("{:3d}  {} --> {}  {}".format(
            i, fmt.srt_timestamp(cue["start"]), fmt.srt_timestamp(cue["end"]),
            cue["text"].replace("\n", " / ")))
    if len(result["cues"]) > 10:
        print("  ... ほか {} 件".format(len(result["cues"]) - 10))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
