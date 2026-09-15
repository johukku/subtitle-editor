# -*- coding: utf-8 -*-
"""字幕フォーマット出力 (SRT / VTT / ASS / JSON)

Whisper 字幕作成ツールの subtitle_formats.py の複製。ASS のコメント行だけ変えてある。
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata


# ---------------------------------------------------------------- timestamps

def _split_ms(seconds: float):
    if seconds is None or seconds < 0:
        seconds = 0.0
    total_ms = int(round(float(seconds) * 1000))
    h, total_ms = divmod(total_ms, 3_600_000)
    m, total_ms = divmod(total_ms, 60_000)
    s, ms = divmod(total_ms, 1000)
    return h, m, s, ms


def srt_timestamp(seconds: float) -> str:
    h, m, s, ms = _split_ms(seconds)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def vtt_timestamp(seconds: float) -> str:
    h, m, s, ms = _split_ms(seconds)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# ------------------------------------------------------------------ cleaning

def _clean(text: str) -> str:
    text = (text or "").strip()
    # 連続する空白を1つに（改行は保持）
    text = re.sub(r"[ \t\u3000]+", " ", text)
    # \u5168\u89d2\u6587\u5b57\u3069\u3046\u3057\u306e\u9593\u306e\u534a\u89d2\u30b9\u30da\u30fc\u30b9\u306f\u4e0d\u8981\u306a\u306e\u3067\u843d\u3068\u3059
    text = _strip_fullwidth_spaces(text)
    # \u884c\u3054\u3068\u306e\u524d\u5f8c\u7a7a\u767d\u3082\u843d\u3068\u3059\uff08\u81ea\u52d5\u6298\u308a\u8fd4\u3057\u5f8c\u306e\u898b\u6804\u3048\u7528\uff09
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()


def _strip_fullwidth_spaces(text: str) -> str:
    """\u300c\u3053\u3093\u306b\u3061\u306f\u3002 \u3053\u308c\u306f\u300d\u306e\u3088\u3046\u306a\u5168\u89d2\u3069\u3046\u3057\u306e\u9593\u306e\u534a\u89d2\u30b9\u30da\u30fc\u30b9\u3092\u53d6\u308a\u9664\u304f\u3002"""
    if " " not in text:
        return text
    chars = list(text)
    out = []
    for i, ch in enumerate(chars):
        if ch == " " and 0 < i < len(chars) - 1:
            prev, nxt = chars[i - 1], chars[i + 1]
            if (unicodedata.east_asian_width(prev) in ("W", "F")
                    and unicodedata.east_asian_width(nxt) in ("W", "F")):
                continue
        out.append(ch)
    return "".join(out)


# -------------------------------------------------------------- \u81ea\u52d5\u6298\u308a\u8fd4\u3057

# \u884c\u982d\u306b\u6765\u3066\u306f\u3044\u3051\u306a\u3044\u6587\u5b57\uff08\u53e5\u8aad\u70b9\u30fb\u9589\u3058\u62ec\u5f27\u30fb\u5c0f\u66f8\u304d\u4eee\u540d\u30fb\u9577\u97f3\u306a\u3069\uff09
_NO_LINE_START = (
    "\u3001\u3002\uff0c\uff0e,.\u30fb:\uff1a;\uff1b?\uff1f!\uff01"
    ")\uff09]\uff3d}\uff5d\u3009\u300b\u300d\u300f\u3011\u3015"
    "\u30fc\u301c\uff5e\u2026\u2025\u309d\u309e\u3005"
    "\u3041\u3043\u3045\u3047\u3049\u3063\u3083\u3085\u3087\u308e\u3095\u3096"
    "\u30a1\u30a3\u30a5\u30a7\u30a9\u30c3\u30e3\u30e5\u30e7\u30ee\u30f5\u30f6"
    "'\u2019\"\u201d"
)

# \u884c\u672b\u306b\u6765\u3066\u306f\u3044\u3051\u306a\u3044\u6587\u5b57\uff08\u958b\u304d\u62ec\u5f27\u306a\u3069\uff09
_NO_LINE_END = "([\uff5b{\u3014\u3010\u300c\u300e\u3008\u300a\uff08\uff3b'\u2018\"\u201c"


def display_width(text):
    """\u5168\u89d2\u3092 1.0\u3001\u534a\u89d2\u3092 0.5 \u3068\u3057\u3066\u6587\u5b57\u5217\u306e\u8868\u793a\u5e45\u3092\u8fd4\u3059\u3002"""
    total = 0.0
    for ch in text:
        total += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.5
    return total


def wrap_text(text, max_chars=20):
    """\u5168\u89d2\u63db\u7b97 max_chars \u5e45\u3067\u6298\u308a\u8fd4\u3057\u3066\u884c\u306e\u30ea\u30b9\u30c8\u3092\u8fd4\u3059\uff08\u7c21\u6613\u7981\u5247\u51e6\u7406\u3064\u304d\uff09\u3002"""
    text = _strip_fullwidth_spaces(re.sub(r"\s+", " ", (text or "").strip()))
    if not text:
        return []
    if max_chars <= 0 or display_width(text) <= max_chars:
        return [text]

    # 行数が同じなら幅を均等に割ったほうが読みやすいので、目標幅を先に決める
    total = display_width(text)
    n_lines = int(math.ceil(total / max_chars))
    target = min(float(max_chars), total / n_lines + 0.5)

    lines = _greedy_wrap(text, target)
    if len(lines) > n_lines:
        # 禁則処理の影響で行数が増えてしまった場合は最大幅で詰め直す
        lines = _greedy_wrap(text, float(max_chars))
    return lines or [text]


def _greedy_wrap(text, width_limit):
    """指定幅で貪欲に折り返す（禁則処理つき）。"""
    lines = []
    rest = text
    while rest:
        if display_width(rest) <= width_limit:
            lines.append(rest)
            break

        # \u307e\u305a\u5e45\u3074\u3063\u305f\u308a\u307e\u3067\u8a70\u3081\u308b
        cut = 0
        width = 0.0
        for i, ch in enumerate(rest):
            w = 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.5
            if width + w > width_limit:
                break
            width += w
            cut = i + 1
        if cut <= 0:
            cut = 1

        # \u7981\u5247: \u6b21\u884c\u306e\u5148\u982d\u306b\u6765\u3089\u308c\u306a\u3044\u6587\u5b57\u306f\u3001\u3053\u306e\u884c\u306b\u3076\u3089\u4e0b\u3052\u308b\uff082 \u6587\u5b57\u307e\u3067\uff09
        limit = cut + 2
        while cut < len(rest) and cut < limit and rest[cut] in _NO_LINE_START:
            cut += 1

        # \u7981\u5247: \u3053\u306e\u884c\u306e\u672b\u5c3e\u306b\u6765\u3089\u308c\u306a\u3044\u6587\u5b57\u306f\u3001\u6b21\u884c\u3078\u9001\u308b
        while cut > 1 and rest[cut - 1] in _NO_LINE_END:
            cut -= 1

        # \u82f1\u5358\u8a9e\u306e\u9014\u4e2d\u3067\u5207\u3089\u306a\u3044\uff08\u4e21\u5074\u304c\u82f1\u6570\u5b57\u306a\u3089\u76f4\u524d\u306e\u7a7a\u767d\u307e\u3067\u623b\u3059\uff09
        if 0 < cut < len(rest) and rest[cut - 1].isascii() and rest[cut].isascii() \
                and rest[cut - 1].isalnum() and rest[cut].isalnum():
            space = rest.rfind(" ", 0, cut)
            if space > 0:
                cut = space

        line = rest[:cut].strip()
        if line:
            lines.append(line)
        rest = rest[cut:].lstrip()

    return lines or [text]


def split_segments(segments, max_chars=20, max_lines=2):
    """\u9577\u3044\u30bb\u30b0\u30e1\u30f3\u30c8\u3092\u6298\u308a\u8fd4\u3057\u3001max_lines \u3092\u8d85\u3048\u308b\u5206\u306f\u8907\u6570\u306e\u5b57\u5e55\u306b\u5206\u5272\u3059\u308b\u3002

    \u5206\u5272\u3057\u305f\u3068\u304d\u306e\u8868\u793a\u6642\u9593\u306f\u3001\u5404\u304b\u305f\u307e\u308a\u306e\u6587\u5b57\u6570\u6bd4\u3067\u6309\u5206\u3059\u308b\u3002
    """
    if max_chars <= 0:
        return list(segments)

    out = []
    for seg in segments:
        text = _clean(seg.get("text", ""))
        if not text:
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or 0.0)

        lines = wrap_text(text, max_chars)
        if not lines:
            continue
        if len(lines) <= max_lines:
            out.append({"start": start, "end": end, "text": "\n".join(lines)})
            continue

        groups = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        weights = [max(1.0, display_width("".join(g))) for g in groups]
        total = sum(weights)
        duration = max(0.0, end - start)
        cursor = start
        for i, (group, weight) in enumerate(zip(groups, weights)):
            stop = end if i == len(groups) - 1 else min(end, cursor + duration * weight / total)
            out.append({"start": cursor, "end": stop, "text": "\n".join(group)})
            cursor = stop
    return out


def _usable_segments(segments):
    """空テキスト・逆転タイムスタンプを除去した整形済みセグメントを返す。"""
    out = []
    prev_end = 0.0
    for seg in segments:
        text = _clean(seg.get("text", ""))
        if not text:
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or 0.0)
        if start < prev_end:
            start = prev_end
        if end <= start:
            end = start + 0.2
        out.append({"start": start, "end": end, "text": text})
        prev_end = end
    return out


# ------------------------------------------------------------------- writers

def write_srt(segments, path: str) -> str:
    lines = []
    for i, seg in enumerate(_usable_segments(segments), start=1):
        lines.append(str(i))
        lines.append(f"{srt_timestamp(seg['start'])} --> {srt_timestamp(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return path


def write_vtt(segments, path: str) -> str:
    lines = ["WEBVTT", ""]
    for seg in _usable_segments(segments):
        lines.append(f"{vtt_timestamp(seg['start'])} --> {vtt_timestamp(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return path


# ASS のスタイル。値はすべて「出力解像度そのままのピクセル数」で持つ。
# 解像度に応じた実際の数値は burn_in.build_style() が組み立てる。
DEFAULT_ASS_STYLE = {
    "font": "Yu Gothic UI",
    "size": 54,
    "bold": 0,
    "primary": "&H00FFFFFF",         # 文字色（&HAABBGGRR / AA は 00 が不透明）
    "outline_colour": "&H00000000",  # フチの色。BorderStyle=4 では文字を囲む箱の色
    "back_colour": "&H73000000",     # BorderStyle=4 のときの帯の色
    "border_style": 1,               # 1=フチ取り / 4=帯（libass 拡張）
    "outline": 3.0,
    "shadow": 0.0,
    "alignment": 2,                  # テンキー配置。2 = 下中央
    "margin_l": 60,
    "margin_r": 60,
    "margin_v": 60,
    "play_res": (1920, 1080),
}

_ASS_STYLE_FORMAT = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
    "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)


def ass_timestamp(seconds: float) -> str:
    h, m, s, ms = _split_ms(seconds)
    return f"{h:d}:{m:02d}:{s:02d}.{ms // 10:02d}"


def _ass_text(text: str) -> str:
    """ASS の 1 行に埋め込める形にする。

    波括弧は書式指定の開始と解釈されて字幕が消えてしまうので、全角に逃がす。
    （文字起こしの結果に波括弧が出ることはまずないが、出たときに壊れるよりよい）
    """
    text = text.replace("{", "｛").replace("}", "｝")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\n", "\\N")


def write_ass(segments, path: str, style=None) -> str:
    """ASS 字幕を書き出す。焼き込みのときはこの形式を FFmpeg に渡す。

    SRT を直接渡すとフォントや大きさが libass 任せになるので、
    見た目を決められるこちらを経由する。
    """
    st = dict(DEFAULT_ASS_STYLE)
    if style:
        st.update(style)
    play_x, play_y = st["play_res"]

    values = [
        "Default", st["font"], _num(st["size"]),
        st["primary"], st["primary"], st["outline_colour"], st["back_colour"],
        -1 if st["bold"] else 0, 0, 0, 0,     # Bold は ASS では -1 が「太字」
        100, 100, 0, 0,
        st["border_style"], _num(st["outline"]), _num(st["shadow"]),
        st["alignment"], _num(st["margin_l"]), _num(st["margin_r"]), _num(st["margin_v"]),
        1,
    ]

    lines = [
        "[Script Info]",
        "; johukku 字幕エディター",
        "ScriptType: v4.00+",
        f"PlayResX: {int(play_x)}",
        f"PlayResY: {int(play_y)}",
        "WrapStyle: 2",            # 折り返しはこちらで済ませてあるので libass には触らせない
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: " + _ASS_STYLE_FORMAT,
        "Style: " + ",".join(str(v) for v in values),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for seg in _usable_segments(segments):
        lines.append("Dialogue: 0,{},{},Default,,0,0,0,,{}".format(
            ass_timestamp(seg["start"]), ass_timestamp(seg["end"]), _ass_text(seg["text"])))
    lines.append("")

    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("\n".join(lines))
    return path


def _num(value) -> str:
    """ASS に書く数値。整数なら小数点を付けない。"""
    value = round(float(value), 2)
    return str(int(value)) if value == int(value) else str(value)


def write_json(result: dict, path: str, source_path: str = "") -> str:
    segs = _usable_segments(result.get("segments", []))
    payload = {
        "source": os.path.basename(source_path) if source_path else "",
        "language": result.get("language", ""),
        "duration": round(float(result.get("duration") or 0.0), 3),
        "model": result.get("model", ""),
        "device": result.get("device", ""),
        "text": _clean(result.get("text", "")),
        "segments": [
            {
                "id": i,
                "start": round(s["start"], 3),
                "end": round(s["end"], 3),
                "text": s["text"],
            }
            for i, s in enumerate(segs)
        ],
    }
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


WRITERS = {
    "srt": ("srt", write_srt),
    "vtt": ("vtt", write_vtt),
    "ass": ("ass", write_ass),
    "json": ("json", write_json),
}


def build_output_path(source_path: str, out_dir: str, lang_code: str, ext: str) -> str:
    """元ファイル名 + 言語コード + 拡張子 で出力パスを組み立てる（重複時は連番）。"""
    stem = os.path.splitext(os.path.basename(source_path))[0]
    lang = (lang_code or "xx").strip() or "xx"
    base = os.path.join(out_dir, f"{stem}.{lang}")
    candidate = f"{base}.{ext}"
    n = 2
    while os.path.exists(candidate):
        candidate = f"{base}_{n}.{ext}"
        n += 1
    return candidate
