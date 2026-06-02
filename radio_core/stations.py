#!/usr/bin/env python3
# 局IDはRadiko APIの正式station_idに準拠する
STATIONS: list[dict] = [
    # FM局
    {"id": "INT",          "name": "interfm",           "freq": 76.1e6, "type": "fm"},
    {"id": "FM-TOSHIMA",   "name": "FMとしま",          "freq": 77.5e6, "type": "fm"},
    {"id": "BAYFM78",      "name": "bayfm78",           "freq": 78.0e6, "type": "fm"},
    {"id": "MUSASHINO",    "name": "むさしのFM",        "freq": 78.2e6, "type": "fm"},
    {"id": "FM-FUJI",      "name": "FM FUJI",           "freq": 78.6e6, "type": "fm"},
    {"id": "KATSUSHIKA",   "name": "かつしかFM",        "freq": 78.9e6, "type": "fm"},
    {"id": "TAMALAKE",     "name": "多摩レイクサイドFM", "freq": 79.0e6, "type": "fm"},
    {"id": "KAWASAKI",     "name": "かわさきFM",        "freq": 79.1e6, "type": "fm"},
    {"id": "NACK5",        "name": "NACK5",             "freq": 79.5e6, "type": "fm"},
    {"id": "FMT",          "name": "TOKYO FM",          "freq": 80.0e6, "type": "fm"},
    {"id": "FMJ",          "name": "J-WAVE",            "freq": 81.3e6, "type": "fm"},
    {"id": "JOAK-FM",      "name": "NHK-FM 東京",       "freq": 82.5e6, "type": "fm"},
    {"id": "SETAGAYA",     "name": "FM世田谷",          "freq": 83.4e6, "type": "fm"},
    {"id": "FM-TACHIKAWA", "name": "FMたちかわ",        "freq": 83.6e6, "type": "fm"},
    {"id": "CHOFU",        "name": "調布FM",            "freq": 83.8e6, "type": "fm"},
    {"id": "YFM",          "name": "FMヨコハマ",        "freq": 84.7e6, "type": "fm"},
    # AM局
    {"id": "JOAK",         "name": "NHKラジオ第1",      "freq": 594e3,  "type": "am"},
    {"id": "JOAB",         "name": "NHKラジオ第2",      "freq": 693e3,  "type": "am"},
    # ワイドFM / FM補完放送
    {"id": "AFN",          "name": "AFN Tokyo",         "freq": 89.1e6, "type": "fm"},
    {"id": "TBS",          "name": "TBSラジオ",         "freq": 90.5e6, "type": "fm"},
    {"id": "QRR",          "name": "文化放送",          "freq": 91.6e6, "type": "fm"},
    {"id": "LFR",          "name": "ニッポン放送",      "freq": 93.0e6, "type": "fm"},
    {"id": "JORF",         "name": "ラジオ日本",        "freq": 92.4e6, "type": "fm"},
]

_station_map: dict[str, dict] = {s["id"]: s for s in STATIONS}


def get_station(station_id: str) -> dict | None:
    return _station_map.get(station_id)


def get_station_freq(station_id: str) -> float:
    station = _station_map.get(station_id)
    if station is None:
        raise KeyError(f"Unknown station ID: {station_id}")
    return station["freq"]
