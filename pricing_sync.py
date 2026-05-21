from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from .tracker_db import CREDITS_PER_USD, LOGGER, PACKAGE_DIR
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tracker_db import CREDITS_PER_USD, LOGGER, PACKAGE_DIR


PRICING_DOCS_URL = "https://docs.comfy.org/tutorials/partner-nodes/pricing"
PRICING_CACHE_PATH = PACKAGE_DIR / "official_pricing_cache.json"

CATEGORIES = {"Image", "Video", "Audio", "Text", "3D"}
KNOWN_PROVIDERS = {
    "BFL",
    "Anthropic",
    "Bria",
    "ByteDance",
    "ElevenLabs",
    "Freepik",
    "Google",
    "HappyHorse",
    "Hitpaw",
    "Hunyuan 3D",
    "Ideogram",
    "Kling",
    "Lightricks",
    "Luma",
    "Meshy",
    "Minimax",
    "Moonvalley",
    "OpenAI",
    "Pika",
    "PixVerse",
    "Pixverse",
    "Quiver",
    "Recraft",
    "Reve",
    "Rodin",
    "Runway",
    "Sonilo",
    "Stability",
    "Stability AI",
    "Tencent",
    "Topaz",
    "Tripo",
    "Vidu",
    "Wan",
    "WAN",
    "Wavespeed",
    "xAI",
    "Other (Cloud)",
}


class PricingTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.provider = ""
        self.rows: list[dict[str, Any]] = []
        self._skip_tag = ""
        self._heading_tag = ""
        self._heading_parts: list[str] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_tag = tag
            return

        if tag in {"h2", "h3"}:
            self._heading_tag = tag
            self._heading_parts = []
            return

        if tag == "tr":
            self._row = []
            return

        if tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_tag:
            return
        text = " ".join(data.split())
        if not text:
            return

        if self._heading_tag:
            self._heading_parts.append(text)

        if self._cell_parts is not None:
            self._cell_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == self._skip_tag:
            self._skip_tag = ""
            return

        if tag == self._heading_tag:
            heading = _clean_heading(" ".join(self._heading_parts))
            if heading in KNOWN_PROVIDERS:
                self.provider = heading
            self._heading_tag = ""
            self._heading_parts = []
            return

        if tag in {"td", "th"} and self._cell_parts is not None and self._row is not None:
            self._row.append(" ".join(self._cell_parts).strip())
            self._cell_parts = None
            return

        if tag == "tr" and self._row is not None:
            row = self._row
            self._row = None
            parsed = _parse_table_row(row, self.provider)
            if parsed:
                self.rows.append(parsed)


def _fetch_html(url: str = PRICING_DOCS_URL, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ComfyUI-Credit-Tracker/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _clean_heading(value: str) -> str:
    return " ".join(value.replace("\u200b", " ").split())


def _split_product_and_configuration(prefix: str) -> tuple[str, str]:
    markers = [
        " endpoint:",
        " model:",
        " resolution:",
        " mode:",
        " generateAudio:",
        " generate_audio:",
        " size:",
        " specs:",
        " operation:",
        " duration:",
        " quality:",
        " NA ",
    ]
    positions = [prefix.find(marker) for marker in markers if prefix.find(marker) >= 0]
    if not positions:
        return prefix.strip(), ""

    index = min(positions)
    return prefix[:index].strip(), prefix[index:].strip()


def _parse_credit_unit(value: str) -> tuple[float, str] | None:
    # Some rows contain extra terms, for example "14.77 / run + 6.33 / extra MP".
    # The primary unit is still useful for lookup and dashboard display.
    match = re.search(r"(?P<credits>\d+(?:\.\d+)?)\s*/\s*(?P<unit>[^+]+)", value)
    if not match:
        return None
    return float(match.group("credits")), " ".join(match.group("unit").split())


def _parse_table_row(cells: list[str], provider: str) -> dict[str, Any] | None:
    if len(cells) < 4:
        return None

    product_name, configuration, credits_text, category = cells[:4]
    if product_name == "Product Name" or category not in CATEGORIES:
        return None

    parsed_credit = _parse_credit_unit(credits_text)
    if parsed_credit is None:
        return None

    credits, unit = parsed_credit
    raw_line = f"{product_name} {configuration} {credits_text} {category}".strip()
    return {
        "provider": provider or "Unknown",
        "product_name": product_name.strip(),
        "configuration": configuration.strip(),
        "credits": credits,
        "unit": unit,
        "category": category,
        "raw_credits": credits_text.strip(),
        "raw_line": raw_line,
    }


def parse_pricing_html(html: str) -> list[dict[str, Any]]:
    parser = PricingTableParser()
    parser.feed(html)
    return parser.rows


def parse_pricing_text(text: str) -> list[dict[str, Any]]:
    """Parse the text view returned by docs tools; kept as a fallback for tests."""
    rows: list[dict[str, Any]] = []
    provider = ""
    pricing_pattern = re.compile(
        r"^(?P<prefix>.+?)\s+(?P<credits>\d+(?:\.\d+)?)\s*/\s*(?P<unit>.+?)\s+(?P<category>Image|Video|Audio|Text|3D)$"
    )

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue

        if line in KNOWN_PROVIDERS:
            provider = line
            continue

        match = pricing_pattern.match(line)
        if not match:
            continue

        prefix = match.group("prefix").strip()
        product_name, configuration = _split_product_and_configuration(prefix)
        rows.append(
            {
                "provider": provider or "Unknown",
                "product_name": product_name,
                "configuration": configuration,
                "credits": float(match.group("credits")),
                "unit": match.group("unit").strip(),
                "category": match.group("category"),
                "raw_credits": f"{match.group('credits')} / {match.group('unit').strip()}",
                "raw_line": line,
            }
        )

    return rows


def sync_pricing_cache(url: str = PRICING_DOCS_URL) -> dict[str, Any]:
    html = _fetch_html(url)
    rows = parse_pricing_html(html)
    if not rows:
        LOGGER.warning("HTML pricing table parse returned no rows; trying plain-text fallback.")
        rows = parse_pricing_text(html)
    payload = {
        "source_url": url,
        "credits_per_usd": CREDITS_PER_USD,
        "synced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "row_count": len(rows),
        "rows": rows,
    }
    PRICING_CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOGGER.info("Synced %s official pricing rows from %s", len(rows), url)
    return payload


def load_pricing_cache() -> dict[str, Any]:
    if not PRICING_CACHE_PATH.exists():
        return {
            "source_url": PRICING_DOCS_URL,
            "credits_per_usd": CREDITS_PER_USD,
            "synced_at": "",
            "row_count": 0,
            "rows": [],
        }
    try:
        return json.loads(PRICING_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Could not read official pricing cache: %s", exc)
        return {
            "source_url": PRICING_DOCS_URL,
            "credits_per_usd": CREDITS_PER_USD,
            "synced_at": "",
            "row_count": 0,
            "rows": [],
        }


def search_pricing_cache(query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    cache = load_pricing_cache()
    rows = cache.get("rows", [])
    if not isinstance(rows, list):
        return []

    normalized = query.casefold().strip()
    if normalized:
        rows = [
            row for row in rows
            if normalized in json.dumps(row, ensure_ascii=True).casefold()
        ]

    return rows[: max(1, min(int(limit), 500))]


if __name__ == "__main__":
    payload = sync_pricing_cache()
    print(f"Synced {payload['row_count']} rows from {payload['source_url']}")
    print(f"Saved: {PRICING_CACHE_PATH}")
