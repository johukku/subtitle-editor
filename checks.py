# -*- coding: utf-8 -*-
"""字幕の点検

読みにくい字幕・壊れた字幕を見つけて、一覧と編集欄に印を出すための判定。
しきい値は設定で変えられる（DEFAULTS が初期値）。

「問題」は保存や焼き込みの前に直したほうがよいもの、
「注意」は見る人が読み切れないおそれのあるもの。
"""

from __future__ import annotations

import subtitle_formats as fmt

PROBLEM = "問題"
WARN = "注意"

DEFAULTS = {
    "max_chars": 20,       # 1 行の全角換算の文字数
    "max_lines": 2,        # 行数
    "max_cps": 7.0,        # 1 秒あたりの全角換算の文字数
    "min_duration": 0.5,   # 秒
    "max_duration": 8.0,   # 秒
}

LIMITS = {
    "max_chars": (8, 60),
    "max_lines": (1, 4),
    "max_cps": (2.0, 20.0),
    "min_duration": (0.1, 3.0),
    "max_duration": (3.0, 30.0),
}


def settings_from(data):
    """設定ファイルの値を、範囲に収めて返す。壊れていれば初期値。"""
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        lo, hi = LIMITS[key]
        try:
            value = type(default)(data.get(key, default))
        except (TypeError, ValueError, AttributeError):
            value = default
        out[key] = max(lo, min(hi, value))
    return out


def text_width(text):
    """改行を除いた本文の表示幅（全角 1.0、半角 0.5）。"""
    return sum(fmt.display_width(line) for line in (text or "").split("\n"))


def cps(cue):
    """1 秒あたりの文字数。長さが無ければ None。"""
    duration = float(cue["end"]) - float(cue["start"])
    if duration <= 0:
        return None
    return text_width(cue["text"]) / duration


def check_one(cue, prev, settings):
    """1 つの字幕を調べて [(レベル, 説明), ...] を返す。prev は 1 つ前の字幕（無ければ None）。"""
    found = []
    text = cue.get("text") or ""
    start = float(cue["start"])
    end = float(cue["end"])
    duration = end - start

    if not text.strip():
        found.append((PROBLEM, "本文が空です"))
    if end <= start:
        found.append((PROBLEM, "終了が開始より前です"))
    if prev is not None and start < float(prev["end"]) - 0.001:
        found.append((PROBLEM, "前の字幕と時間が重なっています"))

    lines = [line for line in text.split("\n") if line.strip()]
    widest = max((fmt.display_width(line) for line in lines), default=0.0)
    if widest > settings["max_chars"]:
        found.append((WARN, "1 行が長すぎます（{:.0f} 文字。{} 文字まで）".format(
            widest, settings["max_chars"])))
    if len(lines) > settings["max_lines"]:
        found.append((WARN, "{} 行あります（{} 行まで）".format(len(lines), settings["max_lines"])))

    if duration > 0 and text.strip():
        rate = text_width(text) / duration
        if rate > settings["max_cps"]:
            found.append((WARN, "読み切れないおそれ（{:.1f} 文字/秒。{:.0f} 文字/秒まで）".format(
                rate, settings["max_cps"])))
    if 0 < duration < settings["min_duration"]:
        found.append((WARN, "表示が短すぎます（{:.2f} 秒）".format(duration)))
    if duration > settings["max_duration"]:
        found.append((WARN, "表示が長すぎます（{:.1f} 秒）".format(duration)))
    return found


def check_all(cues, settings):
    """すべての字幕を調べて、字幕ごとの結果の list を返す。"""
    results = []
    prev = None
    for cue in cues:
        results.append(check_one(cue, prev, settings))
        prev = cue
    return results


def mark(result):
    """一覧に出す印。問題があれば「！」、注意だけなら「△」、無ければ空。"""
    if any(level == PROBLEM for level, _ in result):
        return "！"
    if result:
        return "△"
    return ""


def count(results):
    """(問題の数, 注意の数)。字幕単位ではなく項目単位で数える。"""
    problems = sum(1 for r in results for level, _ in r if level == PROBLEM)
    warns = sum(1 for r in results for level, _ in r if level == WARN)
    return problems, warns
