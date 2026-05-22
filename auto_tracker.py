from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import datetime
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    from server import PromptServer
except Exception:
    PromptServer = None

try:
    from .tracker_db import (
        CREDITS_PER_USD,
        LOGGER,
        format_summary,
        has_dedupe_key,
        load_pricing_table,
        log_credit_usage,
        log_credit_usage_with_estimate,
        make_dedupe_key,
    )
    from .official_usage import remember_auth_from_prompt
except ImportError:
    from tracker_db import CREDITS_PER_USD, LOGGER, format_summary, has_dedupe_key, load_pricing_table, log_credit_usage, log_credit_usage_with_estimate, make_dedupe_key
    from official_usage import remember_auth_from_prompt


AUTO_TRACKING_ENABLED = True
TRACK_UNMAPPED_RUNTIME_PRICE_NODES = True
STATUS_PATH = Path(__file__).resolve().parent / "tracker_status.json"
API_NODE_CATALOG_PATH = Path(__file__).resolve().parent / "api_node_catalog.json"
COMFY_ROOT_DIR = Path(__file__).resolve().parents[2]
COMFY_API_NODES_DIR = COMFY_ROOT_DIR / "comfy_api_nodes"
API_NODE_CATALOG_CACHE: dict[str, dict[str, Any]] | None = None
BRICK_SAVER_CLASS_TYPES = {"SaveArchVizImage", "SaveArchVizSequence", "SaveArchVizVideo"}
BRICK_SAVER_DISPLAY_NAMES = {"Save Brick Image", "Save Brick Sequence", "Save Brick Video"}
BRICK_SAVER_DEFAULT_PROJECT = "0000_base"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
NODE_RESULT_STATUSES = {"executed", "execution_success"}

QUANTITY_KEYS = (
    "quantity",
    "num_outputs",
    "number_of_outputs",
    "num_images",
    "number_of_images",
    "batch_size",
    "image_count",
    "count",
)

DURATION_KEYS = (
    "duration_seconds",
    "duration",
    "video_duration",
    "length_seconds",
    "seconds",
)

RESOLUTION_KEYS = (
    "resolution",
    "size",
    "image_size",
    "video_size",
    "output_resolution",
    "width",
    "height",
    "aspect_ratio",
)

MODEL_KEYS = (
    "model",
    "model_name",
    "model_id",
)

SUMMARY_KEYS = (
    "model",
    "model_name",
    "model_id",
    "resolution",
    "duration",
    "duration_seconds",
    "aspect_ratio",
    "response_modalities",
    "number_of_images",
    "num_images",
    "seed",
)


def _node_schema_id(node_cls: Any) -> str:
    """Return the Comfy API node schema id when available.

    The runtime helper get_node_id() returns the current canvas node id from
    hidden.unique_id, not the stable node class name, so use the schema first.
    """
    try:
        schema = node_cls.define_schema()
        node_id = getattr(schema, "node_id", "")
        if node_id:
            return str(node_id)
    except Exception:
        pass

    return str(getattr(node_cls, "__name__", "") or node_cls)


def _estimate_kling_video_usd(inputs: dict[str, Any]) -> float:
    model = _scalar_text(_input_value(inputs, ("model_name", "model"), "")).casefold()
    mode = _scalar_text(_input_value(inputs, ("mode",), "")).casefold()
    duration = _scalar_text(_input_value(inputs, ("duration",), "5")).casefold()
    is_10s = "10" in duration

    if "v2-5-turbo" in model:
        return 0.7 if is_10s else 0.35
    if "v2-1-master" in model or "v2-master" in model:
        return 2.8 if is_10s else 1.4
    if "v2-1" in model or "v1-6" in model or "v1-5" in model:
        if "pro" in mode:
            return 0.98 if is_10s else 0.49
        return 0.56 if is_10s else 0.28
    if "v1" in model:
        if "pro" in mode:
            return 0.98 if is_10s else 0.49
        return 0.28 if is_10s else 0.14
    return 0.14


def _estimate_kling_mode_label_usd(inputs: dict[str, Any]) -> float:
    mode_label = _scalar_text(_input_value(inputs, ("mode",), "")).casefold()
    if not mode_label:
        return 0.0
    return _estimate_kling_video_usd(
        {
            "model_name": mode_label,
            "mode": mode_label,
            "duration": "10" if "10" in mode_label else "5",
        }
    )


def _kling_resolution(inputs: dict[str, Any], default: str = "1080p") -> str:
    value = _input_value(inputs, ("resolution",), None)
    if value is None:
        value = _input_nested(inputs, "model.resolution", default)
    return _scalar_text(value).lower() or default


def _kling_duration(inputs: dict[str, Any], default: float = 5.0) -> float:
    value = _input_nested(inputs, "model.duration", None)
    if value is None:
        value = _input_nested(inputs, "multi_shot.duration", None)
    if value is None:
        value = _input_value(inputs, ("duration",), default)
    return max(0.0, _safe_float(value, default))


def _kling_multishot_duration(inputs: dict[str, Any], default: float = 5.0) -> float:
    multi_shot = inputs.get("multi_shot")
    if isinstance(multi_shot, dict):
        label = str(multi_shot.get("multi_shot", "disabled"))
        if label != "disabled":
            total = 0.0
            for index in range(1, 7):
                total += _safe_float(multi_shot.get(f"storyboard_{index}_duration"), 0.0)
            return total or default
        return _safe_float(multi_shot.get("duration"), default)

    label = str(_input_value(inputs, ("multi_shot",), "disabled"))
    if label != "disabled":
        total = 0.0
        for index in range(1, 7):
            total += _safe_float(_input_nested(inputs, f"multi_shot.storyboard_{index}_duration"), 0.0)
        return total or default
    return _kling_duration(inputs, default)


def _estimate_kling_omni_video_usd(inputs: dict[str, Any]) -> float:
    resolution = _kling_resolution(inputs)
    mode = "4k" if resolution == "4k" else ("std" if resolution == "720p" else "pro")
    model_name = _scalar_text(_input_value(inputs, ("model_name",), "")).casefold()
    generate_audio = _truthy(_input_value(inputs, ("generate_audio",), False))
    has_v3_audio = "v3" in model_name and generate_audio
    rates = {"std": 0.112, "pro": 0.14, "4k": 0.42} if has_v3_audio else {"std": 0.084, "pro": 0.112, "4k": 0.42}
    return rates.get(mode, 0.112) * _kling_duration(inputs, 5.0)


def _estimate_kling_v3_video_usd(inputs: dict[str, Any]) -> float:
    resolution = _kling_resolution(inputs)
    generate_audio = _truthy(_input_value(inputs, ("generate_audio",), True))
    audio_key = "on" if generate_audio else "off"
    rates = {
        "4k": {"off": 0.42, "on": 0.42},
        "1080p": {"off": 0.112, "on": 0.168},
        "720p": {"off": 0.084, "on": 0.126},
    }
    return rates.get(resolution, rates["1080p"]).get(audio_key, 0.168) * _kling_multishot_duration(inputs, 5.0)


def _estimate_kling_v3_first_last_usd(inputs: dict[str, Any]) -> float:
    resolution = _kling_resolution(inputs)
    generate_audio = _truthy(_input_value(inputs, ("generate_audio",), True))
    audio_key = "on" if generate_audio else "off"
    rates = {
        "4k": {"off": 0.42, "on": 0.42},
        "1080p": {"off": 0.112, "on": 0.168},
        "720p": {"off": 0.084, "on": 0.126},
    }
    return rates.get(resolution, rates["1080p"]).get(audio_key, 0.168) * _kling_duration(inputs, 5.0)


def _estimate_kling_omni_image_usd(inputs: dict[str, Any]) -> float:
    resolution = _scalar_text(_input_value(inputs, ("resolution",), "1K")).casefold()
    model_name = _scalar_text(_input_value(inputs, ("model_name",), "")).casefold()
    series_amount = _scalar_text(_input_value(inputs, ("series_amount",), "disabled"))
    base = {"1k": 0.028, "2k": 0.028, "4k": 0.056}.get(resolution, 0.028)
    multiplier = 1 if model_name == "kling-image-o1" or series_amount == "disabled" else max(1, _safe_int(series_amount, 1))
    return base * multiplier


def _estimate_kling_image_generation_usd(inputs: dict[str, Any]) -> float:
    model_name = _scalar_text(_input_value(inputs, ("model_name",), "")).casefold()
    image_connected = _is_connected_input(inputs.get("image"))
    if "kling-v1-5" in model_name:
        base = 0.028 if image_connected else 0.014
    elif "kling-v3" in model_name:
        base = 0.028
    else:
        base = 0.014
    return base * max(1, _safe_int(_input_value(inputs, ("n",), 1), 1))


def _estimate_kling_audio_video_usd(inputs: dict[str, Any]) -> float:
    duration = _kling_duration(inputs, 5.0)
    generate_audio = _truthy(_input_value(inputs, ("generate_audio",), True))
    return 0.07 * duration * (2 if generate_audio else 1)


def _estimate_kling_motion_usd(inputs: dict[str, Any]) -> float:
    # This node is billed per second of the reference video. The prompt JSON
    # normally does not include that media duration, so only estimate when a
    # caller has supplied duration metadata.
    duration = _duration_from_inputs(inputs, 0.0)
    if duration <= 0:
        return 0.0
    mode = _scalar_text(_input_value(inputs, ("mode",), "pro")).casefold()
    return (0.07 if mode == "std" else 0.112) * duration


def _estimate_kling_avatar_usd(inputs: dict[str, Any]) -> float:
    duration = _duration_from_inputs(inputs, 0.0)
    if duration <= 0:
        return 0.0
    mode = _scalar_text(_input_value(inputs, ("mode",), "pro")).casefold()
    return (0.056 if mode == "std" else 0.112) * duration


def _estimate_price_badge_credits(
    class_type: str,
    inputs: dict[str, Any],
    prompt: dict[str, Any] | None = None,
) -> float:
    normalized = _normalize(class_type)
    usd = 0.0
    if normalized in {"klingtexttovideonode", "klingstartendframenode"}:
        usd = _estimate_kling_mode_label_usd(inputs)
    elif normalized == "klingimage2videonode":
        usd = _estimate_kling_video_usd(inputs)
    elif normalized in {
        "klingomniprotexttovideonode",
        "klingomniprofirstlastframenode",
        "klingomniproimagetovideonode",
    }:
        usd = _estimate_kling_omni_video_usd(inputs)
    elif normalized == "klingomniprovideotovideonode":
        resolution = _kling_resolution(inputs)
        usd = (0.084 if resolution == "720p" else 0.112) * _kling_duration(inputs, 3.0)
    elif normalized == "klingomniproeditvideonode":
        duration = _duration_from_inputs(inputs, 0.0)
        if duration <= 0 and prompt is not None:
            duration = _infer_connected_video_duration(prompt, inputs)
        if duration > 0:
            resolution = _kling_resolution(inputs)
            usd = (0.126 if resolution == "720p" else 0.168) * duration
    elif normalized == "klingomniproimagenode":
        usd = _estimate_kling_omni_image_usd(inputs)
    elif normalized == "klingcameracontrolt2vnode":
        usd = 0.14
    elif normalized == "klingcameracontroli2vnode":
        usd = 0.49
    elif normalized == "klingvideoextendnode":
        usd = 0.28
    elif normalized == "klingdualcharactervideoeffectnode":
        usd = _estimate_kling_video_usd(inputs)
    elif normalized == "klingsingleimagevideoeffectnode":
        effect_scene = _scalar_text(_input_value(inputs, ("effect_scene",), "")).casefold()
        usd = 0.49 if ("dizzydizzy" in effect_scene or "bloombloom" in effect_scene) else 0.28
    elif normalized in {"klinglipsyncaudiotovideonode", "klinglipsynctexttovideonode"}:
        usd = 0.1
    elif normalized == "klingvirtualtryonnode":
        usd = 0.7
    elif normalized == "klingimagegenerationnode":
        usd = _estimate_kling_image_generation_usd(inputs)
    elif normalized in {"klingtexttovideowithaudio", "klingimagetovideowithaudio"}:
        usd = _estimate_kling_audio_video_usd(inputs)
    elif normalized == "klingmotioncontrol":
        usd = _estimate_kling_motion_usd(inputs)
    elif normalized == "klingvideonode":
        usd = _estimate_kling_v3_video_usd(inputs)
    elif normalized == "klingfirstlastframenode":
        usd = _estimate_kling_v3_first_last_usd(inputs)
    elif normalized == "klingavatarnode":
        usd = _estimate_kling_avatar_usd(inputs)

    return round(max(0.0, usd) * CREDITS_PER_USD, 4)


def _provider_from_module(module_name: str) -> str:
    key = module_name.removeprefix("nodes_").casefold()
    return {
        "bfl": "BFL",
        "bria": "Bria",
        "bytedance": "ByteDance",
        "elevenlabs": "ElevenLabs",
        "gemini": "Google",
        "grok": "Grok",
        "hitpaw": "HitPaw",
        "hunyuan3d": "Hunyuan 3D",
        "ideogram": "Ideogram",
        "kling": "Kling",
        "ltxv": "LTXV",
        "luma": "Luma",
        "magnific": "Magnific",
        "meshy": "Meshy",
        "minimax": "MiniMax",
        "moonvalley": "Moonvalley",
        "openai": "OpenAI",
        "pixverse": "PixVerse",
        "quiver": "Quiver",
        "recraft": "Recraft",
        "reve": "Reve",
        "rodin": "Rodin",
        "runway": "Runway",
        "sonilo": "Sonilo",
        "sora": "OpenAI",
        "stability": "Stability AI",
        "topaz": "Topaz",
        "tripo": "Tripo",
        "veo2": "Google Veo",
        "vidu": "Vidu",
        "wan": "Wan",
        "wavespeed": "WaveSpeed",
    }.get(key, key.replace("_", " ").title())


def _literal_assignment(block: str, name: str) -> str:
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(['\"])(.*?)\1", block, flags=re.MULTILINE)
    return match.group(2) if match else ""


def _schema_literal(block: str, name: str, constants: dict[str, str]) -> str:
    literal = re.search(rf"{re.escape(name)}\s*=\s*(['\"])(.*?)\1", block)
    if literal:
        return literal.group(2)

    class_ref = re.search(rf"{re.escape(name)}\s*=\s*cls\.([A-Z_]+)", block)
    if class_ref:
        return constants.get(class_ref.group(1), "")

    return ""


def _fixed_usd_from_price_badge(block: str) -> tuple[float, str]:
    match = re.search(
        r"expr\s*=\s*(?:[rRuUfFbB]*)?(['\"]{3}|['\"])\s*"
        r"\{\s*['\"]type['\"]\s*:\s*['\"]usd['\"]\s*,\s*['\"]usd['\"]\s*:\s*([0-9.]+)",
        block,
        flags=re.DOTALL,
    )
    if not match:
        return 0.0, ""

    suffix_match = re.search(r"['\"]suffix['\"]\s*:\s*['\"]([^'\"]+)['\"]", block)
    return _safe_float(match.group(2), 0.0), (suffix_match.group(1) if suffix_match else "")


def _class_blocks(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^class\s+(\w+)\s*\(", text, flags=re.MULTILINE))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((match.group(1), text[match.start():end]))
    return blocks


def _load_api_node_catalog() -> dict[str, dict[str, Any]]:
    global API_NODE_CATALOG_CACHE
    if API_NODE_CATALOG_CACHE is not None:
        return API_NODE_CATALOG_CACHE

    catalog: dict[str, dict[str, Any]] = {}
    if not COMFY_API_NODES_DIR.exists():
        API_NODE_CATALOG_CACHE = catalog
        return catalog

    for path in sorted(COMFY_API_NODES_DIR.glob("nodes_*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            LOGGER.warning("Could not scan API node file %s: %s", path, exc)
            continue

        provider = _provider_from_module(path.stem)
        for class_name, block in _class_blocks(text):
            if "is_api_node=True" not in block and "price_badge" not in block:
                continue

            constants = {
                "NODE_ID": _literal_assignment(block, "NODE_ID"),
                "DISPLAY_NAME": _literal_assignment(block, "DISPLAY_NAME"),
            }
            node_id = _schema_literal(block, "node_id", constants) or class_name
            display_name = _schema_literal(block, "display_name", constants) or node_id
            fixed_usd, fixed_suffix = _fixed_usd_from_price_badge(block)
            catalog[_normalize(node_id)] = {
                "provider": provider,
                "class_name": class_name,
                "node_id": node_id,
                "display_name": display_name,
                "module": path.name,
                "has_price_badge": "price_badge" in block or "PriceBadge" in block,
                "has_price_extractor": "price_extractor=" in block,
                "fixed_usd": fixed_usd,
                "fixed_suffix": fixed_suffix,
            }
            catalog[_normalize(class_name)] = catalog[_normalize(node_id)]

    try:
        rows = sorted({item["node_id"]: item for item in catalog.values()}.values(), key=lambda row: (row["provider"], row["node_id"]))
        API_NODE_CATALOG_PATH.write_text(json.dumps({"row_count": len(rows), "rows": rows}, indent=2), encoding="utf-8")
    except Exception:
        pass

    API_NODE_CATALOG_CACHE = catalog
    return catalog


def _catalog_entry_for_node(class_type: str, node_title: str = "") -> dict[str, Any] | None:
    catalog = _load_api_node_catalog()
    for value in (class_type, node_title):
        normalized = _normalize(value)
        if normalized in catalog:
            return catalog[normalized]
    return None


def _catalog_fallback_credits(entry: dict[str, Any] | None, inputs: dict[str, Any]) -> float:
    if not entry:
        return 0.0

    usd = _safe_float(entry.get("fixed_usd"), 0.0)
    if usd <= 0:
        return 0.0

    suffix = str(entry.get("fixed_suffix", "")).casefold()
    if "/second" in suffix:
        duration = _duration_from_inputs(inputs, 0.0)
        usd *= duration if duration > 0 else 0.0
    elif "/minute" in suffix:
        duration = _duration_from_inputs(inputs, 0.0)
        usd *= (duration / 60.0) if duration > 0 else 0.0
    elif "/1k chars" in suffix or "/1k characters" in suffix:
        text = " ".join(_scalar_text(value) for value in inputs.values() if not isinstance(value, (list, tuple, dict)))
        usd *= max(1.0, len(text) / 1000.0) if text else 0.0

    return round(max(0.0, usd) * CREDITS_PER_USD, 4)


def _write_status(event: str, details: dict[str, Any] | None = None) -> None:
    try:
        payload = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "details": details or {},
        }
        STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _normalize(value: Any) -> str:
    return "".join(ch for ch in str(value).casefold() if ch.isalnum())


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (list, tuple)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 1) -> int:
    if isinstance(value, (list, tuple)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _input_value(inputs: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    normalized_keys = {_normalize(key) for key in keys}
    for input_name, value in inputs.items():
        if _normalize(input_name) in normalized_keys:
            return value
    return default


def _input_nested(inputs: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in inputs:
        return inputs[key]

    current: Any = inputs
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _scalar_text(value: Any) -> str:
    if value is None or isinstance(value, (list, tuple, dict)):
        return ""
    return str(value)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).casefold().strip() in {"true", "1", "yes", "on"}


def _duration_from_inputs(inputs: dict[str, Any], default: float = 0.0) -> float:
    value = _input_value(inputs, DURATION_KEYS, default)
    if isinstance(value, str):
        match = next((token for token in value.replace("/", " ").split() if token.rstrip("s").isdigit()), "")
        if match:
            value = match.rstrip("s")
    return max(0.0, _safe_float(value, default))


def _media_duration_from_path(value: Any) -> float:
    text = _scalar_text(value).strip()
    if not text:
        return 0.0

    candidates: list[Path] = []
    raw_path = Path(text)
    if raw_path.suffix.casefold() not in VIDEO_EXTENSIONS:
        return 0.0

    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.extend(
            [
                COMFY_ROOT_DIR / "input" / raw_path,
                COMFY_ROOT_DIR / "output" / raw_path,
                COMFY_ROOT_DIR / "temp" / raw_path,
            ]
        )

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if not resolved.exists():
            continue
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(resolved),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            continue
        duration = _safe_float((result.stdout or "").strip(), 0.0)
        if duration > 0:
            return duration
    return 0.0


def _duration_from_media_inputs(inputs: dict[str, Any]) -> float:
    for key in ("video", "reference_video", "filename", "file", "path", "video_path", "upload"):
        duration = _media_duration_from_path(_input_value(inputs, (key,), ""))
        if duration > 0:
            return duration
    return 0.0


def _node_duration_from_prompt(
    prompt: dict[str, Any],
    node_id: str,
    seen: set[str] | None = None,
) -> float:
    seen = seen or set()
    if node_id in seen:
        return 0.0
    seen.add(node_id)

    node_data = prompt.get(str(node_id))
    if not isinstance(node_data, dict):
        return 0.0
    inputs = node_data.get("inputs", {})
    if not isinstance(inputs, dict):
        inputs = {}

    explicit = _duration_from_inputs(inputs, 0.0)
    if explicit > 0:
        return explicit

    class_type = str(node_data.get("class_type", ""))
    normalized = _normalize(class_type)
    if normalized == "klingvideonode":
        duration = _kling_multishot_duration(inputs, 0.0)
        if duration > 0:
            return duration
    if normalized in {
        "klingfirstlastframenode",
        "klingomniprotexttovideonode",
        "klingomniprofirstlastframenode",
        "klingomniproimagetovideonode",
        "klingomniprovideotovideonode",
    }:
        duration = _kling_duration(inputs, 0.0)
        if duration > 0:
            return duration

    media_duration = _duration_from_media_inputs(inputs)
    if media_duration > 0:
        return media_duration

    for value in inputs.values():
        if _is_connected_input(value):
            duration = _node_duration_from_prompt(prompt, str(value[0]), seen)
            if duration > 0:
                return duration
    return 0.0


def _infer_connected_video_duration(prompt: dict[str, Any], inputs: dict[str, Any]) -> float:
    for key in ("video", "reference_video"):
        value = _input_value(inputs, (key,), None)
        if _is_connected_input(value):
            duration = _node_duration_from_prompt(prompt, str(value[0]), set())
            if duration > 0:
                return duration

    return _duration_from_media_inputs(inputs)


def _is_connected_input(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) >= 2


def _extract_model_name(inputs: dict[str, Any]) -> str:
    return _scalar_text(_input_value(inputs, MODEL_KEYS, ""))


def _extract_input_summary(inputs: dict[str, Any]) -> str:
    summary: dict[str, Any] = {}
    normalized_keys = {_normalize(key) for key in SUMMARY_KEYS}
    for key, value in inputs.items():
        if _normalize(key) not in normalized_keys:
            continue
        if isinstance(value, (list, tuple, dict)):
            continue
        summary[key] = value
    try:
        return json.dumps(summary, ensure_ascii=True, sort_keys=True)
    except Exception:
        return ""


def _workflow_title_map(json_data: dict[str, Any]) -> dict[str, str]:
    extra_data = json_data.get("extra_data", {})
    if not isinstance(extra_data, dict):
        return {}
    extra_pnginfo = extra_data.get("extra_pnginfo", {})
    if not isinstance(extra_pnginfo, dict):
        return {}
    workflow = extra_pnginfo.get("workflow", {})
    if not isinstance(workflow, dict):
        return {}
    nodes = workflow.get("nodes", [])
    if not isinstance(nodes, list):
        return {}

    title_map: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        title = node.get("title") or node.get("type")
        if node_id is not None and isinstance(title, str) and title.strip():
            title_map[str(node_id)] = title.strip()
    return title_map


def _extract_quantity(inputs: dict[str, Any]) -> int:
    return max(1, _safe_int(_input_value(inputs, QUANTITY_KEYS, 1), 1))


def _extract_duration(inputs: dict[str, Any]) -> float:
    return max(0.0, _safe_float(_input_value(inputs, DURATION_KEYS, 0.0), 0.0))


def _extract_resolution(inputs: dict[str, Any]) -> str:
    width = _input_value(inputs, ("width",), None)
    height = _input_value(inputs, ("height",), None)
    if width is not None and height is not None:
        if not isinstance(width, (list, tuple)) and not isinstance(height, (list, tuple)):
            return f"{width}x{height}"

    value = _input_value(inputs, RESOLUTION_KEYS, "")
    if isinstance(value, (list, tuple)) or value is None:
        return ""
    return str(value)


def _pricing_aliases(partner_name: str, settings: dict[str, Any]) -> list[str]:
    aliases = [partner_name]
    for key in ("aliases", "auto_detect_class_types", "class_types"):
        value = settings.get(key, [])
        if isinstance(value, str):
            aliases.append(value)
        elif isinstance(value, list):
            aliases.extend(str(item) for item in value)
    return aliases


def _match_pricing_entry(
    class_type: str,
    pricing_table: dict[str, dict[str, Any]],
) -> str | None:
    normalized_class_type = _normalize(class_type)
    for partner_name, settings in pricing_table.items():
        if not isinstance(settings, dict):
            continue

        for alias in _pricing_aliases(partner_name, settings):
            normalized_alias = _normalize(alias)
            if not normalized_alias:
                continue
            if normalized_alias == normalized_class_type:
                return partner_name

    return None


def _find_partner_nodes(
    prompt: dict[str, Any],
    title_map: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    pricing_table = load_pricing_table()
    detected: dict[str, dict[str, Any]] = {}
    title_map = title_map or {}

    for node_id, node_data in prompt.items():
        if not isinstance(node_data, dict):
            continue

        class_type = str(node_data.get("class_type", ""))
        if class_type == "CreditTrackerLogger":
            continue

        inputs = node_data.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}

        meta = node_data.get("_meta", {})
        if not isinstance(meta, dict):
            meta = {}
        node_title = meta.get("title") or title_map.get(str(node_id)) or class_type
        catalog_entry = _catalog_entry_for_node(class_type, str(node_title))

        partner_name = _match_pricing_entry(class_type, pricing_table)
        if partner_name is None:
            partner_name = _match_pricing_entry(str(node_title), pricing_table)
        if partner_name is None:
            partner_name = _match_pricing_entry(f"{class_type} {node_title}", pricing_table)
        if partner_name is None:
            if catalog_entry is None:
                continue
            partner_name = str(catalog_entry.get("display_name") or catalog_entry.get("node_id") or class_type)
        elif catalog_entry is not None and partner_name in {"Kling", "Veo", "Runway"}:
            # Provider-level pricing-table aliases are useful for detection, but
            # reports are more useful when built-in API nodes keep their exact
            # display name.
            partner_name = str(catalog_entry.get("display_name") or partner_name)

        fallback_credits = _estimate_price_badge_credits(class_type, inputs, prompt)
        if fallback_credits <= 0:
            fallback_credits = _catalog_fallback_credits(catalog_entry, inputs)

        detected[str(node_id)] = {
            "partner_name": partner_name,
            "class_type": class_type,
            "node_title": str(node_title),
            "provider": str(catalog_entry.get("provider", "")) if catalog_entry else "",
            "model_name": _extract_model_name(inputs),
            "input_summary": _extract_input_summary(inputs),
            "quantity": _extract_quantity(inputs),
            "duration_seconds": _extract_duration(inputs),
            "resolution": _extract_resolution(inputs),
            "fallback_credits": fallback_credits,
        }

    return detected


def _first_text(*values: Any, default: str) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _sanitize_brick_project_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r'[<>:"/\\|?*]', "-", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"-{2,}", "-", text)
    text = text.strip(" .-_\t")
    if not text:
        return ""
    if text.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        text = f"_{text}"
    return text[:120].strip()


def _unique_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = value.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            unique.append(cleaned)
    return unique


def _preferred_brick_project(projects: list[str]) -> str:
    for project in projects:
        if project.casefold() != BRICK_SAVER_DEFAULT_PROJECT.casefold():
            return project
    return projects[0] if projects else ""


def _brick_projects_from_prompt(prompt: dict[str, Any], title_map: dict[str, str] | None = None) -> list[str]:
    title_map = title_map or {}
    projects: list[str] = []

    for node_id, node_data in prompt.items():
        if not isinstance(node_data, dict):
            continue

        class_type = str(node_data.get("class_type", ""))
        meta = node_data.get("_meta", {})
        if not isinstance(meta, dict):
            meta = {}
        node_title = str(meta.get("title") or title_map.get(str(node_id)) or "")

        if class_type not in BRICK_SAVER_CLASS_TYPES and node_title not in BRICK_SAVER_DISPLAY_NAMES:
            continue

        inputs = node_data.get("inputs", {})
        if not isinstance(inputs, dict):
            continue

        project_name = inputs.get("project_name")
        if not isinstance(project_name, str):
            continue

        clean_project_name = _sanitize_brick_project_name(project_name)
        if clean_project_name:
            projects.append(clean_project_name)

    return _unique_text(projects)


def _brick_projects_from_workflow(workflow: dict[str, Any]) -> list[str]:
    nodes = workflow.get("nodes", [])
    if not isinstance(nodes, list):
        return []

    projects: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("type") or node.get("class_type") or "")
        title = str(node.get("title") or "")
        if class_type not in BRICK_SAVER_CLASS_TYPES and title not in BRICK_SAVER_DISPLAY_NAMES:
            continue

        properties = node.get("properties", {})
        if isinstance(properties, dict):
            for key in ("project_name", "Project Name", "project"):
                clean_project_name = _sanitize_brick_project_name(properties.get(key))
                if clean_project_name:
                    projects.append(clean_project_name)

        widgets_values = node.get("widgets_values", [])
        if isinstance(widgets_values, list) and widgets_values:
            # Brick Saver's first visible widget after images is project_name.
            clean_project_name = _sanitize_brick_project_name(widgets_values[0])
            if clean_project_name:
                projects.append(clean_project_name)

    return _unique_text(projects)


def _project_context_note(metadata: dict[str, str]) -> str:
    source = metadata.get("project_source", "")
    project_names = metadata.get("project_names", "")
    if source == "brick_saver" and project_names:
        return f"; brick_saver_projects={project_names}"
    if source == "credit_tracker_metadata":
        return "; project_source=credit_tracker_metadata"
    return ""


def _metadata_from_json(json_data: dict[str, Any]) -> dict[str, str]:
    prompt = json_data.get("prompt")
    if not isinstance(prompt, dict):
        prompt = {}

    extra_data = json_data.get("extra_data", {})
    if not isinstance(extra_data, dict):
        extra_data = {}

    extra_pnginfo = extra_data.get("extra_pnginfo", {})
    if not isinstance(extra_pnginfo, dict):
        extra_pnginfo = {}

    workflow = extra_pnginfo.get("workflow", {})
    if not isinstance(workflow, dict):
        workflow = {}

    tracker_meta = {}
    for source in (
        json_data.get("credit_tracker"),
        extra_data.get("credit_tracker"),
        extra_pnginfo.get("credit_tracker"),
        workflow.get("credit_tracker"),
    ):
        if isinstance(source, dict):
            tracker_meta.update(source)

    brick_projects = _unique_text(
        [
            *_brick_projects_from_prompt(prompt, _workflow_title_map(json_data)),
            *_brick_projects_from_workflow(workflow),
        ]
    )
    explicit_project = _first_text(
        tracker_meta.get("project_name"),
        tracker_meta.get("project"),
        default="",
    )
    project_name = explicit_project or (_preferred_brick_project(brick_projects) if brick_projects else "General")

    return {
        "project_name": project_name,
        "project_names": ", ".join(brick_projects),
        "project_source": "credit_tracker_metadata" if explicit_project else ("brick_saver" if brick_projects else "default"),
        "user_name": _first_text(
            tracker_meta.get("user_name"),
            tracker_meta.get("user"),
            extra_data.get("client_id"),
            default="Unknown",
        ),
        "workflow_name": _first_text(
            tracker_meta.get("workflow_name"),
            tracker_meta.get("workflow"),
            workflow.get("name"),
            workflow.get("title"),
            extra_pnginfo.get("workflow_name"),
            default="Auto-detected Workflow",
        ),
    }


class AutomaticCreditTracker:
    def __init__(self) -> None:
        self.prompt_data: dict[str, dict[str, Any]] = {}
        self.executed_nodes: dict[str, set[str]] = defaultdict(set)
        self.started_nodes: dict[str, set[str]] = defaultdict(set)
        self.node_start_times: dict[tuple[str, str], float] = {}
        self.logged_nodes: set[tuple[str, str]] = set()
        self.runtime_prices: dict[tuple[str, str], float] = {}

    def on_prompt(self, json_data: dict[str, Any]) -> dict[str, Any]:
        if not AUTO_TRACKING_ENABLED:
            return json_data

        try:
            remember_auth_from_prompt(json_data)
        except Exception as exc:
            LOGGER.warning("Could not cache Comfy account auth for official usage sync: %s", exc)

        prompt = json_data.get("prompt")
        if not isinstance(prompt, dict):
            return json_data

        prompt_id = str(json_data.get("prompt_id") or uuid.uuid4())
        json_data["prompt_id"] = prompt_id

        metadata = _metadata_from_json(json_data)
        detected_nodes = _find_partner_nodes(prompt, _workflow_title_map(json_data))
        if detected_nodes or metadata.get("project_source") != "default":
            self.prompt_data[prompt_id] = {
                "metadata": metadata,
                "detected_nodes": detected_nodes,
            }
        if detected_nodes:
            LOGGER.info(
                "Automatic tracking found %s priced node(s) in prompt %s.",
                len(detected_nodes),
                prompt_id,
            )
            _write_status(
                "prompt_detected",
                {
                    "prompt_id": prompt_id,
                    "nodes": detected_nodes,
                    "project_name": metadata["project_name"],
                    "project_source": metadata.get("project_source", "default"),
                },
            )
        elif metadata.get("project_source") == "brick_saver":
            _write_status(
                "brick_project_detected",
                {
                    "prompt_id": prompt_id,
                    "project_name": metadata["project_name"],
                    "project_names": metadata.get("project_names", ""),
                },
            )

        return json_data

    def handle_event(self, event: str, data: Any) -> None:
        if not isinstance(data, dict):
            return

        prompt_id = data.get("prompt_id")
        if prompt_id is None:
            return
        prompt_id = str(prompt_id)

        if event == "executing":
            node_id = data.get("node")
            if node_id is not None:
                node_id = str(node_id)
                self.started_nodes[prompt_id].add(node_id)
                self.node_start_times[(prompt_id, node_id)] = time.perf_counter()
            return

        if event == "executed":
            node_id = data.get("node")
            if node_id is not None:
                node_id = str(node_id)
                self.executed_nodes[prompt_id].add(node_id)
                self.log_node(prompt_id, node_id, "executed")
            return

        if event == "execution_success":
            self.flush(prompt_id, "execution_success")
            return

        if event in {"execution_error", "execution_interrupted"}:
            self.flush(prompt_id, event)

    def flush(self, prompt_id: str, status: str) -> None:
        stored = self.prompt_data.get(prompt_id)
        executed = self.executed_nodes.pop(prompt_id, set())
        started = self.started_nodes.pop(prompt_id, set())
        if not stored:
            return

        detected_nodes = stored["detected_nodes"]
        billable_nodes = executed | started
        logged_count = 0

        for node_id, detected in detected_nodes.items():
            if node_id not in billable_nodes:
                continue
            if self.log_node(prompt_id, node_id, status):
                logged_count += 1

        for key in list(self.node_start_times):
            if key[0] == prompt_id:
                self.node_start_times.pop(key, None)
        for key in list(self.runtime_prices):
            if key[0] == prompt_id:
                self.runtime_prices.pop(key, None)
        for key in list(self.logged_nodes):
            if key[0] == prompt_id:
                self.logged_nodes.discard(key)
        self.prompt_data.pop(prompt_id, None)

        if logged_count:
            LOGGER.info(
                "Automatic tracking logged %s priced node(s) for prompt %s.",
                logged_count,
                prompt_id,
            )

    def log_node(self, prompt_id: str, node_id: str, status: str) -> bool:
        key = (prompt_id, node_id)
        if key in self.logged_nodes:
            return False

        stored = self.prompt_data.get(prompt_id)
        if not stored:
            return False

        detected = stored["detected_nodes"].get(node_id)
        if not detected:
            return False

        metadata = stored["metadata"]
        duration_seconds = float(detected["duration_seconds"])
        if duration_seconds <= 0:
            start_time = self.node_start_times.get((prompt_id, node_id))
            if start_time is not None:
                duration_seconds = max(0.0, time.perf_counter() - start_time)

        notes = (
            "Auto-tracked from workflow execution; "
            f"prompt_id={prompt_id}; node_id={node_id}; "
            f"class_type={detected['class_type']}; status={status}"
            f"{_project_context_note(metadata)}"
        )

        try:
            runtime_credits = self.runtime_prices.get(key)
            if runtime_credits is not None:
                dedupe_key = make_dedupe_key("runtime_price", prompt_id, node_id, detected["class_type"], runtime_credits)
                if has_dedupe_key(dedupe_key):
                    self.logged_nodes.add(key)
                    return False
                record = log_credit_usage_with_estimate(
                    project_name=metadata["project_name"],
                    user_name=metadata["user_name"],
                    workflow_name=metadata["workflow_name"],
                    partner_node_name=detected["partner_name"],
                    pricing_mode="runtime_price",
                    quantity=int(detected["quantity"]),
                    duration_seconds=duration_seconds,
                    resolution=str(detected["resolution"]),
                    estimated_credits=runtime_credits,
                    notes=f"{notes}; runtime price captured from ComfyUI API node",
                    prompt_id=prompt_id,
                    node_id=node_id,
                    node_class_type=str(detected["class_type"]),
                    node_title=str(detected.get("node_title", "")),
                    model_name=str(detected.get("model_name", "")),
                    input_summary=str(detected.get("input_summary", "")),
                    source="runtime_price",
                    dedupe_key=dedupe_key,
                )
            else:
                fallback_credits = _safe_float(detected.get("fallback_credits"), 0.0)
                dedupe_key = make_dedupe_key("prompt_scan", prompt_id, node_id, detected["class_type"])
                if has_dedupe_key(dedupe_key):
                    self.logged_nodes.add(key)
                    return False
                if status not in NODE_RESULT_STATUSES:
                    record = log_credit_usage_with_estimate(
                        project_name=metadata["project_name"],
                        user_name=metadata["user_name"],
                        workflow_name=metadata["workflow_name"],
                        partner_node_name=detected["partner_name"],
                        pricing_mode="execution_error_unconfirmed",
                        quantity=int(detected["quantity"]),
                        duration_seconds=duration_seconds,
                        resolution=str(detected["resolution"]),
                        estimated_credits=0.0,
                        notes=f"{notes}; excluded from spend estimate because execution did not complete",
                        prompt_id=prompt_id,
                        node_id=node_id,
                        node_class_type=str(detected["class_type"]),
                        node_title=str(detected.get("node_title", "")),
                        model_name=str(detected.get("model_name", "")),
                        input_summary=str(detected.get("input_summary", "")),
                        source="prompt_scan_error",
                        dedupe_key=dedupe_key,
                    )
                elif fallback_credits > 0:
                    record = log_credit_usage_with_estimate(
                        project_name=metadata["project_name"],
                        user_name=metadata["user_name"],
                        workflow_name=metadata["workflow_name"],
                        partner_node_name=detected["partner_name"],
                        pricing_mode="price_badge_estimate",
                        quantity=int(detected["quantity"]),
                        duration_seconds=duration_seconds,
                        resolution=str(detected["resolution"]),
                        estimated_credits=fallback_credits,
                        notes=f"{notes}; estimated from API node price badge formula",
                        prompt_id=prompt_id,
                        node_id=node_id,
                        node_class_type=str(detected["class_type"]),
                        node_title=str(detected.get("node_title", "")),
                        model_name=str(detected.get("model_name", "")),
                        input_summary=str(detected.get("input_summary", "")),
                        source="prompt_scan_price_badge",
                        dedupe_key=dedupe_key,
                    )
                else:
                    record = log_credit_usage(
                        project_name=metadata["project_name"],
                        user_name=metadata["user_name"],
                        workflow_name=metadata["workflow_name"],
                        partner_node_name=detected["partner_name"],
                        quantity=int(detected["quantity"]),
                        duration_seconds=duration_seconds,
                        resolution=str(detected["resolution"]),
                        notes=notes,
                        prompt_id=prompt_id,
                        node_id=node_id,
                        node_class_type=str(detected["class_type"]),
                        node_title=str(detected.get("node_title", "")),
                        model_name=str(detected.get("model_name", "")),
                        input_summary=str(detected.get("input_summary", "")),
                        source="prompt_scan",
                        dedupe_key=dedupe_key,
                    )
            self.logged_nodes.add(key)
            LOGGER.info(format_summary(record))
            _write_status(
                "runtime_price_logged",
                {
                    "prompt_id": prompt_id,
                    "node_id": node_id,
                    "class_type": detected["class_type"],
                    "partner_node_name": detected["partner_name"],
                    "project_name": metadata["project_name"],
                    "project_source": metadata.get("project_source", "default"),
                    "estimated_credits": record.estimated_credits,
                },
            )
            return True
        except Exception as exc:
            LOGGER.warning(
                "Automatic tracker could not log prompt %s node %s: %s",
                prompt_id,
                node_id,
                exc,
            )
            return False

    def capture_runtime_price(self, prompt_id: str, node_id: str, price_usd: float) -> None:
        credits = round(max(0.0, float(price_usd)) * CREDITS_PER_USD, 4)
        self.runtime_prices[(str(prompt_id), str(node_id))] = credits

    def log_runtime_price_direct(
        self,
        prompt_id: str,
        node_id: str,
        class_type: str,
        price_usd: float,
    ) -> bool:
        """Log API-node runtime price even if prompt JSON detection did not run."""
        prompt_id = str(prompt_id)
        node_id = str(node_id)
        key = (prompt_id, node_id)
        if key in self.logged_nodes:
            return False

        credits = round(max(0.0, float(price_usd)) * CREDITS_PER_USD, 4)
        if credits <= 0:
            return False

        stored = self.prompt_data.get(prompt_id, {})
        metadata = stored.get(
            "metadata",
            {
                "project_name": "General",
                "project_names": "",
                "project_source": "default",
                "user_name": "Unknown",
                "workflow_name": "Auto-detected Workflow",
            },
        )

        detected = stored.get("detected_nodes", {}).get(node_id)
        if detected:
            partner_name = detected["partner_name"]
            quantity = int(detected["quantity"])
            duration_seconds = float(detected["duration_seconds"])
            resolution = str(detected["resolution"])
            node_title = str(detected.get("node_title", ""))
            model_name = str(detected.get("model_name", ""))
            input_summary = str(detected.get("input_summary", ""))
        else:
            partner_name = _match_pricing_entry(class_type, load_pricing_table())
            if partner_name is None:
                if not TRACK_UNMAPPED_RUNTIME_PRICE_NODES:
                    LOGGER.warning(
                        "Runtime price captured for unpriced API node class_type=%s, node_id=%s.",
                        class_type,
                        node_id,
                    )
                    return False
                partner_name = class_type
            quantity = 1
            duration_seconds = 0.0
            resolution = ""
            node_title = class_type
            model_name = ""
            input_summary = ""

        notes = (
            "Auto-tracked from ComfyUI API-node runtime price; "
            f"prompt_id={prompt_id}; node_id={node_id}; class_type={class_type}"
            f"{_project_context_note(metadata)}"
        )
        source = "runtime_price" if _match_pricing_entry(class_type, load_pricing_table()) else "runtime_price_unmapped"
        dedupe_key = make_dedupe_key(source, prompt_id, node_id, class_type, credits)
        if has_dedupe_key(dedupe_key):
            self.logged_nodes.add(key)
            return False

        try:
            record = log_credit_usage_with_estimate(
                project_name=metadata["project_name"],
                user_name=metadata["user_name"],
                workflow_name=metadata["workflow_name"],
                partner_node_name=partner_name,
                pricing_mode="runtime_price",
                quantity=quantity,
                duration_seconds=duration_seconds,
                resolution=resolution,
                estimated_credits=credits,
                notes=notes,
                prompt_id=prompt_id,
                node_id=node_id,
                node_class_type=class_type,
                node_title=node_title,
                model_name=model_name,
                input_summary=input_summary,
                source=source,
                dedupe_key=dedupe_key,
            )
            self.logged_nodes.add(key)
            LOGGER.info(format_summary(record))
            _write_status(
                "runtime_price_logged",
                {
                    "prompt_id": prompt_id,
                    "node_id": node_id,
                    "class_type": class_type,
                    "partner_node_name": partner_name,
                    "project_name": metadata["project_name"],
                    "project_source": metadata.get("project_source", "default"),
                    "estimated_credits": credits,
                },
            )
            return True
        except Exception as exc:
            LOGGER.warning(
                "Automatic tracker could not log runtime price for prompt %s node %s: %s",
                prompt_id,
                node_id,
                exc,
            )
            return False


AUTO_TRACKER = AutomaticCreditTracker()


def register_auto_tracker() -> None:
    server = getattr(PromptServer, "instance", None) if PromptServer is not None else None
    if server is None:
        LOGGER.warning("Automatic tracker could not register because PromptServer is unavailable.")
        return

    if getattr(server, "_credit_tracker_auto_registered", False):
        return

    server.add_on_prompt_handler(AUTO_TRACKER.on_prompt)

    original_send_sync = server.send_sync

    def wrapped_send_sync(event, data, sid=None):
        try:
            AUTO_TRACKER.handle_event(event, data)
        except Exception as exc:
            LOGGER.warning("Automatic tracker event handling failed: %s", exc)
        return original_send_sync(event, data, sid)

    server.send_sync = wrapped_send_sync

    try:
        from comfy_execution.utils import get_executing_context
        from comfy_api_nodes.util import client as api_client

        original_display_text = api_client._display_text

        def wrapped_display_text(node_cls, text, *, status=None, price=None):
            if price is not None:
                try:
                    context = get_executing_context()
                    if context is not None:
                        class_type = _node_schema_id(node_cls)
                        AUTO_TRACKER.capture_runtime_price(context.prompt_id, context.node_id, float(price))
                        AUTO_TRACKER.log_runtime_price_direct(
                            context.prompt_id,
                            context.node_id,
                            class_type,
                            float(price),
                        )
                except Exception as exc:
                    LOGGER.warning("Automatic tracker could not capture runtime price: %s", exc)
            return original_display_text(node_cls, text, status=status, price=price)

        api_client._display_text = wrapped_display_text
    except Exception as exc:
        LOGGER.warning("Automatic tracker could not patch API-node runtime price capture: %s", exc)

    server._credit_tracker_auto_registered = True
    _write_status("registered", {"runtime_price_capture": True, "prompt_hook": True})
    LOGGER.info("Automatic credit tracking registered. Add pricing-table aliases to track nodes without adding a logger node.")
