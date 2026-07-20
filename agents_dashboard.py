#!/usr/bin/env python3
"""Claude Agents Dashboard — a compact, always-visible GTK window that polls
`claude agents --json` and shows, per session:

    • session name
    • state + status  (shown as a human badge derived from both fields)
    • working directory

Sessions that need YOU (waiting on a permission prompt, or blocked asking a
question) are pushed to the top, painted in an alarm colour, gently pulse, and
raise the dashboard. A sound plays only if a session keeps waiting on you for
more than a few seconds (so prompts you answer right away stay silent).

Usage:
    ./agents_dashboard.py [--interval SECONDS] [--top] [--no-sound] [--no-desktop]

    --interval N   poll every N seconds        (default 1)
    --top          keep above all windows and on every workspace
    --no-sound     don't play a sound on attention events
    --no-desktop   don't install/update the ~/.local/share .desktop entry
                   (that entry is what gives the app its dock/taskbar icon)
"""
import argparse
import base64
import glob
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

# `--top` needs always-on-top + on-all-workspaces. Wayland refuses those to a
# native client (GTK silently ignores set_keep_above()/stick()), but Xwayland
# restores them — Mutter honours _NET_WM_STATE_ABOVE / _STICKY for X11 windows.
# GDK reads GDK_BACKEND when `import gi` initialises it, so this MUST run before
# that import; setting it later (e.g. in main()) is too late and silently leaves
# us on Wayland. Only when --top is requested and Xwayland is available; an
# explicit GDK_BACKEND still wins.
if ("--top" in sys.argv and os.environ.get("WAYLAND_DISPLAY")
        and os.environ.get("DISPLAY")):
    os.environ.setdefault("GDK_BACKEND", "x11")

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GLib, Pango, GdkPixbuf  # noqa: E402


# ── classification ──────────────────────────────────────────────────────────
# Mirrors ~/.local/bin/claude-status. Priority: lower sorts higher (more urgent)
CATEGORIES = {
    "permission": {"glyph": "⛔", "label": "NEEDS PERMISSION", "prio": 0, "attention": True},   # ⛔
    "question":   {"glyph": "✋", "label": "WAITING ON YOU",   "prio": 1, "attention": True},   # ✋
    "working":    {"glyph": "⚙", "label": "WORKING",          "prio": 2, "attention": False},  # ⚙
    "done":       {"glyph": "✔", "label": "DONE",             "prio": 3, "attention": False},  # ✔
    "idle":       {"glyph": "●", "label": "IDLE",             "prio": 4, "attention": False},  # ●
}

# Attention sound: only beep once a session has needed you for this long, so a
# prompt you answer instantly stays silent. ATTENTION_SOUND is a freedesktop /
# Yaru sound-theme id, played via canberra-gtk-play.
SOUND_DELAY_MS = 5000
ATTENTION_SOUND = "message-new-instant"


def classify(s):
    state, status = s.get("state"), s.get("status")
    if status == "waiting":                       return "permission"
    if state == "blocked":                        return "question"
    if state == "done":                           return "done"
    if status == "busy" or state == "working":    return "working"
    return "idle"


def shorten_path(p):
    if not p:
        return "?"
    home = os.path.expanduser("~")
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def fetch():
    """Return a list of sessions, or {'_error': msg} on failure."""
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return {"_error": (out.stderr or "non-zero exit").strip()[:200]}
        return json.loads(out.stdout or "[]")
    except Exception as e:  # noqa: BLE001 — surface anything to the UI
        return {"_error": str(e)[:200]}


# ── "currently working on" ────────────────────────────────────────────────
# `claude agents --json` has no live-activity field, so we peek at the tail of
# the session transcript (~/.claude/projects/<enc-cwd>/<sessionId>.jsonl) and
# summarise the newest assistant action. Only the last TAIL_BYTES are read, and
# callers cache by file mtime+size — see DashboardWindow._activity_for.
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
TAIL_BYTES = 131072
MAX_ACTIVITY = 2000        # hard cap so one activity can't blow up the row


def _cap(s, n=MAX_ACTIVITY):
    # Trim and length-cap while PRESERVING newlines — this is the full form
    # shown when a row is expanded (e.g. a whole multi-line command).
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _oneline(s, n=400):
    # Collapse all whitespace to single spaces for the collapsed one-line
    # preview (ellipsized by width); newlines would otherwise break the layout.
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _tool_phrase(block):
    """Human phrase for a tool_use content block (length capped by caller)."""
    name = block.get("name") or "tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        return "$ " + cmd if cmd else "$ bash"      # full command, all lines
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "✎ " + os.path.basename(inp.get("file_path") or inp.get("notebook_path") or "")
    if name == "Read":
        return "read " + os.path.basename(inp.get("file_path") or "")
    if name in ("Grep", "Glob"):
        return "search " + (inp.get("pattern") or inp.get("query") or "")
    if name in ("Task", "Agent"):
        return "→ " + (inp.get("description") or inp.get("subagent_type") or "subagent")
    if name in ("WebFetch", "WebSearch"):
        return "web " + (inp.get("url") or inp.get("query") or "")
    if name == "TodoWrite":
        return "updating plan"
    return name


def read_activity(path):
    """Summarise the newest assistant action from a transcript's tail. Reads at
    most TAIL_BYTES; returns '' on any problem (never raises)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            chunk = f.read()
    except OSError:
        return ""
    lines = chunk.decode("utf-8", "replace").splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]                       # drop the partial first line
    raw = ""
    for line in reversed(lines):                # newest → oldest
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str):
            if content.strip():
                raw = content
                break
            continue
        if not isinstance(content, list):
            continue
        found = ""
        for b in reversed(content):             # last block of newest turn
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                found = _tool_phrase(b)
                break
            if b.get("type") == "text" and b.get("text", "").strip():
                found = b["text"]
                break
        if found:
            raw = found
            break
    return _cap(raw)


# ── resource usage (CPU / memory) ─────────────────────────────────────────
# `claude agents --json` reports only a session's own pid, but an agent that
# runs CI / a test suite spawns a whole subtree of processes, so we sum CPU +
# resident memory over the pid *and every descendant*, read straight from
# /proc (no psutil dependency). CPU is derived from the change in utime+stime
# jiffies between two samples, so it needs the previous snapshot kept on the
# window — see DashboardWindow._sample_resources.
try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")          # scheduler jiffies per second
except (ValueError, OSError, AttributeError):
    _CLK_TCK = 100
try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")      # bytes per RSS page
except (ValueError, OSError, AttributeError):
    _PAGE_SIZE = 4096
_NCPU = os.cpu_count() or 1                       # logical cores (CPU% divisor)
CPU_WARN_PCT = 10.0                               # per-session CPU highlight (% of
CPU_CRIT_PCT = 25.0                               # total capacity): >warn = yellow,
                                                  # >crit = red


def read_proc_stats():
    """Snapshot every process from /proc.

    Returns {pid: (ppid, jiffies, rss_bytes)} where jiffies is cumulative
    user+system CPU time and rss_bytes is resident memory. Returns {} when
    /proc is unavailable (non-Linux); never raises."""
    stats = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return stats
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open("/proc/" + name + "/stat", "rb") as f:
                data = f.read()
        except OSError:
            continue                    # process exited between listdir and open
        try:
            # comm (field 2) is parenthesised and may itself contain spaces or
            # ')'; everything after the LAST ')' is fixed and space-separated.
            tail = data[data.rindex(b")") + 1:].split()
            ppid = int(tail[1])                       # field 4
            jiffies = int(tail[11]) + int(tail[12])   # utime (14) + stime (15)
            rss = int(tail[21]) * _PAGE_SIZE          # rss pages (24) -> bytes
        except (ValueError, IndexError):
            continue
        stats[int(name)] = (ppid, jiffies, rss)
    return stats


def read_system_cpu():
    """Aggregate CPU jiffies from /proc/stat as (busy, total), or None. System
    CPU% is the change in busy/total between two samples."""
    try:
        with open("/proc/stat", "rb") as f:
            line = f.readline()
    except OSError:
        return None
    parts = line.split()
    if len(parts) < 5 or parts[0] != b"cpu":
        return None
    try:
        vals = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    total = sum(vals)
    return (total - idle, total)


def read_system_mem():
    """Physical memory as (used_bytes, total_bytes) from /proc/meminfo, or None.
    'used' = MemTotal - MemAvailable (what `free` reports as used)."""
    info = {}
    try:
        with open("/proc/meminfo", "rb") as f:
            for line in f:
                key, _, rest = line.partition(b":")
                info[key] = rest
    except OSError:
        return None

    def kb(key):
        try:
            return int(info[key].split()[0]) * 1024
        except (KeyError, ValueError, IndexError):
            return None

    total, avail = kb(b"MemTotal"), kb(b"MemAvailable")
    if not total or avail is None:
        return None
    return (max(0, total - avail), total)


def fmt_mem(nbytes):
    """Resident bytes -> short human string (KB / MB / GB)."""
    if not nbytes:
        return "0 MB"
    mb = nbytes / 1048576.0
    if mb < 1:
        return "%d KB" % max(1, nbytes // 1024)
    if mb < 1024:
        return ("%.1f MB" % mb) if mb < 10 else ("%d MB" % round(mb))
    return "%.1f GB" % (mb / 1024.0)


def fmt_cpu(pct):
    """CPU percent of total system capacity (0-100 across all logical cores)."""
    if pct is None:
        return ""
    return ("%.1f%%" % pct) if pct < 10 else ("%d%%" % round(pct))


def fmt_stats(cpu, mem):
    """Compact 'CPU  MEM' string for a row (skips missing parts)."""
    parts = []
    if cpu is not None:
        parts.append(fmt_cpu(cpu))
    if mem is not None:
        parts.append(fmt_mem(mem))
    return "  ".join(parts)


# ── Claude usage limit (subscription rate-limit windows) ───────────────────
# `claude agents --json` says nothing about account usage, so we call the same
# endpoint the interactive `/usage` panel uses: GET /api/oauth/usage, authorised
# with the OAuth token Claude Code keeps in ~/.claude/.credentials.json. It
# returns the session (5h) and weekly (7d) rate-limit windows, each a percent
# utilisation + reset time. Undocumented, so everything degrades to "hidden" if
# it moves or the token can't be read. Polled on a slow timer, off the main loop.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CRED_PATH = os.path.expanduser("~/.claude/.credentials.json")
USAGE_POLL_MS = 180000            # 3 min between usage polls (endpoint is rate-limited)
USAGE_MAX_BACKOFF_MS = 1800000    # back off to <=30 min after repeated failures (429s)


def _oauth_token():
    """The stored Claude OAuth access token, or None if absent/expired. Claude
    Code refreshes it and rewrites the file, so we just re-read it each poll."""
    try:
        with open(CRED_PATH) as f:
            oauth = (json.load(f) or {}).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return None
    tok, exp = oauth.get("accessToken"), oauth.get("expiresAt")
    if not tok:
        return None
    if isinstance(exp, (int, float)) and exp / 1000.0 < time.time():
        return None                     # expired; wait for Claude Code to refresh
    return tok


def fetch_usage():
    """Return the /api/oauth/usage payload (dict) on success, or a
    {"_error": short_reason} dict on failure. Never raises — runs off-thread."""
    tok = _oauth_token()
    if not tok:
        return {"_error": "not signed in"}
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.load(resp)
        return data if isinstance(data, dict) else {"_error": "bad response"}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return {"_error": "rate limited"}
        if e.code in (401, 403):
            return {"_error": "auth expired"}
        return {"_error": "HTTP %d" % e.code}
    except urllib.error.URLError:
        return {"_error": "offline"}
    except Exception:                    # noqa: BLE001 — JSON / other, all soft
        return {"_error": "unavailable"}


def _usage_cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "claude-agents-dashboard", "usage.json")


def load_usage_cache():
    """Last good usage payload persisted from a previous run, or None."""
    try:
        with open(_usage_cache_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and "_error" not in data else None
    except (OSError, ValueError):
        return None


def save_usage_cache(payload):
    try:
        path = _usage_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f)
    except OSError:
        pass


def fmt_pct(x):
    return "—" if x is None else "%d%%" % round(x)


def _parse_iso(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _reset_rel(iso):
    """'1h 47m' style time-until for an ISO-8601 reset timestamp ('?' on error)."""
    dt = _parse_iso(iso)
    if dt is None:
        return "?"
    secs = int((dt - datetime.now(dt.tzinfo)).total_seconds())
    if secs <= 0:
        return "now"
    days, rem = divmod(secs, 86400)
    hrs, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return "%dd %dh" % (days, hrs)
    if hrs:
        return "%dh %dm" % (hrs, mins)
    return "%dm" % mins


def _reset_abs(iso):
    """Local-time 'Jul 15 15:50' for an ISO-8601 reset timestamp ('?' on error)."""
    dt = _parse_iso(iso)
    return "?" if dt is None else dt.astimezone().strftime("%b %d %H:%M")


# ── styling ─────────────────────────────────────────────────────────────────
CSS = b"""
window                { background-color: #16181d; }

.header               { padding: 10px 14px; }
.title                { font-weight: 800; font-size: 13px; color: #e6e8ee;
                        letter-spacing: 0.4px; }
.subtitle             { font-size: 11px; color: #7f8796; }
/* Shared system-monitor-style meters: Claude usage (left) + CPU/MEM (right).
   Slim rounded-rect bars, small letter-spaced caps, monospace percentages. */
.usage                { margin-top: 5px; margin-bottom: 3px; }
.meter-cap            { font-size: 9px; font-weight: 800; letter-spacing: 0.8px;
                        color: #6b7280; }
.meter-pct            { font-family: monospace; font-size: 10px; font-weight: 700;
                        color: #c7ccd6; }
.meter-pct.warn       { color: #f7c948; }
.meter-pct.crit       { color: #ff5c57; }
.meter-reset          { font-size: 10px; color: #7f8796; margin-left: 2px; }
.usage-err            { font-size: 11px; font-weight: 700; color: #e6a23c;
                        margin-left: 6px; }
progressbar.meter-bar          { min-height: 7px; }
progressbar.meter-bar trough   { min-height: 7px; background-color: #2b2f38;
                                 background-image: none; border: none;
                                 border-radius: 4px; padding: 0; margin: 0; }
progressbar.meter-bar progress { min-height: 7px; background-color: #34d399;
                                 background-image: none; border: none;
                                 border-radius: 4px; margin: 0; }
progressbar.meter-bar.warn progress { background-color: #f7c948; }
progressbar.meter-bar.crit progress { background-color: #ff5c57; }
/* .thin: a 2px secondary bar (the weekly "all models" window) stacked right
   under the main usage bar. Same colour ramp; the two-class selectors outrank
   the .meter-bar rules above, shrinking only the height. */
progressbar.meter-bar.thin          { min-height: 2px; }
progressbar.meter-bar.thin trough   { min-height: 2px; border-radius: 2px; }
progressbar.meter-bar.thin progress { min-height: 2px; border-radius: 2px; }

/* clickable per-category filter toggles in the header */
button.filter         { min-height: 0; min-width: 0; padding: 1px 9px;
                        margin: 2px 0; border: none; border-radius: 20px;
                        background-image: none; box-shadow: none; outline: none;
                        background-color: #20232b; color: #8b93a3;
                        font-size: 11px; font-weight: 800; }
button.filter:hover   { background-color: #2b2f38; }
button.filter.zero    { opacity: 0.35; }

button.filter.permission:checked { background-color: #ff5c57; color: #1a0f0f; }
button.filter.question:checked   { background-color: #f7c948; color: #241f08; }
button.filter.working:checked    { background-color: #fb923c; color: #241606; }
button.filter.done:checked       { background-color: #34d399; color: #062117; }
button.filter.idle:checked       { background-color: #6b7280; color: #14171c; }

/* tiny title-bar toggle in the header (window-control corner) */
button.chrome         { min-height: 0; min-width: 0; padding: 0 7px; margin: 0;
                        border: none; border-radius: 6px; background-image: none;
                        box-shadow: none; outline: none;
                        background-color: transparent; color: #5b6272;
                        font-size: 13px; }
button.chrome:hover   { background-color: #2b2f38; color: #aab2c0; }
button.chrome:checked { color: #c7ccd6; }

list                  { background-color: transparent; }

row                   { padding: 8px 12px; margin: 3px 8px; border-radius: 9px;
                        background-color: #20232b;
                        border-left: 4px solid transparent;
                        transition: background-color 500ms ease; }

row.permission        { background-color: rgba(255, 92, 87, 0.16);
                        border-left-color: #ff5c57; }
row.permission.pulse  { background-color: rgba(255, 92, 87, 0.42); }
row.question          { background-color: rgba(247, 201, 72, 0.14);
                        border-left-color: #f7c948; }
row.question.pulse    { background-color: rgba(247, 201, 72, 0.34); }
row.done              { border-left-color: #34d399; }
row.working           { border-left-color: #fb923c; }
row.idle              { border-left-color: #3a3f4b; }

.name                 { font-weight: 700; font-size: 12px; color: #eceef3; }
.cwd                  { font-size: 10px; color: #828a99; }
/* live activity - code-like (monospace) so it reads distinctly from the .cwd
   working path above it; expanding brightens it and drops it into a panel. */
.activity             { font-family: monospace; font-size: 10px; color: #8f97a7;
                        margin-top: 2px; }
.activity.expanded    { color: #d3d9e3; margin-top: 4px;
                        padding: 5px 8px; border-radius: 6px;
                        background-color: rgba(255, 255, 255, 0.05); }

.glyph                { font-size: 15px; }
.glyph.permission     { color: #ff5c57; }
.glyph.question       { color: #f7c948; }
.glyph.done           { color: #34d399; }
.glyph.working        { color: #fb923c; }
.glyph.idle           { color: #5b6272; }

.badge                { font-size: 9px; font-weight: 800; letter-spacing: 0.6px;
                        padding: 2px 8px; border-radius: 20px;
                        background-color: #2b2f38; color: #aab2c0; }
.badge.permission     { background-color: #ff5c57; color: #1a0f0f; }
.badge.question       { background-color: #f7c948; color: #241f08; }
.badge.done           { background-color: #34d399; color: #062117; }
.badge.working        { background-color: #5a3f2a; color: #ffc99c; }
.badge.idle           { background-color: #2b2f38; color: #8b93a3; }

/* per-agent CPU% + resident memory (self + all child processes) */
.stats                { font-family: monospace; font-size: 9px; color: #79818f; }
/* per-session CPU highlight: filled yellow chip >CPU_WARN_PCT, red >CPU_CRIT_PCT */
.stats.warn           { color: #241f08; font-weight: 800;
                        background-color: #f7c948; border-radius: 6px;
                        padding: 1px 7px; }
.stats.crit           { color: #1a0f0f; font-weight: 800;
                        background-color: #ff5c57; border-radius: 6px;
                        padding: 1px 7px; }

.empty                { color: #6b7280; font-size: 12px; }
.error                { color: #ff8a84; font-size: 11px; padding: 8px 14px; }

/* ultra-minimized (compact-bar) mode: the window gets the .mini class, which
   tightens the header so the whole app collapses to a ~64px strip. The session
   list is hidden; each session is instead shown as one small state square. */
.mini .header         { padding: 3px 10px; }
.mini .usage          { margin-top: 0; margin-bottom: 0; }

/* one state square per session (mini mode): the category glyph on a tile. The
   border is always 2px (transparent when calm) so the pulse ring can appear
   without changing the square's size. Prominence follows whether the session
   needs YOU: the two attention states get a bright, filled tile so they jump
   out; the rest get a dull dark tile with a dimmed glyph so they recede. */
button.square         { min-width: 22px; min-height: 22px; padding: 0; margin: 0;
                        border: 2px solid transparent; border-radius: 6px;
                        background-image: none; box-shadow: none; outline: none;
                        background-color: #1c1f27; color: #565c68;
                        font-size: 12px; }
/* input required -> bright, filled tile (stands out) */
button.square.permission { background-color: #ff5c57; color: #1a0f0f; }
button.square.question   { background-color: #f7c948; color: #241f08; }
/* no input needed -> dull: dark tile, dimmed category-coloured glyph (recedes) */
button.square.working    { background-color: #1c1f27; color: #7a5334; }
button.square.done       { background-color: #1c1f27; color: #2f6f57; }
button.square.idle       { background-color: #1c1f27; color: #464c58; }
button.square.pulse      { border-color: rgba(255, 255, 255, 0.92); }
"""


# ── app icon ────────────────────────────────────────────────────────────────
# A Claude-inspired clay "spark" (sunburst) on a cream tile, embedded so the
# single-file app carries no external asset. Base64 of a 256x256 PNG, decoded
# and loaded via GdkPixbuf's built-in PNG loader (no new dependency).
ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAMAAABrrFhUAAACnVBMVEX////w7ubw7uXx7+bv7ubw7+"
    "bx7ubv7eXv7ebw7ufw7ebx7eXx7ebu5dvq08br1MbowbDv7uXksJvgnYbhnobdjHHdjXDw7eXZe1zt"
    "39XZd1fu4NXqzr/qzsDmu6rmvKrjq5Tjq5XgmH/ch2rqz8Hv6uLZeVnZeFjv6+LoxLTpyLft2s7s28"
    "7pybvoxbTchmjpyLns3dHw6uHdiW3v593ltqPlt6Tv6d/dim/s2s3gnIPipI7ipY7s2MzekXfltKDe"
    "k3nelHnks6DnwK/py7zbgWPbgmPnwa/v6eDafV/bf2Dt3dLv5t3t39TbgGLafF7v6N7ippDr1cfdi3"
    "Dip5Hr1cngnYTnwrLnw7Lhn4fr1sndjnLksZ3lsp3pzb7hn4jqzsHZe13bgGHt3tPbgWLu5t3ho4zd"
    "jXHw6uDafF3ej3ThpI3r0sThoYnu4dfu4tfq08XmuafmuqfmuabaeVrq0MLmvavmvqvaelrae1vjq5"
    "bjrJfu4dbu5NrgmYHgmoHej3XhoIjqz8Dio4vciGvdiWvv6+PltaLr0cPs3NDt49jbgWTqyrvbg2bu"
    "4tjmuKTmuKXpzLzbhWjnv63elXvflXvowrHbhGbw7OTcgmXlsp7u5NnfknfjqpTjqpPpx7jv7OPls5"
    "/ksp7krpnaelvr1sjhoIns18vekHTu5dzs3NLflnznw7PjrJjnwK7r0MLaf2Dox7fs1srs2M3ci2/u"
    "5NvjrZjjrZnox7bfln3diGzfm4LgnILgmYDgm4Lkr5rdj3PhnofoxrXpybru49rdim7fl37ip5LjqJ"
    "Lbg2Xfl3zchmnhpY3afmDekHbipY/ioovelHrfl33oxbXfknjmuqjbhGfgnoXpzL3owLDr0cTltKHx"
    "7ufv7ufw7ecd5ER3AAAAAXRSTlMAQObYZgAADllJREFUeNrtXftDFMcdv0WsYosiPg6mGpATDiU8FH"
    "MqoUarUsAXR+MD8IEataCJRlEhWhNRseAjKiVKrA30rKnGxFRtCja1pSlNW2vbxKbp+2/p7e499r0z"
    "uzPM7o2fH2e+d/v9fHZ3dh7f+Y7HgwqOS+IcirBjyHRQMIo2QViMIkCeNid0YKWfTJuNFSSze/PjwE"
    "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"
)


APP_ID = "claude-agents-dashboard"   # WM_CLASS / Wayland app-id / .desktop name


def load_icon():
    """Decode the embedded icon to a GdkPixbuf, or None if anything goes wrong."""
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(base64.b64decode(ICON_PNG_B64))
        loader.close()
        return loader.get_pixbuf()
    except Exception:  # noqa: BLE001 — never let a bad icon block startup
        return None


def ensure_desktop_entry():
    """Install a .desktop entry + icon file so a GNOME-style dock/taskbar can
    match the running window (by WM_CLASS / app-id == APP_ID, see main()) to an
    icon. set_icon() alone drives only the title bar / Alt-Tab, NOT the dock.
    Idempotent + best-effort; returns a short label of what it wrote, or None."""
    try:
        icon_dir = os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps")
        app_dir = os.path.expanduser("~/.local/share/applications")
        os.makedirs(icon_dir, exist_ok=True)
        os.makedirs(app_dir, exist_ok=True)

        wrote = None
        icon_path = os.path.join(icon_dir, APP_ID + ".png")
        png = base64.b64decode(ICON_PNG_B64)
        if _read_bytes(icon_path) != png:
            with open(icon_path, "wb") as f:
                f.write(png)
            wrote = "icon"

        exec_cmd = "%s %s" % (shlex.quote(sys.executable),
                              shlex.quote(os.path.abspath(__file__)))
        desktop = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Claude Agents Dashboard\n"
            "Comment=Live status of your Claude Code sessions\n"
            "Exec=%s\n"
            "Icon=%s\n"                       # absolute path: no icon-cache needed
            "Terminal=false\n"
            "Categories=Utility;\n"
            "StartupWMClass=%s\n"             # matches the window's WM_CLASS/app-id
        ) % (exec_cmd, icon_path, APP_ID)
        desktop_path = os.path.join(app_dir, APP_ID + ".desktop")
        if _read_text(desktop_path) != desktop:
            with open(desktop_path, "w") as f:
                f.write(desktop)
            wrote = "desktop entry" if wrote is None else "icon + desktop entry"
        return wrote
    except Exception:  # noqa: BLE001 — never block startup on this
        return None


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


CAT_CLASSES = tuple(CATEGORIES.keys())


class DashboardWindow(Gtk.Window):
    def __init__(self, interval=1.0, keep_top=False, sound=True):
        super().__init__(title="Claude Agents Dashboard")
        self.interval = interval
        self.sound = sound
        self.rows = {}                # sessionId -> Gtk.ListBoxRow
        self.prev_cat = {}            # sessionId -> category (for rising-edge)
        self.attention_rows = []      # rows currently pulsing
        self._sound_timers = {}       # sessionId -> pending "still waiting" beep timer
        self._pulse_on = False
        self._fetching = False        # a fetch is in flight (avoid pile-up)
        self.active_filters = set()   # categories to show; empty = show all
        self.filter_buttons = {}      # category -> (ToggleButton, Gtk.Label)
        self._mini = False            # ultra-minimized (compact-bar) mode on/off
        self._normal_size = None      # (w, h) to restore when leaving mini mode
        self.squares = {}             # sessionId -> mini-mode state square (button)
        self.attention_squares = []   # squares currently pulsing (mirrors rows)
        self._activity_cache = {}     # sessionId -> ((mtime, size), activity str)
        self._transcript_path = {}    # sessionId -> resolved transcript path
        self._prev_jiffies = {}       # pid -> cumulative CPU jiffies (last sample)
        self._prev_time = None        # monotonic time of last resource sample
        self._prev_cpu = None         # (busy, total) /proc/stat jiffies (last sample)
        self._usage = load_usage_cache()     # last good usage payload (persisted)
        self._usage_error = None             # short reason string when a fetch fails
        self._usage_fetching = False         # a usage fetch is in flight
        self._usage_backoff = USAGE_POLL_MS   # grows on failure (rate-limit backoff)

        self.set_default_size(430, 460)
        icon = load_icon()
        if icon is not None:
            self.set_icon(icon)
            Gtk.Window.set_default_icon(icon)   # taskbar + any future windows
        else:
            self.set_icon_name("utilities-system-monitor")
        if keep_top:
            self.set_keep_above(True)   # always above other windows
            self.stick()                # visible on every workspace
            # Panel-style: never grab the keyboard focus. A sticky always-on-top
            # window is otherwise the WM's focus pick on every workspace switch
            # (and on present()/first map), yanking focus off your actual work.
            # Mouse interaction (click to expand, filter toggles, header-drag) is
            # unaffected; only Esc-to-quit needs focus, so in --top mode quit via
            # the window's close button instead.
            self.set_accept_focus(False)
            self.set_focus_on_map(False)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self._on_key)   # Esc quits

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        # header ------------------------------------------------------------
        # Wrapped in an EventBox so dragging the header body also moves the
        # window (in addition to the title bar). The filter toggles below have
        # their own windows, so clicks on them stay clicks (only the title /
        # blank areas start a drag).
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header.get_style_context().add_class("header")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title = self.title = Gtk.Label(label="CLAUDE AGENTS DASHBOARD", xalign=0.0)
        title.get_style_context().add_class("title")
        self.summary = Gtk.Label(xalign=1.0)   # total-session count, right-aligned
        self.summary.get_style_context().add_class("subtitle")
        self.summary.set_ellipsize(Pango.EllipsizeMode.END)
        # tiny toggle for the OS title bar, parked in the window-control corner.
        # Active = decorated; toggling flips set_decorated() live. Reachable even
        # in --top mode (clicks don't need focus), so the bar can always return.
        self.chrome_btn = Gtk.ToggleButton()
        self.chrome_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.chrome_btn.set_can_focus(False)
        self.chrome_btn.set_active(True)                # window starts decorated
        self.chrome_btn.add(Gtk.Label(label="▔"))       # a thin "top bar" glyph
        self.chrome_btn.get_style_context().add_class("chrome")
        self.chrome_btn.set_tooltip_text("Toggle the window title bar")
        self.chrome_btn.connect("toggled", self._on_toggle_chrome)

        # ultra-minimize toggle, parked just left of the title-bar toggle. Active =
        # collapsed to a ~64px strip of per-session state squares (+ the meters).
        # Like the chrome toggle it's a mouse-click, so it needs no focus and works
        # even in --top mode.
        self.mini_btn = Gtk.ToggleButton()
        self.mini_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.mini_btn.set_can_focus(False)
        self.mini_btn.add(Gtk.Label(label="▬"))    # "▬" — collapse to a bar
        self.mini_btn.get_style_context().add_class("chrome")
        self.mini_btn.set_tooltip_text("Ultra-minimize to a compact bar")
        self.mini_btn.connect("toggled", self._on_toggle_mini)

        # State-squares strip: only shown in mini mode, where it takes over the
        # title's space (the title is hidden). Horizontal-scrolls if there are
        # more squares than fit; overlay scrollbar so it doesn't steal height.
        self.mini_squares = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.mini_squares.set_valign(Gtk.Align.CENTER)
        self.mini_squares_scroll = Gtk.ScrolledWindow()
        self.mini_squares_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.mini_squares_scroll.set_overlay_scrolling(True)
        self.mini_squares_scroll.set_propagate_natural_height(True)
        self.mini_squares_scroll.add(self.mini_squares)
        self.mini_squares_scroll.set_no_show_all(True)  # hidden until mini mode

        top.pack_start(title, False, False, 0)
        top.pack_start(self.mini_squares_scroll, True, True, 6)  # fills when title hidden
        top.pack_end(self.chrome_btn, False, False, 0)  # far right
        top.pack_end(self.mini_btn, False, False, 0)    # left of the chrome toggle
        top.pack_end(self.summary, False, False, 0)     # left of the toggles
        header.pack_start(top, False, False, 0)

        # Resource band: Claude usage meter on the LEFT, whole-machine CPU + MEM
        # meters stacked on the RIGHT — a compact system-monitor-style readout.
        meters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        meters.get_style_context().add_class("usage")

        # left: Claude subscription usage (session 5h) + reset countdown, with a
        # tiny secondary bar for the weekly "all models" (7d) window beneath it.
        # Hidden until the first success (see _render_usage). The seven_day_opus
        # window is still fetched (kept in self._usage) but intentionally not shown.
        self.usage_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.usage_box.set_valign(Gtk.Align.CENTER)
        self.usage_box.set_no_show_all(True)
        usage_cap = Gtk.Label(label="CLAUDE", xalign=0.0)
        usage_cap.get_style_context().add_class("meter-cap")
        self.usage_bar = Gtk.ProgressBar()          # session (5h) window
        self.usage_bar.get_style_context().add_class("meter-bar")
        # tiny secondary bar right below the main one: the weekly "all models"
        # (seven_day) window. Same colour ramp, 2px tall (.thin); _render_usage
        # shows it only once that window has data.
        self.usage_bar_all = Gtk.ProgressBar()
        for _c in ("meter-bar", "thin"):
            self.usage_bar_all.get_style_context().add_class(_c)
        self.usage_bar_all.set_no_show_all(True)
        usage_bars = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        usage_bars.set_valign(Gtk.Align.CENTER)
        usage_bars.pack_start(self.usage_bar, False, False, 0)
        usage_bars.pack_start(self.usage_bar_all, False, False, 0)
        self.usage_pct = Gtk.Label(xalign=1.0)
        self.usage_pct.get_style_context().add_class("meter-pct")
        self.usage_reset = Gtk.Label(xalign=0.0)  # "3h 55m left" until the reset
        self.usage_reset.get_style_context().add_class("meter-reset")
        self.usage_err = Gtk.Label()              # tiny badge shown on fetch failure
        self.usage_err.get_style_context().add_class("usage-err")
        self.usage_box.pack_start(usage_cap, False, False, 0)
        self.usage_box.pack_start(usage_bars, True, True, 0)
        self.usage_box.pack_end(self.usage_err, False, False, 0)     # far right
        self.usage_box.pack_end(self.usage_reset, False, False, 0)   # left of badge
        self.usage_box.pack_end(self.usage_pct, False, False, 0)     # left of reset
        # Children shown explicitly (box has no_show_all); per-child visibility is
        # then driven by _render_usage. (usage_bar_all stays no_show_all — shown
        # only when the seven_day window has data.)
        for _w in (usage_cap, usage_bars, self.usage_bar, self.usage_pct,
                   self.usage_reset, self.usage_err):
            _w.show()
        meters.pack_start(self.usage_box, True, True, 0)

        # right: whole-machine CPU stacked over MEM, filled by _update_system.
        sys_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.cpu_bar, self.cpu_pct, cpu_meter = self._make_meter("CPU")
        self.mem_bar, self.mem_pct, mem_meter = self._make_meter("MEM")
        sys_col.pack_start(cpu_meter, False, False, 0)
        sys_col.pack_start(mem_meter, False, False, 0)
        meters.pack_end(sys_col, False, False, 0)
        header.pack_start(meters, False, False, 2)

        # per-category filter toggles — click an icon to show only that kind;
        # multiple can be active at once; none active = show everything.
        filt = self.filt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for cat in ("permission", "question", "working", "done", "idle"):
            btn = Gtk.ToggleButton()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.set_can_focus(False)
            lbl = Gtk.Label(label=f"{CATEGORIES[cat]['glyph']} 0")
            btn.add(lbl)
            ctx = btn.get_style_context()
            ctx.add_class("filter")
            ctx.add_class(cat)
            btn.set_tooltip_text(f"Show only: {CATEGORIES[cat]['label']}")
            btn.connect("toggled", self._on_filter_toggled, cat)
            filt.pack_start(btn, False, False, 0)
            self.filter_buttons[cat] = (btn, lbl)

        header.pack_start(filt, False, False, 5)

        header_evt = Gtk.EventBox()
        header_evt.add(header)
        header_evt.connect("button-press-event", self._on_header_press)
        outer.pack_start(header_evt, False, False, 0)

        self.error = Gtk.Label(xalign=0.0)
        self.error.get_style_context().add_class("error")
        self.error.set_line_wrap(True)
        self.error.set_no_show_all(True)
        outer.pack_start(self.error, False, False, 0)

        # scrollable list ---------------------------------------------------
        scroll = self.scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.set_sort_func(self._sort_rows)
        self.listbox.set_filter_func(self._filter_row)
        self.listbox.connect("row-activated", self._on_row_activated)  # click to expand
        placeholder = Gtk.Label(label="No active agents")
        placeholder.get_style_context().add_class("empty")
        placeholder.show()
        self.listbox.set_placeholder(placeholder)
        scroll.add(self.listbox)
        outer.pack_start(scroll, True, True, 0)

        # timers ------------------------------------------------------------
        # One-shot first fetch. NOTE: this MUST return False so the idle source
        # removes itself; poll() returns True (for the recurring timer), and an
        # idle handler that returns True is re-run in a tight busy loop — which
        # here would spawn `claude` subprocesses without bound and lock up the
        # machine. Wrap it so the initial fetch fires exactly once.
        GLib.idle_add(self._first_poll)                            # first fetch
        GLib.timeout_add(int(interval * 1000), self.poll)          # recurring
        GLib.timeout_add(650, self._pulse_tick)                    # attention pulse
        GLib.idle_add(self._first_usage)   # first usage fetch (self-schedules the next)

    # ── window chrome ────────────────────────────────────────────────────────
    def _on_header_press(self, widget, event):
        # Left-click on the header starts a window move (no title bar to grab).
        if event.button == 1:
            self.begin_move_drag(
                event.button, int(event.x_root), int(event.y_root), event.time
            )
        return False

    def _on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()   # → "destroy" signal → Gtk.main_quit
        return False

    def _on_toggle_chrome(self, btn):
        # Add/remove the window-manager title bar at runtime. GTK applies this
        # live (verified on X11/Xwayland); no re-map needed.
        self.set_decorated(btn.get_active())

    def _on_toggle_mini(self, btn):
        self._set_mini(btn.get_active())

    def _set_mini(self, mini):
        # Ultra-minimize: collapse to a ~64px strip showing just the meters and
        # one state square per session (the full list + header text are hidden).
        # The .mini class on the window tightens the header padding (see CSS).
        if mini == self._mini:
            return
        self._mini = mini
        wctx = self.get_style_context()
        if mini:
            self._normal_size = self.get_size()          # to restore on the way out
            wctx.add_class("mini")
            for w in (self.title, self.summary, self.filt, self.error, self.scroll):
                w.hide()
            # no_show_all kept window.show_all() from revealing the strip at
            # startup; clear it now so show_all reaches the auto-viewport GTK
            # inserts between the ScrolledWindow and the box (a bare show() would
            # leave that viewport — hence the squares — hidden).
            self.mini_squares_scroll.set_no_show_all(False)
            self.mini_squares_scroll.show_all()
            # Ask for height 1; GTK clamps up to the content's minimum, so the
            # window shrinks to the compact strip instead of keeping its old size.
            self.resize(self._normal_size[0], 1)
        else:
            wctx.remove_class("mini")
            self.mini_squares_scroll.hide()
            for w in (self.title, self.summary, self.filt, self.scroll):
                w.show()
            # self.error stays hidden; the next _apply re-shows it if still failing
            if self._normal_size:
                self.resize(*self._normal_size)

    # ── filtering ────────────────────────────────────────────────────────────
    def _on_filter_toggled(self, btn, cat):
        if btn.get_active():
            self.active_filters.add(cat)
        else:
            self.active_filters.discard(cat)
        self.listbox.invalidate_filter()

    def _filter_row(self, row):
        # empty filter set → show everything
        return not self.active_filters or getattr(row, "_cat", None) in self.active_filters

    # ── expand / collapse ────────────────────────────────────────────────────
    def _on_row_activated(self, listbox, row):
        self._set_expanded(row, not getattr(row, "_expanded", False))

    def _set_expanded(self, row, expanded):
        row._expanded = expanded
        act = row._act
        if expanded:
            act.set_text(row._activity_full)     # full, multi-line command/message
            act.set_ellipsize(Pango.EllipsizeMode.NONE)
            act.set_line_wrap(True)
            act.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)  # break long paths too
            act.get_style_context().add_class("expanded")
        else:
            act.set_text(row._activity_short)    # one-line preview
            act.set_line_wrap(False)
            act.set_ellipsize(Pango.EllipsizeMode.END)
            act.get_style_context().remove_class("expanded")
            act._forced_h = -1
            act.set_size_request(-1, -1)     # shrink back to one line
        act.set_visible(bool(row._activity_short))
        act.queue_resize()                   # kick a re-allocation → _on_act_alloc

    def _on_act_alloc(self, label, alloc):
        # GtkBox / GtkListBox don't reliably propagate a wrapping label's
        # height-for-width, so the row won't grow to contain expanded text on
        # its own (the text overflows onto the next row). Sidestep that: measure
        # the label's own required height for the width it was just given and
        # pin it as a fixed height request. The container then only has to sum
        # fixed heights, which it does correctly.
        if not label.get_line_wrap() or alloc.width <= 1:
            return
        _min_h, nat_h = label.get_preferred_height_for_width(alloc.width)
        if nat_h != label._forced_h:
            label._forced_h = nat_h
            # defer out of the size-allocate cycle to avoid re-entrancy
            GLib.idle_add(label.set_size_request, -1, nat_h)

    # ── data flow ──────────────────────────────────────────────────────────
    def _first_poll(self):
        self.poll()
        return False  # one-shot: remove this idle source after the first fetch

    def poll(self):
        # Skip if the previous fetch hasn't returned yet, so a slow `claude`
        # call (subprocess timeout is 15s, longer than the poll interval)
        # can't cause overlapping fetches to pile up.
        if not self._fetching:
            self._fetching = True
            threading.Thread(target=self._fetch_thread, daemon=True).start()
        return True  # keep GLib timer alive

    def _fetch_thread(self):
        try:
            data = fetch()
            # Enrich with live activity + resource usage here, in the background
            # thread — file and /proc I/O must never touch the GTK main loop.
            sysres = None
            if isinstance(data, list):
                for s in data:
                    if isinstance(s, dict):
                        s["_activity"] = self._activity_for(s)
                sysres = self._sample_resources(data)
            GLib.idle_add(self._apply, data, sysres)
        finally:
            self._fetching = False

    # ── live activity (transcript tail) ──────────────────────────────────────
    def _transcript_file(self, s):
        sid = s.get("sessionId")
        if not sid:
            return None
        cached = self._transcript_path.get(sid)
        if cached and os.path.exists(cached):
            return cached
        # Claude Code encodes the cwd by turning every "/" into "-".
        cand = os.path.join(PROJECTS_DIR, (s.get("cwd") or "").replace(os.sep, "-"),
                            sid + ".jsonl")
        if os.path.exists(cand):
            self._transcript_path[sid] = cand
            return cand
        # fall back to a glob if the encoding didn't match (done once per session)
        hits = glob.glob(os.path.join(PROJECTS_DIR, "*", sid + ".jsonl"))
        if hits:
            self._transcript_path[sid] = hits[0]
            return hits[0]
        return None

    def _activity_for(self, s):
        path = self._transcript_file(s)
        if not path:
            return ""
        try:
            st = os.stat(path)
        except OSError:
            return ""
        sig = (st.st_mtime, st.st_size)
        sid = s.get("sessionId")
        cached = self._activity_cache.get(sid)
        if cached and cached[0] == sig:     # unchanged transcript → reuse
            return cached[1]
        act = read_activity(path)
        self._activity_cache[sid] = (sig, act)
        return act

    # ── resource usage ───────────────────────────────────────────────────────
    def _sample_resources(self, sessions):
        """Attach CPU%/memory to each session (its pid + every descendant), and
        return whole-machine usage {'cpu', 'mem_pct', 'mem_used', 'mem_total'}
        (or None if /proc is unavailable). Per-session CPU% is of total system
        capacity (all logical cores); the returned 'cpu' is overall system CPU
        from /proc/stat. Runs in the fetch thread; the _prev_* state is
        single-threaded (only _fetching serialises fetches)."""
        stats = read_proc_stats()
        now = time.monotonic()
        if not stats:
            for s in sessions:
                if isinstance(s, dict):
                    s["_cpu"] = s["_mem"] = None
            self._prev_jiffies, self._prev_time, self._prev_cpu = {}, now, None
            return None

        children = {}                       # ppid -> [child pid, ...]
        for pid, (ppid, _j, _r) in stats.items():
            children.setdefault(ppid, []).append(pid)

        # CPU jiffies used per pid since the previous snapshot. A pid we haven't
        # seen before contributes 0 (not its whole lifetime), so a freshly
        # spawned test/CI process doesn't show up as a one-off spike.
        prev = self._prev_jiffies
        dt = (now - self._prev_time) if self._prev_time else 0.0
        delta = {pid: jf - prev.get(pid, jf) for pid, (_p, jf, _r) in stats.items()}

        def descendants(root):
            seen, stack = set(), [root]
            while stack:                    # iterative DFS (guards against cycles)
                p = stack.pop()
                if p in seen or p not in stats:
                    continue
                seen.add(p)
                stack.extend(children.get(p, ()))
            return seen

        all_pids = set()
        for s in sessions:
            if not isinstance(s, dict):
                continue
            try:
                pid = int(s.get("pid"))
            except (TypeError, ValueError):
                s["_cpu"] = s["_mem"] = None
                continue
            if pid not in stats:
                s["_cpu"] = s["_mem"] = None
                continue
            tree = descendants(pid)
            all_pids |= tree
            s["_mem"] = sum(stats[p][2] for p in tree)
            djf = sum(delta.get(p, 0) for p in tree)
            s["_cpu"] = (djf / _CLK_TCK / dt / _NCPU * 100.0) if dt > 0 else 0.0

        self._prev_jiffies = {pid: jf for pid, (_p, jf, _r) in stats.items()}
        self._prev_time = now

        # whole-machine usage for the header meters (system CPU from /proc/stat's
        # busy/total delta; memory from /proc/meminfo). CPU is 0 on the first
        # sample (no previous counter to diff against).
        sysres = {"cpu": 0.0, "mem_pct": None, "mem_used": None, "mem_total": None}
        cpu_now = read_system_cpu()
        if cpu_now and self._prev_cpu:
            d_busy = cpu_now[0] - self._prev_cpu[0]
            d_total = cpu_now[1] - self._prev_cpu[1]
            if d_total > 0:
                sysres["cpu"] = max(0.0, min(100.0, d_busy / d_total * 100.0))
        self._prev_cpu = cpu_now
        mem = read_system_mem()
        if mem:
            used, total = mem
            sysres.update(mem_used=used, mem_total=total,
                          mem_pct=(used / total * 100.0) if total else None)
        return sysres

    def _apply(self, data, sysres=None):
        if isinstance(data, dict) and "_error" in data:
            self.error.set_text("⚠  claude agents unavailable: " + data["_error"])
            self.error.show()
            self._update_system(None)
            return
        self.error.hide()

        sessions = data if isinstance(data, list) else []
        counts = {c: 0 for c in CATEGORIES}
        seen = set()

        for s in sessions:
            sid = s.get("sessionId") or f"pid-{s.get('pid')}"
            seen.add(sid)
            cat = classify(s)
            counts[cat] += 1

            row = self.rows.get(sid)
            if row is None:
                row = self._make_row()
                self.rows[sid] = row
                self.listbox.add(row)
            self._update_row(row, s, cat)

            # mirror the session as a mini-mode state square (same key)
            sq = self.squares.get(sid)
            if sq is None:
                sq = self._make_square()
                self.squares[sid] = sq
                self.mini_squares.pack_start(sq, False, False, 0)
            self._update_square(sq, s, cat)

            # Entering an attention state (from a non-attention one) raises the
            # dashboard now and arms a delayed beep; leaving it cancels a beep
            # that hasn't fired yet. See _arm_sound / _sound_due.
            prev = self.prev_cat.get(sid)
            was_attn = prev in CATEGORIES and CATEGORIES[prev]["attention"]
            now_attn = CATEGORIES[cat]["attention"]
            if now_attn and not was_attn:
                self.present()
                self._arm_sound(sid)
            elif was_attn and not now_attn:
                self._cancel_sound(sid)
            self.prev_cat[sid] = cat

        # drop sessions that vanished (rows and their mirror squares)
        for sid in list(self.rows):
            if sid not in seen:
                self.listbox.remove(self.rows.pop(sid))
                self.prev_cat.pop(sid, None)
                self._cancel_sound(sid)
        for sid in list(self.squares):
            if sid not in seen:
                self.mini_squares.remove(self.squares.pop(sid))

        self.listbox.invalidate_sort()
        self.listbox.invalidate_filter()
        self.attention_rows = [r for r in self.rows.values() if r._attention]
        # Order squares like the rows (attention first, then by name) and refresh
        # the pulsing set — same prio/name keys as _sort_rows.
        for i, sq in enumerate(sorted(self.squares.values(),
                                      key=lambda w: (w._prio, w._sortname))):
            self.mini_squares.reorder_child(sq, i)
        self.attention_squares = [w for w in self.squares.values() if w._attention]
        self._update_summary(counts)
        self._update_system(sysres)
        return False  # one-shot idle callback

    # ── row widgets ─────────────────────────────────────────────────────────
    def _make_row(self):
        row = Gtk.ListBoxRow()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        glyph = Gtk.Label()
        glyph.get_style_context().add_class("glyph")

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        name = Gtk.Label(xalign=0.0)
        name.get_style_context().add_class("name")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        cwd = Gtk.Label(xalign=0.0)
        cwd.get_style_context().add_class("cwd")
        cwd.set_ellipsize(Pango.EllipsizeMode.START)
        text.pack_start(name, False, False, 0)
        text.pack_start(cwd, False, False, 0)

        badge = Gtk.Label()
        badge.get_style_context().add_class("badge")
        badge.set_halign(Gtk.Align.END)

        # right column: state badge on top, live CPU%/memory beneath it. The
        # stats label is a sibling of the badge (not the activity line) so it
        # tracks the row header, and stays hidden until /proc yields a sample.
        stats = Gtk.Label(xalign=1.0)
        stats.get_style_context().add_class("stats")
        stats.set_halign(Gtk.Align.END)
        stats.set_no_show_all(True)
        rightcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rightcol.set_valign(Gtk.Align.CENTER)
        rightcol.pack_start(badge, False, False, 0)
        rightcol.pack_start(stats, False, False, 0)

        top.pack_start(glyph, False, False, 0)
        top.pack_start(text, True, True, 0)
        top.pack_end(rightcol, False, False, 0)

        # Activity is a full-width sibling BELOW the glyph/name/badge row, not
        # nested inside it. A horizontal box is width-for-height and won't
        # propagate a wrapping label's height-for-width — a nested wrapping
        # label would overflow its row. As a direct child of this vertical box
        # the wrapped height is measured correctly, so the row grows to fit.
        act = Gtk.Label(xalign=0.0)
        act.get_style_context().add_class("activity")
        act.set_ellipsize(Pango.EllipsizeMode.END)
        act.set_no_show_all(True)          # hidden until it has activity text
        act.set_margin_start(27)           # align under the name column
        act._forced_h = -1                 # pinned wrapped height (see _on_act_alloc)
        act.connect("size-allocate", self._on_act_alloc)

        outer.pack_start(top, False, False, 0)
        outer.pack_start(act, False, False, 0)
        row.add(outer)

        row._glyph, row._name, row._cwd, row._badge = glyph, name, cwd, badge
        row._stats = stats
        row._act = act
        row._prio, row._sortname, row._attention = 99, "", False
        row._cat = None
        row._expanded = False
        row._activity_full = ""     # newline-preserving, shown when expanded
        row._activity_short = ""    # one-line preview, shown when collapsed
        row.show_all()
        return row

    def _update_row(self, row, s, cat):
        meta = CATEGORIES[cat]
        name = " ".join((s.get("name") or os.path.basename(s.get("cwd", "")) or "?").split())

        row._name.set_text(name)
        row._cwd.set_text(shorten_path(s.get("cwd", "")))
        activity = s.get("_activity") or ""
        row._activity_full = activity
        row._activity_short = _oneline(activity)
        # show the full (multi-line) form while expanded, the one-liner otherwise
        row._act.set_text(row._activity_full if row._expanded else row._activity_short)
        row._act.set_visible(bool(activity))
        row._glyph.set_text(meta["glyph"])
        row._badge.set_text(meta["label"])
        row._prio = meta["prio"]
        row._sortname = name.lower()
        row._attention = meta["attention"]
        row._cat = cat

        cpu, mem = s.get("_cpu"), s.get("_mem")
        if cpu is None and mem is None:
            row._stats.set_visible(False)      # pid gone, or /proc unavailable
        else:
            row._stats.set_text(fmt_stats(cpu, mem))
            sctx = row._stats.get_style_context()
            sctx.remove_class("warn")
            sctx.remove_class("crit")
            if cpu is not None and cpu > CPU_CRIT_PCT:
                sctx.add_class("crit")         # >25% total CPU -> red chip
            elif cpu is not None and cpu > CPU_WARN_PCT:
                sctx.add_class("warn")         # >10% total CPU -> yellow chip
            row._stats.set_visible(True)

        tip = (f"{name}\ncwd: {s.get('cwd','?')}\nstate: {s.get('state','—')}   "
               f"status: {s.get('status','—')}\nkind: {s.get('kind','—')}   "
               f"pid: {s.get('pid','—')}")
        if cpu is not None or mem is not None:
            tip += (f"\ncpu: {fmt_cpu(cpu) or '—'}   "
                    f"mem: {fmt_mem(mem) if mem is not None else '—'}"
                    "   (incl. child processes)")
        if activity:
            tip += f"\nactivity: {row._activity_short}"
        row.set_tooltip_text(tip)

        for widget in (row, row._glyph, row._badge):
            ctx = widget.get_style_context()
            for c in CAT_CLASSES:
                ctx.remove_class(c)
            ctx.add_class(cat)
        if not meta["attention"]:
            row.get_style_context().remove_class("pulse")

    # ── mini-mode state squares ──────────────────────────────────────────────
    def _make_square(self):
        # A small button = one session's state tile. It's a button (not a plain
        # box) so CSS reliably paints the rounded, filled background and it gives
        # click feedback; clicking it leaves mini mode. Fixed size via CSS.
        sq = Gtk.Button()
        sq.set_relief(Gtk.ReliefStyle.NONE)
        sq.set_can_focus(False)
        sq.get_style_context().add_class("square")
        glyph = Gtk.Label()
        sq.add(glyph)
        sq._glyph = glyph
        sq._prio, sq._sortname, sq._attention = 99, "", False
        sq.connect("clicked", self._on_square_clicked)
        sq.show_all()
        return sq

    def _update_square(self, sq, s, cat):
        meta = CATEGORIES[cat]
        name = " ".join((s.get("name") or os.path.basename(s.get("cwd", "")) or "?").split())
        sq._glyph.set_text(meta["glyph"])
        sq._prio = meta["prio"]
        sq._sortname = name.lower()
        sq._attention = meta["attention"]
        ctx = sq.get_style_context()
        for c in CAT_CLASSES:
            ctx.remove_class(c)
        ctx.add_class(cat)
        if not meta["attention"]:
            ctx.remove_class("pulse")
        # tooltip carries what the collapsed square can't show
        cpu, mem = s.get("_cpu"), s.get("_mem")
        tip = f"{name} — {meta['label']}\n{shorten_path(s.get('cwd', ''))}"
        act = s.get("_activity") or ""
        if act:
            tip += "\n" + _oneline(act, 120)
        if cpu is not None or mem is not None:
            tip += (f"\ncpu: {fmt_cpu(cpu) or '—'}   "
                    f"mem: {fmt_mem(mem) if mem is not None else '—'}")
        sq.set_tooltip_text(tip)

    def _on_square_clicked(self, sq):
        # Clicking any square pops back to the full view (and raises the window).
        self.mini_btn.set_active(False)     # -> _on_toggle_mini -> _set_mini(False)
        self.present()

    def _sort_rows(self, a, b):
        if a._prio != b._prio:
            return -1 if a._prio < b._prio else 1
        if a._sortname != b._sortname:
            return -1 if a._sortname < b._sortname else 1
        return 0

    # ── attention pulse ──────────────────────────────────────────────────────
    def _pulse_tick(self):
        self._pulse_on = not self._pulse_on
        # Rows (normal mode) and state squares (mini mode) pulse in lockstep.
        for w in self.attention_rows + self.attention_squares:
            ctx = w.get_style_context()
            if self._pulse_on:
                ctx.add_class("pulse")
            else:
                ctx.remove_class("pulse")
        return True

    # ── header summary ───────────────────────────────────────────────────────
    def _update_summary(self, counts):
        total = sum(counts.values())
        self.summary.set_text(f"{total} session{'s' if total != 1 else ''}")
        for cat, (btn, lbl) in self.filter_buttons.items():
            n = counts.get(cat, 0)
            lbl.set_text(f"{CATEGORIES[cat]['glyph']} {n}")
            ctx = btn.get_style_context()
            if n:
                ctx.remove_class("zero")
            else:
                ctx.add_class("zero")   # dim categories with no sessions

    def _make_meter(self, caption):
        # A compact, fixed-width caption + progress bar + percent (the two stacked
        # right-hand meters line up because caption/percent widths are pinned).
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cap = Gtk.Label(label=caption, xalign=0.0)
        cap.get_style_context().add_class("meter-cap")
        cap.set_width_chars(3)                  # "CPU" / "MEM" align
        bar = Gtk.ProgressBar()
        bar.set_valign(Gtk.Align.CENTER)
        bar.set_size_request(72, -1)            # compact fixed-width gauge
        bar.get_style_context().add_class("meter-bar")
        pct = Gtk.Label(xalign=1.0)
        pct.set_width_chars(4)                  # room for "100%"
        pct.get_style_context().add_class("meter-pct")
        box.pack_start(cap, False, False, 0)
        box.pack_start(bar, False, False, 0)
        box.pack_end(pct, False, False, 0)
        return bar, pct, box

    def _set_meter(self, bar, pct_label, value):
        # Fill + label a meter and colour it green -> warn >=75% -> crit >=90%.
        if value is None:
            return
        bar.set_fraction(max(0.0, min(1.0, value / 100.0)))
        pct_label.set_text("%d%%" % round(value))
        sev = "crit" if value >= 90 else "warn" if value >= 75 else None
        for w in (bar, pct_label):
            ctx = w.get_style_context()
            ctx.remove_class("warn")
            ctx.remove_class("crit")
            if sev:
                ctx.add_class(sev)

    def _update_system(self, sysres):
        # Whole-machine CPU + memory meters in the header. Left untouched when
        # there's nothing to sample (no /proc), so the last values stay shown.
        if not sysres:
            return
        self._set_meter(self.cpu_bar, self.cpu_pct, sysres.get("cpu"))
        self._set_meter(self.mem_bar, self.mem_pct, sysres.get("mem_pct"))
        self.cpu_bar.set_tooltip_text(
            "System CPU usage: %d%%" % round(sysres.get("cpu") or 0))
        used, total = sysres.get("mem_used"), sysres.get("mem_total")
        if used is not None and total:
            self.mem_bar.set_tooltip_text(
                "System memory: %s / %s (%d%%)"
                % (fmt_mem(used), fmt_mem(total), round(sysres.get("mem_pct") or 0)))

    # ── Claude usage limit ───────────────────────────────────────────────────
    def _first_usage(self):
        # One-shot idle (MUST return False; see the _first_poll gotcha). Shows any
        # cached numbers right away, then kicks the first live fetch.
        self._render_usage()
        self._poll_usage()
        return False

    def _poll_usage(self):
        # One-shot: start a fetch if none is in flight. The NEXT poll is scheduled
        # in _usage_result with adaptive backoff, so this never recurs by itself.
        if not self._usage_fetching:
            self._usage_fetching = True
            threading.Thread(target=self._usage_thread, daemon=True).start()
        return False

    def _usage_thread(self):
        res = fetch_usage()                 # network I/O, off the GTK main loop
        GLib.idle_add(self._usage_result, res)

    def _usage_result(self, res):
        self._usage_fetching = False
        if isinstance(res, dict) and "_error" not in res:
            self._usage = res               # fresh data
            self._usage_error = None
            save_usage_cache(res)
            self._usage_backoff = USAGE_POLL_MS
        else:
            # Failed (often HTTP 429 — the endpoint is rate-limited): surface a
            # tiny badge, keep the last good numbers, and back off so we stop
            # hammering it. Recovers to the normal cadence on the next success.
            self._usage_error = res.get("_error") if isinstance(res, dict) else "unavailable"
            self._usage_backoff = min(self._usage_backoff * 2, USAGE_MAX_BACKOFF_MS)
        self._render_usage()
        GLib.timeout_add(self._usage_backoff, self._poll_usage)
        return False  # one-shot idle callback

    def _render_usage(self):
        # Percent shown is the session (five_hour) window; the tiny bar beneath it
        # is the weekly "all models" (seven_day) window. seven_day_opus is also in
        # self._usage but not rendered.
        u = self._usage or {}
        fh = u.get("five_hour") or {}
        sd = u.get("seven_day") or {}
        pct = fh.get("utilization")
        pct_all = sd.get("utilization")
        have = pct is not None
        have_all = pct_all is not None
        err = self._usage_error
        if not have and not err:
            self.usage_box.set_visible(False)   # nothing to show yet
            return
        # meter (bar + percent) only when we actually have numbers
        if have:
            self.usage_bar.set_fraction(max(0.0, min(1.0, pct / 100.0)))
            self.usage_pct.set_text(fmt_pct(pct))
            sev = "crit" if pct >= 90 else "warn" if pct >= 75 else None
            for w in (self.usage_bar, self.usage_pct):
                ctx = w.get_style_context()
                ctx.remove_class("warn")
                ctx.remove_class("crit")
                if sev:
                    ctx.add_class(sev)
            r = fh.get("resets_at")
            rel = _reset_rel(r)
            self.usage_reset.set_text(
                "" if rel == "?" else "resetting" if rel == "now" else rel + " left")
            tip = ("Claude session usage (5h): %d%%\nResets in %s (%s)"
                   % (round(pct), rel, _reset_abs(r)))
            # secondary thin bar: weekly all-models window (same colour ramp)
            if have_all:
                self.usage_bar_all.set_fraction(max(0.0, min(1.0, pct_all / 100.0)))
                actx = self.usage_bar_all.get_style_context()
                actx.remove_class("warn")
                actx.remove_class("crit")
                if pct_all >= 90:
                    actx.add_class("crit")
                elif pct_all >= 75:
                    actx.add_class("warn")
                r2 = sd.get("resets_at")
                tip += ("\n\nAll models (7d): %d%%\nResets in %s (%s)"
                        % (round(pct_all), _reset_rel(r2), _reset_abs(r2)))
            self.usage_box.set_tooltip_text(tip)
        self.usage_bar.set_visible(have)
        self.usage_bar_all.set_visible(have and have_all)
        self.usage_pct.set_visible(have)
        self.usage_reset.set_visible(have)
        # tiny error badge instead of swallowing the failure
        if err:
            self.usage_err.set_text("⚠" if have else "⚠ " + err)
            self.usage_err.set_tooltip_text(
                "Claude usage unavailable: %s%s"
                % (err, "\n(showing last known values)" if have else ""))
        self.usage_err.set_visible(bool(err))
        self.usage_box.set_visible(True)

    # ── alerts ────────────────────────────────────────────────────────────────
    def _arm_sound(self, sid):
        # (Re)start this session's countdown; only fires while it keeps waiting.
        self._cancel_sound(sid)
        self._sound_timers[sid] = GLib.timeout_add(
            SOUND_DELAY_MS, self._sound_due, sid)

    def _cancel_sound(self, sid):
        src = self._sound_timers.pop(sid, None)
        if src is not None:
            GLib.source_remove(src)

    def _sound_due(self, sid):
        # Fired SOUND_DELAY_MS after the session started needing you: beep only
        # if it still exists and is still waiting (the prompt wasn't answered).
        self._sound_timers.pop(sid, None)
        cat = self.prev_cat.get(sid)
        if (self.sound and sid in self.rows
                and cat in CATEGORIES and CATEGORIES[cat]["attention"]):
            try:
                subprocess.Popen(
                    ["canberra-gtk-play", "-i", ATTENTION_SOUND],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:  # noqa: BLE001 — sound is best-effort
                pass
        return False  # one-shot timer


def main():
    ap = argparse.ArgumentParser(description="Compact Claude agents status dashboard.")
    ap.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    ap.add_argument("--top", action="store_true",
                    help="keep window above all others and on every workspace")
    ap.add_argument("--no-sound", action="store_true", help="silence attention sounds")
    ap.add_argument("--no-desktop", action="store_true",
                    help="don't install/update the .desktop entry (used for the dock icon)")
    args = ap.parse_args()

    # Stable identity so a GNOME-style dock/taskbar can match our window to the
    # installed .desktop entry — that, not set_icon(), is what drives the dock
    # icon. Must be set before the window maps.
    GLib.set_prgname(APP_ID)
    try:
        Gdk.set_program_class(APP_ID)      # X11 WM_CLASS (Xwayland / --top)
    except Exception:  # noqa: BLE001
        pass
    if not args.no_desktop:
        wrote = ensure_desktop_entry()
        if wrote:
            print("claude-agents-dashboard: installed %s under ~/.local/share"
                  % wrote, file=sys.stderr)

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    win = DashboardWindow(interval=args.interval, keep_top=args.top, sound=not args.no_sound)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
