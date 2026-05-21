from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont

try:
    from .tracker_db import CREDITS_PER_USD, DB_PATH, LOGGER, initialize_database
except ImportError:
    from tracker_db import CREDITS_PER_USD, DB_PATH, LOGGER, initialize_database


BACKGROUND = (248, 250, 252)
PANEL = (255, 255, 255)
INK = (18, 24, 38)
MUTED = (90, 102, 122)
GRID = (218, 226, 236)
BLUE = (40, 113, 221)
TEAL = (24, 150, 137)
RED = (204, 69, 82)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        "arialbd.ttf" if bold else "arial.ttf",
        "segoeuib.ttf" if bold else "segoeui.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    initialize_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _where_clause(project_filter: str, partner_node_filter: str) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []

    if project_filter.strip():
        clauses.append("project_name LIKE ?")
        params.append(f"%{project_filter.strip()}%")

    if partner_node_filter.strip():
        clauses.append("partner_node_name LIKE ?")
        params.append(f"%{partner_node_filter.strip()}%")

    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def _fetch_report_data(
    report_view: str,
    top_n: int,
    project_filter: str,
    partner_node_filter: str,
) -> dict[str, Any]:
    top_n = max(1, min(int(top_n), 50))
    where_sql, params = _where_clause(project_filter, partner_node_filter)

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
            params,
        ).fetchone()

        if report_view == "By Project":
            rows = connection.execute(
                f"""
                SELECT
                    project_name AS name,
                    COUNT(*) AS total_runs,
                    ROUND(COALESCE(SUM(quantity), 0), 4) AS total_quantity,
                    ROUND(COALESCE(SUM(duration_seconds), 0), 4) AS total_duration_seconds,
                    ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
                    ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
                FROM credit_usage
                {where_sql}
                GROUP BY project_name
                ORDER BY total_estimated_credits DESC, total_runs DESC, project_name ASC
                LIMIT ?
                """,
                [*params, top_n],
            ).fetchall()
        else:
            rows = connection.execute(
                f"""
                SELECT
                    partner_node_name AS name,
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
                [*params, top_n],
            ).fetchall()

    return {
        "report_view": report_view,
        "top_n": top_n,
        "project_filter": project_filter.strip(),
        "partner_node_filter": partner_node_filter.strip(),
        "totals": totals,
        "rows": rows,
    }


def _format_number(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "0"
    if numeric.is_integer():
        return f"{numeric:,.0f}"
    return f"{numeric:,.2f}"


def build_report_text(
    report_view: str = "By Partner Node",
    top_n: int = 10,
    project_filter: str = "",
    partner_node_filter: str = "",
) -> str:
    data = _fetch_report_data(report_view, top_n, project_filter, partner_node_filter)
    totals = data["totals"]
    rows = data["rows"]

    lines = [
        "ComfyUI Credit Tracker Report",
        f"View: {data['report_view']}",
        f"Total runs: {totals['total_runs']}",
        f"Total quantity: {_format_number(totals['total_quantity'])}",
        f"Total duration seconds: {_format_number(totals['total_duration_seconds'])}",
        f"Total credits: {_format_number(totals['total_estimated_credits'])}",
        f"Total USD: ${float(totals['total_estimated_usd']):,.2f}",
        f"Credits per USD: {CREDITS_PER_USD:g}",
    ]

    if data["project_filter"] or data["partner_node_filter"]:
        lines.append(
            "Filters: "
            f"project={data['project_filter'] or '*'}, "
            f"partner_node={data['partner_node_filter'] or '*'}"
        )

    lines.append("")
    lines.append("Top results:")

    if not rows:
        lines.append("No usage records found.")
        return "\n".join(lines)

    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. {row['name']} | runs {row['total_runs']} | "
            f"qty {_format_number(row['total_quantity'])} | "
            f"credits {_format_number(row['total_estimated_credits'])} | "
            f"${float(row['total_estimated_usd']):,.2f}"
        )

    return "\n".join(lines)


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    max_chars: int,
    line_gap: int = 4,
) -> int:
    x, y = xy
    lines = textwrap.wrap(text, width=max_chars) or [""]
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += font.size + line_gap
    return y


def build_report_image(
    report_view: str = "By Partner Node",
    top_n: int = 10,
    project_filter: str = "",
    partner_node_filter: str = "",
) -> torch.Tensor:
    data = _fetch_report_data(report_view, top_n, project_filter, partner_node_filter)
    totals = data["totals"]
    rows = data["rows"]

    width = 1280
    row_height = 58
    height = max(720, 330 + max(1, len(rows)) * row_height)
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _font(42, bold=True)
    subtitle_font = _font(22)
    label_font = _font(20, bold=True)
    body_font = _font(19)
    small_font = _font(16)

    draw.text((44, 34), "ComfyUI Credit Tracker", fill=INK, font=title_font)
    draw.text((46, 88), data["report_view"], fill=MUTED, font=subtitle_font)

    total_credits = float(totals["total_estimated_credits"] or 0)
    total_usd = float(totals["total_estimated_usd"] or 0)
    metric_cards = [
        ("Credits Spent", _format_number(total_credits), BLUE),
        ("Estimated USD", f"${total_usd:,.2f}", TEAL),
        ("Runs", _format_number(totals["total_runs"]), RED),
    ]

    card_x = 44
    for label, value, accent in metric_cards:
        draw.rounded_rectangle((card_x, 140, card_x + 360, 244), radius=8, fill=PANEL, outline=GRID)
        draw.rectangle((card_x, 140, card_x + 8, 244), fill=accent)
        draw.text((card_x + 28, 160), label, fill=MUTED, font=small_font)
        draw.text((card_x + 28, 187), value, fill=INK, font=_font(30, bold=True))
        card_x += 396

    filter_text = (
        f"Filters: project={data['project_filter'] or '*'} | "
        f"partner_node={data['partner_node_filter'] or '*'} | "
        f"credits/USD={CREDITS_PER_USD:g}"
    )
    draw.text((46, 270), filter_text, fill=MUTED, font=small_font)

    table_top = 312
    draw.rounded_rectangle((44, table_top, width - 44, height - 38), radius=8, fill=PANEL, outline=GRID)
    draw.text((72, table_top + 24), "Name", fill=INK, font=label_font)
    draw.text((620, table_top + 24), "Credits", fill=INK, font=label_font)
    draw.text((780, table_top + 24), "USD", fill=INK, font=label_font)
    draw.text((900, table_top + 24), "Runs", fill=INK, font=label_font)
    draw.text((1010, table_top + 24), "Share", fill=INK, font=label_font)
    draw.line((68, table_top + 58, width - 68, table_top + 58), fill=GRID, width=2)

    if not rows:
        draw.text((72, table_top + 96), "No usage records found yet.", fill=MUTED, font=body_font)
    else:
        max_credits = max(float(row["total_estimated_credits"] or 0) for row in rows) or 1.0
        y = table_top + 78
        for index, row in enumerate(rows, start=1):
            credits = float(row["total_estimated_credits"] or 0)
            usd = float(row["total_estimated_usd"] or 0)
            bar_width = int(190 * (credits / max_credits)) if credits > 0 else 0
            name = f"{index}. {row['name']}"

            if index % 2 == 0:
                draw.rectangle((68, y - 10, width - 68, y + row_height - 12), fill=(248, 250, 252))

            _draw_wrapped_text(draw, name, (72, y), body_font, INK, max_chars=42)
            draw.text((620, y), _format_number(credits), fill=INK, font=body_font)
            draw.text((780, y), f"${usd:,.2f}", fill=INK, font=body_font)
            draw.text((900, y), _format_number(row["total_runs"]), fill=INK, font=body_font)
            draw.rounded_rectangle((1010, y + 4, 1200, y + 24), radius=5, fill=(232, 238, 247))
            if bar_width:
                draw.rounded_rectangle((1010, y + 4, 1010 + bar_width, y + 24), radius=5, fill=BLUE)

            y += row_height

    array = np.asarray(image).astype(np.float32) / 255.0
    return torch.from_numpy(array)[None,]


def build_message_image(
    title: str,
    message: str,
    accent: tuple[int, int, int] = RED,
) -> torch.Tensor:
    width = 1280
    height = 480
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((44, 44, width - 44, height - 44), radius=8, fill=PANEL, outline=GRID)
    draw.rectangle((44, 44, 54, height - 44), fill=accent)
    draw.text((84, 84), title, fill=INK, font=_font(38, bold=True))
    _draw_wrapped_text(draw, message, (84, 150), _font(22), MUTED, max_chars=86, line_gap=8)

    array = np.asarray(image).astype(np.float32) / 255.0
    return torch.from_numpy(array)[None,]
