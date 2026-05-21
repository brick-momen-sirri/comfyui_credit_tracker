from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
DB_PATH = PACKAGE_DIR / "usage_log.db"
PRICING_TABLE_PATH = PACKAGE_DIR / "pricing_table.json"

# ComfyUI credit conversion rate. Update this one value if the official
# conversion rate changes.
CREDITS_PER_USD = 211.0

LOGGER_NAME = "ComfyUI-Credit-Tracker"
LOGGER = logging.getLogger(LOGGER_NAME)
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))
    LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


DEFAULT_PRICING_TABLE: dict[str, dict[str, Any]] = {
    "Nano Banana": {
        "pricing_mode": "fixed_per_run",
        "credits": 14.7,
        "auto_detect_class_types": [
            "GeminiImageNode",
            "GeminiImage2Node",
            "GeminiNanoBanana2",
        ],
    },
    "Seedance 2.0": {
        "pricing_mode": "per_second",
        "credits_per_second": 20,
        "auto_detect_class_types": [
            "ByteDance2TextToVideoNode",
            "ByteDance2FirstLastFrameNode",
            "ByteDance2ReferenceNode",
        ],
    },
    "Image API Example": {
        "pricing_mode": "per_output",
        "credits_per_output": 10,
        "auto_detect_class_types": [
            "ImageAPIExample",
        ],
    },
    "Unknown Node": {
        "pricing_mode": "manual",
        "credits": 0,
    },
    "Kling": {
        "pricing_mode": "manual",
        "credits": 0,
        "auto_detect_class_types": [
            "KlingTextToVideoNode",
            "KlingOmniProTextToVideoNode",
            "OmniProTextToVideoNode",
            "KlingOmniProFirstLastFrameNode",
            "OmniProFirstLastFrameNode",
            "KlingOmniProImageToVideoNode",
            "OmniProImageToVideoNode",
            "KlingOmniProVideoToVideoNode",
            "OmniProVideoToVideoNode",
            "KlingOmniProEditVideoNode",
            "OmniProEditVideoNode",
            "KlingOmniProImageNode",
            "OmniProImageNode",
            "KlingImage2VideoNode",
            "Kling Image(First Frame) to Video",
            "Kling Image First Frame to Video",
            "KlingCameraControlT2VNode",
            "KlingCameraControlI2VNode",
            "KlingStartEndFrameNode",
            "KlingVideoExtendNode",
            "KlingDualCharacterVideoEffectNode",
            "KlingSingleImageVideoEffectNode",
            "KlingLipSyncAudioToVideoNode",
            "KlingLipSyncTextToVideoNode",
            "KlingVirtualTryOnNode",
            "KlingImageGenerationNode",
            "KlingTextToVideoWithAudio",
            "KlingImageToVideoWithAudio",
            "KlingMotionControl",
            "KlingVideoNode",
            "KlingFirstLastFrameNode",
            "Kling 3.0 First-Last-Frame to Video",
            "Kling 2.6 Image(First Frame) to Video with Audio",
            "KlingAvatarNode",
        ],
    },
    "Veo": {
        "pricing_mode": "manual",
        "credits": 0,
        "auto_detect_class_types": [
            "VeoVideoGenerationNode",
            "Veo3VideoGenerationNode",
            "Veo3FirstLastFrameNode",
        ],
    },
    "Runway": {
        "pricing_mode": "manual",
        "credits": 0,
        "auto_detect_class_types": [
            "RunwayImageToVideoNodeGen3a",
            "RunwayImageToVideoNodeGen4",
            "RunwayFirstLastFrameNode",
            "RunwayTextToImageNode",
        ],
    },
}


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS credit_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    project_name TEXT,
    user_name TEXT,
    workflow_name TEXT,
    partner_node_name TEXT,
    pricing_mode TEXT,
    quantity INTEGER,
    duration_seconds REAL,
    resolution TEXT,
    estimated_credits REAL,
    estimated_usd REAL,
    notes TEXT,
    prompt_id TEXT,
    node_id TEXT,
    node_class_type TEXT,
    node_title TEXT,
    model_name TEXT,
    input_summary TEXT,
    source TEXT,
    dedupe_key TEXT
)
"""

CREATE_BALANCE_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    instance_name TEXT,
    credits REAL,
    usd REAL,
    currency TEXT,
    source TEXT,
    notes TEXT
)
"""


@dataclass(frozen=True)
class UsageRecord:
    timestamp: str
    project_name: str
    user_name: str
    workflow_name: str
    partner_node_name: str
    pricing_mode: str
    quantity: int
    duration_seconds: float
    resolution: str
    estimated_credits: float
    estimated_usd: float
    notes: str
    prompt_id: str = ""
    node_id: str = ""
    node_class_type: str = ""
    node_title: str = ""
    model_name: str = ""
    input_summary: str = ""
    source: str = "manual"
    dedupe_key: str = ""


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text or default


def _safe_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ensure_pricing_table(path: Path = PRICING_TABLE_PATH) -> None:
    """Create a default pricing table if it does not exist."""
    if path.exists():
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_PRICING_TABLE, indent=2), encoding="utf-8")
        LOGGER.warning("Created default pricing table at %s", path)
    except Exception as exc:
        LOGGER.warning("Could not create default pricing table at %s: %s", path, exc)


def load_pricing_table(path: Path = PRICING_TABLE_PATH) -> dict[str, dict[str, Any]]:
    """Load pricing data, falling back to defaults if the JSON cannot be read."""
    ensure_pricing_table(path)

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        LOGGER.warning("Pricing table is missing; using in-memory defaults.")
        return DEFAULT_PRICING_TABLE.copy()
    except json.JSONDecodeError as exc:
        LOGGER.warning("Pricing table JSON is invalid; using in-memory defaults: %s", exc)
        return DEFAULT_PRICING_TABLE.copy()
    except Exception as exc:
        LOGGER.warning("Could not read pricing table; using in-memory defaults: %s", exc)
        return DEFAULT_PRICING_TABLE.copy()

    if not isinstance(data, dict):
        LOGGER.warning("Pricing table root must be a JSON object; using in-memory defaults.")
        return DEFAULT_PRICING_TABLE.copy()

    cleaned: dict[str, dict[str, Any]] = {}
    for name, settings in data.items():
        if isinstance(name, str) and isinstance(settings, dict):
            cleaned[name] = settings
        else:
            LOGGER.warning("Skipping invalid pricing entry: %r", name)

    return cleaned


def initialize_database(db_path: Path = DB_PATH) -> None:
    """Create the SQLite database and usage table if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(CREATE_TABLE_SQL)
        connection.execute(CREATE_BALANCE_SNAPSHOTS_SQL)
        _migrate_database(connection)
        connection.commit()


def _migrate_database(connection: sqlite3.Connection) -> None:
    """Add columns introduced by newer tracker versions."""
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(credit_usage)").fetchall()
    }
    migrations = {
        "prompt_id": "ALTER TABLE credit_usage ADD COLUMN prompt_id TEXT",
        "node_id": "ALTER TABLE credit_usage ADD COLUMN node_id TEXT",
        "node_class_type": "ALTER TABLE credit_usage ADD COLUMN node_class_type TEXT",
        "node_title": "ALTER TABLE credit_usage ADD COLUMN node_title TEXT",
        "model_name": "ALTER TABLE credit_usage ADD COLUMN model_name TEXT",
        "input_summary": "ALTER TABLE credit_usage ADD COLUMN input_summary TEXT",
        "source": "ALTER TABLE credit_usage ADD COLUMN source TEXT",
        "dedupe_key": "ALTER TABLE credit_usage ADD COLUMN dedupe_key TEXT",
    }
    for column, sql in migrations.items():
        if column not in existing_columns:
            connection.execute(sql)

    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_usage_dedupe_key
        ON credit_usage(dedupe_key)
        WHERE dedupe_key IS NOT NULL AND dedupe_key != ''
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_balance_snapshots_timestamp
        ON balance_snapshots(timestamp)
        """
    )


def make_dedupe_key(*parts: Any) -> str:
    cleaned = [str(part).strip() for part in parts if str(part).strip()]
    return "|".join(cleaned)


def has_dedupe_key(dedupe_key: str, db_path: Path = DB_PATH) -> bool:
    if not dedupe_key:
        return False
    initialize_database(db_path)
    with sqlite3.connect(db_path, timeout=30) as connection:
        row = connection.execute(
            "SELECT 1 FROM credit_usage WHERE dedupe_key = ? LIMIT 1",
            (dedupe_key,),
        ).fetchone()
    return row is not None


def record_balance_snapshot(
    *,
    instance_name: str,
    credits: float,
    usd: float,
    currency: str = "usd",
    source: str = "dashboard",
    notes: str = "",
    db_path: Path = DB_PATH,
    min_interval_seconds: int = 300,
) -> bool:
    """Persist the current Comfy account balance, rate-limited unless it changes."""
    initialize_database(db_path)
    now = datetime.now().astimezone()
    timestamp = now.isoformat(timespec="seconds")
    clean_instance = _clean_text(instance_name, "This ComfyUI")
    clean_currency = _clean_text(currency, "usd")
    clean_source = _clean_text(source, "dashboard")

    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        latest = connection.execute(
            """
            SELECT timestamp, credits, usd
            FROM balance_snapshots
            WHERE instance_name = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (clean_instance,),
        ).fetchone()

        if latest:
            last_timestamp = str(latest[0] or "")
            try:
                last_dt = datetime.fromisoformat(last_timestamp)
                elapsed = (now - last_dt).total_seconds()
            except ValueError:
                elapsed = min_interval_seconds + 1
            same_balance = round(_safe_float(latest[1]), 4) == round(float(credits), 4)
            same_usd = round(_safe_float(latest[2]), 4) == round(float(usd), 4)
            if same_balance and same_usd and elapsed < min_interval_seconds:
                return False

        connection.execute(
            """
            INSERT INTO balance_snapshots (
                timestamp,
                instance_name,
                credits,
                usd,
                currency,
                source,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                clean_instance,
                round(float(credits), 4),
                round(float(usd), 4),
                clean_currency,
                clean_source,
                _clean_text(notes, ""),
            ),
        )
        connection.commit()
    return True


def balance_snapshot_summary(db_path: Path = DB_PATH) -> dict[str, Any]:
    initialize_database(db_path)
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        first = connection.execute(
            """
            SELECT *
            FROM balance_snapshots
            ORDER BY timestamp ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        latest = connection.execute(
            """
            SELECT *
            FROM balance_snapshots
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0]

    if not first or not latest:
        return {
            "ok": False,
            "snapshot_count": int(count or 0),
            "message": "No balance snapshots yet",
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


def _find_pricing_entry(
    partner_node_name: str,
    pricing_table: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    if partner_node_name in pricing_table:
        return partner_node_name, pricing_table[partner_node_name]

    normalized = partner_node_name.casefold()
    for name, settings in pricing_table.items():
        if name.casefold() == normalized:
            return name, settings

    return partner_node_name, None


def calculate_credits(
    partner_node_name: str,
    quantity: int,
    duration_seconds: float,
    pricing_table: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, float]:
    pricing_table = pricing_table or load_pricing_table()
    matched_name, entry = _find_pricing_entry(partner_node_name, pricing_table)

    if entry is None:
        LOGGER.warning("Partner node %r was not found in pricing_table.json.", partner_node_name)
        return "unknown", 0.0

    pricing_mode = _clean_text(entry.get("pricing_mode"), "unknown")
    quantity = max(0, quantity)
    duration_seconds = max(0.0, duration_seconds)

    if pricing_mode == "fixed_per_run":
        credits = _safe_float(entry.get("credits"), 0.0) * quantity
    elif pricing_mode == "per_second":
        credits = _safe_float(entry.get("credits_per_second"), 0.0) * duration_seconds * quantity
    elif pricing_mode == "per_output":
        credits = _safe_float(entry.get("credits_per_output"), 0.0) * quantity
    elif pricing_mode == "manual":
        credits = 0.0
    else:
        LOGGER.warning(
            "Partner node %r has unsupported pricing_mode %r.",
            matched_name,
            pricing_mode,
        )
        pricing_mode = "unknown"
        credits = 0.0

    return pricing_mode, round(float(credits), 4)


def build_usage_record(
    project_name: str,
    user_name: str,
    workflow_name: str,
    partner_node_name: str,
    quantity: int,
    duration_seconds: float,
    resolution: str,
    notes: str,
    prompt_id: str = "",
    node_id: str = "",
    node_class_type: str = "",
    node_title: str = "",
    model_name: str = "",
    input_summary: str = "",
    source: str = "manual",
    dedupe_key: str = "",
) -> UsageRecord:
    clean_project_name = _clean_text(project_name, "General")
    clean_user_name = _clean_text(user_name, "Unknown")
    clean_workflow_name = _clean_text(workflow_name, "Untitled Workflow")
    clean_partner_node_name = _clean_text(partner_node_name, "Unknown Partner Node")
    clean_resolution = _clean_text(resolution, "")
    clean_notes = _clean_text(notes, "")
    safe_quantity = max(0, _safe_int(quantity, 1))
    safe_duration_seconds = max(0.0, _safe_float(duration_seconds, 0.0))

    pricing_mode, estimated_credits = calculate_credits(
        clean_partner_node_name,
        safe_quantity,
        safe_duration_seconds,
    )
    estimated_usd = round(estimated_credits / CREDITS_PER_USD, 4) if CREDITS_PER_USD else 0.0

    return UsageRecord(
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        project_name=clean_project_name,
        user_name=clean_user_name,
        workflow_name=clean_workflow_name,
        partner_node_name=clean_partner_node_name,
        pricing_mode=pricing_mode,
        quantity=safe_quantity,
        duration_seconds=safe_duration_seconds,
        resolution=clean_resolution,
        estimated_credits=estimated_credits,
        estimated_usd=estimated_usd,
        notes=clean_notes,
        prompt_id=_clean_text(prompt_id, ""),
        node_id=_clean_text(node_id, ""),
        node_class_type=_clean_text(node_class_type, ""),
        node_title=_clean_text(node_title, ""),
        model_name=_clean_text(model_name, ""),
        input_summary=_clean_text(input_summary, ""),
        source=_clean_text(source, "manual"),
        dedupe_key=_clean_text(dedupe_key, ""),
    )


def insert_usage_record(record: UsageRecord, db_path: Path = DB_PATH, sync_peers: bool = True) -> bool:
    initialize_database(db_path)
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute(
                """
                INSERT INTO credit_usage (
                    timestamp,
                    project_name,
                    user_name,
                    workflow_name,
                    partner_node_name,
                    pricing_mode,
                    quantity,
                    duration_seconds,
                    resolution,
                    estimated_credits,
                    estimated_usd,
                    notes,
                    prompt_id,
                    node_id,
                    node_class_type,
                    node_title,
                    model_name,
                    input_summary,
                    source,
                    dedupe_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.timestamp,
                    record.project_name,
                    record.user_name,
                    record.workflow_name,
                    record.partner_node_name,
                    record.pricing_mode,
                    record.quantity,
                    record.duration_seconds,
                    record.resolution,
                    record.estimated_credits,
                    record.estimated_usd,
                    record.notes,
                    record.prompt_id,
                    record.node_id,
                    record.node_class_type,
                    record.node_title,
                    record.model_name,
                    record.input_summary,
                    record.source,
                    record.dedupe_key,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "dedupe_key" in str(exc):
                LOGGER.info("Skipped duplicate credit usage row: %s", record.dedupe_key)
                return False
            raise
        connection.commit()
    if sync_peers and db_path == DB_PATH:
        try:
            from .peer_sync import enqueue_usage_record_sync
        except ImportError:
            try:
                from peer_sync import enqueue_usage_record_sync
            except Exception:
                enqueue_usage_record_sync = None
        except Exception:
            enqueue_usage_record_sync = None

        if enqueue_usage_record_sync is not None:
            try:
                enqueue_usage_record_sync(record)
            except Exception as exc:
                LOGGER.warning("Could not enqueue peer sync: %s", exc)
    return True


def log_credit_usage(
    project_name: str,
    user_name: str,
    workflow_name: str,
    partner_node_name: str,
    quantity: int,
    duration_seconds: float,
    resolution: str,
    notes: str,
    prompt_id: str = "",
    node_id: str = "",
    node_class_type: str = "",
    node_title: str = "",
    model_name: str = "",
    input_summary: str = "",
    source: str = "manual",
    dedupe_key: str = "",
) -> UsageRecord:
    """Build and persist one credit usage record."""
    record = build_usage_record(
        project_name=project_name,
        user_name=user_name,
        workflow_name=workflow_name,
        partner_node_name=partner_node_name,
        quantity=quantity,
        duration_seconds=duration_seconds,
        resolution=resolution,
        notes=notes,
        prompt_id=prompt_id,
        node_id=node_id,
        node_class_type=node_class_type,
        node_title=node_title,
        model_name=model_name,
        input_summary=input_summary,
        source=source,
        dedupe_key=dedupe_key,
    )
    insert_usage_record(record)
    return record


def log_credit_usage_with_estimate(
    project_name: str,
    user_name: str,
    workflow_name: str,
    partner_node_name: str,
    pricing_mode: str,
    quantity: int,
    duration_seconds: float,
    resolution: str,
    estimated_credits: float,
    notes: str,
    prompt_id: str = "",
    node_id: str = "",
    node_class_type: str = "",
    node_title: str = "",
    model_name: str = "",
    input_summary: str = "",
    source: str = "runtime_price",
    dedupe_key: str = "",
) -> UsageRecord:
    """Persist one usage row with a caller-provided credit estimate."""
    safe_credits = max(0.0, _safe_float(estimated_credits, 0.0))
    record = UsageRecord(
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        project_name=_clean_text(project_name, "General"),
        user_name=_clean_text(user_name, "Unknown"),
        workflow_name=_clean_text(workflow_name, "Untitled Workflow"),
        partner_node_name=_clean_text(partner_node_name, "Unknown Partner Node"),
        pricing_mode=_clean_text(pricing_mode, "runtime_price"),
        quantity=max(0, _safe_int(quantity, 1)),
        duration_seconds=max(0.0, _safe_float(duration_seconds, 0.0)),
        resolution=_clean_text(resolution, ""),
        estimated_credits=round(safe_credits, 4),
        estimated_usd=round(safe_credits / CREDITS_PER_USD, 4) if CREDITS_PER_USD else 0.0,
        notes=_clean_text(notes, ""),
        prompt_id=_clean_text(prompt_id, ""),
        node_id=_clean_text(node_id, ""),
        node_class_type=_clean_text(node_class_type, ""),
        node_title=_clean_text(node_title, ""),
        model_name=_clean_text(model_name, ""),
        input_summary=_clean_text(input_summary, ""),
        source=_clean_text(source, "runtime_price"),
        dedupe_key=_clean_text(dedupe_key, ""),
    )
    insert_usage_record(record)
    return record


def format_summary(record: UsageRecord) -> str:
    credits = f"{record.estimated_credits:g}"
    usd = f"{record.estimated_usd:.2f}"
    return (
        f"Logged {record.partner_node_name} for project {record.project_name}: "
        f"{credits} credits estimated, approx ${usd}"
    )
