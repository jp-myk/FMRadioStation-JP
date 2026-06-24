"""/api/on-air のオフライン・フォールバック挙動のテスト。

Radiko 番組表が取得できなくても（オフライン）、STATIONS を基準に全局のカード
（局名＋再生ボタン）が出るよう、api_on_air が常に 1 局 1 エントリを返すことを検証する。
"""
import datetime

import pytest

# WebUI は GNU Radio/osmosdr 等に依存し、未導入環境では import 時に exit(1) する。
# その場合はテストをスキップする（ASR と同様に環境差を許容）。
try:
    from fm_radio_station.apps import webui
except BaseException as e:  # SystemExit を含む
    pytest.skip(f"fm_radio_station.apps.webui unavailable: {e}", allow_module_level=True)

from fm_radio_station.radio_core.utils import JST


def test_on_air_offline_returns_placeholder_for_all_stations(monkeypatch):
    """番組表が取れない（[] 返却）= オフライン時、全局が placeholder で返る。"""
    monkeypatch.setattr(
        webui._radiko_client,
        "fetch_programs_cached",
        lambda station_id, date, force=False: [],
    )

    results = webui.api_on_air()

    assert len(results) == len(webui.STATIONS)
    ids = {r["station_id"] for r in results}
    assert ids == {s["id"] for s in webui.STATIONS}
    for r in results:
        assert r["station_name"]            # 局名は常にある
        assert r["has_program"] is False
        assert r["title"] == "番組情報なし"
        assert r["progress"] == 0
        assert "is_recording" in r


def test_on_air_marks_currently_airing_program(monkeypatch):
    """現在放送中の番組がある局だけ has_program=True になり、他局は placeholder。"""
    now = datetime.datetime.now(JST)
    target = webui.STATIONS[0]["id"]

    def fake_fetch(station_id, date, force=False):
        if station_id != target:
            return []
        return [{
            "title": "テスト番組",
            "start_time": now - datetime.timedelta(minutes=10),
            "end_time": now + datetime.timedelta(minutes=20),
            "duration": 1800,
            "info": None,
            "pfm": "出演者",
        }]

    monkeypatch.setattr(webui._radiko_client, "fetch_programs_cached", fake_fetch)

    results = webui.api_on_air()
    by_id = {r["station_id"]: r for r in results}

    assert len(results) == len(webui.STATIONS)
    hit = by_id[target]
    assert hit["has_program"] is True
    assert hit["title"] == "テスト番組"
    assert 0 < hit["progress"] < 100
    # 他局は placeholder
    for sid, r in by_id.items():
        if sid != target:
            assert r["has_program"] is False
            assert r["title"] == "番組情報なし"


def test_fetch_programs_cached_force_bypasses_cache(monkeypatch):
    """force=True はキャッシュ（空結果含む）を無視して再取得する。"""
    from fm_radio_station.radio_core.radiko import RadikoClient

    client = RadikoClient()
    calls = {"n": 0}
    current = {"value": []}

    def fake_dicts(station_id, date=None):
        calls["n"] += 1
        return current["value"]

    monkeypatch.setattr(client, "fetch_programs_as_dicts", fake_dicts)

    # 1回目: 空結果を取得しキャッシュ
    assert client.fetch_programs_cached("FMT") == []
    assert calls["n"] == 1

    # オンライン復帰で番組が出たとする
    current["value"] = [{"title": "復帰後の番組"}]

    # force=False: TTL 内なのでキャッシュ済みの空結果が返る（再取得しない）
    assert client.fetch_programs_cached("FMT") == []
    assert calls["n"] == 1

    # force=True: キャッシュを無視して再取得し最新を返す
    assert client.fetch_programs_cached("FMT", force=True) == [{"title": "復帰後の番組"}]
    assert calls["n"] == 2

    # 以降は force=False でも更新後のキャッシュが返る
    assert client.fetch_programs_cached("FMT") == [{"title": "復帰後の番組"}]
    assert calls["n"] == 2
