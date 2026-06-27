#!/usr/bin/env python3
import datetime
import logging
import xml.etree.ElementTree as ET
import xmltodict
import requests
from dataclasses import dataclass
from typing import Optional

from fm_radio_station.radio_core.utils import JST, sanitize_filename

logger = logging.getLogger(__name__)

RADIKO_API_URL = "https://radiko.jp/v3/program/station/date/{YYYYMMDD}/{station_id}.xml"
SCHEDULE_CACHE_TTL = datetime.timedelta(minutes=30)


@dataclass
class RadioProgram:
    """ラジオ番組情報を保持するデータクラス。"""
    station_id: str
    detail: dict
    title: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    duration_sec: int

    @property
    def unique_id(self) -> str:
        """Return a filesystem-safe identifier combining station, start time, and title."""
        return f"{self.station_id}_{self.start_time.strftime('%Y%m%d%H%M')}_{sanitize_filename(self.title)}"


class RadikoClient:
    """Radiko番組表API (XML) クライアント。キャッシュ機能付き。"""

    def __init__(self):
        """Initialise the client with an empty in-memory programme cache."""
        self._cache: dict = {}

    def fetch_programs_for_station(
        self, station_id: str, date: datetime.date = None
    ) -> Optional[list[RadioProgram]]:
        """RadioProgramオブジェクトのリストを返す（radio_scheduler.py用）。"""
        if date is None:
            date = datetime.datetime.now(JST).date()
        date_str = date.strftime("%Y%m%d")
        url = RADIKO_API_URL.format(YYYYMMDD=date_str, station_id=station_id)
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            programs = []
            for prog_node in root.findall(".//prog"):
                start_str = prog_node.get("ft")
                end_str = prog_node.get("to")
                title_el = prog_node.find("title")
                title = title_el.text.strip() if title_el is not None and title_el.text else "No Title"
                start_dt = datetime.datetime.strptime(start_str, "%Y%m%d%H%M%S").replace(tzinfo=JST)
                end_dt = datetime.datetime.strptime(end_str, "%Y%m%d%H%M%S").replace(tzinfo=JST)
                programs.append(RadioProgram(
                    station_id=station_id,
                    detail=xmltodict.parse(ET.tostring(prog_node)),
                    title=title,
                    start_time=start_dt,
                    end_time=end_dt,
                    duration_sec=int((end_dt - start_dt).total_seconds()),
                ))
            return programs
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch schedule for {station_id}: {e}")
            return None
        except ET.ParseError as e:
            logger.error(f"Failed to parse XML for {station_id}: {e}")
            return None

    def fetch_programs_as_dicts(
        self, station_id: str, date: datetime.date = None
    ) -> list[dict]:
        """番組情報を辞書リストで返す（radio_scheduler_webui.py用）。"""
        if date is None:
            date = datetime.datetime.now(JST).date()
        date_str = date.strftime("%Y%m%d")
        url = RADIKO_API_URL.format(YYYYMMDD=date_str, station_id=station_id)
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                return []
            root = ET.fromstring(response.content)
            schedule_list = []
            for prog in root.findall(".//prog"):
                prog_data: dict = {}
                prog_data["id"] = prog.get("id")
                prog_data["ft"] = prog.get("ft")
                prog_data["to"] = prog.get("to")
                title_el = prog.find("title")
                prog_data["title"] = (
                    title_el.text.strip()
                    if title_el is not None and title_el.text and title_el.text.strip()
                    else "（タイトル未取得）"
                )
                for key in ["info", "pfm", "tag", "genre"]:
                    el = prog.find(key)
                    prog_data[key] = el.text.strip() if el is not None and el.text and el.text.strip() else None
                prog_data["start_time"] = datetime.datetime.strptime(
                    prog_data["ft"], "%Y%m%d%H%M%S"
                ).replace(tzinfo=JST)
                prog_data["end_time"] = datetime.datetime.strptime(
                    prog_data["to"], "%Y%m%d%H%M%S"
                ).replace(tzinfo=JST)
                prog_data["duration"] = int(
                    (prog_data["end_time"] - prog_data["start_time"]).total_seconds()
                )
                schedule_list.append(prog_data)
            return schedule_list
        except Exception as e:
            logger.error(f"Failed to fetch/parse schedule for {station_id}: {e}")
            return []

    def fetch_programs_cached(
        self, station_id: str, date: datetime.date = None, force: bool = False
    ) -> list[dict]:
        """fetch_programs_as_dicts のキャッシュ付きラッパー（TTL=30分）。

        force=True のときは TTL に関わらず強制再取得してキャッシュを更新する
        （オフライン時にキャッシュされた空結果を「更新」ボタンで取り直す用途）。
        """
        if date is None:
            date = datetime.datetime.now(JST).date()
        date_str = date.strftime("%Y%m%d")
        now = datetime.datetime.now(JST)
        cache_key = (station_id, date_str)
        if not force and cache_key in self._cache:
            programs, cached_at = self._cache[cache_key]
            if now - cached_at < SCHEDULE_CACHE_TTL:
                return programs
        programs = self.fetch_programs_as_dicts(station_id, date)
        self._cache[cache_key] = (programs, now)
        return programs
