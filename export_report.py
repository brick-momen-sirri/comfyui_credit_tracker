from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Iterable

try:
    from .tracker_db import CREDITS_PER_USD, DB_PATH, initialize_database
except ImportError:
    from tracker_db import CREDITS_PER_USD, DB_PATH, initialize_database


PACKAGE_DIR = Path(__file__).resolve().parent
FULL_CSV_PATH = PACKAGE_DIR / "credit_usage_full.csv"
SUMMARY_BY_NODE_CSV_PATH = PACKAGE_DIR / "credit_usage_summary_by_node.csv"
SUMMARY_BY_PROJECT_CSV_PATH = PACKAGE_DIR / "credit_usage_summary_by_project.csv"


def connect() -> sqlite3.Connection:
    initialize_database(DB_PATH)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def write_rows_to_csv(path: Path, fieldnames: list[str], rows: Iterable[sqlite3.Row]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fieldnames})


def export_full_usage(connection: sqlite3.Connection) -> None:
    fieldnames = [
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
    rows = connection.execute(
        """
        SELECT
            id,
            timestamp,
            project_name,
            user_name,
            workflow_name,
            partner_node_name,
            node_class_type,
            node_title,
            model_name,
            input_summary,
            pricing_mode,
            source,
            quantity,
            duration_seconds,
            resolution,
            estimated_credits,
            estimated_usd,
            prompt_id,
            node_id,
            dedupe_key,
            notes
        FROM credit_usage
        ORDER BY timestamp ASC, id ASC
        """
    ).fetchall()
    write_rows_to_csv(FULL_CSV_PATH, fieldnames, rows)


def export_summary_by_node(connection: sqlite3.Connection) -> list[sqlite3.Row]:
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
        """
        SELECT
            partner_node_name,
            node_class_type,
            COUNT(*) AS total_runs,
            COALESCE(SUM(quantity), 0) AS total_quantity,
            ROUND(COALESCE(SUM(duration_seconds), 0), 4) AS total_duration_seconds,
            ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
            ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
        FROM credit_usage
        GROUP BY partner_node_name, node_class_type
        ORDER BY total_estimated_credits DESC, total_runs DESC, partner_node_name ASC, node_class_type ASC
        """
    ).fetchall()
    write_rows_to_csv(SUMMARY_BY_NODE_CSV_PATH, fieldnames, rows)
    return rows


def export_summary_by_project(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    fieldnames = [
        "project_name",
        "total_runs",
        "total_estimated_credits",
        "total_estimated_usd",
    ]
    rows = connection.execute(
        """
        SELECT
            project_name,
            COUNT(*) AS total_runs,
            ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_estimated_credits,
            ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_estimated_usd
        FROM credit_usage
        GROUP BY project_name
        ORDER BY total_estimated_credits DESC, total_runs DESC, project_name ASC
        """
    ).fetchall()
    write_rows_to_csv(SUMMARY_BY_PROJECT_CSV_PATH, fieldnames, rows)
    return rows


def print_table(title: str, rows: list[sqlite3.Row], name_field: str) -> None:
    print(f"\n{title}")
    if not rows:
        print("  No usage records found.")
        return

    for index, row in enumerate(rows[:10], start=1):
        print(
            f"  {index:>2}. {row[name_field]} - "
            f"{row['total_estimated_credits']:g} credits "
            f"(${row['total_estimated_usd']:.2f})"
        )


def print_terminal_report(
    connection: sqlite3.Connection,
    node_rows: list[sqlite3.Row],
    project_rows: list[sqlite3.Row],
) -> None:
    totals = connection.execute(
        """
        SELECT
            ROUND(COALESCE(SUM(estimated_credits), 0), 4) AS total_credits,
            ROUND(COALESCE(SUM(estimated_usd), 0), 4) AS total_usd,
            COUNT(*) AS total_runs
        FROM credit_usage
        """
    ).fetchone()

    print("ComfyUI Credit Tracker Report")
    print("=============================")
    print(f"Database: {DB_PATH}")
    print(f"Credits per USD: {CREDITS_PER_USD:g}")
    print(f"Total runs: {totals['total_runs']}")
    print(f"Total credits: {totals['total_credits']:g}")
    print(f"Total USD: ${totals['total_usd']:.2f}")

    print_table("Top 10 most expensive partner nodes", node_rows, "partner_node_name")
    print_table("Top 10 most expensive projects", project_rows, "project_name")

    print("\nCSV files written:")
    print(f"  {FULL_CSV_PATH}")
    print(f"  {SUMMARY_BY_NODE_CSV_PATH}")
    print(f"  {SUMMARY_BY_PROJECT_CSV_PATH}")


def main() -> None:
    with connect() as connection:
        export_full_usage(connection)
        node_rows = export_summary_by_node(connection)
        project_rows = export_summary_by_project(connection)
        print_terminal_report(connection, node_rows, project_rows)


if __name__ == "__main__":
    main()
