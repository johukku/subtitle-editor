# 字幕エディター

字幕ファイル（SRT / VTT / ASS）を、**音を聞きながら直して**、保存するか動画に焼き込む Windows 用の GUI ツールです。
Whisper 字幕作成ツールの出力や、動画サイトの自動生成字幕を、公開できる形に仕上げる用途を想定しています。
音声・映像の処理は [FFmpeg](https://ffmpeg.org/) が行い、**初回起動時に自動で取得します**（同梱していません）。

**[→ ダウンロード（最新版）](https://github.com/johukku/subtitle-editor/releases/latest)**　|　
**[→ 使い方](https://johukku.pages.dev/editor/)**

![スクリーンショット](https://johukku.pages.dev/assets/editor.png)

---

## 特徴

- **その字幕の区間だけ聞き直せる** — 一覧で選んで Ctrl+Enter。波形と、その時刻の 1 コマ（字幕を重ねた状態）も見えます
- **時刻は 3 通りで直せる** — 入力欄、「◀今」（いまの再生位置を入れる。キーは `[` と `]`）、波形の帯のドラッグ
- **読みにくい字幕に印が付く** — 1 行が長い、1 秒あたりの文字数が多い、時間が重なっている、など。F3 で次の警告へ
- **自動生成字幕を整理して読み込む** — YouTube の自動生成 VTT に特有の「前の行の再掲」と時刻タグを外して、ふつうの字幕の並びにします
- **分割・結合・挿入・削除・全体をずらす・検索と置換・自動で折り返す** — 元に戻すは 100 段
- **焼き込みは字幕ツールと同じ見た目** — 4 種のスタイル、GPU の自動判定、縦動画でもはみ出さない文字の大きさ
- **Python のインストール不要** — exe をダブルクリックするだけ
- **元のファイルを黙って書き換えない** — 最初の上書きのときだけ「（編集前）」という控えを残します
- **部品を共有** — 他の johukku 製ツールと同じ場所に FFmpeg を置くため、2 本目からは取得を省けます

## 動作環境

| | |
|---|---|
| OS | Windows 10 / 11（64bit） |
| 必要なもの | インターネット接続（初回のみ） |
| 空き容量 | 約 300 MB（FFmpeg を置くため） |

FFmpeg が無くても、字幕の編集と保存はできます。

## 使い方

1. [Releases](https://github.com/johukku/subtitle-editor/releases/latest) から ZIP を取得して展開
2. `字幕エディター.exe` をダブルクリック
3. 初回だけ、部品（FFmpeg）の取得を確認されるので「はい」を押す（数分かかります）
4. 「字幕を開く...」で字幕ファイルを開く（同じ場所に同じ名前の動画があれば、一緒に開くか聞かれます）
5. 一覧から選んで直し、「保存」（Ctrl+S）

詳しい手順は[使い方のページ](https://johukku.pages.dev/editor/usage/)を参照してください。

### 読めるもの・書けるもの

| | 形式 |
|---|---|
| 読み込み | SRT / WebVTT / ASS（`[Events]` の本文と時刻だけ。スタイルは読み捨てます）/ Whisper 字幕作成ツールの JSON |
| 保存 | SRT / WebVTT / ASS（焼き込みと同じスタイルで書き出します） |
| 焼き込み | MP4（元が MKV なら MKV） |

文字コードは UTF-8 → UTF-16 → Shift_JIS の順に判定します。保存は UTF-8 です。

### 主なキー操作

| キー | 動作 |
|---|---|
| Space | 再生 / 停止（本文の入力中は Ctrl+Space） |
| Ctrl+Enter | 選んでいる字幕の区間だけ再生 |
| Ctrl+↓ / Ctrl+↑ | 次 / 前の字幕 |
| `[` / `]` | 開始 / 終了をいまの再生位置に |
| Ctrl+D / Ctrl+J | 本文のカーソル位置で分割 / 次の字幕と結合 |
| Ctrl+Z / Ctrl+Y | 元に戻す / やり直す |
| Ctrl+H | 検索・置換 |
| F3 | 次の警告へ |
| Alt+← / Alt+→ | 3 秒戻る / 進む |

波形はホイールで移動、Ctrl+ホイールで拡大縮小。上の帯はドラッグで時刻を動かせます（端で長さ、真ん中で位置）。

## 動画を再生しない理由

このツールは音だけを再生し、絵は「その時刻の 1 コマ」を切り出して見せます。
字幕を直す作業に要るのは「いま何と言っているか」を聞くことで、動画の同期再生はなくても困りません。
その代わり、標準ライブラリと FFmpeg だけで動き、動画の形式（MKV / VP9 / AV1 など）を選びません。

音の再生には Windows の MCI（`winmm`）を使い、開いた動画から作業用の WAV（24 kHz・モノラル）を切り出して鳴らします。
作業用の WAV は `%TEMP%\johukku-editor-*` に置き、終了時に消します（2 時間の動画で約 340 MB）。

## FFmpeg について

FFmpeg は同梱せず、[yt-dlp のビルド](https://github.com/yt-dlp/FFmpeg-Builds)から自動で取得し、
`%LOCALAPPDATA%\johukku\bin` に置きます。

- FFmpeg は GPL のため、同梱して配布するとソース提供の義務が生じます。
  利用者の PC が公式の配布元から直接取得する形なら、こちらは何も再配布しません
- 他の johukku 製ツールと同じ場所を使うので、すでにあれば取得は省かれます
- 字幕の描画には、この FFmpeg に含まれる libass を使います

## 開発

```
python editor_app.py [字幕ファイル または 動画]    # GUI
python subtitle_io.py <字幕ファイル>               # 読み込みだけを試す
python audio.py <動画>                             # 音声の切り出し・波形・再生を試す
python -m unittest discover -s tests -v            # 読み込みと点検の試験
python _build\make_exe.py                          # 配布用の exe と ZIP を作る
```

実行時の依存ライブラリはありません（標準ライブラリと tkinter のみ）。
ビルドには PyInstaller が要ります（入っていなければ自動で入れます）。

| ファイル | 役割 |
|---|---|
| `editor_app.py` | 画面（tkinter）。一覧・編集欄・波形・再生・焼き込みの取りまとめ |
| `subtitle_io.py` | SRT / VTT / ASS / JSON の読み込み、文字コードの判定、自動生成字幕の整理 |
| `subtitle_formats.py` | SRT / VTT / ASS の書き出し、折り返し、禁則（Whisper 字幕作成ツールの複製） |
| `checks.py` | 字幕の点検（長さ・速さ・重なりなど） |
| `audio.py` | 作業用 WAV の切り出し、波形、MCI での再生、コマの切り出し |
| `burn_in.py` | 焼き込みのスタイル決定・エンコーダ選択・FFmpeg の実行（字幕ツールの複製。動画の情報は ffprobe で取る） |
| `media_info.py` | ffprobe で動画の中身を調べる（メディアコンバーターの複製） |
| `binaries.py` | FFmpeg の取得と共有フォルダの管理（メディアコンバーターの複製） |
| `アンインストール.bat` / `uninstall.ps1` | ツールの削除（共有しているものは 1 つずつ確認） |
| `_build/make_exe.py` | 開発者用：exe と配布 ZIP の生成 |

## アンインストール

`アンインストール.bat` を実行してください。配布物と設定を削除します。
他のツールと共有している FFmpeg は、消すかどうかを確認します。

## ライセンス

MIT License（[LICENSE](LICENSE)）

内部で利用しているソフトウェア（同梱はしていません）:

- FFmpeg — GPL v3 / https://ffmpeg.org/（libass — ISC — を含む）

## 関連

- [Whisper 字幕作成ツール](https://github.com/johukku/whisper-subtitle-tool) — 動画から字幕を作る。その出力をこのツールで直せます
- [メディアダウンローダー](https://github.com/johukku/media-downloader) — URL から動画・音声・字幕を保存する
- [メディアコンバーター](https://github.com/johukku/media-converter) — 動画・音声を変換する
