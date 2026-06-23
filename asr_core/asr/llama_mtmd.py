"""llama.cpp（llama-mtmd-cli・音声マルチモーダル）を subprocess CLI 経由で呼び出す ASR バックエンド。

確定セグメントを一時 WAV に書き出し、``llama-mtmd-cli`` を起動して stdout から認識テキストを
回収する。``ParakeetCppBackend``（asr/parakeet_cpp.py）と同じ流儀（C++ プロセス分離・graceful
disable・temp wav）で、backend 差し替えだけで Qwen3-ASR 等の llama.cpp 音声モデルを使えるようにする。

実ランタイムは ``ggml-org/llama.cpp`` の ``llama-mtmd-cli``（マルチモーダル CLI）を想定:
    llama-mtmd-cli -m <model.gguf> --mmproj <mmproj.gguf> --audio <audio.wav> -p <prompt>
``Qwen/Qwen3-ASR-1.7B`` は ``ggml-org/Qwen3-ASR-1.7B-GGUF``（本体 GGUF + 音声エンコーダ mmproj GGUF
の 2 ファイル）として配布される。本体とは別に **mmproj が必須**。CPU 動作可。入力音声は 16kHz / mono
（既存パイプラインと一致）、出力は生成テキストを stdout へ返す。

CLI フラグや出力フォーマットは ``LLAMA_MTMD_BIN`` のバイナリ実装に依存するため、``_build_command`` /
``_parse_output`` はそこに合わせて調整する。
"""
import os
import re
import shutil
import subprocess
import tempfile

import numpy as np

from asr_core.asr.backend import ASRBackend
from asr_core.config import ASRConfig
from asr_core.wav_io import write_wav_file

# llama.cpp が stdout に混ぜうる進捗/ログ行（行頭タイムスタンプや "llama_" 等の診断）を除去する。
_TIMESTAMP_PREFIX = re.compile(r"^\s*\[[0-9:.\s>\-]+\]\s*")
_LOG_LINE = re.compile(r"^\s*(llama_|ggml_|mtmd_|clip_|main:|encoding|decoding)", re.IGNORECASE)


class LlamaMtmdBackend(ASRBackend):
    def __init__(self, config: ASRConfig):
        self._bin = config.llama_bin
        self._model = config.qwen_model
        self._mmproj = config.qwen_mmproj
        self._language = config.qwen_language
        self._prompt = config.qwen_prompt
        self._timeout = config.asr_timeout_sec
        # バイナリ未ビルド／モデル未配置時は ASR を無効化して黙って続行する（起動時に一度だけ警告）。
        self._resolved_bin = self._resolve_bin(self._bin)
        if self._resolved_bin is None:
            print(
                f"[asr_core] ASR バイナリ '{self._bin}' が見つかりません。"
                " ASR（字幕生成）を無効化して続行します。"
                " LLAMA_MTMD_BIN を設定するか llama.cpp（llama-mtmd-cli）をビルドしてください。"
            )
        elif not self._model or not self._mmproj:
            print(
                "[asr_core] Qwen3-ASR の GGUF（本体／mmproj）が未配置です。"
                " ASR（字幕生成）を無効化して続行します。"
                " QWEN_ASR_MODEL / QWEN_ASR_MMPROJ か data/models を確認してください。"
            )

    @staticmethod
    def _resolve_bin(bin_path: str) -> str | None:
        """実行可能な ASR バイナリの絶対パスを返す。見つからなければ None。"""
        if not bin_path:
            return None
        return shutil.which(bin_path)

    @property
    def _enabled(self) -> bool:
        return (
            self._resolved_bin is not None
            and bool(self._model)
            and bool(self._mmproj)
        )

    @property
    def available(self) -> bool:
        return self._enabled

    def _build_command(self, wav_path: str) -> list[str]:
        """llama-mtmd-cli 推論コマンドを組み立てる。

        ``llama-mtmd-cli -m <本体> --mmproj <mmproj> --audio <wav> -p <prompt> --temp 0 -n <max>``。
        - ``-m`` / ``--mmproj``: 本体 GGUF と音声エンコーダ GGUF（mmproj は必須）。
        - ``--audio <wav>``: 音声入力（16kHz/mono）。
        - ``-p <prompt>``: 単発（非対話）実行のためのプロンプト。Qwen3-ASR は音声を文字起こしする。
          言語は Qwen 流儀（プロンプト/コンテキスト経由）。prompt が空なら language から既定文を作る。
        - ``--temp 0``: 決定的出力。``-n``: 生成上限トークン。
        """
        prompt = self._prompt
        if not prompt:
            # ASR 用の既定プロンプト。language があれば言語を明示する。
            prompt = (
                f"Transcribe the audio into {self._language}."
                if self._language
                else "Transcribe the audio."
            )
        return [
            self._resolved_bin,
            "-m", self._model,
            "--mmproj", self._mmproj,
            "--audio", wav_path,
            "-p", prompt,
            "--temp", "0",
            "-n", "256",
        ]

    @staticmethod
    def _parse_output(stdout: str) -> str:
        """CLI の標準出力から認識テキストを抽出する。

        行頭タイムスタンプ表記と llama.cpp の診断ログ行を除去して 1 本に連結し、Qwen3-ASR の
        構造化出力 ``language <Lang><asr_text>本文…`` から本文だけを取り出す（``<asr_text>`` 以降を
        採用し、残った特殊タグ ``<…>`` を除去）。素のテキスト出力にもフォールバックする。
        """
        lines = []
        for ln in stdout.splitlines():
            ln = _TIMESTAMP_PREFIX.sub("", ln).strip()
            if not ln or _LOG_LINE.match(ln):
                continue
            lines.append(ln)
        text = " ".join(lines).strip()
        m = re.search(r"<asr_text>(.*)", text, re.DOTALL)
        if m:
            text = m.group(1)
        return re.sub(r"<[^>]*>", "", text).strip()

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> str:
        # バイナリ／モデルが無ければ何もせず空テキストを返す（ログ汚染・例外を避ける）。
        if not self._enabled:
            return ""
        fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="asr_seg_")
        os.close(fd)
        try:
            write_wav_file(wav_path, samples, sample_rate)
            cmd = self._build_command(wav_path)
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout,
                check=False,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"llama-mtmd-cli failed (code {proc.returncode}): {err}"
                )
            return self._parse_output(proc.stdout.decode("utf-8", "replace"))
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
