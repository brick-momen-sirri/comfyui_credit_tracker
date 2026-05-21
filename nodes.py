from __future__ import annotations

import time

try:
    from .tracker_db import LOGGER, format_summary, log_credit_usage
    from .report_visual import build_message_image, build_report_image, build_report_text
except ImportError:
    from tracker_db import LOGGER, format_summary, log_credit_usage
    from report_visual import build_message_image, build_report_image, build_report_text


class CreditTrackerLogger:
    """
    ComfyUI node that logs estimated Partner/API Node credit usage to SQLite.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_name": (
                    "STRING",
                    {"default": "General", "multiline": False},
                ),
                "user_name": (
                    "STRING",
                    {"default": "Unknown", "multiline": False},
                ),
                "workflow_name": (
                    "STRING",
                    {"default": "Untitled Workflow", "multiline": False},
                ),
                "partner_node_name": (
                    "STRING",
                    {"default": "Unknown Partner Node", "multiline": False},
                ),
                "quantity": (
                    "INT",
                    {"default": 1, "min": 0, "max": 1_000_000, "step": 1},
                ),
                "duration_seconds": (
                    "FLOAT",
                    {
                        "default": 0.0,
                        "min": 0.0,
                        "max": 1_000_000.0,
                        "step": 0.1,
                        "round": 0.01,
                    },
                ),
                "resolution": (
                    "STRING",
                    {"default": "", "multiline": False},
                ),
                "notes": (
                    "STRING",
                    {"default": "", "multiline": True},
                ),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("summary",)
    FUNCTION = "log_usage"
    CATEGORY = "Credit Tracker"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Force execution when the workflow runs so repeated identical runs are logged.
        return time.time()

    def log_usage(
        self,
        project_name: str = "General",
        user_name: str = "Unknown",
        workflow_name: str = "Untitled Workflow",
        partner_node_name: str = "Unknown Partner Node",
        quantity: int = 1,
        duration_seconds: float = 0.0,
        resolution: str = "",
        notes: str = "",
    ):
        try:
            if not str(partner_node_name).strip() or str(partner_node_name).strip() == "Unknown Partner Node":
                message = (
                    "Credit Tracker Logger skipped logging because partner_node_name "
                    "is still set to the default 'Unknown Partner Node'. "
                    "Fill it in, or remove this logger node and use automatic tracking."
                )
                LOGGER.warning(message)
                return (message,)

            record = log_credit_usage(
                project_name=project_name,
                user_name=user_name,
                workflow_name=workflow_name,
                partner_node_name=partner_node_name,
                quantity=quantity,
                duration_seconds=duration_seconds,
                resolution=resolution,
                notes=notes,
            )
            summary = format_summary(record)
            LOGGER.info(summary)
            return (summary,)
        except Exception as exc:
            message = (
                "Credit Tracker Logger could not write to usage_log.db. "
                f"ComfyUI will continue running. Error: {exc}"
            )
            LOGGER.warning(message)
            return (message,)


class CreditTrackerReportViewer:
    """
    ComfyUI node that renders the SQLite credit usage report as an image and text.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "report_view": (
                    ["By Partner Node", "By Project"],
                    {"default": "By Partner Node"},
                ),
                "top_n": (
                    "INT",
                    {"default": 10, "min": 1, "max": 50, "step": 1},
                ),
                "project_filter": (
                    "STRING",
                    {"default": "", "multiline": False},
                ),
                "partner_node_filter": (
                    "STRING",
                    {"default": "", "multiline": False},
                ),
                "refresh": (
                    "INT",
                    {"default": 0, "min": 0, "max": 1_000_000, "step": 1},
                ),
            },
            "optional": {
                "trigger_image": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("report_image", "report_text")
    FUNCTION = "view_report"
    CATEGORY = "Credit Tracker"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Report data lives outside the workflow graph, so force refresh on each run.
        return time.time()

    def view_report(
        self,
        report_view: str = "By Partner Node",
        top_n: int = 10,
        project_filter: str = "",
        partner_node_filter: str = "",
        refresh: int = 0,
        trigger_image=None,
    ):
        try:
            image = build_report_image(
                report_view=report_view,
                top_n=top_n,
                project_filter=project_filter,
                partner_node_filter=partner_node_filter,
            )
            text = build_report_text(
                report_view=report_view,
                top_n=top_n,
                project_filter=project_filter,
                partner_node_filter=partner_node_filter,
            )
            return (image, text)
        except Exception as exc:
            LOGGER.warning("Credit Tracker Report Viewer failed: %s", exc)
            image = build_message_image(
                "Credit Tracker Report Viewer",
                f"The report could not be rendered. ComfyUI will continue running. Error: {exc}",
            )
            return (image, f"Credit Tracker Report Viewer failed: {exc}")


NODE_CLASS_MAPPINGS = {
    "CreditTrackerLogger": CreditTrackerLogger,
    "CreditTrackerReportViewer": CreditTrackerReportViewer,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CreditTrackerLogger": "Credit Tracker Logger",
    "CreditTrackerReportViewer": "Credit Tracker Report Viewer",
}
