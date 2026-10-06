#!/usr/bin/env python3
"""Daily news stories + optional on-air lines for Chris Farrell.

Runs in the cloud (GitHub Actions) at 7 AM UK every day. It:
  1. Pulls today's headlines from a spread of UK news feeds (BBC, Sky, Guardian).
  2. Asks an LLM to pick FIVE stories and write, for each, a short conversational
     summary plus 2-3 OPTIONAL lines Chris could actually say on air.
  3. Builds the header/frame in code (so the format can never drift).
  4. DMs it to Chris on Telegram and emails him a copy.

Nothing is invented: the model only sees the fetched headline text and is told to
use nothing else. Purely a helper for a presenter - no facts, no lines, make air
that are not in the source material.
"""
import base64
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formatdate

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("NEWS_MODEL", "deepseek/deepseek-v4-flash-0731")

MARKER_PATH = "state/last_sent.txt"
TARGET_HOUR = 7          # 7 AM UK
WINDOW_END_HOUR = 12     # accept any run landing 07:00-12:59 UK
STORIES = 5

# A spread of UK feeds. Each is tried independently; a dead feed is skipped,
# never fatal. London feed gives the local (Chris is Radio Jackie, SW London).
NEWS_FEEDS = [
    ("BBC News", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("BBC UK", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
    ("BBC London", "https://feeds.bbci.co.uk/news/england/london/rss.xml"),
    ("BBC Politics", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
    ("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
    ("Sky News UK", "https://feeds.skynews.com/feeds/rss/uk.xml"),
    ("Sky News Strange", "https://feeds.skynews.com/feeds/rss/strange.xml"),
    ("Sky News Entertainment", "https://feeds.skynews.com/feeds/rss/entertainment.xml"),
    ("Guardian UK", "https://www.theguardian.com/uk/rss"),
    ("Guardian Sport", "https://www.theguardian.com/uk/sport/rss"),
]

# Rotating openers, picked by calendar day so every morning starts slightly
# differently. Keep in code - never let the model write this line.
OPENERS = [
    "Morning, Chris \U0001F44B",
    "Morning, Chris \u2600\uFE0F",
    "Right then, Chris \U0001F3A7",
    "Morning, chief \U0001F44B",
    "Morning, Chris \u2615",
    "Hello, Chris \U0001F3A4",
    "Morning, Chris \U0001F4F0",
]
SUBLINE = "Five things doing the rounds today - and a few lines you could use."

FRAME_TOP = "=" * 4
FRAME_BOTTOM = "=" * 4
RULE = "=" * 44
DIVIDER = "-" * 44
CLOSER = "That's your five. Take what you like, bin the rest."

# Hard content rules the model keeps ignoring - enforce in code as well.
BANNED_LINE = re.compile(r"^\s*(?:#{1,6}\s|\*\*|[-*]\s+\[|bullet:)", re.IGNORECASE)


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def http_get(url, timeout=20, attempts=3):
    """GET with retries for transient blips. 403/429 retried, other 4xx fatal."""
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": "daily-radio-news/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429) and 400 <= e.code < 500:
                return None  # permanent for this feed; caller skips it
            last = e
        except Exception as e:
            last = e
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    log(f"  GET failed after {attempts} attempts: {url} ({last})")
    return None


# --------------------------------------------------------------------------
# News collection
# --------------------------------------------------------------------------
def _text(node):
    return (node.text or "").strip() if node is not None else ""


def fetch_feed(name, url, per_feed=12):
    """Return a list of {source,title,summary,link} from one RSS feed."""
    raw = http_get(url)
    if not raw:
        log(f"  feed skipped (no response): {name}")
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"  feed skipped (bad xml): {name} ({e})")
        return []
    items = root.findall(".//item") or root.findall(
        ".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for it in items[:per_feed]:
        title = _text(it.find("title")) or _text(it.find("{http://www.w3.org/2005/Atom}title"))
        if not title:
            continue
        desc = (_text(it.find("description"))
                or _text(it.find("{http://www.w3.org/2005/Atom}summary")))
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = html.unescape(re.sub(r"\s+", " ", desc)).strip()
        link = _text(it.find("link")) or ""
        if not link:
            ln = it.find("{http://www.w3.org/2005/Atom}link")
            link = ln.get("href") if ln is not None else ""
        out.append({"source": name, "title": html.unescape(title).strip(),
                    "summary": desc[:220], "link": link})
    log(f"  {name}: {len(out)} items")
    return out


def get_headlines():
    """All feeds merged round-robin (so the first N span every source, not just
    the first feed or two), de-duplicated by title."""
    per_feed = []
    for name, url in NEWS_FEEDS:
        per_feed.append(fetch_feed(name, url))
    seen, merged = set(), []
    for i in range(max((len(f) for f in per_feed), default=0)):
        for feed in per_feed:
            if i >= len(feed):
                continue
            item = feed[i]
            key = re.sub(r"\W+", "", item["title"].lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def headlines_blob(items, limit=40):
    lines = []
    for i, it in enumerate(items[:limit], 1):
        s = f"{i}. [{it['source']}] {it['title']}"
        if it["summary"]:
            s += f" - {it['summary']}"
        lines.append(s)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------
PROMPT = """You write daily news notes for Chris Farrell, a UK radio presenter \
(Radio Jackie, South West London, and Greatest Hits Radio 80s). His style: \
REALLY AUTHENTIC, REALLY CONVERSATIONAL, warm, understated, dry-witted. Sounds \
like a mate chatting, never like a newsreader or a press release.

Below are today's real headlines. Pick the FIVE best stories for a presenter to \
talk about on air today, and write them up.

HARD RULES:
- Use ONLY the facts in the headlines below. Never invent names, numbers, \
quotes or details. If a headline is thin, keep the write-up thin.
- AT MOST ONE story about a death or obituary. One is fine if it is a genuine \
talking point; two out of five makes the whole bulletin gloomy.
- No two stories from the same area. Cover five DIFFERENT areas (e.g. UK news, \
politics, showbiz/music/TV, sport, quirky or heartwarming).
- Skip anything gratuitously grim: deaths of private individuals, court cases \
involving children, graphic violence, sexual offences, suicide.
- No fake enthusiasm, no cheesy gags. Wit over jokes. Dry over slapstick.
- NO questions to the listener, NO calls to action.
- Plain hyphens only (-). NEVER use em dashes or en dashes.
- No markdown, no bold markers, no bullet symbols, no headings like "STORY 1:".
- Keep each summary to one or two SHORT sentences.

VOICE CALIBRATION for the "~" lines. They must sound like something he would \
actually say out loud to one listener - understated, warm, specific. Good: \
"£1 million to fix a greenhouse. My mum's had a leaking conservatory since 2011 \
and she's still using a bucket." Bad (too writerly, too jokey): "Hold onto your \
watering cans, folks!" Any line that reads like a press release or a stand-up \
punchline fails. Specific detail beats a generic gag every time.

OUTPUT FORMAT - exactly this, nothing before or after:

1. HEADLINE IN CAPITAL LETTERS
One or two short sentences saying what happened, conversationally.

~ "An optional line he could say - a dry punchline."
~ "Another angle - a relatable observation."
~ "A third option - warmer, or a colourful comparison."

---

2. NEXT HEADLINE IN CAPITALS
Summary sentence.

~ "Option one."
~ "Option two."

---

(and so on for all five)

Give 2-3 "~" option lines per story. Vary their flavour: some dry punchlines, \
some plain authentic observations, some warm or thoughtful closers, occasionally \
a colourful comparison. They are options - Chris picks one or says none. Do not \
force a joke into every one. Under each political story, prefer a neutral, \
non-partisan angle.

TODAY'S HEADLINES
=================
{headlines}
"""


def call_llm(items, attempts=3):
    """Ask the model for the body. Returns plain-text body or raises."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    blob = headlines_blob(items)
    last_err = None
    for i in range(attempts):
        payload = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": PROMPT.replace("{headlines}", blob)}],
            "temperature": 0.9,
            "max_tokens": 4000,
            # This is a reasoning model: left on, it burns the whole token budget
            # on hidden thinking and returns empty content (finish_reason=length).
            # Off, it writes the bulletin in ~20s for a fraction of a penny.
            "reasoning": {"enabled": False},
        }).encode()
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode())
            choice = d["choices"][0]
            content = (choice.get("message") or {}).get("content")
            if content and content.strip():
                return content.strip()
            last_err = f"empty content (finish_reason={choice.get('finish_reason')})"
        except Exception as e:
            last_err = str(e)
        log(f"  LLM attempt {i + 1} failed: {last_err}")
        if i < attempts - 1:
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"LLM failed after {attempts} attempts: {last_err}")


# --------------------------------------------------------------------------
# Format the message
# --------------------------------------------------------------------------
def clean_body(text):
    """Normalise spacing and enforce the hard formatting rules."""
    text = text.replace("\u2014", "-").replace("\u2013", "-")  # em/en dash -> hyphen
    out = []
    for line in text.split("\n"):
        s = line.rstrip()
        if not s.strip():
            out.append("")
            continue
        if BANNED_LINE.match(s):          # strip stray markdown/headings
            s = s.lstrip("#*- ").strip()
            if not s:
                continue
        out.append(s)
    body = "\n".join(out)
    # Collapse 2+ blank lines to exactly one blank line.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def split_stories(body):
    """Split into story blocks on the '---' separator the prompt asks for."""
    parts = re.split(r"\n\s*-{3,}\s*\n", body)
    blocks = [p.strip() for p in parts if p.strip()]
    # Fallback: if the model ignored the separator, split on the numbered headers.
    if len(blocks) <= 1:
        blocks = re.split(r"\n(?=\d+\.\s+[A-Z])", body)
        blocks = [b.strip() for b in blocks if b.strip()]
    return blocks


def build_message(body, uk_date, opener):
    blocks = split_stories(body)
    body_txt = f"\n\n{DIVIDER}\n\n".join(blocks)
    return (
        f"{FRAME_TOP}\n"
        f"{opener}\n"
        f"{SUBLINE}\n\n"
        f"\U0001F4F0 WHAT'S GOING ON TODAY\n"
        f"{uk_date}\n"
        f"{RULE}\n\n"
        f"{body_txt}\n\n"
        f"{RULE}\n"
        f"{CLOSER}\n"
        f"{FRAME_BOTTOM}"
    )


def to_telegram_html(message):
    """HTML-escape everything, then bold the numbered story headers."""
    lines = []
    for line in message.split("\n"):
        esc = html.escape(line)
        if re.match(r"^\d+\.\s+\S", line):
            esc = f"<b>{esc}</b>"
        lines.append(esc)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
def send_telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    payload = urllib.parse.urlencode({
        "chat_id": chat,
        "text": to_telegram_html(message),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=payload, timeout=30) as r:
        d = json.loads(r.read().decode())
    if not d.get("ok"):
        raise RuntimeError(f"Telegram send failed: {d}")
    return d["result"]["message_id"]


def send_email(message, uk_date):
    """Email a plain-text copy via Gmail SMTP. Skipped if creds are absent."""
    addr = os.environ.get("EMAIL_ADDRESS")
    pwd = os.environ.get("EMAIL_PASSWORD")
    to = os.environ.get("EMAIL_TO") or addr
    if not addr or not pwd:
        log("  email skipped (no EMAIL_ADDRESS / EMAIL_PASSWORD)")
        return None
    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = f"\U0001F399 Today's news + lines for radio - {uk_date}"
    msg["From"] = addr
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=45) as s:
        s.login(addr, pwd)
        s.sendmail(addr, [to], msg.as_string())
    return to


# --------------------------------------------------------------------------
# Sent-today marker (contents API) - guarantees one send per day
# --------------------------------------------------------------------------
def _gh_headers():
    h = {"Accept": "application/vnd.github+json", "User-Agent": "daily-radio-news/1.0"}
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _repo():
    return os.environ.get("GITHUB_REPOSITORY", "CGTalent/daily-radio-news")


def _read_marker():
    url = f"https://api.github.com/repos/{_repo()}/contents/{MARKER_PATH}"
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        return base64.b64decode(d.get("content") or "").decode().strip(), d.get("sha")
    except Exception as e:
        log(f"  marker read failed ({e}) - assuming not sent")
        return "", None


def _write_marker(value):
    if not os.environ.get("GITHUB_TOKEN"):
        log("  no GITHUB_TOKEN - cannot record sent marker")
        return False
    _, sha = _read_marker()
    url = f"https://api.github.com/repos/{_repo()}/contents/{MARKER_PATH}"
    body = {"message": f"Mark news sent for {value}",
            "content": base64.b64encode(value.encode()).decode()}
    if sha:
        body["sha"] = sha
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=_gh_headers(), method="PUT")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status in (200, 201)
    except Exception as e:
        log(f"  marker write failed ({e})")
        return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    allow_any = os.environ.get("ALLOW_ANY_HOUR") == "1"
    dry_run = os.environ.get("DRY_RUN") == "1"
    now_uk = datetime.now(ZoneInfo("Europe/London")) if ZoneInfo else datetime.now()
    today_uk = now_uk.strftime("%Y-%m-%d")
    uk_date = f"{now_uk:%A} {now_uk.day} {now_uk:%B %Y}"

    if ZoneInfo is not None and not allow_any:
        h = now_uk.hour
        if h < TARGET_HOUR:
            log(f"Too early (hour={h}); skipping.")
            return
        if h > WINDOW_END_HOUR:
            log(f"Too late (hour={h}); not sending a stale bulletin.")
            return
        marker, _ = _read_marker()
        if marker == today_uk:
            log(f"Already sent today ({today_uk}); skipping.")
            return
        log(f"In window (hour={h}), not yet sent today - sending.")

    log("Fetching feeds...")
    items = get_headlines()
    log(f"  {len(items)} unique headlines collected")
    if not items:
        raise RuntimeError("no headlines fetched - all feeds down")

    log("Asking the model for five stories...")
    body = clean_body(call_llm(items))

    opener = OPENERS[now_uk.toordinal() % len(OPENERS)]
    message = build_message(body, uk_date, opener)

    if dry_run:
        log("\n----- DRY RUN: message that would be sent -----")
        print(message)
        log("----- end (nothing sent) -----")
        return

    log("Sending Telegram DM...")
    mid = send_telegram(message)
    log(f"  OK Telegram DM sent (message_id={mid})")

    # Email is a bonus copy: never let it duplicate the Telegram DM, so record
    # the marker regardless, but exit non-zero so a broken mail path shows red
    # in the run list rather than failing silently.
    log("Sending email copy...")
    email_err = None
    try:
        to = send_email(message, uk_date)
        log(f"  OK email sent to {to}" if to else "  email not sent")
    except Exception as e:
        email_err = e
        log(f"  ERROR email failed: {e}")

    if _write_marker(today_uk):
        log(f"  OK marker written for {today_uk}")
    log("DONE")
    if email_err is not None:
        raise RuntimeError(f"email delivery failed: {email_err}")


if __name__ == "__main__":
    main()
