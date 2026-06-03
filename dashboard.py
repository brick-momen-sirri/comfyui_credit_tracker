from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from aiohttp import web
    from server import PromptServer
except Exception:
    web = None
    PromptServer = None

try:
    from .tracker_db import CREDITS_PER_USD, DB_PATH, LOGGER, UsageRecord, balance_snapshot_summary, initialize_database, insert_usage_record, make_dedupe_key, record_balance_snapshot
    from .pricing_sync import load_pricing_cache, search_pricing_cache, sync_pricing_cache
    from .official_usage import official_usage_summary, remember_auth_headers, sync_official_usage_events
    from .peer_sync import PEER_SYNC_TOKEN
except ImportError:
    from tracker_db import CREDITS_PER_USD, DB_PATH, LOGGER, UsageRecord, balance_snapshot_summary, initialize_database, insert_usage_record, make_dedupe_key, record_balance_snapshot
    from pricing_sync import load_pricing_cache, search_pricing_cache, sync_pricing_cache
    from official_usage import official_usage_summary, remember_auth_headers, sync_official_usage_events
    from peer_sync import PEER_SYNC_TOKEN


FULL_COLUMNS = [
    "id",
    "timestamp",
    "project_name",
    "user_name",
    "workflow_name",
    "partner_node_name",
    "node_class_type",
    "node_title",
    "model_name",
    "input_summary",
    "pricing_mode",
    "source",
    "quantity",
    "duration_seconds",
    "resolution",
    "estimated_credits",
    "estimated_usd",
    "prompt_id",
    "node_id",
    "dedupe_key",
    "notes",
]

PRIVATE_CREDIT_URL = "http://127.0.0.1:8160/abuomar_credit"
INSTANCE_CONFIG_PATH = Path(__file__).resolve().parent / "instance_config.json"
REMOTE_INSTANCES_PATH = Path(__file__).resolve().parent / "remote_instances.json"
BALANCE_CACHE_TTL_SECONDS = 10
STATUS_PATH = Path(__file__).resolve().parent / "tracker_status.json"
BACKUP_DIR = Path(__file__).resolve().parent / "backups"
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
LOCAL_DATE_SQL = "CASE WHEN timestamp IS NOT NULL AND length(timestamp) >= 10 THEN substr(timestamp, 1, 10) ELSE date(timestamp) END"
BALANCE_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "data": {
        "ok": False,
        "credits": 0.0,
        "usd": 0.0,
        "currency": "usd",
        "updated_at": "",
        "error": "Not fetched yet",
    },
}


def _connect() -> sqlite3.Connection:
    initialize_database(DB_PATH)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _clean_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = text[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return ""
    return candidate


def _date_filter_bounds(params: dict[str, str]) -> tuple[str, str]:
    exact_day = _clean_date(params.get("day") or params.get("date"))
    if exact_day:
        return exact_day, exact_day

    date_from = _clean_date(params.get("from"))
    date_to = _clean_date(params.get("to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    if date_from or date_to:
        return date_from, date_to

    days = str(params.get("days") or "").strip()
    if days:
        try:
            days_int = max(1, min(int(days), 3650))
        except ValueError:
            return "", ""
        today = datetime.now().astimezone().date()
        start = today - timedelta(days=days_int - 1)
        return start.isoformat(), today.isoformat()

    return "", ""


def _date_filter_payload(params: dict[str, str]) -> dict[str, Any]:
    date_from, date_to = _date_filter_bounds(params)
    exact_day = date_from if date_from and date_from == date_to else ""
    days = str(params.get("days") or "").strip()
    if exact_day:
        label = exact_day
    elif date_from and date_to:
        label = f"{date_from} to {date_to}"
    elif date_from:
        label = f"From {date_from}"
    elif date_to:
        label = f"Through {date_to}"
    elif days:
        label = f"Last {days} day{'s' if days != '1' else ''}"
    else:
        label = "All time"
    return {
        "from": date_from,
        "to": date_to,
        "single_day": exact_day,
        "label": label,
    }


def _local_day_from_timestamp(timestamp: Any) -> str:
    text = str(timestamp or "").strip()
    if len(text) >= 10:
        return text[:10]
    return _clean_date(text)


def _query_filters(params: dict[str, str]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []

    project = params.get("project", "").strip()
    partner = params.get("partner", "").strip()
    source = params.get("source", "").strip()
    date_from, date_to = _date_filter_bounds(params)
    timestamp_from = params.get("from_ts", "").strip()
    timestamp_to = params.get("to_ts", "").strip()
    include_failed = params.get("include_failed", "").strip() == "1"

    if project:
        clauses.append("project_name LIKE ?")
        values.append(f"%{project}%")
    if partner:
        clauses.append("partner_node_name LIKE ?")
        values.append(f"%{partner}%")
    if source:
        clauses.append("source = ?")
        values.append(source)
    if date_from:
        clauses.append(f"{LOCAL_DATE_SQL} >= ?")
        values.append(date_from)
    if date_to:
        clauses.append(f"{LOCAL_DATE_SQL} <= ?")
        values.append(date_to)
    if timestamp_from:
        clauses.append("timestamp >= ?")
        values.append(timestamp_from)
    if timestamp_to:
        clauses.append("timestamp <= ?")
        values.append(timestamp_to)
    if not include_failed:
        clauses.append(
            "("
            "source = 'runtime_price' "
            "OR notes LIKE '%status=execution_success%' "
            "OR notes NOT LIKE '%status=execution_error%'"
            ")"
        )

    if not clauses:
        return "", values
    return "WHERE " + " AND ".join(clauses), values


def _limit(params: dict[str, str], default: int = 20) -> int:
    try:
        return max(1, min(int(params.get("limit", default)), 200))
    except ValueError:
        return default


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_credit_balance() -> dict[str, Any]:
    now = time.time()
    if now - float(BALANCE_CACHE.get("ts", 0.0)) <= BALANCE_CACHE_TTL_SECONDS:
        return dict(BALANCE_CACHE["data"])

    req = Request(PRIVATE_CREDIT_URL, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=5) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        result = {
            "ok": False,
            "credits": 0.0,
            "usd": 0.0,
            "currency": "usd",
            "updated_at": "",
            "error": f"Credit balance service returned HTTP {exc.code}",
            "body": body,
        }
    except URLError as exc:
        result = {
            "ok": False,
            "credits": 0.0,
            "usd": 0.0,
            "currency": "usd",
            "updated_at": "",
            "error": f"Could not reach credit balance service: {exc}",
        }
    except Exception as exc:
        result = {
            "ok": False,
            "credits": 0.0,
            "usd": 0.0,
            "currency": "usd",
            "updated_at": "",
            "error": str(exc),
        }
    else:
        if not payload.get("ok"):
            result = {
                "ok": False,
                "credits": 0.0,
                "usd": 0.0,
                "currency": "usd",
                "updated_at": "",
                "error": payload.get("error", "Credit balance service failed"),
                "body": payload.get("body", ""),
            }
        else:
            data = payload.get("data") or {}
            credits = _safe_float(data.get("display_balance"), _safe_float(data.get("credits_estimate")))
            usd = _safe_float(data.get("usd_estimate"))
            if not usd and CREDITS_PER_USD:
                usd = round(credits / CREDITS_PER_USD, 4)
            result = {
                "ok": True,
                "credits": credits,
                "usd": usd,
                "currency": data.get("currency", "usd"),
                "updated_at": data.get("updated_at") or data.get("last_updated") or "",
                "error": "",
            }

    BALANCE_CACHE["data"] = result
    BALANCE_CACHE["ts"] = now
    if not result.get("ok"):
        LOGGER.warning("Credit Tracker could not fetch current balance: %s", result.get("error"))
    return dict(result)


def _capture_balance_snapshot(balance: dict[str, Any], source: str = "dashboard") -> None:
    if not balance.get("ok"):
        return
    try:
        record_balance_snapshot(
            instance_name=_local_instance_name(),
            credits=_safe_float(balance.get("credits"), 0.0),
            usd=_safe_float(balance.get("usd"), 0.0),
            currency=str(balance.get("currency") or "usd"),
            source=source,
            notes="Automatic Comfy account balance snapshot",
        )
    except Exception as exc:
        LOGGER.warning("Credit Tracker could not record balance snapshot: %s", exc)


def _latest_backup_file() -> Path | None:
    if not BACKUP_DIR.exists():
        return None
    backups = sorted(BACKUP_DIR.glob("usage_log_*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    return backups[0] if backups else None


def _auto_backup_database() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {
            "ok": False,
            "enabled": True,
            "backup_count": 0,
            "latest_backup": "",
            "latest_backup_at": "",
            "error": "usage_log.db does not exist yet",
        }

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        latest = _latest_backup_file()
        now = time.time()
        created = False
        if latest is None or now - latest.stat().st_mtime >= BACKUP_INTERVAL_SECONDS:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = BACKUP_DIR / f"usage_log_{stamp}.db"
            with sqlite3.connect(DB_PATH, timeout=30) as source:
                with sqlite3.connect(target, timeout=30) as destination:
                    source.backup(destination)
            latest = target
            created = True

        backups = sorted(BACKUP_DIR.glob("usage_log_*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
        latest_stat = latest.stat() if latest else None
        return {
            "ok": True,
            "enabled": True,
            "created": created,
            "backup_count": len(backups),
            "latest_backup": str(latest.resolve()) if latest else "",
            "latest_backup_name": latest.name if latest else "",
            "latest_backup_at": datetime.fromtimestamp(latest_stat.st_mtime).astimezone().isoformat(timespec="seconds") if latest_stat else "",
            "latest_backup_size_mb": round((latest_stat.st_size if latest_stat else 0) / (1024 * 1024), 2),
            "folder": str(BACKUP_DIR.resolve()),
            "error": "",
        }
    except Exception as exc:
        LOGGER.warning("Credit Tracker could not create database backup: %s", exc)
        latest = _latest_backup_file()
        return {
            "ok": False,
            "enabled": True,
            "backup_count": len(list(BACKUP_DIR.glob("usage_log_*.db"))) if BACKUP_DIR.exists() else 0,
            "latest_backup": str(latest.resolve()) if latest else "",
            "latest_backup_name": latest.name if latest else "",
            "latest_backup_at": datetime.fromtimestamp(latest.stat().st_mtime).astimezone().isoformat(timespec="seconds") if latest else "",
            "folder": str(BACKUP_DIR.resolve()),
            "error": str(exc),
        }


def _tracker_status_payload() -> dict[str, Any]:
    try:
        if not STATUS_PATH.exists():
            return {"ok": False, "event": "not_seen", "timestamp": "", "details": {}, "error": "No tracker status file yet"}
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("tracker_status.json must contain an object")
        payload["ok"] = True
        return payload
    except Exception as exc:
        return {"ok": False, "event": "error", "timestamp": "", "details": {}, "error": str(exc)}


def _balance_snapshot_summary_for_filters(params: dict[str, str]) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[Any] = []
    date_from, date_to = _date_filter_bounds(params)

    if date_from:
        clauses.append(f"{LOCAL_DATE_SQL} >= ?")
        values.append(date_from)
    if date_to:
        clauses.append(f"{LOCAL_DATE_SQL} <= ?")
        values.append(date_to)

    if not clauses:
        return balance_snapshot_summary(DB_PATH)

    where_sql = "WHERE " + " AND ".join(clauses)
    with _connect() as connection:
        first = connection.execute(
            f"""
            SELECT *
            FROM balance_snapshots
            {where_sql}
            ORDER BY timestamp ASC, id ASC
            LIMIT 1
            """,
            values,
        ).fetchone()
        latest = connection.execute(
            f"""
            SELECT *
            FROM balance_snapshots
            {where_sql}
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            values,
        ).fetchone()
        count = connection.execute(
            f"SELECT COUNT(*) FROM balance_snapshots {where_sql}",
            values,
        ).fetchone()[0]

    if not first or not latest:
        return {
            "ok": False,
            "snapshot_count": int(count or 0),
            "message": "No balance snapshots for this date range",
        }

    first_credits = _safe_float(first["credits"], 0.0)
    latest_credits = _safe_float(latest["credits"], 0.0)
    first_usd = _safe_float(first["usd"], 0.0)
    latest_usd = _safe_float(latest["usd"], 0.0)
    delta_credits = round(first_credits - latest_credits, 4)
    delta_usd = round(first_usd - latest_usd, 4)
    return {
        "ok": True,
        "snapshot_count": int(count or 0),
        "first": dict(first),
        "latest": dict(latest),
        "balance_delta_credits": delta_credits,
        "balance_delta_usd": delta_usd,
        "real_consumed_credits": max(delta_credits, 0.0),
        "real_consumed_usd": max(delta_usd, 0.0),
        "balance_increased": latest_credits > first_credits,
    }


def _balance_reconciliation_payload(
    tracked_credits: float,
    tracked_usd: float,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    summary = _balance_snapshot_summary_for_filters(params or {})
    if not summary.get("ok"):
        summary.update(
            {
                "tracked_credits": round(tracked_credits, 4),
                "tracked_usd": round(tracked_usd, 4),
                "untracked_credits": 0.0,
                "untracked_usd": 0.0,
            }
        )
        return summary

    first = summary.get("first") or {}
    latest = summary.get("latest") or {}
    first_ts = str(first.get("timestamp") or "").strip()
    latest_ts = str(latest.get("timestamp") or "").strip()
    if first_ts and latest_ts:
        window_params = dict(params or {})
        window_params.pop("days", None)
        window_params.pop("from", None)
        window_params.pop("to", None)
        window_params["from_ts"] = first_ts
        window_params["to_ts"] = latest_ts
        window_params["limit"] = "10000"
        window_payload = _usage_rows_payload(window_params)
        window_rows = window_payload.get("rows", [])
        if (params or {}).get("federated", "1") != "0":
            local_window_payload = {"totals": _usage_totals(window_rows)}
            tracked_totals = _federated_payload(local_window_payload, window_params, 10000).get("totals", {})
        else:
            tracked_totals = _usage_totals(window_rows)
        tracked_credits = _safe_float(tracked_totals.get("total_estimated_credits"), 0.0)
        tracked_usd = _safe_float(tracked_totals.get("total_estimated_usd"), 0.0)
        summary["tracked_window_start"] = first_ts
        summary["tracked_window_end"] = latest_ts
        summary["tracked_window_note"] = "Tracked spend is compared only inside the balance snapshot window."

    real_credits = _safe_float(summary.get("real_consumed_credits"), 0.0)
    real_usd = _safe_float(summary.get("real_consumed_usd"), 0.0)
    summary["tracked_credits"] = round(tracked_credits, 4)
    summary["tracked_usd"] = round(tracked_usd, 4)
    summary["untracked_credits"] = round(real_credits - tracked_credits, 4)
    summary["untracked_usd"] = round(real_usd - tracked_usd, 4)
    return summary


def _system_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    federated = payload.get("federated") or {}
    official = payload.get("official_usage") or {}
    official_totals = official.get("totals") or {}
    reconciliation = payload.get("balance_reconciliation") or {}
    recent = payload.get("recent") or []
    balance = payload.get("balance") or {}
    tracker_status = _tracker_status_payload()
    backup = _auto_backup_database()
    offline_count = int(_safe_float(federated.get("offline_count"), 0.0))
    warnings = payload.get("data_quality") or {}
    warning_count = sum(int(_safe_float(value, 0.0)) for value in warnings.values())
    issues = []
    if offline_count:
        issues.append(f"{offline_count} peer offline")
    if not balance.get("ok"):
        issues.append("balance unavailable")
    if not backup.get("ok"):
        issues.append("backup warning")
    if warning_count:
        issues.append(f"{warning_count} data warning records")
    if not tracker_status.get("ok"):
        issues.append("tracker status unavailable")

    return {
        "ok": not issues,
        "issues": issues,
        "local_instance": _local_instance_name(),
        "peer_online_count": int(_safe_float(federated.get("online_count"), 1.0)),
        "peer_offline_count": offline_count,
        "deduped_duplicate_count": int(_safe_float(federated.get("deduped_duplicate_count"), 0.0)),
        "last_tracker_event": tracker_status,
        "last_run": recent[0] if recent else {},
        "last_balance_snapshot": (reconciliation.get("latest") or {}),
        "last_official_sync": official_totals.get("last_synced_at") or "",
        "backup": backup,
        "warning_count": warning_count,
    }


def _default_remote_instances() -> list[dict[str, Any]]:
    return []


def _default_instance_config() -> dict[str, Any]:
    return {
        "name": "This ComfyUI",
        "base_url": "http://127.0.0.1:8188",
        "notes": "Local tracker instance name shown in the federated dashboard.",
    }


def _ensure_instance_config(path: Path = INSTANCE_CONFIG_PATH) -> None:
    if path.exists():
        return
    try:
        path.write_text(json.dumps(_default_instance_config(), indent=2), encoding="utf-8")
        LOGGER.info("Created local instance config at %s", path)
    except Exception as exc:
        LOGGER.warning("Could not create local instance config at %s: %s", path, exc)


def _load_instance_config(path: Path = INSTANCE_CONFIG_PATH) -> dict[str, Any]:
    _ensure_instance_config(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Could not read local instance config: %s", exc)
        return _default_instance_config()
    if not isinstance(data, dict):
        return _default_instance_config()
    default = _default_instance_config()
    return {
        "name": str(data.get("name") or default["name"]).strip(),
        "base_url": str(data.get("base_url") or default["base_url"]).strip().rstrip("/"),
        "notes": str(data.get("notes") or "").strip(),
    }


def _local_instance_name() -> str:
    return _load_instance_config().get("name", "This ComfyUI")


def _ensure_remote_instances_config(path: Path = REMOTE_INSTANCES_PATH) -> None:
    if path.exists():
        return
    try:
        path.write_text(json.dumps(_default_remote_instances(), indent=2), encoding="utf-8")
        LOGGER.info("Created remote instances config at %s", path)
    except Exception as exc:
        LOGGER.warning("Could not create remote instances config at %s: %s", path, exc)


def _load_remote_instances(path: Path = REMOTE_INSTANCES_PATH) -> list[dict[str, Any]]:
    _ensure_remote_instances_config(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        LOGGER.warning("Remote instances config is invalid JSON: %s", exc)
        return []
    except Exception as exc:
        LOGGER.warning("Could not read remote instances config: %s", exc)
        return []

    if not isinstance(data, list):
        LOGGER.warning("Remote instances config must be a JSON list.")
        return []

    instances: list[dict[str, Any]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            continue
        instances.append(
            {
                "name": str(item.get("name") or f"Remote {index}").strip(),
                "base_url": base_url,
                "enabled": bool(item.get("enabled", True)),
                "notes": str(item.get("notes") or "").strip(),
            }
        )
    return instances


def _remote_summary_url(base_url: str, params: dict[str, str]) -> str:
    query_params = {key: value for key, value in params.items() if value and key != "federated"}
    query_params["federated"] = "0"
    query = urlencode(query_params)
    suffix = f"?{query}" if query else ""
    return f"{base_url.rstrip('/')}/credit-tracker/api/summary{suffix}"


def _remote_usage_rows_url(base_url: str, params: dict[str, str], limit: int = 10000) -> str:
    query_params = {key: value for key, value in params.items() if value and key != "federated"}
    query_params["limit"] = str(limit)
    query = urlencode(query_params)
    return f"{base_url.rstrip('/')}/credit-tracker/api/usage-rows?{query}"


def _fetch_remote_summary(instance: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    if not instance.get("enabled", True):
        return {
            "ok": False,
            "status": "disabled",
            "name": instance["name"],
            "base_url": instance["base_url"],
            "error": "Disabled in remote_instances.json",
            "totals": {},
        }

    url = _remote_summary_url(instance["base_url"], params)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=4) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except HTTPError as exc:
        return {
            "ok": False,
            "status": "error",
            "name": instance["name"],
            "base_url": instance["base_url"],
            "error": f"HTTP {exc.code}; tracker may not be installed or reachable",
            "totals": {},
        }
    except URLError as exc:
        return {
            "ok": False,
            "status": "offline",
            "name": instance["name"],
            "base_url": instance["base_url"],
            "error": str(exc),
            "totals": {},
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "name": instance["name"],
            "base_url": instance["base_url"],
            "error": str(exc),
            "totals": {},
        }

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if not isinstance(payload, dict):
        payload = {}
    payload["_instance"] = {
        "ok": True,
        "status": "online",
        "name": instance["name"],
        "base_url": instance["base_url"],
        "error": "",
        "elapsed_ms": elapsed_ms,
    }
    return payload


def _fetch_remote_usage_rows(instance: dict[str, Any], params: dict[str, str]) -> list[dict[str, Any]]:
    url = _remote_usage_rows_url(instance["base_url"], params)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=6) as response:
        raw = response.read().decode("utf-8")
        payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _sum_total(rows: list[dict[str, Any]], key: str) -> float:
    return round(sum(_safe_float(row.get(key), 0.0) for row in rows), 4)


def _merge_grouped_rows(rows: list[dict[str, Any]], group_keys: list[str], limit: int) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(group_key, "") for group_key in group_keys)
        target = merged.setdefault(
            key,
            {
                **{group_key: row.get(group_key, "") for group_key in group_keys},
                "total_runs": 0,
                "total_quantity": 0.0,
                "total_duration_seconds": 0.0,
                "total_estimated_credits": 0.0,
                "total_estimated_usd": 0.0,
            },
        )
        target["total_runs"] += int(_safe_float(row.get("total_runs"), 0.0))
        target["total_quantity"] += _safe_float(row.get("total_quantity"), 0.0)
        target["total_duration_seconds"] += _safe_float(row.get("total_duration_seconds"), 0.0)
        target["total_estimated_credits"] += _safe_float(row.get("total_estimated_credits"), 0.0)
        target["total_estimated_usd"] += _safe_float(row.get("total_estimated_usd"), 0.0)

    for row in merged.values():
        for key in ("total_quantity", "total_duration_seconds", "total_estimated_credits", "total_estimated_usd"):
            row[key] = round(row[key], 4)

    return sorted(
        merged.values(),
        key=lambda row: (-_safe_float(row.get("total_estimated_credits"), 0.0), -int(row.get("total_runs") or 0), str(row.get(group_keys[0], ""))),
    )[:limit]


def _usage_row_identity(row: dict[str, Any]) -> str:
    dedupe_key = str(row.get("dedupe_key") or "").strip()
    if dedupe_key:
        return f"dedupe:{dedupe_key}"

    prompt_id = str(row.get("prompt_id") or "").strip()
    node_id = str(row.get("node_id") or "").strip()
    node_class_type = str(row.get("node_class_type") or "").strip()
    credits = str(row.get("estimated_credits") or "").strip()
    if prompt_id and node_id:
        return f"prompt:{prompt_id}|{node_id}|{node_class_type}|{credits}"

    # Manual rows without prompt/dedupe data should not be merged across machines.
    return f"local:{row.get('instance_name', '')}|{row.get('id', '')}|{row.get('timestamp', '')}"


def _imported_from_instance(notes: Any) -> str:
    text = str(notes or "")
    origin_marker = "origin_instance="
    origin_start = text.find(origin_marker)
    if origin_start >= 0:
        origin_start += len(origin_marker)
        origin_end = text.find(";", origin_start)
        if origin_end < 0:
            origin_end = len(text)
        return text[origin_start:origin_end].strip()

    marker = "Imported from "
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = text.find(";", start)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def _with_instance_name(row: dict[str, Any], fallback_instance: str) -> dict[str, Any]:
    explicit_origin = _imported_from_instance(row.get("notes")) or str(row.get("instance_name") or "").strip()
    return {
        **row,
        "instance_name": explicit_origin or fallback_instance,
        "origin_known": bool(explicit_origin),
    }


def _dedupe_usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identity_counts: dict[str, int] = {}
    for row in rows:
        identity = _usage_row_identity(row)
        identity_counts[identity] = identity_counts.get(identity, 0) + 1

    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = _usage_row_identity(row)
        if identity in unique:
            continue
        selected = dict(row)
        selected["_identity_count"] = identity_counts.get(identity, 1)

        # Older tracker builds did not stamp origin_instance on direct runtime-price
        # rows. If the row exists on only one database, it is not a copied duplicate,
        # so attribute it to the database it came from. Duplicated un-stamped rows
        # still stay in the review bucket.
        if (
            not selected.get("origin_known")
            and selected["_identity_count"] == 1
            and str(selected.get("instance_name") or "").strip()
        ):
            selected["origin_known"] = True
            selected["origin_inferred"] = True
        unique[identity] = selected
    return list(unique.values())


def _filter_rows_by_timestamp_window(rows: list[dict[str, Any]], params: dict[str, str]) -> list[dict[str, Any]]:
    timestamp_from = str(params.get("from_ts") or "").strip()
    timestamp_to = str(params.get("to_ts") or "").strip()
    date_from, date_to = _date_filter_bounds(params)
    if not timestamp_from and not timestamp_to and not date_from and not date_to:
        return rows

    filtered: list[dict[str, Any]] = []
    for row in rows:
        timestamp = str(row.get("timestamp") or "").strip()
        local_day = _local_day_from_timestamp(timestamp)
        if (date_from or date_to) and not local_day:
            continue
        if date_from and local_day and local_day < date_from:
            continue
        if date_to and local_day and local_day > date_to:
            continue
        if timestamp_from and timestamp < timestamp_from:
            continue
        if timestamp_to and timestamp > timestamp_to:
            continue
        filtered.append(row)
    return filtered


def _instance_unique_totals(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = (
            str(row.get("instance_name") or "").strip()
            if row.get("origin_known")
            else "Origin unknown / copied DB"
        )
        if not name:
            name = "Unknown"
        target = grouped.setdefault(
            name,
            {
                "unique_runs": 0,
                "unique_credits": 0.0,
                "unique_usd": 0.0,
            },
        )
        target["unique_runs"] += 1
        target["unique_credits"] += _safe_float(row.get("estimated_credits"), 0.0)
        target["unique_usd"] += _safe_float(row.get("estimated_usd"), 0.0)
    for row in grouped.values():
        row["unique_credits"] = round(row["unique_credits"], 4)
        row["unique_usd"] = round(row["unique_usd"], 4)
    return grouped


def _usage_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_runs": len(rows),
        "total_quantity": _sum_total(rows, "quantity"),
        "total_duration_seconds": _sum_total(rows, "duration_seconds"),
        "total_estimated_credits": _sum_total(rows, "estimated_credits"),
        "total_estimated_usd": _sum_total(rows, "estimated_usd"),
    }


def _group_usage_rows(rows: list[dict[str, Any]], group_key: str, limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get(group_key) or "").strip() or "Unknown"
        target = grouped.setdefault(
            name,
            {
                group_key: name,
                "total_runs": 0,
                "total_quantity": 0.0,
                "total_duration_seconds": 0.0,
                "total_estimated_credits": 0.0,
                "total_estimated_usd": 0.0,
            },
        )
        target["total_runs"] += 1
        target["total_quantity"] += _safe_float(row.get("quantity"), 0.0)
        target["total_duration_seconds"] += _safe_float(row.get("duration_seconds"), 0.0)
        target["total_estimated_credits"] += _safe_float(row.get("estimated_credits"), 0.0)
        target["total_estimated_usd"] += _safe_float(row.get("estimated_usd"), 0.0)

    for row in grouped.values():
        for key in ("total_quantity", "total_duration_seconds", "total_estimated_credits", "total_estimated_usd"):
            row[key] = round(row[key], 4)

    return sorted(
        grouped.values(),
        key=lambda row: (-_safe_float(row.get("total_estimated_credits"), 0.0), -int(row.get("total_runs") or 0), str(row.get(group_key, ""))),
    )[:limit]


def _group_usage_rows_by_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = _local_day_from_timestamp(row.get("timestamp"))
        if not day:
            day = "Unknown"
        target = grouped.setdefault(
            day,
            {
                "day": day,
                "total_runs": 0,
                "total_quantity": 0.0,
                "total_duration_seconds": 0.0,
                "total_estimated_credits": 0.0,
                "total_estimated_usd": 0.0,
            },
        )
        target["total_runs"] += 1
        target["total_quantity"] += _safe_float(row.get("quantity"), 0.0)
        target["total_duration_seconds"] += _safe_float(row.get("duration_seconds"), 0.0)
        target["total_estimated_credits"] += _safe_float(row.get("estimated_credits"), 0.0)
        target["total_estimated_usd"] += _safe_float(row.get("estimated_usd"), 0.0)

    for row in grouped.values():
        for key in ("total_quantity", "total_duration_seconds", "total_estimated_credits", "total_estimated_usd"):
            row[key] = round(_safe_float(row.get(key), 0.0), 4)

    return sorted(grouped.values(), key=lambda row: str(row.get("day") or ""))


def _model_label(row: dict[str, Any]) -> str:
    for key in ("model_name", "node_title", "node_class_type"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "Unknown Model"


def _group_usage_rows_by_model(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        partner = str(row.get("partner_node_name") or "").strip() or "Unknown Partner"
        model = _model_label(row)
        key = (partner, model)
        target = grouped.setdefault(
            key,
            {
                "partner_node_name": partner,
                "model_name": model,
                "total_runs": 0,
                "total_quantity": 0.0,
                "total_duration_seconds": 0.0,
                "total_estimated_credits": 0.0,
                "total_estimated_usd": 0.0,
                "avg_credits_per_run": 0.0,
                "avg_usd_per_run": 0.0,
            },
        )
        target["total_runs"] += 1
        target["total_quantity"] += _safe_float(row.get("quantity"), 0.0)
        target["total_duration_seconds"] += _safe_float(row.get("duration_seconds"), 0.0)
        target["total_estimated_credits"] += _safe_float(row.get("estimated_credits"), 0.0)
        target["total_estimated_usd"] += _safe_float(row.get("estimated_usd"), 0.0)

    for row in grouped.values():
        runs = max(1, int(row.get("total_runs") or 0))
        row["avg_credits_per_run"] = round(_safe_float(row.get("total_estimated_credits"), 0.0) / runs, 4)
        row["avg_usd_per_run"] = round(_safe_float(row.get("total_estimated_usd"), 0.0) / runs, 4)
        for key in ("total_quantity", "total_duration_seconds", "total_estimated_credits", "total_estimated_usd"):
            row[key] = round(_safe_float(row.get(key), 0.0), 4)

    return sorted(
        grouped.values(),
        key=lambda row: (-_safe_float(row.get("total_estimated_credits"), 0.0), -int(row.get("total_runs") or 0), str(row.get("partner_node_name", "")), str(row.get("model_name", ""))),
    )[:limit]


def _federated_payload(local_payload: dict[str, Any], params: dict[str, str], limit: int) -> dict[str, Any]:
    local_name = _local_instance_name()
    local_totals = local_payload.get("totals") or {}
    instance_rows = [
        {
            "name": local_name,
            "base_url": "local",
            "status": "online",
            "ok": True,
            "runs": int(_safe_float(local_totals.get("total_runs"), 0.0)),
            "credits": _safe_float(local_totals.get("total_estimated_credits"), 0.0),
            "usd": _safe_float(local_totals.get("total_estimated_usd"), 0.0),
            "error": "",
        }
    ]

    local_usage_rows = [
        _with_instance_name(dict(row), local_name)
        for row in _usage_rows_payload({**params, "limit": "10000"}).get("rows", [])
    ]
    all_usage_rows = list(local_usage_rows)
    remote_payloads: list[dict[str, Any]] = []

    for instance in _load_remote_instances():
        remote = _fetch_remote_summary(instance, params)
        instance_meta = remote.get("_instance") or {
            "ok": remote.get("ok", False),
            "status": remote.get("status", "error"),
            "name": instance["name"],
            "base_url": instance["base_url"],
            "error": remote.get("error", "Unknown error"),
        }
        totals = remote.get("totals") if isinstance(remote.get("totals"), dict) else {}
        instance_rows.append(
            {
                "name": instance_meta.get("name", instance["name"]),
                "base_url": instance_meta.get("base_url", instance["base_url"]),
                "status": instance_meta.get("status", "online" if instance_meta.get("ok") else "error"),
                "ok": bool(instance_meta.get("ok")),
                "runs": int(_safe_float(totals.get("total_runs"), 0.0)),
                "credits": _safe_float(totals.get("total_estimated_credits"), 0.0),
                "usd": _safe_float(totals.get("total_estimated_usd"), 0.0),
                "elapsed_ms": instance_meta.get("elapsed_ms", ""),
                "error": instance_meta.get("error", ""),
            }
        )
        if not instance_meta.get("ok"):
            remote_payloads.append({"instance": instance_meta, "error": instance_meta.get("error", "")})
            continue

        try:
            remote_rows = _fetch_remote_usage_rows(instance, params)
        except Exception as exc:
            LOGGER.warning("Could not fetch raw usage rows from %s: %s", instance["name"], exc)
            remote_rows = [
                _with_instance_name(dict(row), str(instance_meta.get("name", instance["name"])))
                for row in remote.get("recent", [])
            ]
            instance_rows[-1]["error"] = f"Raw-row dedupe unavailable: {exc}"

        all_usage_rows.extend(
            _with_instance_name(dict(row), str(instance_meta.get("name", instance["name"])))
            for row in remote_rows
        )
        remote_payloads.append({"instance": instance_meta})

    all_usage_rows = _filter_rows_by_timestamp_window(all_usage_rows, params)
    deduped_usage_rows = _dedupe_usage_rows(all_usage_rows)
    combined_totals = _usage_totals(deduped_usage_rows)
    recent_rows = sorted(deduped_usage_rows, key=lambda row: str(row.get("timestamp") or ""), reverse=True)[:limit]
    unique_by_instance = _instance_unique_totals(deduped_usage_rows)
    for row in instance_rows:
        unique = unique_by_instance.get(str(row.get("name") or ""), {})
        row["raw_runs"] = row.get("runs", 0)
        row["raw_credits"] = row.get("credits", 0.0)
        row["raw_usd"] = row.get("usd", 0.0)
        row["runs"] = int(_safe_float(unique.get("unique_runs"), 0.0))
        row["credits"] = _safe_float(unique.get("unique_credits"), 0.0)
        row["usd"] = _safe_float(unique.get("unique_usd"), 0.0)
    known_instance_names = {str(row.get("name") or "") for row in instance_rows}
    for name, unique in unique_by_instance.items():
        if name in known_instance_names:
            continue
        instance_rows.append(
            {
                "name": name,
                "base_url": "deduped rows without origin_instance",
                "status": "review",
                "ok": True,
                "runs": int(_safe_float(unique.get("unique_runs"), 0.0)),
                "credits": _safe_float(unique.get("unique_credits"), 0.0),
                "usd": _safe_float(unique.get("unique_usd"), 0.0),
                "raw_runs": 0,
                "raw_credits": 0.0,
                "raw_usd": 0.0,
                "error": "",
            }
        )

    real_instance_rows = [row for row in instance_rows if row.get("status") != "review"]

    return {
        "config_path": str(REMOTE_INSTANCES_PATH.resolve()),
        "instances": instance_rows,
        "remote_count": max(0, len(real_instance_rows) - 1),
        "online_count": sum(1 for row in real_instance_rows if row.get("ok")),
        "offline_count": sum(1 for row in real_instance_rows if not row.get("ok")),
        "raw_row_count": len(all_usage_rows),
        "deduped_row_count": len(deduped_usage_rows),
        "deduped_duplicate_count": max(0, len(all_usage_rows) - len(deduped_usage_rows)),
        "totals": combined_totals,
        "by_partner": _group_usage_rows(deduped_usage_rows, "partner_node_name", limit),
        "by_project": _group_usage_rows(deduped_usage_rows, "project_name", limit),
        "by_model": _group_usage_rows_by_model(deduped_usage_rows, limit),
        "daily": _group_usage_rows_by_day(deduped_usage_rows),
        "expensive": sorted(deduped_usage_rows, key=lambda row: (_safe_float(row.get("estimated_credits"), 0.0), str(row.get("timestamp") or "")), reverse=True)[:limit],
        "recent": recent_rows,
        "remotes": remote_payloads,
    }


def _summary_payload(params: dict[str, str]) -> dict[str, Any]:
    where_sql, values = _query_filters(params)
    limit = _limit(params)

    with _connect() as connection:
        totals = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total_runs,
                ROUND(COALESCE(SUM(quantity), 0), 4) AS total_quantity,
                ROUND(COALESCE(SUM(duration_seconds), 0), 4) AS total_duration_seconds,
                ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
            FROM credit_usage
            {where_sql}
            """,
            values,
        ).fetchone()

        by_node = connection.execute(
            f"""
            SELECT
                partner_node_name,
                node_class_type,
                COUNT(*) AS total_runs,
                ROUND(COALESCE(SUM(quantity), 0), 4) AS total_quantity,
                ROUND(COALESCE(SUM(duration_seconds), 0), 4) AS total_duration_seconds,
                ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
            FROM credit_usage
            {where_sql}
            GROUP BY partner_node_name, node_class_type
            ORDER BY total_estimated_credits DESC, total_runs DESC, partner_node_name ASC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        by_partner = connection.execute(
            f"""
            SELECT
                partner_node_name,
                COUNT(*) AS total_runs,
                ROUND(COALESCE(SUM(quantity), 0), 4) AS total_quantity,
                ROUND(COALESCE(SUM(duration_seconds), 0), 4) AS total_duration_seconds,
                ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
            FROM credit_usage
            {where_sql}
            GROUP BY partner_node_name
            ORDER BY total_estimated_credits DESC, total_runs DESC, partner_node_name ASC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        by_model = connection.execute(
            f"""
            SELECT
                partner_node_name,
                COALESCE(NULLIF(model_name, ''), NULLIF(node_title, ''), NULLIF(node_class_type, ''), 'Unknown Model') AS model_name,
                COUNT(*) AS total_runs,
                ROUND(COALESCE(SUM(quantity), 0), 4) AS total_quantity,
                ROUND(COALESCE(SUM(duration_seconds), 0), 4) AS total_duration_seconds,
                ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd,
                ROUND(COALESCE(SUM(estimated_credits), 0) / MAX(COUNT(*), 1), 4) AS avg_credits_per_run,
                ROUND(COALESCE(SUM(estimated_usd), 0) / MAX(COUNT(*), 1), 4) AS avg_usd_per_run
            FROM credit_usage
            {where_sql}
            GROUP BY partner_node_name, COALESCE(NULLIF(model_name, ''), NULLIF(node_title, ''), NULLIF(node_class_type, ''), 'Unknown Model')
            ORDER BY total_estimated_credits DESC, total_runs DESC, partner_node_name ASC, model_name ASC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        by_project = connection.execute(
            f"""
            SELECT
                project_name,
                COUNT(*) AS total_runs,
                ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
            FROM credit_usage
            {where_sql}
            GROUP BY project_name
            ORDER BY total_estimated_credits DESC, total_runs DESC, project_name ASC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        by_user = connection.execute(
            f"""
            SELECT
                user_name,
                COUNT(*) AS total_runs,
                ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
            FROM credit_usage
            {where_sql}
            GROUP BY user_name
            ORDER BY total_estimated_credits DESC, total_runs DESC, user_name ASC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        by_workflow = connection.execute(
            f"""
            SELECT
                workflow_name,
                COUNT(*) AS total_runs,
                ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
            FROM credit_usage
            {where_sql}
            GROUP BY workflow_name
            ORDER BY total_estimated_credits DESC, total_runs DESC, workflow_name ASC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        daily = connection.execute(
            f"""
            SELECT
                {LOCAL_DATE_SQL} AS day,
                COUNT(*) AS total_runs,
                ROUND(COALESCE(SUM(quantity), 0), 4) AS total_quantity,
                ROUND(COALESCE(SUM(duration_seconds), 0), 4) AS total_duration_seconds,
                ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
            FROM credit_usage
            {where_sql}
            GROUP BY {LOCAL_DATE_SQL}
            ORDER BY day ASC
            """,
            values,
        ).fetchall()

        expensive = connection.execute(
            f"""
            SELECT {", ".join(FULL_COLUMNS)}
            FROM credit_usage
            {where_sql}
            ORDER BY estimated_credits DESC, id DESC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        data_quality = connection.execute(
            f"""
            SELECT
                SUM(CASE WHEN project_name IS NULL OR project_name = '' OR project_name = 'General' THEN 1 ELSE 0 END) AS missing_project,
                SUM(CASE WHEN user_name IS NULL OR user_name = '' OR user_name = 'Unknown' THEN 1 ELSE 0 END) AS missing_user,
                SUM(CASE WHEN workflow_name IS NULL OR workflow_name = '' OR workflow_name = 'Auto-detected Workflow' OR workflow_name = 'Untitled Workflow' THEN 1 ELSE 0 END) AS missing_workflow,
                SUM(CASE WHEN partner_node_name IS NULL OR partner_node_name = '' OR partner_node_name LIKE 'Unknown%' THEN 1 ELSE 0 END) AS unknown_partner,
                SUM(CASE WHEN node_class_type IS NULL OR node_class_type = '' THEN 1 ELSE 0 END) AS missing_class_type,
                SUM(CASE WHEN source IS NULL OR source = '' OR source != 'runtime_price' THEN 1 ELSE 0 END) AS not_runtime_price
            FROM credit_usage
            {where_sql}
            """,
            values,
        ).fetchone()

        recent = connection.execute(
            f"""
            SELECT {", ".join(FULL_COLUMNS)}
            FROM credit_usage
            {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        filter_options = {
            "projects": [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT project_name
                    FROM credit_usage
                    WHERE project_name IS NOT NULL AND project_name != ''
                    ORDER BY project_name ASC
                    """
                ).fetchall()
            ],
            "partners": [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT partner_node_name
                    FROM credit_usage
                    WHERE partner_node_name IS NOT NULL AND partner_node_name != ''
                    ORDER BY partner_node_name ASC
                    """
                ).fetchall()
            ],
            "sources": [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT source
                    FROM credit_usage
                    WHERE source IS NOT NULL AND source != ''
                    ORDER BY source ASC
                    """
                ).fetchall()
            ],
        }

    balance = _fetch_credit_balance()
    _capture_balance_snapshot(balance, source="dashboard")

    payload = {
        "balance": balance,
        "credits_per_usd": CREDITS_PER_USD,
        "database": str(Path(DB_PATH).resolve()),
        "filters": dict(params),
        "date_range": _date_filter_payload(params),
        "filter_options": filter_options,
        "pricing_cache": {
            key: value
            for key, value in load_pricing_cache().items()
            if key != "rows"
        },
        "official_usage": official_usage_summary(limit),
        "totals": dict(totals),
        "by_node": _rows_to_dicts(by_node),
        "by_partner": _rows_to_dicts(by_partner),
        "by_model": _rows_to_dicts(by_model),
        "by_project": _rows_to_dicts(by_project),
        "by_user": _rows_to_dicts(by_user),
        "by_workflow": _rows_to_dicts(by_workflow),
        "daily": _rows_to_dicts(daily),
        "expensive": _rows_to_dicts(expensive),
        "data_quality": dict(data_quality),
        "recent": _rows_to_dicts(recent),
    }
    if params.get("federated", "1") != "0":
        payload["federated"] = _federated_payload(payload, params, limit)
    tracked_totals = payload.get("federated", {}).get("totals") or payload.get("totals") or {}
    payload["balance_reconciliation"] = _balance_reconciliation_payload(
        _safe_float(tracked_totals.get("total_estimated_credits"), 0.0),
        _safe_float(tracked_totals.get("total_estimated_usd"), 0.0),
        params,
    )
    payload["health"] = _system_health_payload(payload)
    return payload


def _csv_response(filename: str, fieldnames: list[str], rows: list[sqlite3.Row]):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row[field] for field in fieldnames})
    return web.Response(
        text=buffer.getvalue(),
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_rows(report_type: str, params: dict[str, str]):
    where_sql, values = _query_filters(params)
    with _connect() as connection:
        if report_type == "by-node":
            fieldnames = [
                "partner_node_name",
                "node_class_type",
                "total_runs",
                "total_quantity",
                "total_duration_seconds",
                "total_estimated_credits",
                "total_estimated_usd",
            ]
            rows = connection.execute(
                f"""
                SELECT
                    partner_node_name,
                    node_class_type,
                    COUNT(*) AS total_runs,
                    ROUND(COALESCE(SUM(quantity), 0), 4) AS total_quantity,
                    ROUND(COALESCE(SUM(duration_seconds), 0), 4) AS total_duration_seconds,
                    ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                    ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
                FROM credit_usage
                {where_sql}
                GROUP BY partner_node_name, node_class_type
                ORDER BY total_estimated_credits DESC, total_runs DESC, partner_node_name ASC
                """,
                values,
            ).fetchall()
            return _csv_response("credit_usage_summary_by_node.csv", fieldnames, rows)

        if report_type == "by-project":
            fieldnames = [
                "project_name",
                "total_runs",
                "total_estimated_credits",
                "total_estimated_usd",
            ]
            rows = connection.execute(
                f"""
                SELECT
                    project_name,
                    COUNT(*) AS total_runs,
                    ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                    ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
                FROM credit_usage
                {where_sql}
                GROUP BY project_name
                ORDER BY total_estimated_credits DESC, total_runs DESC, project_name ASC
                """,
                values,
            ).fetchall()
            return _csv_response("credit_usage_summary_by_project.csv", fieldnames, rows)

        rows = connection.execute(
            f"""
            SELECT {", ".join(FULL_COLUMNS)}
            FROM credit_usage
            {where_sql}
            ORDER BY timestamp ASC, id ASC
            """,
            values,
        ).fetchall()
        return _csv_response("credit_usage_full.csv", FULL_COLUMNS, rows)


def _usage_rows_payload(params: dict[str, str], *, max_limit: int = 10000) -> dict[str, Any]:
    where_sql, values = _query_filters(params)
    try:
        limit = max(1, min(int(params.get("limit", max_limit)), max_limit))
    except ValueError:
        limit = max_limit

    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT {", ".join(FULL_COLUMNS)}
            FROM credit_usage
            {where_sql}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

    return {
        "instance_name": _local_instance_name(),
        "database": str(Path(DB_PATH).resolve()),
        "rows": _rows_to_dicts(rows),
        "row_count": len(rows),
        "limit": limit,
    }


def _ingest_usage_rows(rows: list[dict[str, Any]], source_instance: str = "peer") -> dict[str, Any]:
    inserted = 0
    skipped = 0
    errors: list[str] = []

    for raw in rows:
        if not isinstance(raw, dict):
            skipped += 1
            continue

        estimated_credits = _safe_float(raw.get("estimated_credits"), 0.0)
        estimated_usd = _safe_float(raw.get("estimated_usd"), 0.0)
        if not estimated_usd and estimated_credits and CREDITS_PER_USD:
            estimated_usd = round(estimated_credits / CREDITS_PER_USD, 6)

        dedupe_key = str(raw.get("dedupe_key") or "").strip()
        if not dedupe_key:
            dedupe_key = make_dedupe_key(
                "peer_import",
                source_instance,
                raw.get("id", ""),
                raw.get("timestamp", ""),
                raw.get("partner_node_name", ""),
                estimated_credits,
            )

        notes = str(raw.get("notes") or "")
        import_note = f"Imported from {source_instance}; original_id={raw.get('id', '')}"
        if import_note not in notes:
            notes = f"{notes}; {import_note}".strip("; ")

        try:
            record = UsageRecord(
                timestamp=str(raw.get("timestamp") or ""),
                project_name=str(raw.get("project_name") or "General"),
                user_name=str(raw.get("user_name") or "Unknown"),
                workflow_name=str(raw.get("workflow_name") or "Auto-detected Workflow"),
                partner_node_name=str(raw.get("partner_node_name") or "Unknown Partner Node"),
                pricing_mode=str(raw.get("pricing_mode") or "unknown"),
                quantity=int(_safe_float(raw.get("quantity"), 1.0)),
                duration_seconds=_safe_float(raw.get("duration_seconds"), 0.0),
                resolution=str(raw.get("resolution") or ""),
                estimated_credits=estimated_credits,
                estimated_usd=estimated_usd,
                notes=notes,
                prompt_id=str(raw.get("prompt_id") or ""),
                node_id=str(raw.get("node_id") or ""),
                node_class_type=str(raw.get("node_class_type") or ""),
                node_title=str(raw.get("node_title") or ""),
                model_name=str(raw.get("model_name") or ""),
                input_summary=str(raw.get("input_summary") or ""),
                source=str(raw.get("source") or "peer_sync"),
                dedupe_key=dedupe_key,
            )
            if insert_usage_record(record, sync_peers=False):
                inserted += 1
            else:
                skipped += 1
        except Exception as exc:
            skipped += 1
            errors.append(str(exc))

    return {
        "ok": not errors,
        "source_instance": source_instance,
        "received": len(rows),
        "inserted": inserted,
        "skipped": skipped,
        "errors": errors[:10],
    }


def _sync_pull_from_peers(params: dict[str, str]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_inserted = 0
    total_skipped = 0

    for instance in _load_remote_instances():
        if not instance.get("enabled", True):
            continue
        try:
            rows = _fetch_remote_usage_rows(instance, {**params, "limit": "10000"})
            result = _ingest_usage_rows(rows, source_instance=str(instance.get("name") or instance.get("base_url") or "peer"))
        except Exception as exc:
            result = {
                "ok": False,
                "source_instance": instance.get("name", instance.get("base_url", "peer")),
                "received": 0,
                "inserted": 0,
                "skipped": 0,
                "errors": [str(exc)],
            }
        total_inserted += int(result.get("inserted") or 0)
        total_skipped += int(result.get("skipped") or 0)
        results.append(result)

    return {
        "ok": all(result.get("ok") for result in results) if results else True,
        "inserted": total_inserted,
        "skipped": total_skipped,
        "peers": results,
    }


def _is_peer_sync_authorized(request, payload: dict[str, Any]) -> bool:
    if not PEER_SYNC_TOKEN:
        return True
    header_token = request.headers.get("X-Credit-Tracker-Token", "")
    payload_token = str(payload.get("sync_token") or "")
    return header_token == PEER_SYNC_TOKEN or payload_token == PEER_SYNC_TOKEN


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ComfyUI Credit Tracker</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #121827; }
    header { padding: 24px 36px 18px; background: #fff; border-bottom: 1px solid #dbe3ef; position: sticky; top: 0; z-index: 20; }
    h1 { margin: 0; font-size: 30px; letter-spacing: 0; }
    header p { margin: 6px 0 0; color: #526177; }
    main { padding: 20px 36px 40px; }
    .header-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; margin-bottom: 18px; }
    .header-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .export-menu { position: relative; }
    .export-options { display: none; position: absolute; right: 0; top: 42px; min-width: 180px; padding: 6px; background: #fff; border: 1px solid #cfd8e6; border-radius: 8px; box-shadow: 0 16px 40px rgba(15, 23, 42, 0.14); z-index: 30; }
    .export-options.open { display: grid; gap: 4px; }
    .export-options a { color: #121827; text-decoration: none; padding: 9px 10px; border-radius: 6px; font-size: 14px; }
    .export-options a:hover { background: #f1f5fb; }
    .filter-bar { display: grid; grid-template-columns: minmax(150px, 180px) minmax(260px, auto) minmax(160px, 1fr) minmax(190px, 1.2fr) minmax(150px, 180px) 90px 92px; gap: 10px; align-items: end; }
    .range-inputs { display: grid; }
    .range-inputs[hidden] { display: none; }
    .custom-range { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .custom-range[hidden] { display: none; }
    .cards, .grid { display: grid; gap: 16px; }
    label { display: grid; gap: 5px; font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; letter-spacing: .04em; }
    input, select, button, a.button { height: 36px; border: 1px solid #cfd8e6; border-radius: 7px; background: #fff; color: #121827; padding: 0 10px; font-size: 14px; box-sizing: border-box; }
    button, a.button { display: inline-grid; place-items: center; background: #276fe0; color: #fff; border-color: #276fe0; text-decoration: none; cursor: pointer; font-weight: 800; }
    button.ghost, a.button.ghost { background: transparent; color: #276fe0; border-color: #b8c9e8; }
    button.ghost:hover, a.button.ghost:hover { background: #eef5ff; }
    button.refresh { height: 36px; padding: 0 12px; font-size: 13px; }
    .cards { grid-template-columns: 1.6fr 1.2fr 1fr .9fr; margin-bottom: 18px; }
    .card, section { background: #fff; border: 1px solid #dbe3ef; border-radius: 8px; }
    .card { padding: 18px; min-height: 104px; }
    .card .label { display: block; color: #64748b; font-size: 11px; margin-bottom: 8px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; }
    .card .value { font-size: 42px; line-height: 1; font-weight: 900; letter-spacing: 0; }
    .card .sub { margin-top: 10px; color: #526177; font-size: 14px; }
    .network-card { border-color: #b8c9e8; box-shadow: inset 5px 0 0 #276fe0; }
    .network-card .value { font-size: 58px; color: #0f3f9b; }
    .network-card .sub strong { color: #121827; }
    .network-card .local-sub { color: #64748b; font-size: 13px; }
    .balance-positive { color: #078067; }
    .card .split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .card .mini-value { font-size: 26px; font-weight: 900; line-height: 1.1; }
    .card.warning-card.active { background: #fff7ed; border-color: #fdba74; }
    .warning-icon { display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 999px; background: #e2e8f0; color: #64748b; font-weight: 900; margin-right: 8px; }
    .warning-card.active .warning-icon { background: #f59e0b; color: #fff; }
    .project-overview { margin-bottom: 18px; overflow: hidden; }
    .project-overview[hidden] { display: none; }
    .project-summary { display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 12px; padding: 18px; border-bottom: 1px solid #edf1f6; }
    .project-metric { border: 1px solid #e2e8f0; border-radius: 7px; padding: 12px; background: #fbfdff; min-height: 78px; }
    .project-metric span { display: block; color: #64748b; font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; }
    .project-metric strong { display: block; font-size: 24px; line-height: 1.1; font-weight: 900; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .project-metric small { display: block; margin-top: 6px; color: #526177; font-size: 12px; overflow-wrap: anywhere; }
    .project-body { display: grid; grid-template-columns: 1.15fr .85fr; gap: 18px; padding: 18px; }
    .project-block { min-width: 0; }
    .project-block h3 { margin: 0 0 10px; font-size: 15px; }
    .project-highlights { display: grid; gap: 10px; margin-bottom: 18px; }
    .project-highlight { display: grid; grid-template-columns: minmax(130px, .8fr) 1fr; gap: 12px; padding: 10px 0; border-bottom: 1px solid #edf1f6; }
    .project-highlight span { color: #64748b; font-size: 12px; font-weight: 800; text-transform: uppercase; }
    .project-highlight strong { overflow-wrap: anywhere; }
    .grid { grid-template-columns: 1fr 1fr; }
    section { overflow: visible; }
    section h2 { margin: 0; padding: 16px 18px; font-size: 18px; border-bottom: 1px solid #dbe3ef; }
    .section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 18px; border-bottom: 1px solid #dbe3ef; }
    .section-head h2 { padding: 0; border-bottom: 0; }
    button.compact { height: 30px; padding: 0 10px; font-size: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; table-layout: fixed; }
    th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #edf1f6; vertical-align: top; overflow: visible; }
    th { color: #526177; font-size: 12px; text-transform: uppercase; }
    th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
    #instances { table-layout: fixed; }
    #instances th, #instances td { vertical-align: middle; }
    #instances th:nth-child(1), #instances td:nth-child(1) { width: 18%; }
    #instances th:nth-child(2), #instances td:nth-child(2) { width: 130px; }
    #instances th:nth-child(3), #instances td:nth-child(3) { width: 130px; text-align: right; }
    #instances th:nth-child(4), #instances td:nth-child(4),
    #instances th:nth-child(5), #instances td:nth-child(5) { width: 140px; text-align: right; }
    #instances th:nth-child(6), #instances td:nth-child(6) { width: auto; padding-left: 28px; }
    #instances td:nth-child(6) { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    code { font-family: Consolas, monospace; font-size: 12px; color: #526177; }
    .wide { grid-column: 1 / -1; }
    .chart { width: 100%; height: 240px; display: block; }
    .daily-summary { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 12px; padding: 18px; border-bottom: 1px solid #edf1f6; }
    .daily-summary-item { border: 1px solid #e2e8f0; border-radius: 7px; padding: 12px; background: #fbfdff; }
    .daily-summary-item span { display: block; color: #64748b; font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; }
    .daily-summary-item strong { display: block; font-size: 24px; line-height: 1.1; font-weight: 900; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .truncate { position: relative; display: inline-block; max-width: 100%; cursor: help; overflow: visible; }
    .truncate:hover::after { content: attr(data-full); position: absolute; left: 0; top: 120%; z-index: 50; max-width: 460px; min-width: 180px; padding: 8px 10px; color: #fff; background: #111827; border-radius: 6px; box-shadow: 0 12px 30px rgba(15, 23, 42, .24); white-space: normal; overflow-wrap: anywhere; font-family: Inter, Segoe UI, Arial, sans-serif; font-size: 12px; line-height: 1.35; }
    .share-layout { display: grid; grid-template-columns: minmax(280px, 420px) 1fr; gap: 18px; align-items: center; padding: 18px; }
    .share-chart { width: 100%; height: 300px; display: block; }
    .share-legend { display: grid; gap: 10px; }
    .share-row { display: grid; grid-template-columns: 14px minmax(150px, 1fr) 100px 110px 110px; gap: 10px; align-items: center; }
    .swatch { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
    .share-head { color: #526177; font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .share-name { overflow-wrap: anywhere; }
    .share-value { text-align: right; font-variant-numeric: tabular-nums; }
    .status-line { padding: 0 18px 14px; color: #526177; font-size: 13px; }
    .status-line strong { color: #121827; }
    .status-line.error { color: #b4233b; }
    .health-grid { display: grid; grid-template-columns: repeat(5, minmax(150px, 1fr)); gap: 12px; padding: 18px; }
    .health-tile { border: 1px solid #e2e8f0; border-radius: 7px; padding: 12px; background: #fbfdff; min-height: 82px; }
    .health-tile.good { border-color: #bbf7d0; background: #f0fdf4; }
    .health-tile.warn { border-color: #fdba74; background: #fff7ed; }
    .health-tile.bad { border-color: #fecdd3; background: #fff1f2; }
    .health-label { display: block; color: #64748b; font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; }
    .health-value { display: block; font-size: 22px; font-weight: 900; line-height: 1.1; }
    .health-note { display: block; margin-top: 7px; color: #526177; font-size: 12px; overflow-wrap: anywhere; }
    .reconcile { display: grid; grid-template-columns: repeat(5, minmax(140px, 1fr)); gap: 12px; padding: 18px; }
    .reconcile-item { border: 1px solid #e2e8f0; border-radius: 7px; padding: 12px; background: #fbfdff; }
    .reconcile-item span { display: block; color: #64748b; font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; }
    .reconcile-item strong { display: block; font-size: 24px; line-height: 1.1; font-weight: 900; font-variant-numeric: tabular-nums; }
    .reconcile-item.warning { background: #fff7ed; border-color: #fdba74; color: #121827; }
    .badge { display: inline-grid; place-items: center; min-width: 68px; height: 24px; padding: 0 8px; border-radius: 999px; font-size: 12px; font-weight: 800; text-transform: uppercase; }
    .badge.online { color: #05603a; background: #dcfce7; }
    .badge.offline, .badge.error { color: #9f1239; background: #ffe4e6; }
    .badge.disabled { color: #475569; background: #e2e8f0; }
    .bars { padding: 16px 18px 20px; display: grid; gap: 12px; }
    .bar-row { display: grid; grid-template-columns: minmax(150px, 1fr) 2fr auto; gap: 12px; align-items: center; }
    .bar-track { height: 12px; background: #e8eef7; border-radius: 999px; overflow: hidden; }
    .bar-fill { height: 100%; background: #276fe0; border-radius: 999px; }
    .warning { color: #b4233b; font-weight: 700; }
    details.collapsible-section { background: #fff; border: 1px solid #dbe3ef; border-radius: 8px; }
    details.collapsible-section summary { cursor: pointer; padding: 16px 18px; font-size: 18px; font-weight: 800; list-style: none; border-bottom: 1px solid transparent; }
    details.collapsible-section summary::-webkit-details-marker { display: none; }
    details.collapsible-section summary::after { content: "+"; float: right; color: #276fe0; font-weight: 900; }
    details.collapsible-section[open] summary { border-bottom-color: #dbe3ef; }
    details.collapsible-section[open] summary::after { content: "-"; }
    @media (max-width: 1100px) { .header-row, .filter-bar, .cards, .grid, .share-layout, .reconcile, .health-grid, .project-summary, .project-body, .daily-summary { grid-template-columns: 1fr; display: grid; } .header-actions { justify-content: start; } .share-row { grid-template-columns: 14px minmax(120px, 1fr) 80px 90px; } .share-budget { display: none; } header { position: static; } }
  </style>
</head>
<body>
  <header>
    <div class="header-row">
      <div>
        <h1>ComfyUI Credit Tracker</h1>
        <p>Background Partner/API Node spend monitor</p>
      </div>
      <div class="header-actions">
        <div class="export-menu">
          <button class="ghost" onclick="toggleExportMenu(event)">&#8595; Export Data</button>
          <div class="export-options" id="exportOptions">
            <a id="fullCsv" href="#">Full CSV</a>
            <a id="nodeCsv" href="#">Node CSV</a>
            <a id="projectCsv" href="#">Project CSV</a>
          </div>
        </div>
        <button class="ghost" onclick="syncPeersNow(this)">Sync Peers Now</button>
        <button class="ghost" onclick="syncOfficialUsage(this)">Sync Account Usage</button>
        <button class="ghost" onclick="syncPricing(this)">Sync Pricing</button>
      </div>
    </div>
    <div class="filter-bar">
      <label>Date Range
        <select id="dateRange" onchange="handleDateRangeChange()">
          <option value="all">All Time</option>
          <option value="today">Today</option>
          <option value="day">Specific Day</option>
          <option value="7">Last 7 Days</option>
          <option value="30">Last 30 Days</option>
          <option value="month">This Month</option>
          <option value="custom">Custom Range...</option>
        </select>
      </label>
      <div class="range-inputs" id="rangeInputs" hidden>
        <label id="singleDayField" hidden>Day <input id="day" type="date" onchange="loadData()"></label>
        <div class="custom-range" id="customRange" hidden>
          <label>From <input id="from" type="date" onchange="loadData()"></label>
          <label>To <input id="to" type="date" onchange="loadData()"></label>
        </div>
      </div>
      <label>Project <select id="project" onchange="loadData()"><option value="">All projects</option></select></label>
      <label>Partner Node <select id="partner" onchange="loadData()"><option value="">All nodes</option></select></label>
      <label>Source <select id="source" onchange="loadData()"><option value="">All sources</option></select></label>
      <label>Rows <input id="limit" type="number" min="1" max="200" value="20" onchange="loadData()"></label>
      <button class="refresh" onclick="loadData()">Refresh</button>
    </div>
  </header>
  <main>
    <div class="cards">
      <div class="card network-card">
        <span class="label">Network Credits Spent</span>
        <div class="value" id="networkCredits">0</div>
        <div class="sub">Network USD <strong id="networkUsd">$0.00</strong></div>
        <div class="sub local-sub">This PC only <strong id="credits">0</strong> credits / <strong id="usd">$0.00</strong></div>
      </div>
      <div class="card">
        <span class="label">Current Balance</span>
        <div class="value" id="balance">--</div>
        <div class="sub">Balance USD <strong id="balanceUsd">--</strong></div>
      </div>
      <div class="card">
        <span class="label">Efficiency</span>
        <div class="split">
          <div><div class="mini-value" id="runs">0</div><div class="sub">Runs</div></div>
          <div><div class="mini-value" id="rate">211</div><div class="sub">Credits/USD</div></div>
        </div>
      </div>
      <div class="card warning-card" id="warningsCard">
        <span class="label"><span class="warning-icon">!</span>Data Warnings</span>
        <div class="value" id="warnings">0</div>
        <div class="sub">Records needing review</div>
      </div>
    </div>
    <section class="project-overview" id="projectOverview" hidden>
      <div class="section-head">
        <h2 id="projectOverviewTitle">Project Overview</h2>
        <span class="badge online" id="projectOverviewBadge">Project</span>
      </div>
      <div class="project-summary" id="projectSummary"></div>
      <div class="project-body">
        <div class="project-block">
          <h3>Spend By Model</h3>
          <table id="projectModels"></table>
        </div>
        <div class="project-block">
          <h3>Project Highlights</h3>
          <div class="project-highlights" id="projectHighlights"></div>
          <h3>Most Expensive Runs</h3>
          <table id="projectExpensive"></table>
        </div>
      </div>
    </section>
    <div class="grid">
      <section class="wide">
        <div class="section-head"><h2>System Health & Backups</h2><span class="badge" id="healthBadge">Checking</span></div>
        <div class="status-line" id="healthStatus">Reading tracker health</div>
        <div class="health-grid" id="healthGrid"></div>
      </section>
      <section class="wide"><h2>Credits Over Time</h2><canvas class="chart" id="dailyChart"></canvas></section>
      <section class="wide"><div class="section-head"><h2>Daily Breakdown</h2><span class="badge" id="dailyRangeBadge">All Time</span></div><div class="status-line" id="dailyStatus">Select a date range to review daily spend.</div><div class="daily-summary" id="dailySummary"></div><table id="dailyBreakdown"></table></section>
      <section class="wide"><h2>Spend Share by Partner Node</h2><div class="share-layout"><canvas class="share-chart" id="partnerShareChart"></canvas><div class="share-legend" id="partnerShareLegend"></div></div></section>
      <section class="wide"><h2>Balance Reconciliation</h2><div class="status-line" id="balanceReconciliationStatus">Waiting for balance snapshots</div><div class="reconcile" id="balanceReconciliation"></div></section>
      <section class="wide"><h2>Federated Instances</h2><div class="status-line" id="federatedStatus">Reading local tracker only</div><table id="instances"></table></section>
      <section class="wide"><h2>Network Spend by Partner Node</h2><table id="networkNodes"></table></section>
      <section><h2>Top Partner Nodes</h2><table id="nodes"></table></section>
      <section><h2>Top Projects</h2><table id="projects"></table></section>
      <section><h2>Top Users</h2><table id="users"></table></section>
      <section><h2>Top Workflows</h2><table id="workflows"></table></section>
      <section><h2>Data Quality</h2><div class="bars" id="quality"></div></section>
      <section><div class="section-head"><h2>Most Expensive Runs</h2><button class="ghost compact" id="toggleExpensive" onclick="toggleExpensive()">View All</button></div><table id="expensive"></table></section>
      <section class="wide"><h2>Official Comfy Account Usage</h2><div class="status-line" id="officialStatus">Not synced yet</div><table id="officialUsage"></table></section>
      <section class="wide"><div class="section-head"><h2>Network Recent Runs</h2><button class="ghost compact" id="toggleNetworkRecent" onclick="toggleNetworkRecent()">View All</button></div><table id="networkRecent"></table></section>
      <section class="wide"><div class="section-head"><h2>Recent Runs</h2><button class="ghost compact" id="toggleRecent" onclick="toggleRecent()">View All</button></div><table id="recent"></table></section>
      <details class="wide collapsible-section" id="pricingDetails"><summary>Official Pricing Cache</summary><table id="pricing"></table></details>
    </div>
  </main>
  <script>
    const fmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
    const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });
    const dateFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
    const shareColors = ["#276fe0", "#1c9a8a", "#d24b63", "#8b6bd9", "#e58f2a", "#2f8ccf", "#5c7c2f", "#a95f37", "#64748b"];
    const numericHeads = new Set(["Runs", "Tasks", "Credits", "USD", "Share", "Budget Used", "Avg / Run"]);
    let lastDashboardData = null;
    let showAllRecent = false;
    let showAllNetworkRecent = false;
    let showAllExpensive = false;
    function localDateValue(date = new Date()) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    }
    function params() {
      const q = new URLSearchParams();
      const range = document.getElementById("dateRange").value;
      const today = localDateValue();
      if (range === "today") {
        q.set("day", today);
        q.set("from", today);
        q.set("to", today);
      } else if (range === "day") {
        const day = document.getElementById("day").value.trim();
        if (day) {
          q.set("day", day);
          q.set("from", day);
          q.set("to", day);
        }
      } else if (range === "7" || range === "30") {
        q.set("days", range);
      } else if (range === "month") {
        const now = new Date();
        q.set("from", localDateValue(new Date(now.getFullYear(), now.getMonth(), 1)));
        q.set("to", today);
      } else if (range === "custom") {
        for (const id of ["from", "to"]) {
          const value = document.getElementById(id).value.trim();
          if (value) q.set(id, value);
        }
      }
      for (const id of ["project", "partner", "source", "limit"]) {
        const value = document.getElementById(id).value.trim();
        if (value) q.set(id, value);
      }
      return q;
    }
    function handleDateRangeChange() {
      const range = document.getElementById("dateRange").value;
      document.getElementById("rangeInputs").hidden = range !== "day" && range !== "custom";
      document.getElementById("singleDayField").hidden = range !== "day";
      document.getElementById("customRange").hidden = range !== "custom";
      if (range === "day" && !document.getElementById("day").value) {
        document.getElementById("day").value = localDateValue();
      }
      loadData();
    }
    function toggleExportMenu(event) {
      event.stopPropagation();
      document.getElementById("exportOptions").classList.toggle("open");
    }
    document.addEventListener("click", () => document.getElementById("exportOptions").classList.remove("open"));
    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
    }
    function trunc(value, limit = 15) {
      const text = String(value ?? "");
      if (text.length <= limit) return esc(text);
      return `<span class="truncate" data-full="${esc(text)}">${esc(text.slice(0, limit))}...</span>`;
    }
    function table(id, head, rows, render) {
      const el = document.getElementById(id);
      el.innerHTML = `<thead><tr>${head.map(h => `<th class="${numericHeads.has(h) ? "num" : ""}">${h}</th>`).join("")}</tr></thead><tbody>${rows.map(render).join("") || `<tr><td colspan="${head.length}">No records</td></tr>`}</tbody>`;
    }
    function visibleRows(rows, expanded, count = 5) {
      const safeRows = rows || [];
      return expanded ? safeRows : safeRows.slice(0, count);
    }
    function toggleRecent() {
      showAllRecent = !showAllRecent;
      if (lastDashboardData) renderRecentRuns(lastDashboardData);
    }
    function toggleNetworkRecent() {
      showAllNetworkRecent = !showAllNetworkRecent;
      if (lastDashboardData?.federated) renderFederated(lastDashboardData.federated);
    }
    function toggleExpensive() {
      showAllExpensive = !showAllExpensive;
      if (lastDashboardData) renderExpensiveRuns(lastDashboardData);
    }
    function setSelectOptions(id, values, allLabel) {
      const select = document.getElementById(id);
      const current = select.value;
      const unique = [...new Set((values || []).filter(Boolean))];
      select.innerHTML = `<option value="">${allLabel}</option>` + unique.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
      if (current && !unique.includes(current)) {
        select.insertAdjacentHTML("beforeend", `<option value="${esc(current)}">${esc(current)}</option>`);
      }
      select.value = current;
    }
    function updateFilterOptions(options) {
      setSelectOptions("project", options.projects || [], "All projects");
      setSelectOptions("partner", options.partners || [], "All nodes");
      setSelectOptions("source", options.sources || [], "All sources");
    }
    function drawDailyChart(rows) {
      const canvas = document.getElementById("dailyChart");
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const pad = { left: 54, right: 22, top: 20, bottom: 42 };
      const w = rect.width - pad.left - pad.right;
      const h = rect.height - pad.top - pad.bottom;
      if (!rows.length) {
        const cx = rect.width / 2;
        const cy = rect.height / 2 - 4;
        ctx.strokeStyle = "#b8c7da";
        ctx.lineWidth = 3;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(cx - 86, cy + 34);
        ctx.lineTo(cx - 86, cy - 34);
        ctx.lineTo(cx + 86, cy - 34);
        ctx.stroke();
        ctx.strokeStyle = "#8aa7d6";
        ctx.beginPath();
        ctx.moveTo(cx - 70, cy + 18);
        ctx.lineTo(cx - 30, cy - 6);
        ctx.lineTo(cx + 8, cy + 6);
        ctx.lineTo(cx + 52, cy - 24);
        ctx.stroke();
        ctx.fillStyle = "#526177";
        ctx.font = "600 14px Segoe UI, Arial";
        ctx.textAlign = "center";
        ctx.fillText("No credit usage recorded in this date range.", cx, cy + 62);
        ctx.textAlign = "left";
        return;
      }
      if (rows.length === 1) {
        const row = rows[0];
        const credits = Number(row.total_estimated_credits || 0);
        const usd = Number(row.total_estimated_usd || 0);
        const cx = rect.width / 2;
        const base = pad.top + h;
        const barHeight = Math.max(14, h * 0.72);
        ctx.strokeStyle = "#dbe3ef";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pad.left, pad.top);
        ctx.lineTo(pad.left, base);
        ctx.lineTo(pad.left + w, base);
        ctx.stroke();
        ctx.fillStyle = "#276fe0";
        ctx.fillRect(cx - 24, base - barHeight, 48, barHeight);
        ctx.fillStyle = "#121827";
        ctx.font = "700 18px Segoe UI, Arial";
        ctx.textAlign = "center";
        ctx.fillText(`${fmt.format(credits)} credits`, cx, base - barHeight - 18);
        ctx.fillStyle = "#526177";
        ctx.font = "12px Segoe UI, Arial";
        ctx.fillText(`${row.day || ""} | ${money.format(usd)}`, cx, rect.height - 16);
        ctx.textAlign = "left";
        return;
      }
      ctx.strokeStyle = "#dbe3ef";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, pad.top);
      ctx.lineTo(pad.left, pad.top + h);
      ctx.lineTo(pad.left + w, pad.top + h);
      ctx.stroke();
      const max = Math.max(...rows.map(r => Number(r.total_estimated_credits || 0)), 1);
      const step = rows.length > 1 ? w / (rows.length - 1) : w;
      const points = rows.map((r, i) => ({
        x: pad.left + i * step,
        y: pad.top + h - (Number(r.total_estimated_credits || 0) / max) * h,
      }));
      const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + h);
      gradient.addColorStop(0, "rgba(39, 111, 224, 0.26)");
      gradient.addColorStop(1, "rgba(39, 111, 224, 0.02)");
      ctx.beginPath();
      ctx.moveTo(points[0].x, pad.top + h);
      points.forEach(point => ctx.lineTo(point.x, point.y));
      ctx.lineTo(points[points.length - 1].x, pad.top + h);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();
      ctx.strokeStyle = "#276fe0";
      ctx.lineWidth = 3;
      ctx.beginPath();
      points.forEach((point, i) => {
        if (i === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.stroke();
      ctx.fillStyle = "#121827";
      points.forEach(point => {
        ctx.beginPath();
        ctx.arc(point.x, point.y, 4, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.fillStyle = "#526177";
      ctx.font = "12px Segoe UI, Arial";
      ctx.fillText(fmt.format(max), 10, pad.top + 8);
      ctx.fillText("0", 28, pad.top + h + 4);
      const first = rows[0]?.day || "";
      const last = rows[rows.length - 1]?.day || "";
      ctx.fillText(first, pad.left, rect.height - 16);
      ctx.textAlign = "right";
      ctx.fillText(last, pad.left + w, rect.height - 16);
      ctx.textAlign = "left";
    }
    function renderDailyBreakdown(data) {
      const source = data?.federated || data || {};
      const rows = source.daily || data?.daily || [];
      const totals = source.totals || data?.totals || {};
      const range = data?.date_range || {};
      const badge = document.getElementById("dailyRangeBadge");
      const status = document.getElementById("dailyStatus");
      const summary = document.getElementById("dailySummary");
      const totalRuns = Number(totals.total_runs || 0);
      const totalCredits = Number(totals.total_estimated_credits || 0);
      const totalUsd = Number(totals.total_estimated_usd || 0);
      const avgUsd = totalRuns ? totalUsd / totalRuns : 0;
      const sortedRows = [...rows].sort((a, b) => String(b.day || "").localeCompare(String(a.day || "")));

      badge.textContent = range.single_day ? "Specific Day" : (range.label || "All Time");
      badge.className = `badge ${range.single_day ? "online" : ""}`;
      if (range.single_day) {
        status.innerHTML = `Daily total for <strong>${esc(range.single_day)}</strong>: <strong>${fmt.format(totalCredits)}</strong> credits / <strong>${money.format(totalUsd)}</strong> across <strong>${fmt.format(totalRuns)}</strong> run(s).`;
      } else {
        status.innerHTML = `Daily spend grouped by stored local calendar date. Active range: <strong>${esc(range.label || "All time")}</strong>.`;
      }
      summary.innerHTML = `
        <div class="daily-summary-item"><span>Total Days</span><strong>${fmt.format(rows.length)}</strong></div>
        <div class="daily-summary-item"><span>Total Runs</span><strong>${fmt.format(totalRuns)}</strong></div>
        <div class="daily-summary-item"><span>Credits Spent</span><strong>${fmt.format(totalCredits)}</strong></div>
        <div class="daily-summary-item"><span>USD Spent</span><strong>${money.format(totalUsd)}<small style="display:block;margin-top:6px;color:#526177;font-size:12px;">${money.format(avgUsd)} / run</small></strong></div>
      `;
      table("dailyBreakdown", ["Date", "Runs", "Credits", "USD", "Avg / Run"], sortedRows, r => {
        const runs = Number(r.total_runs || 0);
        const usd = Number(r.total_estimated_usd || 0);
        return `<tr><td>${esc(r.day || "-")}</td><td class="num">${fmt.format(runs)}</td><td class="num">${fmt.format(r.total_estimated_credits || 0)}</td><td class="num">${money.format(usd)}</td><td class="num">${money.format(runs ? usd / runs : 0)}</td></tr>`;
      });
    }
    function partnerShareRows(rows) {
      const ranked = (rows || [])
        .map(row => ({ ...row, credits: Number(row.total_estimated_credits || 0) }))
        .filter(row => row.credits > 0)
        .sort((a, b) => b.credits - a.credits);
      const visible = ranked.slice(0, 8);
      const otherCredits = ranked.slice(8).reduce((sum, row) => sum + row.credits, 0);
      const otherUsd = ranked.slice(8).reduce((sum, row) => sum + Number(row.total_estimated_usd || 0), 0);
      const otherRuns = ranked.slice(8).reduce((sum, row) => sum + Number(row.total_runs || 0), 0);
      if (otherCredits > 0) {
        visible.push({
          partner_node_name: "Other",
          total_runs: otherRuns,
          total_estimated_credits: otherCredits,
          total_estimated_usd: otherUsd,
          credits: otherCredits,
        });
      }
      return visible;
    }
    function drawPartnerShareChart(rows, totalCredits) {
      const canvas = document.getElementById("partnerShareChart");
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const cx = rect.width / 2;
      const cy = rect.height / 2;
      const radius = Math.max(70, Math.min(rect.width, rect.height) * 0.38);
      const inner = radius * 0.58;
      if (!rows.length || totalCredits <= 0) {
        ctx.fillStyle = "#526177";
        ctx.font = "14px Segoe UI, Arial";
        ctx.textAlign = "center";
        ctx.fillText("No credit usage recorded", cx, cy);
        ctx.textAlign = "left";
        return;
      }
      let angle = -Math.PI / 2;
      rows.forEach((row, index) => {
        const slice = (Number(row.total_estimated_credits || 0) / totalCredits) * Math.PI * 2;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, radius, angle, angle + slice);
        ctx.closePath();
        ctx.fillStyle = shareColors[index % shareColors.length];
        ctx.fill();
        angle += slice;
      });
      ctx.beginPath();
      ctx.arc(cx, cy, inner, 0, Math.PI * 2);
      ctx.fillStyle = "#fff";
      ctx.fill();
      ctx.textAlign = "center";
      ctx.fillStyle = "#526177";
      ctx.font = "13px Segoe UI, Arial";
      ctx.fillText("Tracked Spend", cx, cy - 10);
      ctx.fillStyle = "#121827";
      ctx.font = "700 24px Segoe UI, Arial";
      ctx.fillText(fmt.format(totalCredits), cx, cy + 20);
      ctx.textAlign = "left";
    }
    function renderPartnerShare(rows, totalCredits, balance) {
      const shareRows = partnerShareRows(rows);
      drawPartnerShareChart(shareRows, totalCredits);
      const balanceCredits = balance?.ok ? Number(balance.credits || 0) : 0;
      const budgetBase = balance?.ok ? balanceCredits + totalCredits : 0;
      const head = `<div class="share-row share-head"><span></span><span>Partner</span><span class="share-value">Share</span><span class="share-value">Credits</span><span class="share-value share-budget">Budget Used</span></div>`;
      const body = shareRows.map((row, index) => {
        const credits = Number(row.total_estimated_credits || 0);
        const spendPct = totalCredits ? (credits / totalCredits) * 100 : 0;
        const budgetPct = budgetBase ? `${fmt.format((credits / budgetBase) * 100)}%` : "--";
        return `<div class="share-row"><span class="swatch" style="background:${shareColors[index % shareColors.length]}"></span><span class="share-name">${esc(row.partner_node_name)}</span><span class="share-value">${fmt.format(spendPct)}%</span><span class="share-value">${fmt.format(credits)}</span><span class="share-value share-budget">${budgetPct}</span></div>`;
      }).join("");
      document.getElementById("partnerShareLegend").innerHTML = shareRows.length ? head + body : `<div>No records</div>`;
    }
    function qualityPanel(data, total) {
      const items = [
        ["Missing project", data.missing_project],
        ["Missing user", data.missing_user],
        ["Missing workflow", data.missing_workflow],
        ["Unknown partner", data.unknown_partner],
        ["Missing class type", data.missing_class_type],
        ["Not runtime price", data.not_runtime_price],
      ];
      document.getElementById("quality").innerHTML = items.map(([label, value]) => {
        const count = Number(value || 0);
        const pct = total ? Math.round((count / total) * 100) : 0;
        return `<div class="bar-row"><span class="${count ? "warning" : ""}">${esc(label)}</span><div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div><strong>${count}</strong></div>`;
      }).join("");
      return items.reduce((sum, [, value]) => sum + Number(value || 0), 0);
    }
    function findBrowserComfyAuth() {
      const candidates = [];
      const directToken = window.app?.api?.authToken || window.comfyAPI?.authToken;
      const directKey = window.app?.api?.apiKey || window.comfyAPI?.apiKey;
      if (directToken) candidates.push({ Authorization: `Bearer ${directToken}`, source: "ComfyUI browser API token" });
      if (directKey) candidates.push({ "X-API-KEY": directKey, source: "ComfyUI browser API key" });

      try {
        for (let i = 0; i < localStorage.length; i++) {
          const key = localStorage.key(i) || "";
          const value = localStorage.getItem(key) || "";
          if (!value || (!key.startsWith("firebase:authUser:") && !value.includes("stsTokenManager"))) continue;
          const parsed = JSON.parse(value);
          const token = parsed?.stsTokenManager?.accessToken;
          if (token) candidates.push({ Authorization: `Bearer ${token}`, source: "ComfyUI browser login" });
        }
      } catch (error) {
        console.warn("Credit Tracker could not inspect browser auth:", error);
      }
      return candidates[0] || null;
    }
    async function primeOfficialAuthFromBrowser() {
      const auth = findBrowserComfyAuth();
      if (!auth) return false;
      try {
        const response = await fetch("/credit-tracker/api/official-usage/auth", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(auth),
        });
        const data = await response.json();
        return !!data.ok;
      } catch (error) {
        console.warn("Credit Tracker could not send browser auth:", error);
        return false;
      }
    }
    async function syncPricing(button) {
      const buttonText = button?.textContent || "Sync Official Pricing";
      if (button) {
        button.textContent = "Syncing...";
        button.disabled = true;
      }
      try {
        const response = await fetch("/credit-tracker/api/pricing/sync", { method: "POST" });
        const data = await response.json();
        alert(`Synced ${data.row_count || 0} official pricing rows.`);
        await loadData();
      } catch (error) {
        alert(`Pricing sync failed: ${error}`);
      } finally {
        if (button) {
          button.textContent = buttonText;
          button.disabled = false;
        }
      }
    }
    async function syncOfficialUsage(button) {
      const buttonText = button?.textContent || "Sync Account Usage";
      if (button) {
        button.textContent = "Syncing...";
        button.disabled = true;
      }
      try {
        await primeOfficialAuthFromBrowser();
        const response = await fetch("/credit-tracker/api/official-usage/sync", { method: "POST" });
        const data = await response.json();
        if (!data.ok) {
          alert(`Account usage sync failed: ${data.error || "Unknown error"}`);
        } else {
          alert(`Synced ${data.fetched || 0} official account usage events. New rows: ${data.inserted || 0}.`);
        }
        await loadData();
      } catch (error) {
        alert(`Account usage sync failed: ${error}`);
      } finally {
        if (button) {
          button.textContent = buttonText;
          button.disabled = false;
        }
      }
    }
    async function syncPeersNow(button) {
      const buttonText = button?.textContent || "Sync Peers Now";
      if (button) {
        button.textContent = "Syncing...";
        button.disabled = true;
      }
      try {
        const response = await fetch("/credit-tracker/api/peer-sync/pull", { method: "POST" });
        const data = await response.json();
        if (!data.ok) {
          alert(`Peer sync finished with warnings. Inserted ${data.inserted || 0}, skipped ${data.skipped || 0}.`);
        } else {
          alert(`Peer sync complete. Inserted ${data.inserted || 0}, skipped ${data.skipped || 0}.`);
        }
        await loadData();
      } catch (error) {
        alert(`Peer sync failed: ${error}`);
      } finally {
        if (button) {
          button.textContent = buttonText;
          button.disabled = false;
        }
      }
    }
    function renderBalanceReconciliation(reconciliation) {
      const status = document.getElementById("balanceReconciliationStatus");
      const container = document.getElementById("balanceReconciliation");
      if (!reconciliation?.ok) {
        status.classList.remove("error");
        status.innerHTML = `${esc(reconciliation?.message || "No balance snapshots yet")} | Snapshots: <strong>${fmt.format(reconciliation?.snapshot_count || 0)}</strong>`;
        container.innerHTML = `<div class="reconcile-item"><span>Tracked Spend</span><strong>${fmt.format(reconciliation?.tracked_credits || 0)}</strong></div>`;
        return;
      }

      const first = reconciliation.first || {};
      const latest = reconciliation.latest || {};
      const realConsumed = Number(reconciliation.real_consumed_credits || 0);
      const tracked = Number(reconciliation.tracked_credits || 0);
      const gap = Number(reconciliation.untracked_credits || 0);
      const possibleUntracked = Math.max(gap, 0);
      const gapIsWarning = gap > 1;
      status.classList.toggle("error", gapIsWarning);
      let direction = reconciliation.balance_increased ? "Balance increased since first snapshot, likely because credits were purchased or the snapshot window restarted." : "Real consumed is calculated from first snapshot balance minus latest snapshot balance.";
      if (gap < -1) direction = "Tracker estimate is higher than balance-snapshot consumption because some tracked rows happened before the first saved balance snapshot.";
      status.innerHTML = `Snapshots: <strong>${fmt.format(reconciliation.snapshot_count || 0)}</strong> | First: <strong>${esc(first.timestamp || "-")}</strong> | Latest: <strong>${esc(latest.timestamp || "-")}</strong> | ${esc(direction)}`;
      container.innerHTML = `
        <div class="reconcile-item"><span>Starting Balance</span><strong>${fmt.format(first.credits || 0)}</strong></div>
        <div class="reconcile-item"><span>Current Balance</span><strong>${fmt.format(latest.credits || 0)}</strong></div>
        <div class="reconcile-item"><span>Real Consumed</span><strong>${fmt.format(realConsumed)}</strong></div>
        <div class="reconcile-item"><span>Tracker Estimate</span><strong>${fmt.format(tracked)}</strong></div>
        <div class="reconcile-item ${gapIsWarning ? "warning" : ""}"><span>Possible Untracked</span><strong>${fmt.format(possibleUntracked)}</strong></div>
      `;
    }
    function healthClass(ok, warn = false) {
      if (!ok) return "bad";
      return warn ? "warn" : "good";
    }
    function renderHealth(health) {
      const badge = document.getElementById("healthBadge");
      const status = document.getElementById("healthStatus");
      const issues = health?.issues || [];
      badge.className = `badge ${health?.ok ? "online" : "error"}`;
      badge.textContent = health?.ok ? "Healthy" : "Review";
      status.classList.toggle("error", !health?.ok);
      status.innerHTML = issues.length
        ? `Needs attention: <strong>${esc(issues.join(", "))}</strong>`
        : `All key tracker services look healthy. Local instance: <strong>${esc(health?.local_instance || "-")}</strong>`;

      const backup = health?.backup || {};
      const lastRun = health?.last_run || {};
      const trackerEvent = health?.last_tracker_event || {};
      const snapshot = health?.last_balance_snapshot || {};
      const offline = Number(health?.peer_offline_count || 0);
      const backupOk = !!backup.ok;
      const trackerOk = !!trackerEvent.ok;
      const snapshotOk = !!snapshot.timestamp;
      const officialOk = !!health?.last_official_sync;
      document.getElementById("healthGrid").innerHTML = `
        <div class="health-tile ${healthClass(offline === 0)}"><span class="health-label">Peer Sync</span><strong class="health-value">${fmt.format(health?.peer_online_count || 0)} online</strong><span class="health-note">${offline ? `${fmt.format(offline)} offline` : `${fmt.format(health?.deduped_duplicate_count || 0)} duplicates deduped`}</span></div>
        <div class="health-tile ${healthClass(backupOk)}"><span class="health-label">Auto Backup</span><strong class="health-value">${backupOk ? "On" : "Check"}</strong><span class="health-note">${backup.latest_backup_at ? `${esc(backup.latest_backup_name || "latest")} | ${esc(backup.latest_backup_at)}` : esc(backup.error || "No backup yet")}</span></div>
        <div class="health-tile ${healthClass(trackerOk)}"><span class="health-label">Auto Tracker</span><strong class="health-value">${esc(trackerEvent.event || "Unknown")}</strong><span class="health-note">${esc(trackerEvent.timestamp || trackerEvent.error || "-")}</span></div>
        <div class="health-tile ${healthClass(snapshotOk)}"><span class="health-label">Balance Snapshot</span><strong class="health-value">${snapshotOk ? fmt.format(snapshot.credits || 0) : "--"}</strong><span class="health-note">${esc(snapshot.timestamp || "Waiting for snapshot")}</span></div>
        <div class="health-tile ${officialOk ? "good" : "warn"}"><span class="health-label">Official Usage Sync</span><strong class="health-value">${officialOk ? "Synced" : "Not synced"}</strong><span class="health-note">${esc(health?.last_official_sync || "Optional account sync")}</span></div>
      `;
    }
    function renderRecentRuns(data) {
      const rows = data?.recent || [];
      const visible = visibleRows(rows, showAllRecent);
      const button = document.getElementById("toggleRecent");
      button.textContent = showAllRecent ? "Show Less" : `View All (${fmt.format(rows.length)})`;
      button.style.display = rows.length > 5 ? "inline-grid" : "none";
      table("recent", ["Time", "Partner", "Title", "Model", "Source", "Credits", "Prompt", "Node"], visible, r => `<tr><td>${esc(r.timestamp)}</td><td>${trunc(r.partner_node_name, 24)}</td><td>${trunc(r.node_title, 24)}</td><td><code>${trunc(r.model_name, 20)}</code></td><td>${esc(r.source)}</td><td class="num">${fmt.format(r.estimated_credits)}</td><td><code>${trunc(r.prompt_id, 15)}</code></td><td><code>${trunc(r.node_id, 15)}</code></td></tr>`);
    }
    function renderExpensiveRuns(data) {
      const rows = data?.expensive || [];
      const visible = visibleRows(rows, showAllExpensive);
      const button = document.getElementById("toggleExpensive");
      button.textContent = showAllExpensive ? "Show Less" : `View All (${fmt.format(rows.length)})`;
      button.style.display = rows.length > 5 ? "inline-grid" : "none";
      table("expensive", ["Time", "Partner", "Model", "Credits", "USD"], visible, r => `<tr><td>${esc(r.timestamp)}</td><td>${trunc(r.partner_node_name, 28)}</td><td><code>${trunc(r.model_name || r.node_title || r.node_class_type, 24)}</code></td><td class="num">${fmt.format(r.estimated_credits)}</td><td class="num">${money.format(r.estimated_usd)}</td></tr>`);
    }
    function renderOfficialUsage(official) {
      const status = document.getElementById("officialStatus");
      const totals = official?.totals || {};
      const auth = official?.auth || {};
      const synced = totals.last_synced_at ? `Last synced: <strong>${esc(totals.last_synced_at)}</strong>` : "Not synced yet";
      const authText = auth.ok ? `Auth: <strong>${esc(auth.source || "available")}</strong>` : `Auth needed: ${esc(auth.error || "missing")}`;
      status.classList.toggle("error", !auth.ok);
      status.innerHTML = `${synced} | Official events: <strong>${fmt.format(totals.total_events || 0)}</strong> | Official credits: <strong>${fmt.format(totals.total_credits || 0)}</strong> | ${authText}`;
      table("officialUsage", ["API", "Model", "Runs", "Credits", "USD"], official?.by_api || [], r => `<tr><td>${trunc(r.api_name || "API", 28)}</td><td><code>${trunc(r.model || "-", 24)}</code></td><td class="num">${fmt.format(r.total_events)}</td><td class="num">${fmt.format(r.total_credits)}</td><td class="num">${money.format(r.total_usd)}</td></tr>`);
    }
    function renderFederated(federated) {
      const status = document.getElementById("federatedStatus");
      const totals = federated?.totals || {};
      const online = Number(federated?.online_count || 0);
      const offline = Number(federated?.offline_count || 0);
      const duplicates = Number(federated?.deduped_duplicate_count || 0);
      const config = federated?.config_path || "remote_instances.json";
      document.getElementById("networkCredits").textContent = fmt.format(totals.total_estimated_credits || 0);
      document.getElementById("networkUsd").textContent = money.format(totals.total_estimated_usd || 0);
      status.classList.toggle("error", offline > 0);
      status.innerHTML = `Network total is deduped unique spend: <strong>${fmt.format(totals.total_estimated_credits || 0)}</strong> credits / <strong>${money.format(totals.total_estimated_usd || 0)}</strong> across <strong>${fmt.format(online)}</strong> online instance(s). Copied/synced duplicate rows ignored: <strong>${fmt.format(duplicates)}</strong>.`;
      table("instances", ["Instance", "Status", "Unique Runs", "Unique USD", "Raw DB USD", "URL / Error"], federated?.instances || [], r => {
        const statusClass = esc(r.status || (r.ok ? "online" : "error")).toLowerCase();
        const detail = r.ok ? r.base_url : (r.error || r.base_url);
        return `<tr><td>${trunc(r.name, 28)}</td><td><span class="badge ${statusClass}">${esc(r.status || (r.ok ? "online" : "error"))}</span></td><td class="num">${fmt.format(r.runs || 0)}</td><td class="num">${money.format(r.usd || 0)}</td><td class="num">${money.format(r.raw_usd || 0)}</td><td>${trunc(detail, 56)}</td></tr>`;
      });
      table("networkNodes", ["Partner", "Runs", "Credits", "USD"], federated?.by_partner || [], r => `<tr><td>${trunc(r.partner_node_name, 34)}</td><td class="num">${fmt.format(r.total_runs)}</td><td class="num">${fmt.format(r.total_estimated_credits)}</td><td class="num">${money.format(r.total_estimated_usd)}</td></tr>`);
      const recentRows = federated?.recent || [];
      const visibleRecent = visibleRows(recentRows, showAllNetworkRecent);
      const recentButton = document.getElementById("toggleNetworkRecent");
      recentButton.textContent = showAllNetworkRecent ? "Show Less" : `View All (${fmt.format(recentRows.length)})`;
      recentButton.style.display = recentRows.length > 5 ? "inline-grid" : "none";
      table("networkRecent", ["Instance", "Time", "Partner", "Model", "Source", "Credits"], visibleRecent, r => `<tr><td>${trunc(r.instance_name, 24)}</td><td>${esc(r.timestamp)}</td><td>${trunc(r.partner_node_name, 26)}</td><td><code>${trunc(r.model_name || r.node_title || r.node_class_type, 24)}</code></td><td>${trunc(r.source, 18)}</td><td class="num">${fmt.format(r.estimated_credits)}</td></tr>`);
    }
    function renderProjectOverview(data) {
      const projectName = document.getElementById("project").value.trim();
      const panel = document.getElementById("projectOverview");
      if (!projectName) {
        panel.hidden = true;
        return;
      }

      panel.hidden = false;
      const source = data.federated || data;
      const totals = source.totals || data.totals || {};
      const byModel = source.by_model || data.by_model || [];
      const expensive = source.expensive || data.expensive || [];
      const runs = Number(totals.total_runs || 0);
      const credits = Number(totals.total_estimated_credits || 0);
      const usd = Number(totals.total_estimated_usd || 0);
      const avgUsd = runs > 0 ? usd / runs : 0;
      const mostUsed = [...byModel].sort((a, b) => Number(b.total_runs || 0) - Number(a.total_runs || 0))[0];
      const topSpend = [...byModel].sort((a, b) => Number(b.total_estimated_usd || 0) - Number(a.total_estimated_usd || 0))[0];
      const highestAvg = [...byModel].sort((a, b) => Number(b.avg_usd_per_run || 0) - Number(a.avg_usd_per_run || 0))[0];

      document.getElementById("projectOverviewTitle").textContent = `Project Overview: ${projectName}`;
      document.getElementById("projectOverviewBadge").textContent = `${fmt.format(runs)} tasks`;
      document.getElementById("projectSummary").innerHTML = `
        <div class="project-metric"><span>Total AI Tasks</span><strong>${fmt.format(runs)}</strong><small>Partner/API node calls</small></div>
        <div class="project-metric"><span>Credits Spent</span><strong>${fmt.format(credits)}</strong><small>${money.format(usd)}</small></div>
        <div class="project-metric"><span>Average / Task</span><strong>${money.format(avgUsd)}</strong><small>${fmt.format(runs ? credits / runs : 0)} credits</small></div>
        <div class="project-metric"><span>Most Used</span><strong>${trunc(mostUsed?.partner_node_name || "-", 22)}</strong><small>${fmt.format(mostUsed?.total_runs || 0)} runs</small></div>
        <div class="project-metric"><span>Top Spend</span><strong>${trunc(topSpend?.partner_node_name || "-", 22)}</strong><small>${money.format(topSpend?.total_estimated_usd || 0)}</small></div>
        <div class="project-metric"><span>Highest Avg</span><strong>${trunc(highestAvg?.partner_node_name || "-", 22)}</strong><small>${money.format(highestAvg?.avg_usd_per_run || 0)} / run</small></div>
      `;

      document.getElementById("projectHighlights").innerHTML = `
        <div class="project-highlight"><span>Most Used Model</span><strong>${trunc(mostUsed ? `${mostUsed.partner_node_name} | ${mostUsed.model_name}` : "-", 46)}</strong></div>
        <div class="project-highlight"><span>Most Expensive Total</span><strong>${trunc(topSpend ? `${topSpend.partner_node_name} | ${topSpend.model_name}` : "-", 46)} - ${money.format(topSpend?.total_estimated_usd || 0)}</strong></div>
        <div class="project-highlight"><span>Highest Cost / Run</span><strong>${trunc(highestAvg ? `${highestAvg.partner_node_name} | ${highestAvg.model_name}` : "-", 46)} - ${money.format(highestAvg?.avg_usd_per_run || 0)}</strong></div>
      `;

      table("projectModels", ["Partner", "Model", "Runs", "Credits", "USD", "Avg / Run", "Share"], byModel, r => {
        const share = credits > 0 ? (Number(r.total_estimated_credits || 0) / credits) * 100 : 0;
        return `<tr><td>${trunc(r.partner_node_name, 28)}</td><td><code>${trunc(r.model_name, 24)}</code></td><td class="num">${fmt.format(r.total_runs)}</td><td class="num">${fmt.format(r.total_estimated_credits)}</td><td class="num">${money.format(r.total_estimated_usd)}</td><td class="num">${money.format(r.avg_usd_per_run || 0)}</td><td class="num">${fmt.format(share)}%</td></tr>`;
      });
      table("projectExpensive", ["Time", "Partner", "Model", "USD"], expensive.slice(0, 5), r => `<tr><td>${esc(r.timestamp)}</td><td>${trunc(r.partner_node_name, 22)}</td><td><code>${trunc(r.model_name || r.node_title || r.node_class_type, 22)}</code></td><td class="num">${money.format(r.estimated_usd || 0)}</td></tr>`);
    }
    async function loadData() {
      const q = params();
      const response = await fetch(`/credit-tracker/api/summary?${q}`);
      const data = await response.json();
      lastDashboardData = data;
      updateFilterOptions(data.filter_options || {});
      document.getElementById("credits").textContent = fmt.format(data.totals.total_estimated_credits || 0);
      document.getElementById("usd").textContent = money.format(data.totals.total_estimated_usd || 0);
      const balance = data.balance || {};
      const balanceEl = document.getElementById("balance");
      balanceEl.textContent = balance.ok ? fmt.format(balance.credits || 0) : "--";
      balanceEl.classList.toggle("balance-positive", balance.ok && Number(balance.credits || 0) > 0);
      document.getElementById("balanceUsd").textContent = balance.ok ? money.format(balance.usd || 0) : "--";
      document.getElementById("runs").textContent = fmt.format(data.totals.total_runs || 0);
      document.getElementById("rate").textContent = fmt.format(data.credits_per_usd);
      const warningCount = qualityPanel(data.data_quality, Number(data.totals.total_runs || 0));
      document.getElementById("warnings").textContent = fmt.format(warningCount);
      document.getElementById("warningsCard").classList.toggle("active", warningCount > 0);
      document.getElementById("fullCsv").href = `/credit-tracker/export/full.csv?${q}`;
      document.getElementById("nodeCsv").href = `/credit-tracker/export/by-node.csv?${q}`;
      document.getElementById("projectCsv").href = `/credit-tracker/export/by-project.csv?${q}`;
      const dailySource = data.federated || data;
      drawDailyChart(dailySource.daily || data.daily || []);
      renderDailyBreakdown(data);
      renderHealth(data.health || {});
      renderPartnerShare(data.by_partner || data.by_node || [], Number(data.totals.total_estimated_credits || 0), balance);
      renderBalanceReconciliation(data.balance_reconciliation || {});
      renderOfficialUsage(data.official_usage || {});
      renderFederated(data.federated || {});
      renderProjectOverview(data);
      table("nodes", ["Partner", "Class", "Runs", "Credits", "USD"], data.by_node, r => `<tr><td>${trunc(r.partner_node_name, 28)}</td><td><code>${trunc(r.node_class_type, 22)}</code></td><td class="num">${fmt.format(r.total_runs)}</td><td class="num">${fmt.format(r.total_estimated_credits)}</td><td class="num">${money.format(r.total_estimated_usd)}</td></tr>`);
      table("projects", ["Project", "Runs", "Credits", "USD"], data.by_project, r => `<tr><td>${trunc(r.project_name, 28)}</td><td class="num">${fmt.format(r.total_runs)}</td><td class="num">${fmt.format(r.total_estimated_credits)}</td><td class="num">${money.format(r.total_estimated_usd)}</td></tr>`);
      table("users", ["User", "Runs", "Credits", "USD"], data.by_user, r => `<tr><td>${trunc(r.user_name, 28)}</td><td class="num">${fmt.format(r.total_runs)}</td><td class="num">${fmt.format(r.total_estimated_credits)}</td><td class="num">${money.format(r.total_estimated_usd)}</td></tr>`);
      table("workflows", ["Workflow", "Runs", "Credits", "USD"], data.by_workflow, r => `<tr><td>${trunc(r.workflow_name, 28)}</td><td class="num">${fmt.format(r.total_runs)}</td><td class="num">${fmt.format(r.total_estimated_credits)}</td><td class="num">${money.format(r.total_estimated_usd)}</td></tr>`);
      renderExpensiveRuns(data);
      const pricingQuery = document.getElementById("partner").value.trim();
      const pricingResponse = await fetch(`/credit-tracker/api/pricing?query=${encodeURIComponent(pricingQuery)}&limit=20`);
      const pricingData = await pricingResponse.json();
      table("pricing", ["Provider", "Product", "Configuration", "Credits", "Unit", "Category"], pricingData.rows || [], r => `<tr><td>${trunc(r.provider, 20)}</td><td>${trunc(r.product_name, 28)}</td><td>${trunc(r.configuration, 32)}</td><td class="num">${fmt.format(r.credits)}</td><td>${esc(r.unit)}</td><td>${esc(r.category)}</td></tr>`);
      renderRecentRuns(data);
    }
    window.addEventListener("resize", () => loadData());
    loadData();
  </script>
</body>
</html>"""


def register_dashboard_routes() -> None:
    if web is None or PromptServer is None:
        LOGGER.warning("Dashboard routes could not register because aiohttp or PromptServer is unavailable.")
        return

    server = getattr(PromptServer, "instance", None)
    if server is None:
        LOGGER.warning("Dashboard routes could not register because PromptServer is unavailable.")
        return
    if getattr(server, "_credit_tracker_dashboard_registered", False):
        return

    routes = server.routes

    @routes.get("/credit-tracker")
    async def credit_tracker_dashboard(request):
        return web.Response(text=_dashboard_html(), content_type="text/html")

    @routes.get("/credit-tracker/api/summary")
    async def credit_tracker_summary(request):
        payload = _summary_payload(dict(request.query))
        return web.json_response(payload)

    @routes.get("/credit-tracker/api/usage-rows")
    async def credit_tracker_usage_rows(request):
        return web.json_response(_usage_rows_payload(dict(request.query)))

    @routes.post("/credit-tracker/api/ingest-rows")
    async def credit_tracker_ingest_rows(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if not _is_peer_sync_authorized(request, payload):
            return web.json_response({"ok": False, "error": "Unauthorized peer sync"}, status=401)

        rows = payload.get("rows")
        if isinstance(payload.get("row"), dict):
            rows = [payload["row"]]
        if not isinstance(rows, list):
            rows = []

        result = _ingest_usage_rows(
            [row for row in rows if isinstance(row, dict)],
            source_instance=str(payload.get("source_instance") or "peer"),
        )
        return web.json_response(result)

    @routes.post("/credit-tracker/api/peer-sync/pull")
    async def credit_tracker_peer_sync_pull(request):
        result = _sync_pull_from_peers(dict(request.query))
        return web.json_response(result)

    @routes.get("/credit-tracker/api/balance")
    async def credit_tracker_balance(request):
        balance = _fetch_credit_balance()
        _capture_balance_snapshot(balance, source="balance_api")
        return web.json_response(balance)

    @routes.get("/credit-tracker/api/balance-snapshots")
    async def credit_tracker_balance_snapshots(request):
        summary = balance_snapshot_summary(DB_PATH)
        return web.json_response(summary)

    @routes.get("/credit-tracker/api/remotes")
    async def credit_tracker_remotes(request):
        return web.json_response(
            {
                "local_instance": _load_instance_config(),
                "local_config_path": str(INSTANCE_CONFIG_PATH.resolve()),
                "config_path": str(REMOTE_INSTANCES_PATH.resolve()),
                "instances": _load_remote_instances(),
            }
        )

    @routes.get("/credit-tracker/api/official-usage")
    async def credit_tracker_official_usage(request):
        limit = request.query.get("limit", "20")
        return web.json_response(official_usage_summary(limit=int(limit)))

    @routes.post("/credit-tracker/api/official-usage/auth")
    async def credit_tracker_official_usage_auth(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        headers: dict[str, str] = {}
        authorization = payload.get("Authorization") or payload.get("authorization")
        api_key = payload.get("X-API-KEY") or payload.get("x-api-key")
        if isinstance(authorization, str) and authorization.strip():
            headers["Authorization"] = authorization.strip()
        elif isinstance(api_key, str) and api_key.strip():
            headers["X-API-KEY"] = api_key.strip()

        ok = remember_auth_headers(headers, source=str(payload.get("source") or "dashboard browser auth"))
        return web.json_response({"ok": ok})

    @routes.post("/credit-tracker/api/official-usage/sync")
    async def credit_tracker_official_usage_sync(request):
        query = request.query
        payload = sync_official_usage_events(
            limit=int(query.get("limit", "100")),
            max_pages=int(query.get("max_pages", "5")),
            start_date=query.get("start_date", ""),
            end_date=query.get("end_date", ""),
        )
        return web.json_response(payload)

    @routes.get("/credit-tracker/api/pricing")
    async def credit_tracker_pricing(request):
        query = request.query.get("query", "")
        limit = request.query.get("limit", "100")
        rows = search_pricing_cache(query=query, limit=int(limit))
        cache = load_pricing_cache()
        return web.json_response(
            {
                "source_url": cache.get("source_url", ""),
                "synced_at": cache.get("synced_at", ""),
                "row_count": cache.get("row_count", 0),
                "rows": rows,
            }
        )

    @routes.post("/credit-tracker/api/pricing/sync")
    async def credit_tracker_pricing_sync(request):
        payload = sync_pricing_cache()
        return web.json_response(
            {
                "source_url": payload.get("source_url", ""),
                "synced_at": payload.get("synced_at", ""),
                "row_count": payload.get("row_count", 0),
            }
        )

    @routes.get("/credit-tracker/export/{report}.csv")
    async def credit_tracker_export(request):
        report_type = request.match_info.get("report", "full")
        return _export_rows(report_type, dict(request.query))

    server._credit_tracker_dashboard_registered = True
    LOGGER.info("Credit Tracker dashboard registered at /credit-tracker")
