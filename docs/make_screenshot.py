#!/usr/bin/env python3
"""Regenerate docs/screenshot.png for the README.

Renders the REAL DashboardWindow with FAKE injected sessions and captures a PNG.
Follows CLAUDE.md's testing guidance: a real rendered window under the X11
backend, with fetch / _activity_for / _sample_resources / fetch_usage stubbed,
captured via Gdk.pixbuf_get_from_window. It never reads or shows your real
`claude` sessions — every session below is invented.

Usage:
    python3 docs/make_screenshot.py [OUTPUT.png]      # default: docs/screenshot.png

Needs a running display (X11 or Xwayland). On Wayland it forces the X11 backend,
so an X server / Xwayland must be reachable. The window briefly maps on screen
while it renders — that's expected.

NOTE: the app's stylesheet is installed in main() (not in DashboardWindow's
__init__), so we load `CSS` here ourselves. Skip that and the window renders with
the default GTK theme (orange bars, no warn/crit CPU chips) instead of the app's.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

# The X11 backend + a display MUST be set before the module imports gi.
os.environ["GDK_BACKEND"] = "x11"
os.environ.setdefault("DISPLAY", ":0")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "docs", "screenshot.png")
sys.path.insert(0, REPO)

import agents_dashboard as ad                       # noqa: E402
from agents_dashboard import Gtk, Gdk, GLib          # noqa: E402

HOME = os.path.expanduser("~")
MB = 1048576
GB = 1073741824


def _proj(*parts):
    # Under $HOME so shorten_path() renders it as "~/git/..." for any user.
    return os.path.join(HOME, *parts)


# ── fake sessions (cover every category + the CPU highlight tiers) ───────────
SESSIONS = [
    dict(sessionId="s-payments", pid=40101, name="payments-api",
         cwd=_proj("git", "payments-api"), state="working", status="waiting"),
    dict(sessionId="s-mobile", pid=40202, name="mobile-app",
         cwd=_proj("git", "mobile-app"), state="blocked", status="idle"),
    dict(sessionId="s-search", pid=40303, name="search-indexer",
         cwd=_proj("git", "search-indexer"), state="working", status="busy"),
    dict(sessionId="s-web", pid=40404, name="web-dashboard",
         cwd=_proj("git", "web-dashboard"), state="working", status="busy"),
    dict(sessionId="s-infra", pid=40505, name="infra",
         cwd=_proj("git", "infra"), state="working", status="busy"),
    dict(sessionId="s-docs", pid=40606, name="docs",
         cwd=_proj("git", "docs"), state="done", status="idle"),
    dict(sessionId="s-scratch", pid=40707, name="scratch",
         cwd=HOME, state="idle", status="idle"),
]

ACTIVITY = {
    "s-payments": '$ psql "$DATABASE_URL" -f migrations/007_add_refunds_table.sql',
    "s-mobile":   "The auth flow can use either Firebase or our own JWT service "
                  "— which should I wire up? I'll hold here until you decide.",
    "s-search":   "$ cargo test --release --workspace",
    "s-web":      "✎ src/components/UsageChart.tsx",
    "s-infra":    "Tracing the module dependency graph before I refactor the VPC "
                  "into its own Terraform stack.",
    "s-docs":     "Done — regenerated the API reference, fixed 3 broken links, "
                  "and pushed to the docs branch. Anything else?",
    "s-scratch":  "",
}

# (cpu %, mem bytes) per session — search-indexer is a hot test run (red chip),
# web-dashboard is moderately busy (yellow chip), the rest are calm.
RES = {
    "s-payments": (0.4, int(210 * MB)),
    "s-mobile":   (0.2, int(175 * MB)),
    "s-search":   (63.0, int(1.3 * GB)),
    "s-web":      (14.5, int(560 * MB)),
    "s-infra":    (3.1, int(300 * MB)),
    "s-docs":     (0.1, int(140 * MB)),
    "s-scratch":  (0.0, int(85 * MB)),
}

_now = datetime.now(timezone.utc)


def _iso(delta):
    return (_now + delta).isoformat().replace("+00:00", "Z")


USAGE = {
    "five_hour":      {"utilization": 71, "resets_at": _iso(timedelta(hours=1, minutes=47))},
    "seven_day":      {"utilization": 43, "resets_at": _iso(timedelta(days=3, hours=5))},
    "seven_day_opus": {"utilization": 58, "resets_at": _iso(timedelta(days=3, hours=5))},
}

SYSRES = {"cpu": 34.0, "mem_pct": 62.0,
          "mem_used": int(0.62 * 32 * GB), "mem_total": 32 * GB}


# ── stubs (replace the live CLI / /proc / network with the fixtures above) ────
ad.fetch = lambda: [dict(s) for s in SESSIONS]       # fresh dicts each poll
ad.fetch_usage = lambda: dict(USAGE)


def _fake_activity(self, s):
    return ACTIVITY.get(s.get("sessionId"), "")


def _fake_resources(self, sessions):
    for s in sessions:
        cpu, mem = RES.get(s.get("sessionId"), (None, None))
        s["_cpu"], s["_mem"] = cpu, mem
    return dict(SYSRES)


ad.DashboardWindow._activity_for = _fake_activity
ad.DashboardWindow._sample_resources = _fake_resources

# ── load the app stylesheet (main() does this; we bypass main(), so do it here)
_provider = Gtk.CssProvider()
_provider.load_from_data(ad.CSS)
Gtk.StyleContext.add_provider_for_screen(
    Gdk.Screen.get_default(), _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

# ── build + capture ──────────────────────────────────────────────────────────
win = ad.DashboardWindow(interval=5.0, keep_top=False, sound=False)
win._usage = dict(USAGE)          # so the meter shows immediately
win._usage_error = None
win.set_decorated(False)          # no OS title bar in the shot (header has its own)
win.show_all()

WIDTH = 560


def _find_scroll(w):
    while w is not None and not isinstance(w, Gtk.ScrolledWindow):
        w = w.get_parent()
    return w


def _resize_to_fit():
    # Make the scrolled list request its full natural height so every row shows
    # (no scrollbar), then size the window to the content.
    scroll = _find_scroll(win.listbox)
    if scroll is not None:
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
    outer = win.get_child()
    _min_h, nat_h = outer.get_preferred_height_for_width(WIDTH)
    win.resize(WIDTH, nat_h)
    win.present()
    return False


OUT_MINI = OUT[:-4] + "-mini.png" if OUT.endswith(".png") else OUT + "-mini.png"


def _snap(path):
    pb = Gdk.pixbuf_get_from_window(win.get_window(), 0, 0,
                                    win.get_allocated_width(), win.get_allocated_height())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pb.savev(path, "png", [], [])
    print("wrote %s  (%dx%d)" % (path, win.get_allocated_width(), win.get_allocated_height()))


def _capture_full():
    _snap(OUT)
    win.mini_btn.set_active(True)     # collapse to the ultra-minimized strip
    return False


def _capture_mini():
    _snap(OUT_MINI)
    Gtk.main_quit()
    return False


GLib.timeout_add(900, _resize_to_fit)      # let the first poll render
GLib.timeout_add(1500, _resize_to_fit)     # settle wrapped-label heights
GLib.timeout_add(2100, _capture_full)      # full view -> screenshot.png
GLib.timeout_add(2800, _capture_mini)      # mini strip -> screenshot-mini.png
Gtk.main()
