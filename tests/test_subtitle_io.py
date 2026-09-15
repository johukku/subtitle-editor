# -*- coding: utf-8 -*-
"""subtitle_io の読み込みの試験。

    python -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import checks  # noqa: E402
import subtitle_io as sio  # noqa: E402


SRT_STANDARD = (
    "\ufeff1\r\n00:00:01,000 --> 00:00:03,500\r\n<i>こんにちは。</i>\r\n今日は{\\an8}晴れです。\r\n\r\n"
    "2\r\n00:00:04,000 --> 00:00:06,000\r\n<font color=\"#ff0000\">二つ目</font>\r\n\r\n"
    "3\r\n00:00:07.250 --> 00:00:08.000\r\n三つ目\r\n"
)

SRT_NO_BLANK = (
    "1\n00:00:01,000 --> 00:00:02,000\nひとつ\n"
    "2\n00:00:02,000 --> 00:00:03,000\nふたつ\n"
    "3\n00:00:03,000 --> 00:00:04,000\nみっつ\n"
)

SRT_BROKEN = (
    "1\n00:00:01,000 --> 00:00:02,000\nよい\n\n"
    "2\nこれは時刻ではない --> ほんとに\nわるい\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\n\n\n"
    "4\n00:00:09,000 --> 00:00:08,000\n逆転\n"
)

VTT_STANDARD = (
    "WEBVTT - タイトル\n\n"
    "NOTE これはメモ\n続きのメモ\n\n"
    "STYLE\n::cue { color: red }\n\n"
    "intro\n00:01.000 --> 00:03.000 align:start position:10%\n<v 太郎>こんにちは &amp; さようなら\n\n"
    "00:00:04.000 --> 00:00:05.000\n<c.yellow>色つき</c> <b>太字</b>\n"
)

VTT_YOUTUBE = (
    "WEBVTT\nKind: captions\nLanguage: ja\n\n"
    "00:00:00.000 --> 00:00:02.310 align:start position:0%\n \nこんにちは<00:00:00.480><c> 今日は</c><00:00:01.200><c> 字幕の</c>\n\n"
    "00:00:02.310 --> 00:00:02.320 align:start position:0%\nこんにちは 今日は 字幕の\n \n\n"
    "00:00:02.320 --> 00:00:04.150 align:start position:0%\nこんにちは 今日は 字幕の\n話を<00:00:02.800><c> します</c>\n\n"
    "00:00:04.150 --> 00:00:04.160 align:start position:0%\n話を します\n \n\n"
    "00:00:04.160 --> 00:00:06.000 align:start position:0%\n話を します\nよろしく<00:00:05.000><c> お願いします</c>\n\n"
)

ASS_STANDARD = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n"
    "[V4+ Styles]\nFormat: Name, Fontname, Fontsize\nStyle: Default,Yu Gothic UI,54\n\n"
    "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,{\\pos(10,20)}一行目\\N二行目, カンマ入り\n"
    "Comment: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,コメントは読まない\n"
    "Dialogue: 0,0:00:06.25,0:00:07.00,Default,,0,0,0,,{\\an8}上に出す\\h字幕\n"
)

JSON_WHISPER = (
    '{"source": "a.mp4", "language": "ja", "segments": ['
    '{"id": 0, "start": 1.0, "end": 2.5, "text": "最初"},'
    '{"id": 1, "start": 2.5, "end": 4.0, "text": "次"},'
    '{"id": 2, "text": "壊れている"}]}'
)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jsubtest-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text, encoding="utf-8"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding=encoding, newline="") as f:
            f.write(text)
        return path


class TestTimestamps(unittest.TestCase):
    def test_parse_timestamp(self):
        self.assertAlmostEqual(sio.parse_timestamp("00:00:01,500"), 1.5)
        self.assertAlmostEqual(sio.parse_timestamp("01:02:03.456"), 3723.456)
        self.assertAlmostEqual(sio.parse_timestamp("02:03.400"), 123.4)
        self.assertAlmostEqual(sio.parse_timestamp("0:00:06.25"), 6.25)   # ASS の 1/100 秒
        self.assertIsNone(sio.parse_timestamp("abc"))

    def test_parse_time_input(self):
        self.assertAlmostEqual(sio.parse_time_input("83.5"), 83.5)
        self.assertAlmostEqual(sio.parse_time_input("1:23.5"), 83.5)
        self.assertAlmostEqual(sio.parse_time_input("0:01:23,500"), 83.5)
        self.assertAlmostEqual(sio.parse_time_input("１：２３．５"), 83.5)
        with self.assertRaises(ValueError):
            sio.parse_time_input("")
        with self.assertRaises(ValueError):
            sio.parse_time_input("1:2:3:4")
        with self.assertRaises(ValueError):
            sio.parse_time_input("abc")


class TestSrt(Base):
    def test_standard(self):
        result = sio.load(self.write("a.srt", SRT_STANDARD, "utf-8-sig"))
        self.assertEqual(result["format"], "srt")
        self.assertEqual(result["encoding"], "UTF-8")
        cues = result["cues"]
        self.assertEqual(len(cues), 3)
        self.assertAlmostEqual(cues[0]["start"], 1.0)
        self.assertAlmostEqual(cues[0]["end"], 3.5)
        self.assertEqual(cues[0]["text"], "こんにちは。\n今日は晴れです。")
        self.assertEqual(cues[1]["text"], "二つ目")
        self.assertAlmostEqual(cues[2]["start"], 7.25)

    def test_no_blank_lines(self):
        cues = sio.load(self.write("b.srt", SRT_NO_BLANK))["cues"]
        self.assertEqual([c["text"] for c in cues], ["ひとつ", "ふたつ", "みっつ"])

    def test_broken_blocks(self):
        result = sio.load(self.write("c.srt", SRT_BROKEN))
        self.assertEqual([c["text"] for c in result["cues"]], ["よい", "逆転"])
        self.assertEqual(result["skipped"], 2)
        self.assertAlmostEqual(result["cues"][1]["end"], 9.2)   # 逆転は start + 0.2

    def test_shift_jis(self):
        path = self.write("d.srt", "1\n00:00:01,000 --> 00:00:02,000\n日本語の字幕\n", "cp932")
        result = sio.load(path)
        self.assertEqual(result["encoding"], "Shift_JIS")
        self.assertEqual(result["cues"][0]["text"], "日本語の字幕")
        self.assertTrue(any("Shift_JIS" in n for n in result["notes"]))

    def test_empty_file(self):
        with self.assertRaises(sio.LoadError):
            sio.load(self.write("e.srt", "\n\n"))


class TestVtt(Base):
    def test_standard(self):
        result = sio.load(self.write("a.vtt", VTT_STANDARD))
        self.assertEqual(result["format"], "vtt")
        self.assertFalse(result["cleaned"])
        cues = result["cues"]
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0]["start"], 1.0)
        self.assertEqual(cues[0]["text"], "こんにちは & さようなら")
        self.assertEqual(cues[1]["text"], "色つき 太字")

    def test_youtube_auto(self):
        result = sio.load(self.write("b.vtt", VTT_YOUTUBE))
        self.assertTrue(result["cleaned"])
        cues = result["cues"]
        self.assertEqual([c["text"] for c in cues],
                         ["こんにちは今日は字幕の", "話をします", "よろしくお願いします"])
        self.assertAlmostEqual(cues[0]["start"], 0.0)
        self.assertAlmostEqual(cues[0]["end"], 2.31)
        self.assertAlmostEqual(cues[1]["start"], 2.32)
        self.assertAlmostEqual(cues[2]["end"], 6.0)


class TestAss(Base):
    def test_standard(self):
        result = sio.load(self.write("a.ass", ASS_STANDARD, "utf-8-sig"))
        self.assertEqual(result["format"], "ass")
        cues = result["cues"]
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "一行目\n二行目, カンマ入り")
        self.assertAlmostEqual(cues[0]["end"], 3.5)
        self.assertEqual(cues[1]["text"], "上に出す 字幕")
        self.assertAlmostEqual(cues[1]["start"], 6.25)


class TestJson(Base):
    def test_whisper(self):
        result = sio.load(self.write("a.json", JSON_WHISPER))
        self.assertEqual(result["format"], "json")
        self.assertEqual([c["text"] for c in result["cues"]], ["最初", "次"])
        self.assertEqual(result["skipped"], 1)

    def test_other_json(self):
        with self.assertRaises(sio.LoadError):
            sio.load(self.write("b.json", '{"hello": 1}'))


class TestRoundTrip(Base):
    def test_srt_vtt_ass(self):
        source = sio.load(self.write("a.srt", SRT_STANDARD, "utf-8-sig"))["cues"]
        for kind in ("srt", "vtt", "ass"):
            path = os.path.join(self.dir, "out." + kind)
            sio.save(path, source, kind)
            again = sio.load(path)["cues"]
            self.assertEqual(len(again), len(source), kind)
            for a, b in zip(source, again):
                self.assertAlmostEqual(a["start"], b["start"], places=2, msg=kind)
                self.assertAlmostEqual(a["end"], b["end"], places=2, msg=kind)
                self.assertEqual(a["text"], b["text"], kind)

    def test_backup_path(self):
        self.assertEqual(sio.backup_path(r"C:\x\foo.ja.srt"), r"C:\x\foo.ja（編集前）.srt")


class TestSidecars(Base):
    def test_find(self):
        for name in ("movie.mp4", "movie.srt", "movie.ja.srt", "movie.en.vtt",
                     "movie.json", "other.srt", "movie2.srt", "clip.ja.srt", "clip.mkv"):
            self.write(name, "x")
        subs = [os.path.basename(p) for p in sio.find_sidecars(os.path.join(self.dir, "movie.mp4"))]
        self.assertEqual(subs, ["movie.srt", "movie.ja.srt", "movie.en.vtt", "movie.json"])
        media = [os.path.basename(p) for p in sio.find_media_for(os.path.join(self.dir, "clip.ja.srt"))]
        self.assertEqual(media, ["clip.mkv"])
        media = [os.path.basename(p) for p in sio.find_media_for(os.path.join(self.dir, "movie.en.vtt"))]
        self.assertEqual(media, ["movie.mp4"])
        self.assertEqual(sio.find_media_for(os.path.join(self.dir, "other.srt")), [])


class TestChecks(unittest.TestCase):
    def test_rules(self):
        s = checks.settings_from({})
        good = {"start": 0.0, "end": 3.0, "text": "ふつうの字幕"}
        self.assertEqual(checks.check_one(good, None, s), [])

        long_line = {"start": 0.0, "end": 3.0, "text": "あ" * 21}
        self.assertEqual([lv for lv, _ in checks.check_one(long_line, None, s)], [checks.WARN])

        fast = {"start": 0.0, "end": 1.0, "text": "あいうえおかきくけこ"}   # 10 文字/秒
        self.assertTrue(any("読み切れない" in m for _, m in checks.check_one(fast, None, s)))

        overlap = {"start": 2.0, "end": 4.0, "text": "次"}
        self.assertEqual(checks.check_one(overlap, good, s)[0][0], checks.PROBLEM)

        reversed_cue = {"start": 5.0, "end": 4.0, "text": "逆"}
        self.assertTrue(any("終了が開始" in m for _, m in checks.check_one(reversed_cue, None, s)))

        empty = {"start": 0.0, "end": 1.0, "text": "  "}
        self.assertEqual(checks.mark(checks.check_one(empty, None, s)), "！")
        self.assertEqual(checks.mark(checks.check_one(long_line, None, s)), "△")
        self.assertEqual(checks.mark([]), "")

    def test_settings_range(self):
        s = checks.settings_from({"max_chars": 999, "max_cps": "abc", "min_duration": -1})
        self.assertEqual(s["max_chars"], 60)
        self.assertEqual(s["max_cps"], 7.0)
        self.assertEqual(s["min_duration"], 0.1)


if __name__ == "__main__":
    unittest.main()
