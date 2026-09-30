"""Claude Usage Tray: a Windows system tray icon showing your Claude plan usage and the
context size of every active Claude Code conversation.

Left click opens the details panel; right click opens the menu (Refresh, Quit).

Data sources (read-only):
- ~/.claude/sessions/<pid>.json          -> open conversations (name, cwd, status)
- ~/.claude/projects/*/<sessionId>.jsonl -> last `usage` (context size) and conversation title
- https://api.anthropic.com/api/oauth/usage (UNDOCUMENTED endpoint behind Claude Code's /usage),
  authenticated with the OAuth token in ~/.claude/.credentials.json. This app never refreshes
  the token (that would rotate Claude Code's refresh token).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
APP_DIR = Path(__file__).resolve().parent
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CONTEXT_REFRESH_S = 10
USAGE_REFRESH_S = 300  # the endpoint returns 429 when polled too often
USAGE_MIN_INTERVAL_S = 60  # floor even for "Refresh now"
TAIL_BYTES = 512 * 1024
WARN_PCT, CRIT_PCT = 70, 90
NOTIFY_THRESHOLDS = (80, 95)

LIMIT_LABELS = {"session": "Session (5h)", "weekly_all": "Weekly"}
STATUS_LABELS = {"busy": "working", "idle": "idle"}

logging.basicConfig(
    filename=APP_DIR / "claude_usage_tray.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("claude_usage_tray")


# ---------------------------------------------------------------- data

@dataclass
class Limit:
    label: str
    percent: float
    resets_at: datetime | None


@dataclass
class Conversation:
    title: str
    project: str
    status: str
    used: int | None
    window: int

    @property
    def percent(self) -> float | None:
        return None if self.used is None else 100 * self.used / self.window


def context_window(model: str) -> int:
    return 200_000 if "haiku" in model.lower() else 1_000_000


def pid_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def usage_tokens(entry: dict) -> tuple[int, str] | None:
    msg = entry.get("message") or {}
    usage = msg.get("usage")
    if entry.get("type") != "assistant" or entry.get("isSidechain") or not usage:
        return None
    tokens = sum(
        usage.get(k) or 0
        for k in ("input_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens", "output_tokens")
    )
    return tokens, msg.get("model", "")


def scan_transcript(transcript: Path, known_title: str | None):
    """Last main-agent `usage` and most recent title (user rename > AI-generated).

    Reads only the tail of the file; falls back to the whole file only if something is missing.
    """
    size = transcript.stat().st_size
    usage = custom = ai = None
    for start in (max(0, size - TAIL_BYTES), 0):
        with transcript.open("rb") as f:
            f.seek(start)
            lines = f.read().splitlines()
        for raw in reversed(lines):
            wants_usage = usage is None and b'"usage"' in raw
            wants_title = (custom is None and b'"custom-title"' in raw) or (
                ai is None and b'"ai-title"' in raw)
            if not (wants_usage or wants_title):
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue  # line cut at the start of the tail chunk
            kind = entry.get("type")
            if kind == "custom-title" and custom is None:
                custom = entry.get("customTitle") or entry.get("title")
            elif kind == "ai-title" and ai is None:
                ai = entry.get("aiTitle")
            elif wants_usage:
                usage = usage_tokens(entry)
            if usage and custom:
                break
        if start == 0 or (usage and (custom or ai or known_title)):
            break
    return usage, custom or ai


class ConversationReader:
    def __init__(self) -> None:
        self.titles: dict[str, str] = {}

    def read(self) -> list[Conversation]:
        result = []
        for meta_file in (CLAUDE_DIR / "sessions").glob("*.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if not pid_alive(int(meta["pid"])):
                    continue
                session_id = meta["sessionId"]
                transcript = next((CLAUDE_DIR / "projects").glob(f"*/{session_id}.jsonl"), None)
                usage, title = (scan_transcript(transcript, self.titles.get(session_id))
                                if transcript else (None, None))
                if title:
                    self.titles[session_id] = title
                if meta.get("nameSource") not in (None, "derived") and meta.get("name"):
                    title = meta["name"]  # name set by the user inside Claude Code
                used, model = usage if usage else (None, "")
                cwd = meta.get("cwd", "")
                result.append(Conversation(
                    title=title or self.titles.get(session_id) or meta.get("name") or session_id[:8],
                    project=Path(cwd).name if cwd else "?",
                    status=meta.get("status", ""),
                    used=used,
                    window=context_window(model),
                ))
            except Exception:
                log.exception("failed reading %s", meta_file.name)
        return sorted(result, key=lambda c: c.percent or 0, reverse=True)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def fetch_limits() -> list[Limit]:
    creds = json.loads((CLAUDE_DIR / ".credentials.json").read_text(encoding="utf-8"))
    token = creds["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "claude-usage-tray/1.0",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    limits = [
        Limit(LIMIT_LABELS.get(item.get("kind"), item.get("kind", "?")),
              float(item.get("percent") or 0), parse_time(item.get("resets_at")))
        for item in data.get("limits") or []
    ]
    if limits:
        return limits
    # older response shape
    for key, label in (("five_hour", LIMIT_LABELS["session"]), ("seven_day", LIMIT_LABELS["weekly_all"])):
        block = data.get(key)
        if block:
            limits.append(Limit(label, float(block.get("utilization") or 0),
                                parse_time(block.get("resets_at"))))
    return limits


# ---------------------------------------------------------------- presentation

def fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.3g}M" if n >= 1_000_000 else f"{round(n / 1000)}k"


def fmt_reset(when: datetime | None) -> str:
    if not when:
        return ""
    if when.date() == datetime.now().astimezone().date():
        return f"resets {when:%H:%M}"
    return f"resets {when:%a %b %d, %H:%M}"


def color_for(pct: float) -> tuple[int, int, int]:
    if pct >= CRIT_PCT:
        return (220, 53, 69)
    if pct >= WARN_PCT:
        return (240, 173, 78)
    return (40, 167, 69)


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def load_font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_icon(number: str, rgb: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, 63, 63), radius=14, fill=rgb)
    font = load_font(40 if len(number) <= 2 else 30)
    draw.text((32, 33), number, font=font, fill="white", anchor="mm")
    return img


def work_area() -> tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class DetailsPanel:
    """Left-click panel. All Tk calls happen on its own thread."""

    BG, FG, MUTED, TRACK, BORDER = "#202020", "#f3f3f3", "#a8a8a8", "#3a3a3a", "#454545"

    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.requests: queue.Queue[str] = queue.Queue()
        self.hidden_at = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def toggle(self) -> None:
        self.requests.put("toggle")

    def refresh(self) -> None:
        self.requests.put("refresh")

    # -- Tk thread
    def _run(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self.BORDER)
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.root.bind("<FocusOut>", lambda e: self._hide())
        self.root.bind("<Escape>", lambda e: self._hide())
        self.root.after(100, self._poll)
        self.root.mainloop()

    def _poll(self) -> None:
        try:
            while True:
                req = self.requests.get_nowait()
                visible = self.root.state() == "normal"
                if req == "toggle":
                    # clicking the tray icon steals focus from the panel before we get here
                    if visible or time.monotonic() - self.hidden_at < 0.4:
                        self._hide()
                    else:
                        self._show()
                elif req == "refresh" and visible:
                    self._build()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _hide(self) -> None:
        if self.root.state() == "normal":
            self.root.withdraw()
            self.hidden_at = time.monotonic()

    def _show(self) -> None:
        self._build()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _px(self, n: float) -> int:
        return round(n * self.scale)

    def _bar(self, parent, pct: float | None) -> None:
        width, height = self._px(340), self._px(6)
        canvas = tk.Canvas(parent, width=width, height=height, bg=self.BG, highlightthickness=0)
        canvas.create_rectangle(0, 0, width, height, fill=self.TRACK, width=0)
        if pct:
            fill = max(2, round(width * min(pct, 100) / 100))
            canvas.create_rectangle(0, 0, fill, height, fill=hex_color(color_for(pct)), width=0)
        canvas.pack(anchor="w", pady=(self._px(3), 0))

    def _label(self, parent, text: str, *, bold=False, muted=False, size=9, **pack) -> None:
        tk.Label(parent, text=text, bg=self.BG, fg=self.MUTED if muted else self.FG,
                 font=("Segoe UI", size, "bold" if bold else "normal"),
                 anchor="w", justify="left").pack(fill="x", **pack)

    def _row(self, parent, left: str, right: str, pct: float | None, sub: str = "") -> None:
        row = tk.Frame(parent, bg=self.BG)
        row.pack(fill="x", pady=(self._px(8), 0))
        top = tk.Frame(row, bg=self.BG)
        top.pack(fill="x")
        tk.Label(top, text=left, bg=self.BG, fg=self.FG, font=("Segoe UI", 10, "bold"),
                 anchor="w").pack(side="left")
        tk.Label(top, text=right, bg=self.BG, fg=self.FG, font=("Segoe UI", 10),
                 anchor="e").pack(side="right")
        if sub:
            self._label(row, sub, muted=True)
        self._bar(row, pct)

    def _build(self) -> None:
        limits, convs, error, updated = self.snapshot()
        for child in self.root.winfo_children():
            child.destroy()
        body = tk.Frame(self.root, bg=self.BG, padx=self._px(16), pady=self._px(14))
        body.pack(padx=1, pady=1)

        self._label(body, "Claude Code", bold=True, size=12)
        self._label(body, f"updated {updated:%H:%M:%S}" if updated else "loading…", muted=True)

        self._label(body, "PLAN USAGE", muted=True, size=8, pady=(self._px(12), 0))
        for lim in limits:
            self._row(body, lim.label, f"{lim.percent:.0f}%", lim.percent, fmt_reset(lim.resets_at))
        if error:
            self._label(body, f"⚠ {error}", size=9, pady=(self._px(6), 0))

        self._label(body, "ACTIVE CONVERSATIONS", muted=True, size=8, pady=(self._px(16), 0))
        if not convs:
            self._label(body, "none", muted=True, pady=(self._px(6), 0))
        for c in convs:
            status = STATUS_LABELS.get(c.status, c.status)
            ctx = (f"{fmt_tokens(c.used)} / {fmt_tokens(c.window)}  ({c.percent:.0f}%)"
                   if c.used is not None else "no data yet")
            self._row(body, shorten(c.title, 34), ctx, c.percent, f"{c.project} · {status}")

        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        _, _, right, bottom = work_area()
        margin = self._px(12)
        self.root.geometry(f"{w}x{h}+{right - w - margin}+{bottom - h - margin}")


class TrayApp:
    def __init__(self) -> None:
        self.limits: list[Limit] = []
        self.usage_error: str | None = None
        self.conversations: list[Conversation] = []
        self.updated: datetime | None = None
        self.reader = ConversationReader()
        self.notified: dict[str, int] = {}
        self.backoff_until = 0.0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.panel = DetailsPanel(self.snapshot)
        self.icon = pystray.Icon(
            "claude-usage-tray", draw_icon("…", (108, 117, 125)), "Claude Code",
            menu=pystray.Menu(
                pystray.MenuItem("Details", lambda: self.panel.toggle(), default=True),
                pystray.MenuItem("Refresh now", lambda: self.wake.set()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda icon: icon.stop()),
            ))

    def snapshot(self):
        with self.lock:
            return list(self.limits), list(self.conversations), self.usage_error, self.updated

    # -- polling
    def refresh_usage(self) -> float:
        """Refreshes plan usage; returns how many seconds until the next query."""
        retry_in = USAGE_REFRESH_S
        try:
            limits, error = fetch_limits(), None
        except urllib.error.HTTPError as e:
            limits = []
            if e.code == 429:
                try:
                    retry_in = max(USAGE_MIN_INTERVAL_S, float(e.headers.get("Retry-After", "")))
                except ValueError:
                    pass
                self.backoff_until = time.monotonic() + retry_in
                error = f"rate limited; retrying at {datetime.fromtimestamp(time.time() + retry_in):%H:%M}"
            elif e.code == 401:
                error = "token expired: open Claude Code"
            else:
                error = f"HTTP error {e.code}"
            log.warning("usage: HTTP %s, next try in %.0fs", e.code, retry_in)
        except Exception as e:
            limits, error = [], "no connection/credentials"
            log.exception("usage: %s", e)
        with self.lock:
            if not error or not self.limits:
                self.limits = limits
            self.usage_error = error
        self.maybe_notify(limits)
        return retry_in

    def refresh_context(self) -> None:
        convs = self.reader.read()
        with self.lock:
            self.conversations = convs
            self.updated = datetime.now()

    def loop(self) -> None:
        last_usage_at, next_usage_at = -USAGE_MIN_INTERVAL_S, 0.0
        while True:
            now = time.monotonic()
            forced = (self.wake.is_set() and now - last_usage_at >= USAGE_MIN_INTERVAL_S
                      and now >= self.backoff_until)
            self.wake.clear()
            if forced or now >= next_usage_at:
                next_usage_at = now + self.refresh_usage()
                last_usage_at = now
            self.refresh_context()
            self.render()
            self.wake.wait(CONTEXT_REFRESH_S)

    def maybe_notify(self, limits: list[Limit]) -> None:
        for lim in limits:
            crossed = max((t for t in NOTIFY_THRESHOLDS if lim.percent >= t), default=0)
            previous = self.notified.get(lim.label, 0)
            if crossed > previous:
                self.icon.notify(f"{lim.label}: {lim.percent:.0f}% used. {fmt_reset(lim.resets_at)}",
                                 "Claude Code: plan limit")
            self.notified[lim.label] = crossed

    # -- tray icon
    def render(self) -> None:
        limits, convs, error, _ = self.snapshot()
        session = next((l for l in limits if l.label == LIMIT_LABELS["session"]), None)
        pcts = [l.percent for l in limits] + [c.percent for c in convs if c.percent is not None]
        number = f"{session.percent:.0f}" if session else "?"
        self.icon.icon = draw_icon(number, color_for(max(pcts)) if pcts else (108, 117, 125))

        parts = [f"{l.label.split(' ')[0]} {l.percent:.0f}%" for l in limits]
        ctx = [c.percent for c in convs if c.percent is not None]
        parts.append(f"{len(convs)} conversation(s), max context {max(ctx):.0f}%" if ctx
                     else f"{len(convs)} conversation(s)")
        if error:
            parts.append(error)
        self.icon.title = ("Claude Code: " + " · ".join(parts))[:127]
        self.panel.refresh()

    def run(self) -> None:
        def setup(icon):
            icon.visible = True
            threading.Thread(target=self.loop, daemon=True).start()
        self.icon.run(setup=setup)


def already_running() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\claude-usage-tray-singleton")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if already_running():
        sys.exit(0)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp panel on scaled displays
    except Exception:
        pass
    sys.excepthook = lambda *exc: log.critical("fatal error", exc_info=exc)
    threading.excepthook = lambda args: log.critical(
        "error in thread %s", args.thread, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    log.info("started")
    try:
        TrayApp().run()
    finally:
        log.info("stopped")
