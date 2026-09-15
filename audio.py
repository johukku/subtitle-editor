# -*- coding: utf-8 -*-
r"""音声の切り出し・波形・再生・コマの切り出し

動画や音声を開いたら、FFmpeg で作業用の WAV（24000 Hz・モノラル・16bit）を
%TEMP%\johukku-editor-<pid>\ に切り出し、それを

  - 波形: 10 ms ごとの min / max を array('h') で持って Canvas に描く
  - 再生: Windows の MCI（winmm.dll の waveaudio）で「ここからここまで」を鳴らす

に使う。動画そのものは再生しない。絵は「その時刻の 1 コマ」を FFmpeg で PNG にして見せる。

なぜ MCI か:
  標準ライブラリだけで、位置を指定して鳴らし、今どこかを取り、途中で止められるのは
  これしかない（winsound は位置も一時停止も無理、ffplay は位置を外から取れない）。
  WAV 限定なので、先に FFmpeg で WAV にしておく。

作業用 WAV は 1 分あたり約 2.9 MB（2 時間で約 340 MB）。終了時に消す。
"""

from __future__ import annotations

import array
import ctypes
import os
import shutil
import struct
import subprocess
import sys
import tempfile

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

RATE = 24000            # 100 bins/秒 で割り切れる（240 サンプル = 1 bin）
BINS_PER_SEC = 100
WORK_PREFIX = "johukku-editor-"


class AudioError(Exception):
    """音声まわりで失敗した。メッセージはそのまま利用者に見せる。"""


class Cancelled(Exception):
    """利用者による中断。"""


# ------------------------------------------------------------ 作業フォルダ

_workdir = None


def workdir():
    """このプロセス専用の作業フォルダ。無ければ作る。"""
    global _workdir
    if _workdir is None or not os.path.isdir(_workdir):
        _workdir = tempfile.mkdtemp(prefix=WORK_PREFIX)
    return _workdir


def cleanup():
    """終了時に作業フォルダを消す。開いたままの WAV があっても、消せる分だけ消す。"""
    global _workdir
    if _workdir:
        shutil.rmtree(_workdir, ignore_errors=True)
        _workdir = None


# ------------------------------------------------------------ WAV の切り出し

def extract_wav(ffmpeg, src, dst, duration=None, on_progress=None, should_cancel=None):
    """src の音声を dst（WAV）に切り出す。進捗は on_progress(0.0〜1.0)。"""
    if not ffmpeg:
        raise AudioError("FFmpeg が見つかりません。")
    cmd = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1",
           "-i", os.path.abspath(src),
           "-map", "0:a:0", "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le",
           "-map_metadata", "-1",           # LIST チャンクを付けない（MCI が嫌うことがある）
           os.path.abspath(dst)]
    err_path = dst + ".log"
    with open(err_path, "wb") as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=err, creationflags=NO_WINDOW)
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

    tail = _tail(err_path)
    _remove(err_path)
    if proc.returncode != 0 or not os.path.exists(dst):
        _remove(dst)
        low = tail.lower()
        if "matches no streams" in low:
            raise AudioError("このファイルには音声が入っていません。")
        raise AudioError("音声を取り出せませんでした。\n{}".format(tail))
    if on_progress:
        on_progress(1.0)
    return dst


def _parse_out_time(line):
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


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------- 波形

class Peaks:
    """10 ms ごとの min / max。描くときは列ごとにまとめる。

    ズームアウトしたときに数十万 bin を毎回なめないよう、1/10 と 1/100 に
    縮めたものも持っておく（2 時間で 72 万 bin → 7.2 万 → 7,200）。
    """

    def __init__(self, mins, maxs, bins_per_sec=BINS_PER_SEC):
        self.bps = bins_per_sec
        self.duration = len(mins) / float(bins_per_sec)
        self.levels = [(1, mins, maxs)]
        for factor in (10, 100):
            self.levels.append((factor,) + _reduce(mins, maxs, factor))

    def columns(self, t0, t1, n):
        """t0〜t1 秒を n 列に分け、各列の (下, 上) を -1.0〜1.0 で返す。"""
        if n <= 0 or t1 <= t0:
            return []
        per_col = (t1 - t0) * self.bps / n
        factor, mins, maxs = self.levels[0]
        for f, m, x in self.levels:
            if f <= max(1.0, per_col / 2.0):
                factor, mins, maxs = f, m, x
        total = len(mins)
        scale = self.bps / float(factor)
        out = []
        for i in range(n):
            b0 = int((t0 + (t1 - t0) * i / n) * scale)
            b1 = int((t0 + (t1 - t0) * (i + 1) / n) * scale)
            if b1 <= b0:
                b1 = b0 + 1
            b0 = max(0, b0)
            b1 = min(total, b1)
            if b0 >= total or b1 <= b0:
                out.append((0.0, 0.0))
                continue
            out.append((min(mins[b0:b1]) / 32768.0, max(maxs[b0:b1]) / 32768.0))
        return out


def _reduce(mins, maxs, factor):
    n = (len(mins) + factor - 1) // factor
    m2 = array.array("h", [0]) * n
    x2 = array.array("h", [0]) * n
    for i in range(n):
        lo = i * factor
        hi = min(len(mins), lo + factor)
        m2[i] = min(mins[lo:hi])
        x2[i] = max(maxs[lo:hi])
    return m2, x2


def load_peaks(wav_path, on_progress=None, should_cancel=None):
    """WAV を 1 秒ずつ読んで Peaks を作る。2 時間の音声でおよそ 5 秒。"""
    try:
        f = open(wav_path, "rb")
    except OSError as e:
        raise AudioError("音声を読めませんでした。\n\n詳細: {}".format(e))
    with f:
        rate, channels, bits, data_offset, data_size = _wav_layout(f)
        if channels != 1 or bits != 16:
            raise AudioError("作業用の音声の形式が想定と違います（{} ch / {} bit）。".format(
                channels, bits))
        f.seek(data_offset)
        bps = BINS_PER_SEC
        total_samples = data_size // 2
        n_bins = (total_samples * bps + rate - 1) // rate
        mins = array.array("h", [0]) * n_bins
        maxs = array.array("h", [0]) * n_bins
        seconds = (total_samples + rate - 1) // rate
        bin_index = 0
        for second in range(seconds):
            if should_cancel and should_cancel():
                raise Cancelled()
            raw = f.read(rate * 2)
            if not raw:
                break
            if len(raw) % 2:
                raw = raw[:-1]
            chunk = array.array("h")
            chunk.frombytes(raw)
            if sys.byteorder != "little":
                chunk.byteswap()
            count = len(chunk)
            for k in range(bps):
                s0 = k * rate // bps
                s1 = (k + 1) * rate // bps
                if s0 >= count or bin_index >= n_bins:
                    break
                part = chunk[s0:min(s1, count)]
                mins[bin_index] = min(part)
                maxs[bin_index] = max(part)
                bin_index += 1
            if on_progress and seconds and second % 20 == 0:
                on_progress(second / float(seconds))
    if bin_index < n_bins:
        mins = mins[:bin_index]
        maxs = maxs[:bin_index]
    if on_progress:
        on_progress(1.0)
    return Peaks(mins, maxs, bps)


def _wav_layout(f):
    """RIFF のチャンクをたどって (レート, ch, bit, data の位置, data の長さ) を返す。"""
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise AudioError("作業用の音声が WAV になっていません。")
    rate = channels = bits = None
    f.seek(0, os.SEEK_END)
    file_size = f.tell()
    pos = 12
    while pos + 8 <= file_size:
        f.seek(pos)
        chunk_id, size = struct.unpack("<4sI", f.read(8))
        if chunk_id == b"fmt ":
            fmt_data = f.read(16)
            _tag, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", fmt_data)
        elif chunk_id == b"data":
            data_offset = pos + 8
            data_size = size
            if data_size == 0xFFFFFFFF or data_offset + data_size > file_size:
                data_size = file_size - data_offset
            if rate is None:
                raise AudioError("作業用の音声のヘッダが壊れています。")
            return rate, channels, bits, data_offset, data_size
        pos += 8 + size + (size & 1)
    raise AudioError("作業用の音声に data チャンクがありません。")


# -------------------------------------------------------------------- 再生

class Player:
    """MCI の waveaudio で WAV を鳴らす。呼び出しはすべてメインスレッドから。"""

    def __init__(self):
        self.alias = "jsubed{}".format(os.getpid())
        self.opened = False
        self.length = 0.0
        self._winmm = None

    def _mci(self, command):
        if self._winmm is None:
            try:
                self._winmm = ctypes.windll.winmm
            except (AttributeError, OSError):
                raise AudioError("この環境では音声を再生できません（winmm が使えません）。")
        buf = ctypes.create_unicode_buffer(256)
        code = self._winmm.mciSendStringW(command, buf, 255, 0)
        if code:
            ebuf = ctypes.create_unicode_buffer(256)
            self._winmm.mciGetErrorStringW(code, ebuf, 255)
            raise AudioError("音声の再生に失敗しました（{}）。".format(ebuf.value or code))
        return buf.value

    def open(self, wav_path):
        self.close()
        self._mci('open "{}" type waveaudio alias {}'.format(wav_path, self.alias))
        self.opened = True
        self._mci("set {} time format milliseconds".format(self.alias))
        try:
            self.length = int(self._mci("status {} length".format(self.alias))) / 1000.0
        except ValueError:
            self.length = 0.0

    def close(self):
        if not self.opened:
            return
        for command in ("stop", "close"):
            try:
                self._mci("{} {}".format(command, self.alias))
            except AudioError:
                pass
        self.opened = False

    def play(self, start, end=None):
        """start 秒から鳴らす。end を渡すとそこで止まる。"""
        if not self.opened:
            return
        start = max(0.0, float(start))
        command = "play {} from {}".format(self.alias, int(round(start * 1000)))
        if end is not None and end > start:
            command += " to {}".format(int(round(float(end) * 1000)))
        self._mci(command)

    def pause(self):
        if self.opened and self.mode() == "playing":
            self._mci("pause {}".format(self.alias))

    def resume(self):
        if self.opened and self.mode() == "paused":
            self._mci("resume {}".format(self.alias))

    def stop(self):
        if self.opened:
            self._mci("stop {}".format(self.alias))

    def seek(self, seconds):
        if not self.opened:
            return
        self._mci("stop {}".format(self.alias))
        self._mci("seek {} to {}".format(self.alias, int(round(max(0.0, seconds) * 1000))))

    def position(self):
        if not self.opened:
            return 0.0
        try:
            return int(self._mci("status {} position".format(self.alias))) / 1000.0
        except (ValueError, AudioError):
            return 0.0

    def mode(self):
        if not self.opened:
            return ""
        try:
            return self._mci("status {} mode".format(self.alias))
        except AudioError:
            return ""

    def is_playing(self):
        return self.mode() == "playing"


# ----------------------------------------------------------- コマの切り出し

def grab_frame(ffmpeg, video_path, at, out_png, width=480, style=None, segments=None):
    """at 秒の 1 コマを PNG にする。style と segments を渡すと、その字幕も描く。"""
    import burn_in

    return burn_in.make_preview(ffmpeg, video_path, style, out_png, at=max(0.0, at),
                                preview_width=width, segments=segments)


# ------------------------------------------------------------------ 単体

def main(argv):
    """python audio.py <動画> で、切り出し → 波形 → 冒頭 3 秒の再生を一通り試す。"""
    import time

    import binaries

    if len(argv) < 2:
        print("使い方: python audio.py <動画・音声>")
        return 2
    if not binaries.ffmpeg_ok():
        print("FFmpeg がまだありません。")
        return 1
    wav = os.path.join(workdir(), "audio.wav")
    t = time.perf_counter()
    extract_wav(binaries.ffmpeg_path(), argv[1], wav,
                on_progress=lambda p: print("\r  切り出し {:5.1f}%".format(p * 100), end=""))
    print("\n  {:.2f} 秒  {:,} バイト".format(time.perf_counter() - t, os.path.getsize(wav)))
    t = time.perf_counter()
    peaks = load_peaks(wav)
    print("  波形 {:.2f} 秒  {:.1f} 秒分  列の例: {}".format(
        time.perf_counter() - t, peaks.duration, peaks.columns(0, 1, 4)))
    player = Player()
    player.open(wav)
    player.play(0, 3)
    for _ in range(4):
        time.sleep(0.5)
        print("  {} {:.2f}".format(player.mode(), player.position()))
    player.close()
    cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
