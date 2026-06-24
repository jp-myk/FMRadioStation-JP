#!/usr/bin/env python3
import re
import datetime

JST = datetime.timezone(datetime.timedelta(hours=9))


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '-', name).replace(' ', '_').replace('　', '_').strip()


def convert_datetime(o):
    if isinstance(o, datetime.datetime):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")
