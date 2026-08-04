#!/usr/bin/env python3
"""
claudeink - Claude usage limits on an Inky pHAT.

Mirrors the layout of Claude Code's own /usage report: a session window and two
weekly windows (all models, Fable), each with a pill progress bar and its reset
time. Refreshes on the minute so the clock and countdown stay live.

Env config (all optional):
  REFRESH_MINUTES    minutes between panel refreshes, default 1
  CREDENTIALS        path to credentials json, default ~/.claude/.credentials.json
  PLAN_LABEL         text beside the header title, e.g. "Max 5x"
  FONT_REGULAR       .ttf for body text, default DejaVuSans
  FONT_BOLD          .ttf for labels, default DejaVuSans-Bold
  FLIP               set to 1 to rotate 180 degrees
  WARN_AT            utilisation % at which bars switch to the accent colour, default 80
  QUIET_START        hour (0-23) to stop refreshing overnight, e.g. 23
  QUIET_END          hour (0-23) to resume, e.g. 7
  PARTIAL_REFRESH    set to 1 for flash-free partial updates (Waveshare V4/V3)
  FULL_REFRESH_MINUTES  minutes between ghost-clearing full refreshes when
                     partial refresh is on, default 60

Flags:
  --demo   run with synthetic data, no network, no credentials
  --once   render a single frame and exit
  --png    also write the frame to frame.png (handy over ssh)
"""

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

REFRESH_MINUTES = max(1, int(os.environ.get("REFRESH_MINUTES", "1")))
WARN_AT = float(os.environ.get("WARN_AT", "80"))
FLIP = os.environ.get("FLIP", "0") == "1"
PLAN_LABEL = os.environ.get("PLAN_LABEL", "")
PARTIAL_REFRESH = os.environ.get("PARTIAL_REFRESH", "0") == "1"
FULL_REFRESH_MINUTES = max(1, int(os.environ.get("FULL_REFRESH_MINUTES", "60")))

QUIET_START = os.environ.get("QUIET_START")
QUIET_END = os.environ.get("QUIET_END")

CREDENTIALS = Path(
    os.environ.get("CREDENTIALS", str(Path.home() / ".claude" / ".credentials.json"))
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")

BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "claudeink/2.0"

REGULAR_CANDIDATES = [
    os.environ.get("FONT_REGULAR", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
BOLD_CANDIDATES = [
    os.environ.get("FONT_BOLD", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# label, candidate API keys (tried in order), is this a weekly window
ROWS = [
    ("Current session", ["five_hour"], False),
    ("All models", ["seven_day"], True),
    ("Fable", ["seven_day_fable", "seven_day_opus"], True),
]


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------
# credentials + api
# --------------------------------------------------------------------------


def load_credentials():
    with CREDENTIALS.open() as fh:
        raw = json.load(fh)
    return raw.get("claudeAiOauth", raw)


def save_credentials(creds):
    try:
        with CREDENTIALS.open() as fh:
            raw = json.load(fh)
    except Exception:
        raw = {}
    if "claudeAiOauth" in raw:
        raw["claudeAiOauth"] = creds
    else:
        raw = creds
    tmp = CREDENTIALS.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(raw, fh)
    os.chmod(tmp, 0o600)
    tmp.replace(CREDENTIALS)


def token_expired(creds, skew=300):
    exp = creds.get("expiresAt")
    if not exp:
        return False
    return (exp / 1000.0) - skew <= time.time()


def refresh_token(creds):
    """Exchange the refresh token for a new access token.

    Claude Code isn't running on the Pi to do this for us, so without it the
    token dies within hours.
    """
    refresh = creds.get("refreshToken")
    if not refresh:
        raise RuntimeError("no refreshToken in credentials")

    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": CLIENT_ID,
        }
    ).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            # The server rotates the refresh token on every exchange, so a flat
            # rejection nearly always means another client refreshed this one
            # first. Sharing one credentials file between the Pi and a machine
            # running Claude Code does exactly that, in both directions.
            log(
                "refresh rejected (%d): this token was probably already used by "
                "another Claude Code client. The Pi needs its own login, see README."
                % exc.code
            )
        raise

    creds["accessToken"] = payload["access_token"]
    if payload.get("refresh_token"):
        creds["refreshToken"] = payload["refresh_token"]
    if payload.get("expires_in"):
        creds["expiresAt"] = int((time.time() + payload["expires_in"]) * 1000)
    save_credentials(creds)
    log("refreshed access token")
    return creds


def fetch_usage(creds):
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + creds["accessToken"],
            "anthropic-beta": BETA_HEADER,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_reset(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_window(payload, keys):
    """Locate a usage window, tolerating key renames and nesting."""
    for key in keys:
        if isinstance(payload.get(key), dict):
            return payload[key]
    stem = keys[0].split("_")[-1]
    for key, value in payload.items():
        if isinstance(value, dict) and stem in key:
            return value
    return None


PREFER_SCOPED = os.environ.get("PREFER_SCOPED", "Fable")


def rows_from_limits(payload):
    """Read the `limits` array — where the per-model windows actually live."""
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return None

    session, weekly_all, scoped = None, None, []
    for item in limits:
        if not isinstance(item, dict):
            continue

        pct = item.get("percent", item.get("utilization"))
        try:
            pct = max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            pct = None
        reset = parse_reset(item.get("resets_at"))
        kind = item.get("kind")

        if kind == "session":
            session = ("Current session", pct, reset, False)
        elif kind == "weekly_all":
            weekly_all = ("All models", pct, reset, True)
        elif kind == "weekly_scoped":
            scope = item.get("scope") or {}
            model = scope.get("model") or {}
            scoped.append((model.get("display_name") or "Scoped", pct, reset, True))

    # preferred model first, in case more than one scoped window appears
    scoped.sort(key=lambda r: r[0].lower() != PREFER_SCOPED.lower())
    rows = [r for r in (session, weekly_all) if r] + scoped
    return rows[:3] or None


def extract(payload):
    """-> [(label, pct or None, reset datetime or None, weekly), ...]"""
    rows = rows_from_limits(payload)
    if rows:
        return rows

    # fallback: the old top-level keys, in case `limits` goes away again
    out = []
    for label, keys, weekly in ROWS:
        window = find_window(payload, keys) or {}
        pct = window.get("utilization", window.get("utilisation"))
        try:
            pct = max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            pct = None
        reset = parse_reset(window.get("resets_at") or window.get("resetsAt"))
        out.append((label, pct, reset, weekly))
    return out


def all_limits(payload):
    """Every window in the payload, for the web ui and history.

    The panel only has room for three rows; the web ui shows whatever
    the API reports, including any extra per-model scoped windows.
    """
    out = []
    limits = payload.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            pct = item.get("percent", item.get("utilization"))
            try:
                pct = max(0.0, min(100.0, float(pct)))
            except (TypeError, ValueError):
                pct = None
            reset = parse_reset(item.get("resets_at"))
            kind = item.get("kind") or ""
            scope = item.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            if kind == "session":
                label = "Current session"
            elif kind == "weekly_all":
                label = "All models"
            elif kind == "weekly_scoped":
                label = model or "Scoped"
            else:
                label = model or kind or "Window"
            out.append(
                {
                    "label": label,
                    "kind": kind,
                    "percent": pct,
                    "resets_at": reset.isoformat() if reset else None,
                    "weekly": kind.startswith("weekly"),
                }
            )
    if not out:
        for label, pct, reset, weekly in extract(payload):
            out.append(
                {
                    "label": label,
                    "kind": "",
                    "percent": pct,
                    "resets_at": reset.isoformat() if reset else None,
                    "weekly": weekly,
                }
            )
    return out


def reset_text(reset, weekly):
    """Match the wording of the native report."""
    if reset is None:
        return "No reset data"
    local = reset.astimezone()
    if weekly:
        return "Resets %s" % local.strftime("%a %H:%M")
    delta = (reset - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "Resetting now"
    hours, mins = int(delta // 3600), int((delta % 3600) // 60)
    if hours:
        return "Resets in %d hr %d min" % (hours, mins)
    return "Resets in %d min" % max(1, mins)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

_FONT_CACHE = {}


def load_font(candidates, size):
    key = (tuple(candidates), size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = ImageFont.load_default()
    for path in candidates:
        if path and Path(path).exists():
            try:
                font = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    _FONT_CACHE[key] = font
    return font


def width_of(draw, text, font):
    return draw.textbbox((0, 0), text, font=font)[2]


def fit_font(draw, text, candidates, size, max_width, floor=7):
    """Shrink until the text fits the column."""
    while size > floor:
        font = load_font(candidates, size)
        if width_of(draw, text, font) <= max_width:
            return font
        size -= 1
    return load_font(candidates, floor)


def pill(draw, box, radius, outline=None, fill=None, width=1):
    try:
        draw.rounded_rectangle(box, radius=radius, outline=outline, fill=fill, width=width)
    except AttributeError:  # Pillow < 8.2
        draw.rectangle(box, outline=outline, fill=fill, width=width)


def render(size, rows, colours, stale=False):
    """Draw the frame. colours is (white, black, accent) palette indices."""
    width, height = size
    white, black, accent = colours
    pad = 3

    img = Image.new("P", size, white)
    draw = ImageDraw.Draw(img)

    s_title = max(10, int(height * 0.115))
    s_label = max(9, int(height * 0.098))
    s_sub = max(7, int(height * 0.080))
    s_section = max(7, int(height * 0.072))

    # ---- header -----------------------------------------------------------
    header_h = s_title + 7
    f_title = load_font(BOLD_CANDIDATES, s_title)
    f_plan = load_font(REGULAR_CANDIDATES, s_sub)

    draw.text((pad, 1), "Plan usage", black, font=f_title)
    if PLAN_LABEL:
        x = pad + width_of(draw, "Plan usage", f_title) + 4
        draw.text((x, 1 + s_title - s_sub), PLAN_LABEL, black, font=f_plan)

    clock = datetime.now().strftime("%H:%M")
    if stale:
        clock = "! " + clock
    f_clock = load_font(BOLD_CANDIDATES, s_label)
    draw.text(
        (width - pad - width_of(draw, clock, f_clock), 2), clock, black, font=f_clock
    )
    draw.line((0, header_h - 2, width, header_h - 2), fill=black, width=1)

    # ---- layout -----------------------------------------------------------
    section_h = s_section + 4
    body_top = header_h + 1
    body_h = height - body_top
    row_h = (body_h - section_h) // len(rows)

    label_col = int(width * 0.42)
    pct_col = int(width * 0.15)
    bar_x0 = pad + label_col
    bar_x1 = width - pad - pct_col - 3

    y = body_top
    for index, (label, pct, reset, weekly) in enumerate(rows):
        if weekly and index and not rows[index - 1][3]:
            # "Weekly limits" divider, as in the native report
            f_section = load_font(BOLD_CANDIDATES, s_section)
            draw.text((pad, y + 1), "WEEKLY", black, font=f_section)
            line_x = pad + width_of(draw, "WEEKLY", f_section) + 4
            mid = y + 1 + s_section // 2
            draw.line((line_x, mid, width - pad, mid), fill=black, width=1)
            y += section_h

        f_label = fit_font(draw, label, BOLD_CANDIDATES, s_label, label_col - 2)
        sub = reset_text(reset, weekly)
        f_sub = fit_font(draw, sub, REGULAR_CANDIDATES, s_sub, label_col - 2)

        text_h = s_label + 1 + s_sub
        text_top = y + max(0, (row_h - text_h) // 2) - 1
        draw.text((pad, text_top), label, black, font=f_label)
        draw.text((pad, text_top + s_label + 1), sub, black, font=f_sub)

        # bar
        bar_h = max(7, int(row_h * 0.30))
        bar_top = y + (row_h - bar_h) // 2
        radius = bar_h // 2
        pill(draw, (bar_x0, bar_top, bar_x1, bar_top + bar_h), radius, outline=black)

        if pct:
            span = (bar_x1 - bar_x0) - 2
            filled = int(round(span * pct / 100.0))
            if filled >= 2:
                fill = accent if pct >= WARN_AT else black
                pill(
                    draw,
                    (bar_x0 + 1, bar_top + 1, bar_x0 + 1 + filled, bar_top + bar_h - 1),
                    max(1, radius - 1),
                    fill=fill,
                )

        # percentage
        text = "--" if pct is None else "%d%%" % round(pct)
        f_pct = load_font(BOLD_CANDIDATES, s_label)
        draw.text(
            (width - pad - width_of(draw, text, f_pct), bar_top + bar_h // 2 - s_label // 2 - 1),
            text,
            black,
            font=f_pct,
        )

        y += row_h

    if FLIP:
        img = img.rotate(180)
    return img


# --------------------------------------------------------------------------
# display
# --------------------------------------------------------------------------


class Panel:
    def __init__(self):
        from inky.auto import auto

        self.inky = auto()
        self.size = tuple(self.inky.resolution)
        self.colours = (self.inky.WHITE, self.inky.BLACK, self.inky.RED)

    def show(self, img):
        self.inky.set_image(img)
        self.inky.show()

    def rest(self):
        pass


class WavesharePanel:
    """Waveshare 2.13" 122x250 two-colour panel (V4/V3/V2 SSD1680 family).

    The panel has no red, so the accent palette index renders as black.

    With PARTIAL_REFRESH=1 (V4/V3 only) routine updates use the flash-free
    partial waveform, with a flashing full refresh every
    FULL_REFRESH_MINUTES to clear accumulated ghosting. Partial refresh
    needs the controller's image RAM, so the panel then stays awake
    between updates; rest() deep-sleeps it for quiet hours, and the next
    update re-primes it with a full refresh.
    """

    size = (250, 122)
    colours = (0, 1, 2)

    def __init__(self):
        last_exc = None
        for module in ("epd2in13_V4", "epd2in13_V3", "epd2in13_V2"):
            try:
                lib = __import__("waveshare_epd." + module, fromlist=[module])
                self.epd = lib.EPD()
                self.epd.init()
                self.epd.Clear(0xFF)
                self.epd.sleep()
                self.partial = (
                    PARTIAL_REFRESH
                    and hasattr(self.epd, "displayPartial")
                    and hasattr(self.epd, "displayPartBaseImage")
                )
                self.awake = False
                self.last_full = 0.0
                log(
                    "using waveshare driver %s%s"
                    % (
                        module,
                        ", partial refresh on (full every %d min)"
                        % FULL_REFRESH_MINUTES
                        if self.partial
                        else "",
                    )
                )
                return
            except Exception as exc:
                last_exc = exc
        raise RuntimeError("no waveshare 2.13 panel found: %s" % last_exc)

    def show(self, img):
        # palette indices -> bilevel: 0 white, 1 black, 2 accent (black here)
        bw = img.point(lambda p: 255 if p == 0 else 0, mode="1")
        buf = self.epd.getbuffer(bw)

        if (
            self.partial
            and self.awake
            and time.time() - self.last_full < FULL_REFRESH_MINUTES * 60
        ):
            self.epd.displayPartial(buf)
            return

        self.epd.init()
        self.last_full = time.time()
        if self.partial:
            # flashing refresh that also primes the RAM for partials
            self.epd.displayPartBaseImage(buf)
            self.awake = True
        else:
            self.epd.display(buf)
            self.epd.sleep()

    def rest(self):
        """Quiet-hours deep sleep for partial mode (full mode sleeps
        after every update anyway)."""
        if getattr(self, "awake", False):
            self.epd.sleep()
            self.awake = False


def make_panel():
    try:
        return Panel()
    except Exception as exc:
        log("no inky detected (%s), trying waveshare" % exc)
    return WavesharePanel()


class NullPanel:
    """Used with --demo / --png when there's no hardware attached."""

    size = (250, 122)
    colours = (0, 1, 2)

    def show(self, img):
        pass

    def rest(self):
        pass


PALETTE = {0: (255, 255, 255), 1: (0, 0, 0), 2: (220, 40, 40)}


def to_rgb(img):
    out = Image.new("RGB", img.size)
    out.putdata([PALETTE.get(p, (255, 255, 255)) for p in list(img.getdata())])
    return out


def png_bytes(img):
    buf = io.BytesIO()
    to_rgb(img).save(buf, "PNG")
    return buf.getvalue()


def write_png(img, path="frame.png"):
    to_rgb(img).save(path)
    return path


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def in_quiet_hours(now=None):
    if QUIET_START is None or QUIET_END is None:
        return False
    hour = (now or datetime.now()).hour
    start, end = int(QUIET_START), int(QUIET_END)
    return start <= hour < end if start < end else (hour >= start or hour < end)


def wait_for_tick(event=None):
    """Wake just after the next refresh boundary so the clock stays honest.

    Returns True when woken early by the web ui's refresh button.
    """
    now = time.time()
    period = REFRESH_MINUTES * 60
    delay = max(1.0, period - (now % period) + 0.5)
    if event is None:
        time.sleep(delay)
        return False
    if event.wait(timeout=delay):
        event.clear()
        return True
    return False


def demo_rows():
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    return [
        ("Current session", 55.0, now + timedelta(hours=2, minutes=19), False),
        ("All models", 33.0, now + timedelta(days=4, hours=6), True),
        ("Fable", 45.0, now + timedelta(days=4, hours=6, minutes=1), True),
    ]


def main():
    args = sys.argv[1:]
    demo = "--demo" in args
    once = "--once" in args
    png = "--png" in args

    try:
        panel = NullPanel() if demo else make_panel()
    except Exception as exc:
        log("no display detected (%s), falling back to png output" % exc)
        panel = NullPanel()
        png = True

    web_state = None
    if WEB_UI and WEB_PORT and not once:
        try:
            import history
            import web

            web_state = web.start(
                WEB_PORT,
                history_query=history.query,
                meta={
                    "plan": PLAN_LABEL,
                    "warn_at": WARN_AT,
                    "refresh_minutes": REFRESH_MINUTES,
                },
            )
            log("web ui on port %d" % WEB_PORT)
        except Exception as exc:
            log("web ui disabled (%s)" % exc)

    creds = None if demo else load_credentials()
    last_rows = None
    last_limits = None
    payload = None
    backoff = 0.0
    forced = False

    while True:
        stale = False

        if demo:
            rows = demo_rows()
            last_limits = [
                {
                    "label": label,
                    "kind": "",
                    "percent": pct,
                    "resets_at": reset.isoformat() if reset else None,
                    "weekly": weekly,
                }
                for label, pct, reset, weekly in rows
            ]
        elif time.time() < backoff and not forced:
            rows, stale = last_rows, True
        else:
            try:
                if token_expired(creds):
                    creds = refresh_token(creds)
                payload = fetch_usage(creds)
                rows = extract(payload)
                last_limits = all_limits(payload)
                if web_state:
                    import history

                    history.append(last_limits)
                backoff = 0.0
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    backoff = time.time() + 300
                    log("429 from usage api, backing off 5 min")
                elif exc.code in (401, 403):
                    log("auth rejected (%d), attempting refresh" % exc.code)
                    try:
                        creds = refresh_token(creds)
                    except Exception as inner:
                        log("refresh failed: %s" % inner)
                        backoff = time.time() + 600
                else:
                    log("http %d from usage api" % exc.code)
                    backoff = time.time() + 120
                rows, stale = last_rows, True
            except Exception as exc:
                log("poll failed: %s" % exc)
                backoff = time.time() + 120
                rows, stale = last_rows, True

        quiet = in_quiet_hours()
        if quiet and not once:
            panel.rest()

        if rows is not None:
            last_rows = rows
            if once or not quiet:
                img = render(panel.size, rows, panel.colours, stale=stale)
                panel.show(img)
                if png:
                    log("wrote " + write_png(img))
                log(
                    "  ".join(
                        "%s %s" % (l, "--" if p is None else "%d%%" % round(p))
                        for l, p, _, _ in rows
                    )
                )

        if once:
            return
        forced = wait_for_tick(web_state.refresh_event if web_state else None)
        if forced:
            log("refresh requested via web ui")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
