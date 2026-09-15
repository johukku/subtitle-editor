# -*- coding: utf-8 -*-
"""字幕の焼き込み（ハードサブ）

作った字幕を ASS に書き出し、libass 付きの FFmpeg で映像に描き込む。
文字の大きさは映像の解像度と 1 行の文字数から自動で決めるので、
横長でも縦長でも画面からはみ出さない。

エンコーダは GPU（NVENC / QSV / AMF）が使えれば使い、駄目なら
libx264 に落とす。GPU 側は FFmpeg が対応していてもドライバの版が
合わないと開けないことがあるので、本番の前に一瞬だけ試して確かめる。

Whisper 字幕作成ツールの burn_in.py の複製。違うところは 3 つだけ:
  - probe_video() が PyAV ではなく ffprobe（media_info.py）で調べる
  - 作業フォルダの接頭辞が johukku-editor-*（アンインストーラが見分けるため）
  - make_preview() が text の代わりに segments を受け取れる（選んだ字幕をそのまま描く）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import subtitle_formats as fmt

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# プレビューに描く見本の文
SAMPLE_TEXT = "ここに字幕が表示されます。\nThe quick brown fox jumps."


class BurnError(Exception):
    """焼き込みに失敗した。メッセージはそのまま利用者に見せる。"""


class Cancelled(Exception):
    """ユーザーによる中断。"""


# ------------------------------------------------------------------ フォント

# 上から順に探して、最初に見つかったものを使う。
# (ASS に書くフォント名, Windows のフォントフォルダにあるファイル名)
FONT_CANDIDATES = [
    ("Yu Gothic UI", "YuGothR.ttc"),
    ("Meiryo", "meiryo.ttc"),
    ("BIZ UDPGothic", "BIZ-UDGothicR.ttc"),
    ("Noto Sans JP", "NotoSansJP-VF.ttf"),
    ("MS Gothic", "msgothic.ttc"),
]

_font_cache = None


def default_font():
    """その PC にある日本語フォントを 1 つ選ぶ。

    ASS にはフォント名を 1 つしか書けないので、無いものを指定すると
    libass の代替選択任せになる。先に手元を見て決めておく。
    """
    global _font_cache
    if _font_cache:
        return _font_cache

    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name, filename in FONT_CANDIDATES:
        if os.path.exists(os.path.join(folder, filename)):
            _font_cache = name
            return name
    _font_cache = "sans-serif"
    return _font_cache


# ---------------------------------------------------------------- プリセット

# 数値は解像度に対する比率。実際のピクセル数は build_style() が決める。
#   size      : 文字の高さ / 映像の高さ
#   outline   : フチの太さ / 文字の高さ
#   margin_v  : 下の余白 / 映像の高さ
#   margin_h  : 左右の余白 / 映像の幅
PRESETS = [
    ("標準（白文字に黒フチ）", {
        "size": 0.050, "outline": 0.055, "margin_v": 0.055, "margin_h": 0.05,
        "bold": False, "border_style": 1,
        "primary": "&H00FFFFFF", "outline_colour": "&H00000000",
        "back_colour": "&H73000000",
    }),
    ("大きめ（太字・フチ太め）", {
        "size": 0.068, "outline": 0.060, "margin_v": 0.060, "margin_h": 0.05,
        "bold": True, "border_style": 1,
        "primary": "&H00FFFFFF", "outline_colour": "&H00000000",
        "back_colour": "&H73000000",
    }),
    ("帯つき（半透明の黒帯）", {
        "size": 0.048, "outline": 0.0, "margin_v": 0.055, "margin_h": 0.05,
        "bold": False, "border_style": 4,
        "primary": "&H00FFFFFF", "outline_colour": "&H73000000",
        "back_colour": "&H73000000",
    }),
    ("縦動画向け（少し上・太字）", {
        "size": 0.060, "outline": 0.065, "margin_v": 0.170, "margin_h": 0.06,
        "bold": True, "border_style": 1,
        "primary": "&H00FFFFFF", "outline_colour": "&H00000000",
        "back_colour": "&H73000000",
    }),
]

PRESET_LABELS = [label for label, _ in PRESETS]
DEFAULT_PRESET = PRESET_LABELS[0]

# これ以上小さくすると読めないので、はみ出しても縮めない下限
MIN_FONT_PX = 12


def preset_by_label(label):
    for name, preset in PRESETS:
        if name == label:
            return preset
    return PRESETS[0][1]


def max_line_width(segments):
    """全字幕のうち一番長い行の表示幅（全角 1.0 換算）を返す。"""
    widest = 0.0
    for seg in segments:
        for line in str(seg.get("text", "")).split("\n"):
            widest = max(widest, fmt.display_width(line))
    return widest


def build_style(preset, width, height, line_width):
    """プリセットと映像の大きさから、write_ass() に渡すスタイルを組み立てる。

    文字の大きさは「映像の高さに対する比率」と「一番長い行が横幅に収まること」の
    小さいほうを採る。縦長の動画でも画面からはみ出さないのはこのため。
    """
    width = max(16, int(width))
    height = max(16, int(height))
    margin_h = int(round(width * preset["margin_h"]))

    by_height = height * preset["size"]
    usable = max(1.0, width - margin_h * 2)
    by_width = usable / (max(1.0, line_width) + 0.6)
    size = max(MIN_FONT_PX, int(round(min(by_height, by_width))))

    return {
        "font": default_font(),
        "size": size,
        "bold": bool(preset["bold"]),
        "primary": preset["primary"],
        "outline_colour": preset["outline_colour"],
        "back_colour": preset["back_colour"],
        "border_style": preset["border_style"],
        "outline": round(size * preset["outline"], 2),
        "shadow": 0.0,
        "alignment": 2,
        "margin_l": margin_h,
        "margin_r": margin_h,
        "margin_v": int(round(height * preset["margin_v"])),
        "play_res": (width, height),
    }


# --------------------------------------------------------------- 映像の情報

def probe_video(path):
    """ffprobe で映像の大きさ・長さ・音声コーデックを調べる。

    字幕ツールの版は PyAV を使っていた。このツールは PyAV を持たないので、
    コンバーターから持ってきた media_info（ffprobe）で同じ形の dict を作る。
    大きさは回転を反映した見た目のもの（FFmpeg は復号のときに回転を適用するので、
    字幕フィルタが見るのはこちら）。
    has_video が False なら焼き込みの対象にしない（音声だけのファイル）。
    """
    info = {"width": 0, "height": 0, "duration": None,
            "audio_codec": None, "has_video": False}
    try:
        import media_info

        data = media_info.probe(path)
        info["duration"] = data.get("duration")
        if data.get("video"):
            width, height = media_info.display_size(data)
            info["width"] = int(width or 0)
            info["height"] = int(height or 0)
            info["has_video"] = info["width"] > 0 and info["height"] > 0
        if data.get("audio"):
            info["audio_codec"] = data["audio"].get("codec")
    except Exception:
        pass
    return info


# ----------------------------------------------------------- エンコーダ選び

# 上から順に試す。GPU が使えると 1080p でおおむね実時間の数倍で焼ける。
HW_ENCODERS = [
    ("h264_nvenc", ["-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"]),
    ("h264_qsv", ["-preset", "medium", "-global_quality", "23"]),
    ("h264_amf", ["-quality", "balanced", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]),
]
SW_ENCODER = ("libx264", ["-preset", "veryfast", "-crf", "20"])

_encoder_cache = {}


def pick_encoder(ffmpeg, use_gpu=True):
    """使えるエンコーダを (名前, 追加引数) で返す。

    GPU のエンコーダは「FFmpeg が対応している」だけでは足りない。
    ドライバが古いと開く段階で失敗するので、1 コマだけ試して確かめる。
    """
    if not use_gpu:
        return SW_ENCODER

    key = os.path.abspath(ffmpeg)
    if key in _encoder_cache:
        return _encoder_cache[key]

    chosen = SW_ENCODER
    for name, args in HW_ENCODERS:
        if _encoder_works(ffmpeg, name):
            chosen = (name, args)
            break
    _encoder_cache[key] = chosen
    return chosen


def _encoder_works(ffmpeg, name):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=black:s=128x128:r=10:d=0.2",
           "-c:v", name, "-frames:v", "1", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30,
                              creationflags=NO_WINDOW)
        return proc.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------- 音声の扱い

# MP4 にそのまま入れられる音声。これ以外は AAC に変換する
# （Opus や Vorbis は MP4 に入れても再生できない機器があるため）。
MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}


def _audio_args(audio_codec, container):
    if not audio_codec:
        return ["-an"]
    if container == "mkv" or audio_codec in MP4_SAFE_AUDIO:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k"]


def output_extension(source_path):
    """MKV はそのまま、それ以外は MP4 で出す。"""
    return ".mkv" if os.path.splitext(source_path)[1].lower() == ".mkv" else ".mp4"


# ----------------------------------------------------------------- 焼き込み

def burn(ffmpeg, video_path, segments, out_path, style,
         duration=None, audio_codec=None, use_gpu=True,
         on_progress=None, should_cancel=None, log=None):
    """segments を video_path に焼き込んで out_path に書き出す。

    GPU で始めて失敗した場合は libx264 でやり直す。
    """
    if not ffmpeg:
        raise BurnError("FFmpeg が見つかりません。")

    container = "mkv" if out_path.lower().endswith(".mkv") else "mp4"

    # 字幕は ASCII 名で作業フォルダに置き、そこを作業ディレクトリにして相対名で渡す。
    # フィルタの引数はコロンや円記号の扱いが面倒なので、そもそも通さない。
    workdir = tempfile.mkdtemp(prefix="johukku-editor-burn-")
    ass_path = os.path.join(workdir, "sub.ass")
    err_path = os.path.join(workdir, "ffmpeg.log")
    try:
        fmt.write_ass(segments, ass_path, style)

        encoder, enc_args = pick_encoder(ffmpeg, use_gpu)
        if log:
            log("エンコーダ: {}".format(encoder))

        attempts = [(encoder, enc_args)]
        if encoder != SW_ENCODER[0]:
            attempts.append(SW_ENCODER)

        last_error = ""
        for index, (name, args) in enumerate(attempts):
            if index > 0 and log:
                log("GPU でのエンコードに失敗したので CPU でやり直します。")
            cmd = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-nostats", "-progress", "pipe:1",
                   "-i", os.path.abspath(video_path),
                   "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                   "-vf", "subtitles=sub.ass",
                   "-c:v", name]
            cmd += args
            cmd += ["-pix_fmt", "yuv420p"]
            cmd += _audio_args(audio_codec, container)
            if container == "mp4":
                cmd += ["-movflags", "+faststart"]
            cmd.append(os.path.abspath(out_path))

            try:
                ok, last_error = _run(cmd, workdir, err_path, duration,
                                      on_progress, should_cancel)
            except Cancelled:
                _discard(out_path)     # 途中まで書けた動画は残さない
                raise
            if ok:
                return out_path

        _discard(out_path)
        raise BurnError("字幕の焼き込みに失敗しました。\n{}".format(last_error))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _discard(path):
    """中断や失敗で途中まで書けた出力を消す。"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _run(cmd, workdir, err_path, duration, on_progress, should_cancel):
    """FFmpeg を回して (成功したか, エラー文) を返す。"""
    with open(err_path, "wb") as err:
        proc = subprocess.Popen(cmd, cwd=workdir, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=err,
                                creationflags=NO_WINDOW)
        try:
            for raw in proc.stdout:
                if should_cancel and should_cancel():
                    _stop(proc)
                    raise Cancelled()
                if not (on_progress and duration and duration > 0):
                    continue
                seconds = _parse_out_time(raw.decode("utf-8", "replace"))
                if seconds is not None:
                    on_progress(max(0.0, min(1.0, seconds / duration)))
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            proc.wait()

    if proc.returncode == 0:
        if on_progress:
            on_progress(1.0)
        return True, ""
    return False, _tail(err_path)


def _parse_out_time(line):
    """-progress の出力から現在位置（秒）を取り出す。

    out_time_ms という名前なのに中身はマイクロ秒、という FFmpeg 側の
    古い癖があるので、どちらもマイクロ秒として扱う。
    """
    line = line.strip()
    for key in ("out_time_us=", "out_time_ms="):
        if line.startswith(key):
            try:
                return int(line[len(key):]) / 1000000.0
            except ValueError:
                return None
    return None


def _stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _tail(path, limit=600):
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace").strip()
        return text[-limit:] if text else "（FFmpeg からの詳細はありません）"
    except OSError:
        return "（FFmpeg からの詳細はありません）"


# --------------------------------------------------------------- プレビュー

def make_preview(ffmpeg, video_path, style, out_png, at=None, text=SAMPLE_TEXT,
                 preview_width=0, segments=None):
    """動画の 1 コマに字幕を描いて PNG に書き出す。

    text を渡すと、その文を見本として描く（字幕ツールの「見本を見る」）。
    segments を渡すと、その字幕をそのままの時刻で描く。at がその字幕の区間なら
    画面に出るし、区間外なら字幕なしのコマになる（エディターの「絵」はこちら）。
    style が None なら字幕を描かず、コマだけを切り出す。

    preview_width を指定すると、字幕を描いたあとにその幅まで縮小する。
    本番と同じ大きさで描いてから縮めるので、見え方は焼き上がりと同じになる。
    """
    if not ffmpeg:
        raise BurnError("FFmpeg が見つかりません。")

    workdir = tempfile.mkdtemp(prefix="johukku-editor-preview-")
    ass_path = os.path.join(workdir, "sub.ass")
    err_path = os.path.join(workdir, "ffmpeg.log")
    try:
        chain = ""
        if style is not None:
            if segments is None:
                segments = [{"start": 0.0, "end": 10.0, "text": text}]
            elif at and at > 0:
                # -ss で頭出しすると FFmpeg は時刻を 0 から数え直すので、
                # 字幕のほうも同じだけ前にずらして、そのコマに出る字幕が出るようにする
                segments = [{"start": max(0.0, s["start"] - at), "end": max(0.05, s["end"] - at),
                             "text": s["text"]} for s in segments if s["end"] > at]
            fmt.write_ass(segments, ass_path, style)
            chain = "subtitles=sub.ass"
        if preview_width and 0 < preview_width < (style["play_res"][0] if style else 1 << 30):
            chain += ("," if chain else "") + "scale={}:-2:flags=bilinear".format(int(preview_width))
        if not chain:
            chain = "null"
        cmd = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
        if at and at > 0:
            cmd += ["-ss", "{:.3f}".format(at)]
        cmd += ["-i", os.path.abspath(video_path),
                "-vf", chain,
                "-frames:v", "1", "-update", "1",
                os.path.abspath(out_png)]
        with open(err_path, "wb") as err:
            proc = subprocess.run(cmd, cwd=workdir, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=err,
                                  creationflags=NO_WINDOW)
        if proc.returncode != 0 or not os.path.exists(out_png):
            raise BurnError("プレビューを作れませんでした。\n{}".format(_tail(err_path)))
        return out_png
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
