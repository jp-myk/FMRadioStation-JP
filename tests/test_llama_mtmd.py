"""LlamaMtmdBackend の検証（実バイナリ不要・subprocess をモック）。

実ランタイムは llama.cpp（``llama-mtmd-cli -m <gguf> --mmproj <mmproj> --audio <wav>``）を
想定。バイナリは ``shutil.which`` で解決するため、テストでは解決をモックして有効化する。
"""
import subprocess
from unittest import mock

import numpy as np

from fm_radio_station.asr_core.asr.llama_mtmd import LlamaMtmdBackend
from fm_radio_station.asr_core.config import ASRConfig


def _completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _backend(bin_path="llama-mtmd-cli", model="m.gguf", mmproj="mm.gguf",
             language="Japanese", prompt=""):
    """バイナリ解決をモックして有効化済みの backend を作る。"""
    cfg = ASRConfig(
        asr_model="qwen3-asr-1.7b",
        llama_bin=bin_path,
        qwen_model=model,
        qwen_mmproj=mmproj,
        qwen_language=language,
        qwen_prompt=prompt,
    )
    with mock.patch("fm_radio_station.asr_core.asr.llama_mtmd.shutil.which", return_value="/usr/bin/" + bin_path):
        return LlamaMtmdBackend(cfg)


def test_command_uses_model_mmproj_audio():
    backend = _backend()
    cmd = backend._build_command("/tmp/a.wav")
    assert cmd[0] == "/usr/bin/llama-mtmd-cli"
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "m.gguf"
    assert "--mmproj" in cmd and cmd[cmd.index("--mmproj") + 1] == "mm.gguf"
    assert "--audio" in cmd and cmd[cmd.index("--audio") + 1] == "/tmp/a.wav"
    # prompt 空 → language を含む既定プロンプトが入る
    assert "-p" in cmd and "Japanese" in cmd[cmd.index("-p") + 1]


def test_command_uses_explicit_prompt():
    backend = _backend(prompt="書き起こして")
    cmd = backend._build_command("/tmp/a.wav")
    assert cmd[cmd.index("-p") + 1] == "書き起こして"


def test_disabled_when_bin_missing():
    cfg = ASRConfig(asr_model="qwen3-asr-1.7b", qwen_model="m.gguf", qwen_mmproj="mm.gguf")
    with mock.patch("fm_radio_station.asr_core.asr.llama_mtmd.shutil.which", return_value=None):
        backend = LlamaMtmdBackend(cfg)
    assert backend.available is False
    assert backend.transcribe(np.zeros(16000, dtype=np.int16), 16000) == ""


def test_disabled_when_mmproj_missing():
    cfg = ASRConfig(
        asr_model="qwen3-asr-1.7b", llama_bin="llama-mtmd-cli",
        qwen_model="m.gguf", qwen_mmproj="",
    )
    with mock.patch("fm_radio_station.asr_core.asr.llama_mtmd.shutil.which", return_value="/usr/bin/llama-mtmd-cli"):
        backend = LlamaMtmdBackend(cfg)
    assert backend.available is False  # mmproj 必須


def test_transcribe_parses_stdout_and_cleans_tempfile():
    backend = _backend()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout="こんにちは 世界\n".encode("utf-8"))

    with mock.patch("fm_radio_station.asr_core.asr.llama_mtmd.subprocess.run", side_effect=fake_run), \
         mock.patch("fm_radio_station.asr_core.asr.llama_mtmd.write_wav_file"):
        text = backend.transcribe(np.zeros(16000, dtype=np.int16), 16000)
    assert text == "こんにちは 世界"
    assert "--audio" in captured["cmd"]


def test_parse_output_strips_log_lines():
    out = "llama_model_loader: loaded\nmtmd_helper: encoding\n本日は晴天なり\n"
    assert LlamaMtmdBackend._parse_output(out) == "本日は晴天なり"


def test_parse_output_extracts_qwen_asr_text_wrapper():
    # Qwen3-ASR は `language <Lang><asr_text>本文` 形式で出力する → 本文のみ取り出す。
    out = "\nlanguage Japanese<asr_text>た方々に哀悼の意を表します。\n\n"
    assert LlamaMtmdBackend._parse_output(out) == "た方々に哀悼の意を表します。"
