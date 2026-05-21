from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from .tracker_db import LOGGER
except ImportError:
    from tracker_db import LOGGER


PACKAGE_DIR = Path(__file__).resolve().parent
INSTANCE_CONFIG_PATH = PACKAGE_DIR / "instance_config.json"
REMOTE_INSTANCES_PATH = PACKAGE_DIR / "remote_instances.json"
PEER_SYNC_ENABLED = os.environ.get("CREDIT_TRACKER_PEER_SYNC", "1").strip().lower() not in {"0", "false", "no"}
PEER_SYNC_TOKEN = os.environ.get("CREDIT_TRACKER_SYNC_TOKEN", "").strip()


def _load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def local_instance_name() -> str:
    data = _load_json(INSTANCE_CONFIG_PATH, {})
    if isinstance(data, dict):
        name = str(data.get("name") or "").strip()
        if name:
            return name
    return "ComfyUI Tracker"


def remote_instances() -> list[dict[str, Any]]:
    data = _load_json(REMOTE_INSTANCES_PATH, [])
    if not isinstance(data, list):
        return []

    remotes: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", True):
            continue
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            continue
        remotes.append(
            {
                "name": str(item.get("name") or base_url).strip(),
                "base_url": base_url,
            }
        )
    return remotes


def _record_to_dict(record: Any) -> dict[str, Any]:
    if is_dataclass(record):
        return asdict(record)
    if isinstance(record, dict):
        return dict(record)
    return {}


def push_usage_record(record: Any) -> None:
    if not PEER_SYNC_ENABLED:
        return

    row = _record_to_dict(record)
    if not row:
        return

    for remote in remote_instances():
        _post_rows(remote, [row])


def enqueue_usage_record_sync(record: Any) -> None:
    if not PEER_SYNC_ENABLED:
        return

    thread = threading.Thread(
        target=push_usage_record,
        args=(record,),
        name="CreditTrackerPeerSync",
        daemon=True,
    )
    thread.start()


def _post_rows(remote: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "source_instance": local_instance_name(),
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }
    if PEER_SYNC_TOKEN:
        payload["sync_token"] = PEER_SYNC_TOKEN

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if PEER_SYNC_TOKEN:
        headers["X-Credit-Tracker-Token"] = PEER_SYNC_TOKEN

    url = f"{remote['base_url']}/credit-tracker/api/ingest-rows"
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=4) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {"ok": True}
    except HTTPError as exc:
        LOGGER.warning("Peer sync to %s failed with HTTP %s", remote.get("name"), exc.code)
    except URLError as exc:
        LOGGER.warning("Peer sync could not reach %s: %s", remote.get("name"), exc)
    except Exception as exc:
        LOGGER.warning("Peer sync to %s failed: %s", remote.get("name"), exc)
    return {"ok": False}
