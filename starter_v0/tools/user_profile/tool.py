from __future__ import annotations

import os
from typing import Any

import requests

from tools._shared import TIMEOUT, err


def _twitter_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    key = os.getenv("RAPIDAPI_KEY")
    host = os.getenv("RAPIDAPI_TWITTER_HOST", "twitter-api45.p.rapidapi.com")
    if not key:
        raise RuntimeError("Missing RAPIDAPI_KEY env var")
    response = requests.get(
        f"https://{host}{path}",
        params=params,
        headers={"x-rapidapi-key": key, "x-rapidapi-host": host},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def get_user_profile(screenname: str = "") -> dict[str, Any]:
    try:
        data = _twitter_get("/screenname.php", {"screenname": screenname})
        handle = data.get("profile") or screenname
        name = data.get("name") or handle
        summary = (
            f"{data.get('sub_count', 0)} followers · {data.get('friends', 0)} following · "
            f"{data.get('statuses_count', 0)} tweets. {data.get('desc') or ''}"
        ).strip()
        item = {
            "title": name,
            "summary": summary,
            "url": f"https://x.com/{handle}" if handle else "",
            "source": f"@{handle}" if handle else "x.com",
            "date": data.get("created_at"),
            "metrics": {
                "followers": data.get("sub_count"),
                "following": data.get("friends"),
                "statuses": data.get("statuses_count"),
            },
        }
        return {"tool": "get_user_profile", "screenname": screenname, "items": [item]}
    except Exception as exc:
        return err("get_user_profile", exc)
