#!/usr/bin/env python3
"""ラジオ局の定義。

局リストは Python リテラルではなく ``config/tunnels.yaml`` から読み込む。
パスは環境変数 ``TUNNELS_CONFIG`` で上書きできる（既定は実行ディレクトリの
``config/tunnels.yaml``。解決は ``fm_radio_station.paths`` に集約）。

局IDは Radiko API の正式 station_id に準拠する。
"""
import os

import yaml

from fm_radio_station import paths


def _load_stations() -> list[dict]:
    """``config/tunnels.yaml`` を読み込んで局リストを返す。

    freq は YAML 上では整数 Hz で記述されている（PyYAML が ``76.1e6`` を文字列に
    してしまうため）。ここで float に変換し、従来のリテラル定義と同じ型・値を保つ。
    設定ファイルが無い／``stations`` が空のときは、局は必須なので明示的に失敗する。
    """
    path = str(paths.tunnels_config_file())
    if not os.path.exists(path):
        raise RuntimeError(f"局設定ファイルが見つかりません: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("stations")
    if not raw:
        raise RuntimeError(f"局設定 'stations' が空または未定義です: {path}")
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "freq": float(s["freq"]),
            "type": s["type"],
        }
        for s in raw
    ]


STATIONS: list[dict] = _load_stations()

_station_map: dict[str, dict] = {s["id"]: s for s in STATIONS}


def get_station(station_id: str) -> dict | None:
    """Return the station dict for *station_id*, or None if not found."""
    return _station_map.get(station_id)


def get_station_freq(station_id: str) -> float:
    """Return the centre frequency (Hz) for *station_id*, raising KeyError if unknown."""
    station = _station_map.get(station_id)
    if station is None:
        raise KeyError(f"Unknown station ID: {station_id}")
    return station["freq"]
