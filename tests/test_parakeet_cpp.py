"""ParakeetCppBackend の検証（実バイナリ不要・subprocess をモック）。

実ランタイムは parakeet.cpp（``parakeet-cli transcribe --model <gguf> --input <wav>``）を
想定。バイナリは ``shutil.which`` で解決するため、テストでは解決をモックして有効化する。
"""
import subprocess
from unittest import mock

import numpy as np

from asr_core.asr.parakeet_cpp import ParakeetCppBackend
from asr_core.config import ASRConfig


def _completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _backend(bin_path="parakeet-cli", model="m.gguf", language="ja"):
    """バイナリ解決をモックして有効化済みの backend を作る。

    decoder 検証を config/asr.yaml の選択（デプロイ設定で変わりうる）から独立させるため、
    asr_model は parakeet-tdt（decoder=tdt）に明示固定する。
    """
    cfg = ASRConfig(
        asr_model="parakeet-tdt-0.6b-ja",
        parakeet_bin=bin_path,
        parakeet_model=model,
        parakeet_language=language,
    )
    with mock.patch("asr_core.asr.parakeet_cpp.shutil.which", return_value="/usr/bin/" + bin_path):
        return ParakeetCppBackend(cfg)


def test_command_uses_transcribe_model_input_decoder_and_language():
    backend = _backend(bin_path="parakeet-cli", model="m.gguf", language="ja")
    cmd = backend._build_command("/tmp/a.wav")
    assert cmd[0] == "/usr/bin/parakeet-cli"
    assert cmd[1] == "transcribe"
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "m.gguf"
    assert "--input" in cmd and cmd[cmd.index("--input") + 1] == "/tmp/a.wav"
    # TDT デコーダを明示し、--lang で言語を固定する。
    assert "--decoder" in cmd and cmd[cmd.index("--decoder") + 1] == "tdt"
    assert "--lang" in cmd and cmd[cmd.index("--lang") + 1] == "ja"


def test_command_omits_language_when_unset_but_keeps_decoder():
    backend = _backend(language="")
    cmd = backend._build_command("/tmp/a.wav")
    assert "--lang" not in cmd
    assert "--decoder" in cmd  # デコーダ明示は言語設定に依らず常に付く


def test_command_omits_decoder_for_nemotron_rnnt_model():
    # nemotron は RNN-T。parakeet-cli の --decoder は ctc|tdt のみ受け付ける（rnnt は無効）ため、
    # nemotron プロファイルは decoder="" に派生し、--decoder を省略してデフォルトデコーダに任せる。
    cfg = ASRConfig(
        asr_model="nemotron-3.5-asr-streaming-0.6b",
        parakeet_bin="parakeet-cli",
        parakeet_model="m.gguf",
    )
    assert cfg.parakeet_decoder == ""
    # 多言語モデルは BCP-47 ロケール（bare 'ja' ではなく ja-JP）。
    assert cfg.parakeet_language == "ja-JP"
    with mock.patch(
        "asr_core.asr.parakeet_cpp.shutil.which", return_value="/usr/bin/parakeet-cli"
    ):
        backend = ParakeetCppBackend(cfg)
    cmd = backend._build_command("/tmp/a.wav")
    assert "--decoder" not in cmd  # 省略してランタイムの arch 判定（rnnt_greedy）に委ねる
    assert "--lang" in cmd and cmd[cmd.index("--lang") + 1] == "ja-JP"


def test_transcribe_parses_stdout_and_cleans_tempfile():
    backend = _backend()
    samples = np.zeros(8000, dtype=np.int16)
    seen_paths = []

    def fake_run(cmd, **kwargs):
        wav_path = cmd[cmd.index("--input") + 1]
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


# --- backend 派生・ファクトリ（asr_model → backend） ---

def test_post_init_derives_backend_from_profile():
    from asr_core.asr import build_backend
    from asr_core.asr.llama_mtmd import LlamaMtmdBackend

    # parakeet 系プロファイル → backend=parakeet_cpp
    c1 = ASRConfig(asr_model="parakeet-tdt-0.6b-ja")
    assert c1.backend == "parakeet_cpp"
    assert c1.parakeet_decoder == "tdt"

    # qwen3 プロファイル → backend=llama_mtmd ＋ qwen フィールド派生。
    # GGUF 実在時はパスが解決される（未配置だと "" になるため存在をモック）。
    with mock.patch("asr_core.config.os.path.exists", return_value=True):
        c2 = ASRConfig(asr_model="qwen3-asr-1.7b")
    assert c2.backend == "llama_mtmd"
    assert c2.qwen_model.endswith("Qwen3-ASR-1.7B-Q8_0.gguf")
    assert c2.qwen_mmproj.endswith("mmproj-Qwen3-ASR-1.7B-Q8_0.gguf")
    assert c2.qwen_language == "Japanese"

    # ファクトリは backend ごとに正しいクラスを返す
    with mock.patch("asr_core.asr.parakeet_cpp.shutil.which", return_value="/usr/bin/parakeet-cli"):
        assert isinstance(build_backend(c1), ParakeetCppBackend)
    with mock.patch("asr_core.asr.llama_mtmd.shutil.which", return_value="/usr/bin/llama-mtmd-cli"):
        assert isinstance(build_backend(c2), LlamaMtmdBackend)


def test_disabled_when_model_unset_returns_empty():
    """GGUF 未設定でも例外を投げず空文字を返す。"""
    cfg = ASRConfig(parakeet_bin="parakeet-cli", parakeet_model="")
    with mock.patch("asr_core.asr.parakeet_cpp.shutil.which", return_value="/usr/bin/parakeet-cli"):
        backend = ParakeetCppBackend(cfg)
    with mock.patch("subprocess.run", side_effect=AssertionError("呼ばれてはいけない")):
        assert backend.transcribe(np.zeros(16, dtype=np.int16), 16000) == ""
