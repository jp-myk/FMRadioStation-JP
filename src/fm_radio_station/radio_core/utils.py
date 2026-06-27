#!/usr/bin/env python3
import re
import datetime

JST = datetime.timezone(datetime.timedelta(hours=9))


def sanitize_filename(name: str) -> str:
    """Replace filesystem-unsafe characters in *name* and return a safe filename string."""
    return re.sub(r'[\\/:*?"<>|]', '-', name).replace(' ', '_').replace('　', '_').strip()


def convert_datetime(o):
    """JSON-serialise *o* as an ISO-like datetime string; raise TypeError for other types."""
    if isinstance(o, datetime.datetime):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")
