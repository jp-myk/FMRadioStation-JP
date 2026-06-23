"""parakeet（C++/ggml ランタイム）を subprocess CLI 経由で呼び出す ASR バックエンド。

確定セグメントを一時 WAV に書き出し、推論バイナリを起動して stdout から
テキストを回収する。C++ プロセスを完全分離するため、クラッシュやメモリリークが
Python 本体に波及しない。ffmpeg を subprocess で扱う既存の
``radio_core/transcoder.py`` と同じ流儀。

実ランタイムは ``mudler/parakeet.cpp`` の ``parakeet-cli`` バイナリを想定:
    parakeet-cli transcribe --model <model.gguf> --input <audio.wav> [--decoder <dec>] --lang ja
デコーダはモデルに応じて切替える（``config/asr.yaml`` の models プロファイル由来）。
``parakeet-tdt-0.6b-ja`` は TDT/CTC ハイブリッドのため ``--decoder tdt`` を明示する。
``nemotron-3.5-asr-streaming-0.6b`` は RNN-T だが parakeet-cli の ``--decoder`` は ``ctc|tdt`` しか
受け付けない（``rnnt`` は無効）ため、**``--decoder`` を省略**してデフォルトデコーダに任せる
（ランタイムが arch=rnnt から自動で rnnt_greedy を選ぶ）。``--lang ja`` で言語を固定する。
入力音声は 16kHz / mono（既存パイプラインと一致）、出力は既定で plain text を stdout へ返す。

CLI フラグや出力フォーマットは ``PARAKEET_CPP_BIN`` のバイナリ実装に依存するため、
``_build_command`` / ``_parse_output`` はそこに合わせて調整する。
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

# 行頭のタイムスタンプ表記 `[00:00:00.000 --> 00:00:02.000]`（whisper.cpp 系）を除去する。
_TIMESTAMP_PREFIX = re.compile(r"^\s*\[[0-9:.\s>\-]+\]\s*")


class ParakeetCppBackend(ASRBackend):
    def __init__(self, config: ASRConfig):
        self._bin = config.parakeet_bin
        self._model = config.parakeet_model
        self._decoder = config.parakeet_decoder
        self._language = config.parakeet_language
        self._timeout = config.asr_timeout_sec
        # バイナリが未ビルド／未配置のときは、セグメント毎に FileNotFoundError を
        # 投げてログを汚さず、ASR を無効化して黙って続行する。起動時に一度だけ警告。
        self._resolved_bin = self._resolve_bin(self._bin)
        if self._resolved_bin is None:
            print(
                f"[asr_core] ASR バイナリ '{self._bin}' が見つかりません。"
                " ASR（字幕生成）を無効化して続行します。"
                " PARAKEET_CPP_BIN を設定するか parakeet.cpp をビルドしてください。"
            )
        elif not self._model:
            print(
                "[asr_core] PARAKEET_MODEL（GGUF パス）が未設定です。"
                " ASR（字幕生成）を無効化して続行します。"
            )

    @staticmethod
    def _resolve_bin(bin_path: str) -> str | None:
        """実行可能な ASR バイナリの絶対パスを返す。見つからなければ None。

        PATH 上のコマンド名でも、直接指定された実行可能パスでも解決できる。
        """
        if not bin_path:
            return None
        return shutil.which(bin_path)

    @property
    def _enabled(self) -> bool:
        return self._resolved_bin is not None and bool(self._model)

    @property
    def available(self) -> bool:
        return self._enabled

    def _build_command(self, wav_path: str) -> list[str]:
        """parakeet-cli 推論コマンドを組み立てる。

        ``parakeet-cli transcribe --model <model.gguf> --input <wav> [--decoder <dec>] [--lang <lang>]``。
        - ``transcribe``: parakeet-cli の音声→テキスト サブコマンド（必須）。
        - ``--decoder <dec>``: 値があるときのみ付与。parakeet-cli が受け付けるのは ``ctc|tdt`` のみで、
          parakeet-tdt は ``tdt``（TDT/CTC ハイブリッド）を明示する。nemotron などの RNN-T モデルは
          decoder を空にして **省略**し、デフォルトデコーダに任せる（ランタイムが arch=rnnt から
          自動で rnnt_greedy を選ぶ。``--decoder rnnt`` は無効値で弾かれる）。
          値は ``config/asr.yaml`` のモデルプロファイル（``ASRConfig.parakeet_decoder``）由来。
        - ``--lang <lang>``: 言語を固定する（設定されている時のみ付与）。
        """
        cmd = [
            self._resolved_bin,
            "transcribe",
            "--model", self._model,
            "--input", wav_path,
        ]
        if self._decoder:
            cmd += ["--decoder", self._decoder]
        if self._language:
            cmd += ["--lang", self._language]
        return cmd

    @staticmethod
    def _parse_output(stdout: str) -> str:
        """CLI の標準出力から認識テキストを抽出する。

        行頭のタイムスタンプ表記があれば除去し、空行を捨てて 1 行に連結する。
        プレーンテキスト出力・whisper.cpp 系のタイムスタンプ付き出力の両方に対応。
        """
        lines = []
        for ln in stdout.splitlines():
            ln = _TIMESTAMP_PREFIX.sub("", ln).strip()
            if ln:
                lines.append(ln)
        return " ".join(lines).strip()

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
                    f"parakeet-cli failed (code {proc.returncode}): {err}"
                )
            return self._parse_output(proc.stdout.decode("utf-8", "replace"))
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
