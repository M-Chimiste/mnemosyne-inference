"""Self-contained Fleet dashboard, including its nonce-protected assets."""
from importlib.resources import files

DASHBOARD_HTML = files("mnemosyne_fleet").joinpath("dashboard.html").read_text(encoding="utf-8")
