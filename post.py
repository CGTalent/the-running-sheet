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
    ("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
    ("BBC London", "https://feeds.bbci.co.uk/news/england/london/rss.xml"),
    ("Sky Entertainment", "https://feeds.skynews.com/feeds/rss/entertainment.xml"),
    ("Sky News Strange", "https://feeds.skynews.com/feeds/rss/strange.xml"),
    ("Guardian Culture", "https://www.theguardian.com/culture/rss"),
    ("Guardian TV & Radio", "https://www.theguardian.com/tv-and-radio/rss"),
    ("Guardian Life & Style", "https://www.theguardian.com/lifeandstyle/rss"),
    ("Guardian Music", "https://www.theguardian.com/music/rss"),
    ("Guardian Sport", "https://www.theguardian.com/uk/sport/rss"),
    ("NME Music", "https://www.nme.com/news/music/feed"),
    ("Rolling Stone UK", "https://www.rollingstone.co.uk/feed/"),
    ("Radio Times", "https://www.radiotimes.com/feed/"),
]

# Chris's show is light and entertainment-led - he is not there to deliver the
# news. Anything heavy, serious, political or upsetting is banned, and the ban is
# enforced HERE in code (a prompt instruction alone is not reliable). These terms
# are checked against each headline before the model ever sees it, and again
# against the finished stories as a safety net.
HEAVY_TERMS = re.compile(
    r"\b(" + "|".join([
        # war / military / foreign affairs
        "war", "wars", "warfare", "military", "armed forces", "army", "navy", "raf",
        "air force", "missile", "missiles", "drone", "drones", "bomber", "bombers",
        "fighter jet", "nato", "nuclear", "ceasefire", "invasion", "troops",
        "airstrike", "air strike", "hostage", "hostages", "terror", "terrorist",
        "terrorism", "terror attack", "geopolitics", "geopolitical", "sanctions",
        "putin", "kremlin", "ukraine", "russia", "iran", "israel", "gaza", "hamas",
        "hezbollah", "lebanon", "taiwan", "north korea", "pentagon", "white house",
        # politics / elections
        "election", "elections", "ballot", "by-election", "byelection", "parliament",
        "westminster", "mp", "mps", "tory", "tories", "conservative party",
        "labour party", "reform uk", "lib dem", "lib dems", "snp", "badenoch",
        "farage", "starmer", "burnham", "chancellor", "budget", "manifesto",
        "polling", "opinion poll",
        # crime / courts / violence / tragedy
        "police", "arrest", "arrested", "charged", "court", "trial", "jailed",
        "prison", "sentence", "murder", "manslaughter", "assault", "abuse", "rape",
        "sexual", "paedophile", "grooming", "stab", "stabbing", "shooting",
        "shot dead", "killed", "fatal", "inquest", "tragedy", "tragic", "suicide",
        "overdose", "attacked", "victim",
        # grim money / health / misery
        "inflation", "recession", "mortgage", "interest rates", "cost of living",
        "unemployment", "redundancies", "redundancy", "job cuts", "layoffs",
        "nhs", "hospital", "cancer", "terminal illness", "ambulance", "flood",
        "floods", "storm damage", "crisis", "strike action", "industrial action",
        "walkout", "protest", "protesters", "riot", "riots", "migrant", "migrants",
        "asylum", "immigration", "deportation", "small boats",
        # hatred / extremism
        "racism", "racist", "far-right", "extremist", "extremism", "antisemitic",
        "antisemitism", "islamophobia", "hate crime",
    ]) + r")\b",
    re.IGNORECASE,
)


def is_heavy(item):
    """True if a headline/summary trips the heavy-topic filter."""
    return bool(HEAVY_TERMS.search(f"{item['title']} {item['summary']}"))

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
SUBLINE = "{n} things doing the rounds today - and a few lines you could use."
NUM_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}

FRAME_TOP = "=" * 4
FRAME_BOTTOM = "=" * 4
RULE = "=" * 44
DIVIDER = "-" * 44

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
    the first feed or two), de-duplicated by title, with anything heavy dropped
    before the model ever sees it."""
    per_feed = []
    for name, url in NEWS_FEEDS:
        per_feed.append(fetch_feed(name, url))
    seen, merged, dropped = set(), [], 0
    for i in range(max((len(f) for f in per_feed), default=0)):
        for feed in per_feed:
            if i >= len(feed):
                continue
            item = feed[i]
            key = re.sub(r"\W+", "", item["title"].lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            if is_heavy(item):
                dropped += 1
                continue
            merged.append(item)
    log(f"  {dropped} heavy/serious headlines filtered out")
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
PROMPT = """You write the daily light-entertainment brief for Chris Farrell, a UK \
radio presenter (Radio Jackie, South West London, and Greatest Hits Radio 80s). His \
show is LIGHT, ENTERTAINING and CONVERSATIONAL. He is there to amuse people and \
keep them company - he is NOT a newsreader. His style: REALLY AUTHENTIC, REALLY \
CONVERSATIONAL, warm, understated, dry-witted. Sounds like a mate chatting, never \
like a newsreader or a press release.

Below are today's real headlines. Pick the FIVE best stories for him to talk about \
on air today, and write them up.

THE RULE ABOVE ALL OTHERS - AUTHENTICITY. Every line must sound like something he \
would actually say out loud to ONE listener, sitting in a studio, on a Saturday. \
Follow this priority order, in order:
1. AUTHENTICITY FIRST - it has to sound like him, speaking naturally. If a line \
would not come out of his mouth, it is worthless no matter how clever it is.
2. HUMOUR - if there is a colourful or interesting way to say it, say it that way. \
Add a humorous line wherever one genuinely fits - but never force one.
3. HUMAN CONNECTION - believable observations, real-life situations, genuine warmth.
4. INTERESTING CONTENT - a good story, a telling detail, a memory, an observation.
The overall brief: colourful, engaging and humorous, but ALWAYS believable as \
something he'd really say on the radio. Never "writerly", never like an ad, never \
like a press release.

TOP PRIORITY - KEEP IT LIGHT. This outranks everything else:
- NO war, military, defence, missiles, drones, terrorism, or foreign affairs.
- NO party politics, elections, campaigns, budgets, or political rows.
- NO crime, courts, arrests, sentencing, violence, abuse or tragedy.
- NO grim money talk, cost-of-living misery, recession or job losses.
- NOTHING distressing on any subject. If a story has a sad or frightening angle, \
skip it and pick another instead.
- If a story could open a proper news bulletin, it is the WRONG story for him.
- DO pick the light, current things people are actually chatting about: showbiz and \
celebrity, music, TV and film, sport, funny or heartwarming or quirky stories, \
local London and South West London life, animals, food, nostalgia, odd auctions, \
records, weather, and everyday-life surprises.

OTHER HARD RULES:
- Use ONLY the facts in the headlines below. Never invent names, numbers, quotes \
or details. If a headline is thin, keep the write-up thin.
- GEOGRAPHY DOES NOT MATTER MUCH. A story about a musician, a concert, a TV show, \
a film or a sporting event is just as usable wherever it happened - do not rule \
stories out for not being local to him. If ONE of the headlines is genuinely \
London or South West London in a way that is interesting to talk about (Kingston, \
Teddington, Surbiton, Wimbledon, Richmond, Twickenham, Hampton Court, Croydon, \
Brixton, and so on), prefer it for ONE story - but never force it, and never pick \
a local story that is dull just to have one. Never mention the geography rule in \
your output.
- AT MOST ONE story about a death or obituary, and only if it is a genuinely \
well-known showbiz or music figure people will want to talk about.
- No two stories from the same area. Cover five DIFFERENT areas.
- No fake enthusiasm, no cheesy gags. Wit over jokes. Dry over slapstick.
- FIRST-PERSON LINES: an everyday hypothetical or general preference is welcome \
("I'd just grow cacti instead", "I can't get £277 for my shopping-list notebook", \
"that tops being clipped by the printer"). But NEVER invent a specific claim about \
his life, his family, his past, or things he has supposedly done or places he has \
supposedly been. No "my mum...", no "my dad's...", no "when I was...", no \
"I once...", no "I remember...". He has to be able to say every line truthfully \
on air, so keep first-person general and never biographical.
- NO questions to the listener, NO calls to action.
- Plain hyphens only (-). NEVER use em dashes or en dashes.
- No markdown, no bold markers, no bullet symbols, no headings like "STORY 1:".
- Keep each summary to ONE sentence, TWO at the very most - never three. It is \
only there to set up his link, so give just enough detail to make sense. No extra \
context, no background, no scene-setting.

VOICE CALIBRATION for the "~" lines. They must sound like something he would \
actually say out loud to one listener - understated, warm, specific. Good: \
"£66 million for a greenhouse. Mine cost £40 in B&Q and it's still standing, so \
I'm not sure what they're doing differently." Bad (too writerly, too jokey): \
"Hold onto your watering cans, folks!" Bad (a fabricated personal anecdote he \
cannot truthfully say): "My dad's been fixing his lean-to for a decade." Any line \
that reads like a press release or a stand-up punchline fails. Specific detail \
beats a generic gag every time - the detail just has to be about the STORY, not \
about his life.

OUTPUT FORMAT - exactly this, nothing before or after. The number in square \
brackets MUST be the item number of the headline you used from TODAY'S HEADLINES \
below - it is how the story's link gets attached, so it has to be the right one:

1. [12] HEADLINE IN CAPITAL LETTERS
One or two short sentences saying what happened, conversationally.

~ "An optional line he could say - a dry punchline."
~ "Another angle - a relatable observation."
~ "A third option - warmer, or a colourful comparison."

---

2. [7] NEXT HEADLINE IN CAPITALS
Summary sentence.

~ "Option one."
~ "Option two."
~ "Option three."

---

(and so on for all five)

Give exactly THREE "~" option lines for every story - he wants a choice. Vary \
their flavour: one dry punchline, one plain authentic observation, one warm or \
thoughtful closer or colourful comparison. They are options - Chris picks one, \
or says none. Do not force a joke into every one, and never use the same shape \
twice in the same story.

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
def drop_heavy_blocks(body):
    """Safety net: drop any finished story block that still trips the heavy
    filter, so a serious story can never reach Chris's phone."""
    blocks = split_stories(body)
    kept, dropped = [], []
    for b in blocks:
        # Judge the STORY (headline + summary), not the joke lines - a stray word
        # in a punchline should never knock out a perfectly good light story.
        head_part = b.split("~", 1)[0]
        hit = HEAVY_TERMS.search(head_part)
        if hit:
            dropped.append(f"{b.split(chr(10), 1)[0][:70]}  [matched: {hit.group(0)!r}]")
        else:
            kept.append(b)
    if dropped:
        for d in dropped:
            log(f"  dropped heavy story: {d}")
    return f"\n\n---\n\n".join(kept)


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


# Fabricated autobiography: the model likes inventing "my mum..." / "I once..."
# lines. Chris has to be able to say every line truthfully on air, so any option
# line making a specific claim about his life or family is dropped.
CLAIM_LINES = re.compile(
    r"(\bmy\s+(?:mum|dad|mother|father|wife|husband|girlfriend|partner|son|"
    r"daughter|kid|kids|child|children|brother|sister|nan|gran|grandma|grandad|"
    r"aunt|uncle|cousin|neighbour|neighbours|mate|mates|mother-in-law)\b"
    r"|\bI\s+(?:once|used to|remember|grew up|went to|visited|drove|bought|paid"
    r"|spent|tried|had to)\b"
    r"|\bwhen I was\b|\bback in my\b)",
    re.IGNORECASE,
)


def scrub_claims(body):
    """Drop option lines that invent a specific claim about Chris's own life."""
    out, dropped = [], []
    for line in body.split("\n"):
        if line.strip().startswith("~") and CLAIM_LINES.search(line):
            dropped.append(line.strip()[:80])
            continue
        out.append(line)
    for d in dropped:
        log(f"  dropped invented personal claim: {d}")
    return "\n".join(out)


def trim_summary(block):
    """Keep each story's setup to at most TWO sentences - Chris only needs enough
    to set up his link. Deterministic trim, so it cannot regress."""
    head, sep, rest = block.partition("\n")
    if not sep:
        return block
    idx = rest.find("~")
    if idx == -1:
        return block
    summary, tail = rest[:idx], rest[idx:]
    sentences = re.split(r"(?<=[.!?])\s+", summary.strip())
    if len(sentences) > 2:
        summary = " ".join(sentences[:2])
    else:
        summary = summary.strip()
    return f"{head}\n{summary}\n\n{tail.lstrip()}"


def apply_brief(body):
    """Rebuild the body with every story's summary trimmed to two sentences."""
    blocks = [trim_summary(b) for b in split_stories(body)]
    return "\n\n---\n\n".join(b.strip() for b in blocks if b.strip())


def linkify(body, items):
    """Attach the source article link to every story.

    The prompt makes the model tag each story with the list number it used
    ('2. [17] HEADLINE'); we map that back to the real URL. If the model mangles
    the tag, fall back to matching the headline against the pool by word overlap.
    """
    by_index = {i: it for i, it in enumerate(items, 1)}
    out = []
    for pos, block in enumerate(split_stories(body), 1):
        lines = block.split("\n")
        link = None
        m = re.match(r"^(\d+)\.\s*\[(\d+)\]\s*(.*)$", lines[0])
        if m:
            idx = int(m.group(2))
            if idx in by_index:
                link = by_index[idx]["link"]
            body_line = m.group(3).strip()
        else:
            body_line = re.sub(r"^\d+\.\s*", "", lines[0]).strip()
        # Renumber sequentially, so dropping a story never leaves a gap (1,2,4,5).
        lines[0] = f"{pos}. {body_line}"
        if not link:
            link = fuzzy_link(body_line, items)
        if link:
            lines += ["", f"Link: {link}"]
        out.append("\n".join(lines).strip())
    return "\n\n---\n\n".join(out)


STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "as",
    "at", "is", "are", "was", "were", "after", "over", "new", "says", "said",
    "its", "his", "her", "their", "from", "by", "that", "this", "it's", "amid",
    "into", "up", "out", "as", "be", "been", "will", "has", "have", "had",
}


def fuzzy_link(headline, items, threshold=0.34):
    """Best-effort fallback: match a story headline to its source item."""
    def toks(s):
        return {w for w in re.findall(r"[a-z']+", s.lower())
                if w not in STOPWORDS and len(w) > 2}
    ht = toks(headline)
    if not ht:
        return None
    best, best_score = None, 0.0
    for it in items:
        score = len(ht & toks(it["title"])) / len(ht)
        if score > best_score:
            best, best_score = it, score
    if best and best_score >= threshold:
        log(f"  link matched by headline guess ({best_score:.2f}): {best['title'][:60]}")
        return best["link"]
    return None


def build_message(body, uk_date, opener):
    blocks = split_stories(body)
    n = len(blocks) or STORIES
    word = NUM_WORDS.get(n, str(n)).capitalize()
    body_txt = f"\n\n{DIVIDER}\n\n".join(blocks)
    return (
        f"{FRAME_TOP}\n"
        f"{opener}\n"
        f"{SUBLINE.format(n=word)}\n\n"
        f"\U0001F4F0 THE RUNNING SHEET\n"
        f"{uk_date}\n"
        f"{RULE}\n\n"
        f"{body_txt}\n\n"
        f"{RULE}\n"
        f"That's your {NUM_WORDS.get(n, str(n))}. Take what you like, bin the rest.\n"
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
    msg["Subject"] = f"\U0001F399 The Running Sheet - {uk_date}"
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
    return os.environ.get("GITHUB_REPOSITORY", "CGTalent/the-running-sheet")


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
    body = drop_heavy_blocks(body)
    body = apply_brief(body)
    body = scrub_claims(body)
    body = linkify(body, items)
    if not all("Link: " in b for b in split_stories(body)):
        log("  WARNING: at least one story came back without a link")
    short = [b for b in split_stories(body) if len([l for l in b.split("\n") if l.strip().startswith("~")]) < 2]
    if short:
        log(f"  WARNING: {len(short)} story/stories came back with fewer than 2 option lines")

    opener = OPENERS[now_uk.toordinal() % len(OPENERS)]
    message = build_message(body, uk_date, opener)

    if dry_run:
        log("\n----- DRY RUN: message that would be sent -----")
        print(message)
        log("----- end (nothing sent) -----")
        return

    log("Sending Telegram DM...")
    if os.environ.get("SKIP_TELEGRAM") == "1":
        log("  Telegram skipped (SKIP_TELEGRAM=1)")
    else:
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
