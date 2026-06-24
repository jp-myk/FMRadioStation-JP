"""fm_radio_station: SDR ベースの FM ラジオ聴取・録音アプリ。

サブパッケージ:
- radio_core: SDR 受信・Radiko 番組表・局定義・トランスコード等のコア。
- asr_core:   音声認識（字幕生成）バックエンドと設定。
- apps:       実行エントリ（receiver / scheduler / webui）。console scripts から呼ぶ。
- web:        WebUI のテンプレート/静的アセット（パッケージ同梱）。
"""
