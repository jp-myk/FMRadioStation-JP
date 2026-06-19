"""ParakeetCppBackend の検証（実バイナリ不要・subprocess をモック）。

実ランタイムは CrispASR（``crispasr -m <gguf> -f <wav>``）を想定。バイナリは
``shutil.which`` で解決するため、テストでは解決をモックして有効化する。
"""
import subprocess
from unittest import mock

import numpy as np

from asr_core.asr.parakeet_cpp import ParakeetCppBackend
from asr_core.config import ASRConfig


def _completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _backend(bin_path="crispasr", model="m.gguf", language="ja"):
    """バイナリ解決をモックして有効化済みの backend を作る。"""
    cfg = ASRConfig(parakeet_bin=bin_path, parakeet_model=model, parakeet_language=language)
    with mock.patch("asr_core.asr.parakeet_cpp.shutil.which", return_value="/usr/bin/" + bin_path):
        return ParakeetCppBackend(cfg)


def test_command_uses_model_wav_backend_and_language():
    backend = _backend(bin_path="crispasr", model="m.gguf", language="ja")
    cmd = backend._build_command("/tmp/a.wav")
    assert cmd[0] == "/usr/bin/crispasr"
    assert cmd[1:5] == ["-m", "m.gguf", "-f", "/tmp/a.wav"]
    # parakeet backend を明示し、-l で言語固定して LID（whisper 追加ロード）を回避する。
    assert "--backend" in cmd and cmd[cmd.index("--backend") + 1] == "parakeet"
    assert "-l" in cmd and cmd[cmd.index("-l") + 1] == "ja"


def test_command_omits_language_when_unset():
    backend = _backend(language="")
    cmd = backend._build_command("/tmp/a.wav")
    assert "-l" not in cmd
    assert "--backend" in cmd  # backend 明示は言語設定に依らず常に付く


def test_transcribe_parses_stdout_and_cleans_tempfile():
    backend = _backend()
    samples = np.zeros(8000, dtype=np.int16)
    seen_paths = []

    def fake_run(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        seen_paths.append(wav_path)
        import os
        assert os.path.exists(wav_path)  # 推論中は一時 WAV が存在
        return _completed(stdout="  こんにちは \n 世界 \n".encode("utf-8"))

    with mock.patch("subprocess.run", side_effect=fake_run):
        text = backend.transcribe(samples, 16000)

    assert text == "こんにちは 世界"
    import os
    assert not os.path.exists(seen_paths[0])  # 後始末済み


def test_transcribe_strips_timestamp_prefixes():
    backend = _backend()
    stdout = "[00:00:00.000 --> 00:00:02.000]  おはよう\n[00:00:02.000 --> 00:00:03.000] ございます\n"
    with mock.patch("subprocess.run", return_value=_completed(stdout=stdout.encode("utf-8"))):
        assert backend.transcribe(np.zeros(16, dtype=np.int16), 16000) == "おはよう ございます"


def test_transcribe_raises_on_nonzero_exit():
    backend = _backend()
    with mock.patch("subprocess.run", return_value=_completed(stderr=b"boom", returncode=2)):
        try:
            backend.transcribe(np.zeros(16, dtype=np.int16), 16000)
        except RuntimeError as e:
            assert "boom" in str(e)
        else:
            raise AssertionError("RuntimeError が送出されるべき")


def test_disabled_when_binary_missing_returns_empty():
    """バイナリが PATH に無ければ例外を投げず空文字を返す（ログ汚染回避）。"""
    cfg = ASRConfig(parakeet_bin="no-such-bin", parakeet_model="m.gguf")
    with mock.patch("asr_core.asr.parakeet_cpp.shutil.which", return_value=None):
        backend = ParakeetCppBackend(cfg)
    with mock.patch("subprocess.run", side_effect=AssertionError("呼ばれてはいけない")):
        assert backend.transcribe(np.zeros(16, dtype=np.int16), 16000) == ""


def test_disabled_when_model_unset_returns_empty():
    """GGUF 未設定でも例外を投げず空文字を返す。"""
    cfg = ASRConfig(parakeet_bin="crispasr", parakeet_model="")
    with mock.patch("asr_core.asr.parakeet_cpp.shutil.which", return_value="/usr/bin/crispasr"):
        backend = ParakeetCppBackend(cfg)
    with mock.patch("subprocess.run", side_effect=AssertionError("呼ばれてはいけない")):
        assert backend.transcribe(np.zeros(16, dtype=np.int16), 16000) == ""
