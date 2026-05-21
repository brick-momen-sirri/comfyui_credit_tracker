from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .tracker_db import CREDITS_PER_USD, DB_PATH, LOGGER
except ImportError:
    from tracker_db import CREDITS_PER_USD, DB_PATH, LOGGER


OFFICIAL_API_BASE_URL = os.environ.get("COMFY_OFFICIAL_API_BASE_URL", "https://api.comfy.org").rstrip("/")
OFFICIAL_USAGE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS official_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE,
    created_at TEXT,
    event_type TEXT,
    api_name TEXT,
    model TEXT,
    credits REAL,
    usd REAL,
    raw_params TEXT,
    synced_at TEXT
)
"""

AUTH_CACHE: dict[str, Any] = {
    "headers": {},
    "source": "",
    "updated_at": "",
}


def initialize_official_usage_table(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(OFFICIAL_USAGE_TABLE_SQL)
        connection.commit()


def remember_auth_from_prompt(json_data: dict[str, Any]) -> None:
    """Keep Comfy account auth in memory only so account usage can be synced."""
    extra_data = json_data.get("extra_data", {})
    if not isinstance(extra_data, dict):
        return

    auth_token = extra_data.get("auth_token_comfy_org")
    api_key = extra_data.get("api_key_comfy_org")
    headers: dict[str, str] = {}
    source = ""

    if isinstance(auth_token, str) and auth_token.strip():
        headers = {"Authorization": f"Bearer {auth_token.strip()}"}
        source = "current Comfy login token"
    elif isinstance(api_key, str) and api_key.strip():
        headers = {"X-API-KEY": api_key.strip()}
        source = "current Comfy API key"

    if headers:
        AUTH_CACHE.update(
            {
                "headers": headers,
                "source": source,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )


def remember_auth_headers(headers: dict[str, Any], source: str = "dashboard browser auth") -> bool:
    """Cache caller-provided auth headers in memory without writing secrets to disk."""
    clean: dict[str, str] = {}
    authorization = headers.get("Authorization") or headers.get("authorization")
    api_key = headers.get("X-API-KEY") or headers.get("x-api-key")

    if isinstance(authorization, str) and authorization.strip():
        clean["Authorization"] = authorization.strip()
    elif isinstance(api_key, str) and api_key.strip():
        clean["X-API-KEY"] = api_key.strip()

    if not clean:
        return False

    AUTH_CACHE.update(
        {
            "headers": clean,
            "source": source,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return True


def _env_auth_headers() -> tuple[dict[str, str], str]:
    auth_token = os.environ.get("COMFY_ACCOUNT_AUTH_TOKEN", "").strip()
    if auth_token:
        return {"Authorization": f"Bearer {auth_token}"}, "COMFY_ACCOUNT_AUTH_TOKEN"

    for name in ("COMFY_ACCOUNT_API_KEY", "COMFY_API_KEY", "COMFYUI_API_KEY"):
        api_key = os.environ.get(name, "").strip()
        if api_key:
            return {"X-API-KEY": api_key}, name

    return {}, ""


def auth_status() -> dict[str, Any]:
    cached_headers = AUTH_CACHE.get("headers")
    if isinstance(cached_headers, dict) and cached_headers:
        return {
            "ok": True,
            "source": AUTH_CACHE.get("source", "cached Comfy auth"),
            "updated_at": AUTH_CACHE.get("updated_at", ""),
        }

    env_headers, source = _env_auth_headers()
    if env_headers:
        return {"ok": True, "source": source, "updated_at": ""}

    return {
        "ok": False,
        "source": "",
        "updated_at": "",
        "error": (
            "No Comfy account auth available yet. Run one Partner/API node in this ComfyUI session, "
            "or set COMFY_ACCOUNT_AUTH_TOKEN / COMFY_ACCOUNT_API_KEY before starting ComfyUI."
        ),
    }


def _auth_headers() -> tuple[dict[str, str], str]:
    cached_headers = AUTH_CACHE.get("headers")
    if isinstance(cached_headers, dict) and cached_headers:
        return {str(k): str(v) for k, v in cached_headers.items()}, str(AUTH_CACHE.get("source", "cached Comfy auth"))
    return _env_auth_headers()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_text(params: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_credits(event_type: str, params: dict[str, Any]) -> float:
    if event_type != "api_usage_completed":
        return 0.0

    for key in (
        "credits",
        "credit",
        "credit_cost",
        "credits_used",
        "consumed_credits",
        "total_credits",
        "cost",
    ):
        credits = _safe_float(params.get(key), 0.0)
        if credits > 0:
            return credits
    return 0.0


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    event_type = str(event.get("event_type") or "")
    credits = _extract_credits(event_type, params)
    return {
        "event_id": str(event.get("event_id") or ""),
        "created_at": str(event.get("createdAt") or event.get("created_at") or ""),
        "event_type": event_type,
        "api_name": _first_text(params, "api_name", "provider", "api", "service"),
        "model": _first_text(params, "model", "model_name", "product_name"),
        "credits": credits,
        "usd": round(credits / CREDITS_PER_USD, 6) if credits and CREDITS_PER_USD else 0.0,
        "raw_params": json.dumps(params, ensure_ascii=True, sort_keys=True),
    }


def _request_events(
    *,
    page: int,
    limit: int,
    start_date: str = "",
    end_date: str = "",
    event_filter: str = "api_usage_completed",
) -> dict[str, Any]:
    headers, source = _auth_headers()
    if not headers:
        raise RuntimeError(auth_status()["error"])

    query: dict[str, Any] = {"page": page, "limit": limit}
    if event_filter:
        query["filter"] = event_filter
    if start_date:
        query["start_date"] = start_date
    if end_date:
        query["end_date"] = end_date

    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **headers,
    }
    url = f"{OFFICIAL_API_BASE_URL}/customers/events?{urlencode(query)}"
    request = Request(url, headers=request_headers, method="GET")
    with urlopen(request, timeout=15) as response:
        raw = response.read().decode("utf-8")
        LOGGER.info("Synced official Comfy usage page %s using %s.", page, source)
        return json.loads(raw) if raw else {}


def sync_official_usage_events(
    *,
    limit: int = 100,
    max_pages: int = 5,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    initialize_official_usage_table(DB_PATH)
    limit = max(1, min(int(limit), 100))
    max_pages = max(1, min(int(max_pages), 50))
    synced_at = datetime.now(timezone.utc).isoformat()
    fetched = 0
    inserted = 0
    events_out: list[dict[str, Any]] = []

    try:
        for page in range(1, max_pages + 1):
            payload = _request_events(
                page=page,
                limit=limit,
                start_date=start_date,
                end_date=end_date,
            )
            events = payload.get("events")
            if not isinstance(events, list) or not events:
                break

            with sqlite3.connect(DB_PATH, timeout=30) as connection:
                connection.execute("PRAGMA busy_timeout = 30000")
                for raw_event in events:
                    if not isinstance(raw_event, dict):
                        continue
                    event = _normalize_event(raw_event)
                    if not event["event_id"]:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO official_usage_events (
                            event_id, created_at, event_type, api_name, model,
                            credits, usd, raw_params, synced_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event["event_id"],
                            event["created_at"],
                            event["event_type"],
                            event["api_name"],
                            event["model"],
                            event["credits"],
                            event["usd"],
                            event["raw_params"],
                            synced_at,
                        ),
                    )
                    inserted += int(cursor.rowcount or 0)
                    fetched += 1
                    events_out.append(event)
                connection.commit()

            total_pages = int(payload.get("totalPages") or 0)
            if total_pages and page >= total_pages:
                break

    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            pass
        LOGGER.warning("Official Comfy usage sync failed with HTTP %s: %s", exc.code, body)
        return {
            "ok": False,
            "error": f"Official Comfy usage API returned HTTP {exc.code}",
            "body": body,
            "auth": auth_status(),
        }
    except URLError as exc:
        LOGGER.warning("Official Comfy usage sync could not reach Comfy API: %s", exc)
        return {"ok": False, "error": f"Could not reach official Comfy API: {exc}", "auth": auth_status()}
    except Exception as exc:
        LOGGER.warning("Official Comfy usage sync failed: %s", exc)
        return {"ok": False, "error": str(exc), "auth": auth_status()}

    return {
        "ok": True,
        "fetched": fetched,
        "inserted": inserted,
        "synced_at": synced_at,
        "auth": auth_status(),
        "events": events_out[:20],
    }


def official_usage_summary(limit: int = 20, db_path: Path = DB_PATH) -> dict[str, Any]:
    initialize_official_usage_table(db_path)
    limit = max(1, min(int(limit), 200))
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        totals = connection.execute(
            """
            SELECT
                COUNT(*) AS total_events,
                ROUND(COALESCE(SUM(credits), 0), 4) AS total_credits,
                ROUND(COALESCE(SUM(usd), 0), 4) AS total_usd,
                MAX(synced_at) AS last_synced_at
            FROM official_usage_events
            WHERE event_type = 'api_usage_completed'
            """
        ).fetchone()
        by_api = connection.execute(
            """
            SELECT
                api_name,
                model,
                COUNT(*) AS total_events,
                ROUND(COALESCE(SUM(credits), 0), 4) AS total_credits,
                ROUND(COALESCE(SUM(usd), 0), 4) AS total_usd
            FROM official_usage_events
            WHERE event_type = 'api_usage_completed'
            GROUP BY api_name, model
            ORDER BY total_credits DESC, total_events DESC, api_name ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        recent = connection.execute(
            """
            SELECT event_id, created_at, event_type, api_name, model, credits, usd, raw_params, synced_at
            FROM official_usage_events
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return {
        "auth": auth_status(),
        "totals": dict(totals),
        "by_api": [dict(row) for row in by_api],
        "recent": [dict(row) for row in recent],
    }
