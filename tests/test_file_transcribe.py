"""file_transcribe（録音ファイル → WebVTT バッチ）の検証。

torch / silero / 実バイナリは不要。発話区間検出（_detect_speech）と parakeet-cli
バックエンドをモックし、WebVTT 整形・区間→キュー変換・起動ガードを確認する。
"""
import os
import tempfile
from unittest import mock

import numpy as np

from fm_radio_station.asr_core import file_transcribe as ft
from fm_radio_station.asr_core.config import ASRConfig


def test_fmt_ts_webvtt_format():
    assert ft._fmt_ts(0) == "00:00:00.000"
    assert ft._fmt_ts(1.5) == "00:00:01.500"
    assert ft._fmt_ts(3661.25) == "01:01:01.250"
    assert ft._fmt_ts(-3) == "00:00:00.000"


def test_write_vtt_structure():
    cues = [
        (0.0, 2.0, " こんにちは "),
        (2.5, 4.0, "世界"),
        (4.0, 5.0, "   "),          # 空白のみは出力しない
        (6.0, 6.0, "末尾"),          # end<=start は +0.5s 補正
    ]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.vtt")
        ft._write_vtt(cues, path)
        body = open(path, encoding="utf-8").read()
    assert body.startswith("WEBVTT\n")
    assert "00:00:00.000 --> 00:00:02.000\nこんにちは" in body
    assert "00:00:02.500 --> 00:00:04.000\n世界" in body
    assert "00:00:06.000 --> 00:00:06.500\n末尾" in body
    assert body.count("-->") == 3  # 空白セグメントは除外


def _fake_backend(available=True, transcribe=None):
    """build_backend を差し替えるためのフェイク backend を返すファクトリ。"""
    be = mock.Mock()
    be.available = available
    if transcribe is not None:
        be.transcribe.side_effect = transcribe
    return lambda config: be


def test_transcribe_raises_when_backend_unavailable():
    cfg = ASRConfig(parakeet_bin="nope", parakeet_model="m.gguf")
    with mock.patch("fm_radio_station.asr_core.file_transcribe.build_backend", _fake_backend(available=False)):
        try:
            ft.transcribe_wav_to_vtt("/tmp/x.wav", "/tmp/x.vtt", cfg)
        except RuntimeError as e:
            assert "利用できません" in str(e)
        else:
            raise AssertionError("RuntimeError が送出されるべき")


def test_available_requires_backend_and_vad_onnx():
    cfg = ASRConfig(parakeet_bin="parakeet-cli", parakeet_model="m.gguf")
    # backend 利用可 ＋ vad onnx 実在 → True
    with mock.patch("fm_radio_station.asr_core.file_transcribe.build_backend", _fake_backend(available=True)), \
         mock.patch("fm_radio_station.asr_core.file_transcribe.os.path.exists", return_value=True):
        assert ft.asr_batch_available(cfg) is True
    # backend 利用不可 → False
    with mock.patch("fm_radio_station.asr_core.file_transcribe.build_backend", _fake_backend(available=False)), \
         mock.patch("fm_radio_station.asr_core.file_transcribe.os.path.exists", return_value=True):
        assert ft.asr_batch_available(cfg) is False
    # vad onnx 不在 → False
    with mock.patch("fm_radio_station.asr_core.file_transcribe.build_backend", _fake_backend(available=True)), \
         mock.patch("fm_radio_station.asr_core.file_transcribe.os.path.exists", return_value=False):
        assert ft.asr_batch_available(cfg) is False


def test_transcribe_wav_to_vtt_end_to_end_mocked():
    cfg = ASRConfig(parakeet_bin="parakeet-cli", parakeet_model="m.gguf")
    samples = np.zeros(16000 * 5, dtype=np.int16)  # 5s 相当
    # 公式 get_speech_timestamps が返す形（サンプルインデックス）
    regions = [{"start": 16000, "end": 32000}, {"start": 48000, "end": 64000}]
    texts = iter(["こんにちは", "世界"])

    with tempfile.TemporaryDirectory() as d:
        vtt = os.path.join(d, "rec.vtt")
        with mock.patch(
            "fm_radio_station.asr_core.file_transcribe.build_backend",
            _fake_backend(available=True, transcribe=lambda chunk, sr: next(texts)),
        ), \
             mock.patch("fm_radio_station.asr_core.file_transcribe.os.path.exists", return_value=True), \
             mock.patch("fm_radio_station.asr_core.file_transcribe.read_wav_int16", return_value=(samples, 16000)), \
             mock.patch("fm_radio_station.asr_core.file_transcribe._detect_speech", return_value=regions):
            ft.transcribe_wav_to_vtt("/tmp/rec.wav", vtt, cfg)
        body = open(vtt, encoding="utf-8").read()

    # 区間 [1.0,2.0] と [3.0,4.0] のキューが順に並ぶ
    assert "00:00:01.000 --> 00:00:02.000\nこんにちは" in body
    assert "00:00:03.000 --> 00:00:04.000\n世界" in body
    assert body.index("こんにちは") < body.index("世界")
