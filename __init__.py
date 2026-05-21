"""
ComfyUI-Credit-Tracker

A small ComfyUI custom node package for estimating and logging Partner/API Node
credit usage to SQLite.
"""

try:
    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
except Exception as exc:
    print(f"[ComfyUI-Credit-Tracker] Failed to load custom nodes: {exc}")
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}

try:
    from .auto_tracker import register_auto_tracker

    register_auto_tracker()
except Exception as exc:
    print(f"[ComfyUI-Credit-Tracker] Failed to register automatic tracker: {exc}")

try:
    from .dashboard import register_dashboard_routes

    register_dashboard_routes()
except Exception as exc:
    print(f"[ComfyUI-Credit-Tracker] Failed to register dashboard routes: {exc}")

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
