# CLAUDE.md

Guidance for working on this repository.

## What this is

A single-file desktop dashboard, **`agents_dashboard.py`** (~1060 lines, no other
source). It polls `claude agents --json` on an interval and shows one row per
Claude Code session: name, working directory, a state badge, a live "currently
working on" line, and the session's **CPU% + resident memory** (self + all child
processes). The header shows **whole-machine CPU + memory meters** and a
**Claude subscription usage** meter (a progress bar for the session / 5h
rate-limit window).
Sessions needing attention (waiting on a permission prompt, or blocked on a
question) sort to the top, pulse, and raise the window; a sound plays only once
a session has kept waiting on you for `SOUND_DELAY_MS` (5s). The icon is a
Claude-inspired clay sunburst, embedded as base64 (no asset file): it's the
window icon *and* is installed as a `.desktop` entry so a GNOME-style dock shows
it (see the dock-icon note below).

- **Language / stack:** Python 3 + PyGObject / **GTK 3** (`gi`, `Gtk 3.0`).
- **Runtime deps:** GTK 3 + gobject-introspection (system packages). No pip
  packages, no build step. `canberra-gtk-play` plays the attention sound
  (`ATTENTION_SOUND`, a freedesktop/Yaru sound-theme id) if present (optional;
  failures are swallowed).
- **Run:** `./agents_dashboard.py [--interval SECONDS] [--top] [--no-sound]`
  - `--interval` default **1.0s**; `--top` = always-on-top + on every workspace;
    `--no-sound` silences attention sounds; `--no-desktop` skips installing the
    `.desktop` entry (which provides the dock/taskbar icon).

## Architecture (all in `agents_dashboard.py`)

- Module-level helpers: `classify()` (state/status → category), `fetch()` (runs
  the CLI, 15s timeout, returns list or `{"_error": ...}`), the activity
  extractors `read_activity()` / `_tool_phrase()` / `_cap()` / `_oneline()`, and
  the resource helpers `read_proc_stats()` (one `/proc` snapshot) /
  `read_system_cpu()` (`/proc/stat`) / `read_system_mem()` (`/proc/meminfo`) / `fmt_mem()` /
  `fmt_cpu()` / `fmt_stats()`, and the usage helpers `fetch_usage()` /
  `_oauth_token()` / `fmt_pct()` / `_reset_rel()` / `_reset_abs()`.
- `CATEGORIES` dict defines the 5 categories (permission, question, working,
  done, idle) with glyph/label/**prio** (lower = sorts higher)/attention flag.
- `CSS` — the entire stylesheet as a **`bytes`** literal.
- `DashboardWindow(Gtk.Window)` — builds the UI and owns the poll loop.

### Poll cycle
`GLib.timeout_add` → `poll()` spawns a **daemon thread** → `_fetch_thread()` runs
`fetch()`, **enriches each session with `_activity`** (transcript I/O) **and with
`_cpu`/`_mem`** via `_sample_resources()` (a `/proc` scan) — all off the main
loop — then `GLib.idle_add(_apply, data, totals)` renders on the GTK thread.
Rows are reused, keyed by `sessionId`; `_apply` adds/updates/removes rows and
re-runs sort + filter.

### Resource usage (CPU / memory)
`_sample_resources()` reads every `/proc/<pid>/stat` once, builds the ppid→child
tree, and for each session sums **RSS** and **CPU jiffies** over its pid + all
descendants (so an agent running CI/tests reflects the whole subtree). CPU% is
the change in `utime+stime` jiffies since the previous sample ÷ wall-clock Δt ÷
`SC_CLK_TCK` ÷ **`_NCPU`** × 100 — i.e. **percent of total system capacity (all
logical cores), so it stays in 0–100%**. The
previous snapshot lives on the window (`_prev_jiffies` / `_prev_time`); it is
single-threaded-safe only because `_fetching` serialises fetches. Per-session
values render right-aligned under each badge (`.stats`). A session at/above
**`CPU_WARN_PCT`** (10%) / **`CPU_CRIT_PCT`** (25%, both % of total capacity) gets
its stats highlighted as a **filled chip** — yellow `.stats.warn` above warn, red
`.stats.crit` above crit — toggled in `_update_row`, so a CI/test run that's
burning CPU stands out. RSS summed across a tree double-counts
shared pages — it's a gauge, not an accountant.

`_sample_resources` also returns **whole-machine** usage (not the agent sum):
system CPU% from the `/proc/stat` busy/total jiffy delta (`read_system_cpu`,
tracked in `_prev_cpu`) and memory from `/proc/meminfo` (`read_system_mem`,
`MemTotal − MemAvailable`). `_update_system` fills two tiny progress bars
(`cpu_bar`/`mem_bar`, built by `_make_meter`, coloured green → `.warn` ≥75% →
`.crit` ≥90% by `_set_meter`) **stacked on the right** of the resource band, with
the Claude usage meter on the left — same shared system-monitor style.

### Claude usage limit
`fetch_usage()` GETs **`/api/oauth/usage`** on `api.anthropic.com` — the same
**undocumented** endpoint the interactive `/usage` panel uses — authorised with
the OAuth `accessToken` from `~/.claude/.credentials.json` (`_oauth_token()`
re-reads the file each poll, since Claude Code refreshes + rewrites it; skips the
call if `expiresAt` has passed). The payload's `five_hour` / `seven_day` /
`seven_day_opus` windows each carry `utilization` (%) + `resets_at`. Only the
**session (`five_hour`) window is displayed** right now — a `Gtk.ProgressBar`
meter on the left of the resource band (`CLAUDE` cap + bar + `%` + a
**`resets in …` countdown** from `_reset_rel`, shown inline; the shared
`.meter-cap` / `.meter-bar` / `.meter-pct` / `.meter-reset` classes) whose fill +
percent colour green → `.warn` ≥75% → `.crit` ≥90%. `seven_day`/`seven_day_opus`
are still fetched and kept in `self._usage` (re-enabling them is trivial) but
deliberately not rendered. The meter's children (`usage_bar`/`usage_pct`/
`usage_reset`/`usage_err`) are `show()`n individually because the box has
`no_show_all`; per-child visibility is then driven by `_render_usage`.

**This endpoint is rate-limited (HTTP 429).** So the poll self-schedules with
**adaptive backoff** rather than a fixed timer: `_first_usage` (idle) →
`_poll_usage` (one-shot, `_usage_fetching`-guarded) → `_usage_thread` →
`_usage_result` (via `idle_add`), which reschedules the next `_poll_usage` at
`self._usage_backoff` — `USAGE_POLL_MS` (3 min) on success, doubling up to
`USAGE_MAX_BACKOFF_MS` (30 min) on each failure, reset on the next success. Don't
reintroduce a fixed recurring `timeout_add`, and be sparing when testing against
the live endpoint (a burst of calls will 429 you for a while).

On failure `fetch_usage()` returns `{"_error": reason}` (not `None`): `_render_usage`
keeps the last good numbers and shows a **tiny `⚠` badge** (`usage_err`,
`.usage-err`, reason in tooltip) instead of hiding — with no prior data it shows
`⚠ <reason>` alone. The last good payload is also **persisted** (`save_usage_cache`
→ `~/.cache/claude-agents-dashboard/usage.json`) and reloaded at startup
(`load_usage_cache`), so the meter shows immediately on restart even while
rate-limited. Uses stdlib `urllib` only; no new deps.

### Attention (raise + delayed sound)
Entering an attention category (permission/question) from a non-attention one
raises the window immediately (`present()`) and **arms** a one-shot
`GLib.timeout_add(SOUND_DELAY_MS)` per session (`_arm_sound`, tracked in
`self._sound_timers`). When it fires, `_sound_due` re-checks the session still
exists and is *still* waiting before playing `ATTENTION_SOUND` — so a prompt you
answer within 5s never beeps. Leaving attention or vanishing calls
`_cancel_sound` (there is no `_alert` anymore).

### Dock / taskbar icon (the non-obvious part)
`set_icon()`/`set_default_icon()` only set the **title-bar / Alt-Tab** icon
(`load_icon()` decodes `ICON_PNG_B64`; falls back to `utilities-system-monitor`).
A GNOME-style **dock does NOT use that** — it matches the running window to a
`.desktop` file by **WM_CLASS (X11) / app-id (Wayland)** and shows *that file's*
`Icon=`. So two things are needed and both live in `main()`/`ensure_desktop_entry()`:
(1) a stable identity — `GLib.set_prgname(APP_ID)` (Wayland app-id) +
`Gdk.set_program_class(APP_ID)` (X11 WM_CLASS), set **before the window maps**;
(2) an installed entry — `ensure_desktop_entry()` writes
`~/.local/share/applications/<APP_ID>.desktop` (with `StartupWMClass=<APP_ID>`
and an absolute `Icon=` path, so no icon-cache refresh is needed) plus the PNG
under `~/.local/share/icons/hicolor/256x256/apps/`. It's idempotent, best-effort
(never blocks startup), skippable with `--no-desktop`, and — because the entry
must exist *before* GNOME associates the window — **the app must be restarted**
after a first install for the dock icon to appear (a Shell restart / re-login may
be needed if GNOME cached the old association).

## Non-obvious gotchas (read before editing — these cost real debugging time)

1. **Never let a repeating GLib callback used for the first fetch return `True`
   from `idle_add`.** `poll()` returns `True` (for the recurring `timeout_add`).
   The initial fetch is wrapped in `_first_poll()` which returns **`False`** so
   the idle source removes itself. A previous version did `GLib.idle_add(poll)`;
   because `poll` returns `True`, the idle handler re-fired in a tight busy loop,
   spawning `claude` subprocesses without bound and **freezing the machine**.
   `_first_usage()` mirrors this — it returns **`False`** for the same reason
   (`_poll_usage()`, its recurring `timeout_add` twin, returns `True`).
2. **Single in-flight fetch.** `self._fetching` guards `poll()` so a slow CLI
   call (15s timeout > 1s interval) can't stack up overlapping subprocesses. The
   usage poll has its own guard, `self._usage_fetching`.
3. **`--top` on GNOME/Wayland requires the X11 backend.** Wayland forbids a
   client from setting always-on-top / all-workspaces, so GTK silently ignores
   `set_keep_above()`/`stick()`. We route through **Xwayland** by setting
   `GDK_BACKEND=x11`. This MUST happen **before `import gi`** — the check on
   `sys.argv` at the top of the file does it. Setting it later (e.g. in
   `main()`) is too late: GDK has already committed to Wayland. Verified with
   `Gdk.WindowState.ABOVE`/`STICKY` being granted only under the X11 backend.
   `--top` mode also sets `set_accept_focus(False)` + `set_focus_on_map(False)`
   (panel behaviour): a sticky always-on-top window is otherwise the WM's focus
   pick on **every workspace switch**, stealing focus from your real work. The
   cost is that Esc-to-quit needs focus, so in `--top` mode you quit via the
   window's close button (default windowed mode keeps focus + Esc).
4. **`CSS` is a `bytes` literal → ASCII only.** No em-dashes / non-ASCII in that
   block or the module won't import. (Glyphs live in normal `str`, e.g.
   `CATEGORIES`.)
5. **Live activity comes from transcript tails, not the CLI.** `claude agents
   --json` exposes only `pid,id,cwd,kind,startedAt,sessionId,name,status,state`
   — no activity field. `read_activity()` reads the **last `TAIL_BYTES` (128KB)**
   of `~/.claude/projects/<enc-cwd>/<sessionId>.jsonl` (the cwd is encoded by
   replacing every `/` with `-`; a glob fallback covers mismatches) and returns
   the newest assistant action. It is cached by **(mtime, size)** so only
   actively-changing transcripts are re-read, runs in the background thread, and
   never raises. Cap is `MAX_ACTIVITY` (2000 chars).
6. **Wrapping label in a `GtkListBox` row does NOT auto-grow the row.** `GtkBox`/
   `GtkListBox` don't propagate a wrapping label's height-for-width, so expanded
   text overflows onto the next row. Fixed by pinning the label's height: the
   `size-allocate` handler `_on_act_alloc()` measures the label's own
   `get_preferred_height_for_width()` and sets it as a fixed height request
   (guarded against relayout loops via `_forced_h`). The activity label is also
   a **full-width sibling below** the glyph/name/badge row (`_make_row`), not
   nested inside the horizontal box. Each row keeps two forms of the activity:
   `_activity_full` (newline-preserving, shown expanded) and `_activity_short`
   (one-line, shown collapsed); `_set_expanded()` swaps between them.

## UI behaviors worth knowing

- Header count chips are **filter toggles** (`_on_filter_toggled` /
  `_filter_row`): click to show only those categories; multiple allowed; none =
  show all; zero-count chips dim.
- **Click a row** (`row-activated`) to expand/collapse its activity line.
- `Esc` quits (default mode only — `--top` drops focus, so quit via the close
  button there); dragging the title bar **or** the header body moves the window.
- The tiny **`▔` toggle** in the top-right corner shows/hides the OS title bar at
  runtime (`_on_toggle_chrome` → `set_decorated()`, `.chrome` CSS). It's a
  mouse-click, so it works even in `--top` (no focus needed) — meaning the title
  bar can always be brought back even after it (and, in `--top`, `Esc`) are gone.
- Next to it, the **`▬` toggle** ultra-minimizes the window to a compact ~64px
  strip (`_on_toggle_mini` → `_set_mini`, which adds the `.mini` class to the
  window so the header padding tightens). The session list and header text hide;
  each session collapses to one **state square** (`_make_square` /
  `_update_square`: the category glyph on a category-coloured tile, keyed by
  `sessionId` in `self.squares` exactly like the rows, mirrored in the same
  `_apply` loop, and pulsing in lockstep with the rows via `_pulse_tick`). The
  Claude-usage + CPU/MEM meters stay visible (they're reused in place — nothing
  is reparented). Entering mini stores the window size in `self._normal_size` and
  does `resize(w, 1)` so GTK clamps the window down to the strip's minimum;
  clicking any square (`_on_square_clicked`) or the toggle again restores the
  full view and that saved size. Also a mouse-click, so it works in `--top`.
- Rows use CSS classes = category names; the activity label uses `.activity`
  (monospace) and `.activity.expanded`. The mini-mode squares reuse the same
  category class names on a `button.square` base (`.mini` scopes the strip's
  layout tweaks).

## Testing / verifying UI changes

- **Do not trust headless or `GtkOffscreenWindow` harnesses for layout.** They
  don't reproduce the real width constraint (offscreen allocates natural width),
  and `get_preferred_height_for_width` can read stale caches — both gave
  repeatedly misleading numbers during the wrapping-label work.
- **Verify layout on a real rendered window.** Show the real `DashboardWindow`
  under the X11 backend (pass `--top` or set `GDK_BACKEND=x11`), stub `fetch`
  and/or `_activity_for` to inject test data, then capture with
  `Gdk.pixbuf_get_from_window(win.get_window(), ...)` → `pixbuf.savev(path,
  "png", [], [])` and inspect the PNG. This was the only reliable oracle.
- Quick sanity: `python3 -m py_compile agents_dashboard.py`.

## Conventions

- Keep everything in the one file; no external Python deps.
- Match the existing comment density — the gotchas above are annotated inline at
  their code sites; keep those comments if you touch the code.
