"""認識結果のデータ構造。"""
from dataclasses import dataclass


@dataclass
class ASRResult:
    """1 つの音声セグメントに対する認識結果。

    現状は final のみ emit する（``is_final=True``）。partial は将来対応のため
    フィールドとしては保持する。
    """
    segment_id: int          # セグメント連番（0 始まり）
    text: str                # 認識テキスト
    is_final: bool           # 確定なら True（現状は常に True）
    t_start: float           # ストリーム先頭からの開始秒
    t_end: float             # ストリーム先頭からの終了秒
    confidence: float | None = None  # バックエンドが返せば設定
