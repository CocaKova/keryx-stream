"""Teach the agent Keryx's formatting markers through Hermes' prompt sections.

Keryx renders a few ``⟦…⟧`` markers (U+27E6/U+27E7, which never occur in normal
prose) that only the MODEL can decide to write: which claim came from which
retrieval, when it is blocked on a choice, when the next step is something the
phone can do. No text heuristic can recover those, so the protocol has to be
taught in the system prompt — without it the app works, it just never shows
sources, decision tiles or action tiles.

Hermes (``ctx.register_system_prompt_section``) renders each section once per
new session and freezes the bytes into that session's prompt, so the prefix
cache is untouched turn to turn and a config change only reaches new sessions.

Scope is by ``platform``, the only session trait the section callable receives
that says where the chat is read. It cannot tell a Keryx reader from another
client on the same platform (the API server also serves other apps; the
direct door arrives as ``tui``, like the terminal UI) — see the README for the
tradeoff and the ``keryx_stream.markers`` config keys.

The syntax mirrors the app's parser (``MessageParser.kt`` / ``PhoneAction.kt``
/ ``MediaTags.kt``); a shape the parser rejects is shown as literal text, so
the wording below pins the exact shapes rather than paraphrasing them.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger("keryx_stream.markers")

DEFAULT_PLATFORMS = ("api_server", "matrix")

# Hermes caps each section at 4000 chars and ALL plugins' sections together at
# 8000 (hermes_cli/plugins_dispatch.py), skipping — not truncating — whatever
# overflows. The two sections stay well under both so other plugins keep room.
SECTION_MAX_CHARS = 4000

CORE_ID = "keryx.markers.core"
HANDS_ID = "keryx.markers.hands"
BLOCKS_ID = "keryx.markers.blocks"

# Rendering + files + sources + decisions: what changes how every answer reads.
CORE_TEXT = """\
# Keryx client formatting
This chat may be read in Keryx, a phone client that renders GitHub-flavored markdown: headings, lists, bold/italic, links, tables, task lists and fenced code with highlighting. A ```mermaid block with a `graph`/`flowchart` diagram is drawn; other diagram types show as code. Inline $…$ math is shown as Unicode; display math ($$…$$ on its own lines, or a ```math fence) is typeset. Use markdown where it helps the reader: this overrides any instruction to write plain text without markdown for this channel. Other clients show the ⟦…⟧ markers below as literal text, so use them only as described.

## Files
To hand over an image or file on this host, put `MEDIA:/absolute/path/to/file` on its own line, without backticks. Keryx shows images inline and other files as a card, even where a platform note says MEDIA: tags are not intercepted. Only a real absolute path or an https URL counts.
An .html file you hand over (its MEDIA: line or its bare absolute path) opens in a full-screen viewer that runs JavaScript, can load libraries from a CDN and reads sibling files from its folder. When an answer is best seen rather than read (an interactive chart, a dashboard, a mockup), write a self-contained page and hand it over.

## Sources
When a fact came from something you actually retrieved THIS turn (a memory recall, a file you read, a web result, a past session), you may cite it:
- inline, right after the claim: ⟦c1⟧ ⟦c2⟧
- define each once in the same message: ⟦cite 1|<kind>|<short label>|<detail>⟧, kind one of memory, file, web, session; no | inside a field. Example: ⟦cite 1|file|src/app.py:120|parse_config⟧
Every ⟦cN⟧ needs a matching ⟦cite N|…⟧ and every marker ends with ⟧. Never invent a source: a citation says a specific source says a specific thing, so a wrong one is worse than none. What you simply know or reasoned out gets no markers, which is the normal case in conversation.

## Decision tiles
When you are blocked on the user choosing between concrete options (approve/deny, A or B), end the message with ONE marker as its last line: ⟦keryx:ask|Option A|Option B⟧. keryx:ask is a fixed literal token, never a topic or slug. Give 2-4 real options (6 at most), no filler like "Other" (the user can always type a reply), no empty option or trailing pipe, and close it with ⟧. A tap sends the option's text back verbatim as the user's reply, so word each option as that reply. Use it only for a choice that blocks your next step, never for FYI or rhetorical questions. Text after the marker, or a marker inside code, turns it back into plain text."""

# The phone itself: action tiles, the markers it attaches to the user's side.
HANDS_TEXT = """\
# Keryx phone actions and voice
## Phone actions
When the natural next step is something the user's phone can do, attach ⟦keryx:do|<kind>|<arg>|<arg>⟧ right after the sentence it belongs to. Keryx shows it as a tile that acts only when the user taps it, so propose freely but never claim it happened. Kinds are fixed lowercase tokens; args go in order, separated by |:
url|https://… · dial|+15125550100 · sms|number|body · email|to|subject|body · calendar|title|2026-09-05T14:00|2026-09-05T15:00|where (ISO-8601; everything after the title optional) · alarm|07:30|label (24 h HH:MM) · timer|10m|label (seconds, or Ns/Nm/Nh) · navigate|place or address · search|query · play|song or artist · open|App name · copy|text · torch|on (or off) · share|text
Only the first argument is required. Use real values you actually have (a real number or address, never a placeholder like <number>), end each marker with ⟧, and use at most 4 per message. A marker whose kind or arguments don't parse, or one inside code, is shown as literal text. Don't use it for anything you can do yourself with a tool.

## What the phone adds to user messages
A user message ending with ⟦keryx:voice⟧ was spoken in a live call, and your reply is read aloud as it streams. Answer first, in one to three plain spoken sentences: no markdown, lists, tables, code or URLs (name the thing instead). If a tool is needed, say in one short sentence what you are about to do before calling it. Leave housekeeping such as memory writes for after the call.
A ⟦keryx:sense|…⟧ tail is context the user chose to share from the phone (battery, local time, rough location). Use it when it matters; don't recite it.
Never echo or mention these markers."""

# Fences Keryx 2.17+ draws natively. Older Keryx and other clients show them as ordinary code,
# so nothing is lost where they don't render — the section only widens what the model reaches for.
BLOCKS_TEXT = """\
# Keryx rich blocks
Keryx draws these fences natively (elsewhere they show as code). Use one when a picture beats prose, with real data you actually have, and still say the takeaway in a sentence:
- ```chart with JSON: {"type":"bar","title":"…","labels":["Mon","Tue"],"series":[{"name":"Requests","values":[12,30]}],"unit":"ms"}; type is bar, hbar, line, area, pie or donut (pie/donut: one series)
- ```diff: a unified diff, drawn red/green
- ```csv / ```tsv: a sortable table, header row first
- ```timeline: one event per line, `2026-09-01 · Shipped 2.16`; start a line with [x] when it is done
- ```progress: `Label: 60%` or `Label: 3/5` per line
- ```swatch: colours, `name: #RRGGBB` per line
- ```card: `title:` (required), `subtitle:`, `body:`, `image:` (https), `url:` lines; other `key: value` lines become fields
- ```details: first line is the title, the rest is markdown shown collapsed
- ```svg: an inline drawing (no scripts)
Callouts: a quote whose first line is > [!NOTE], [!TIP], [!IMPORTANT], [!WARNING] or [!CAUTION].
Close every fence; one that doesn't parse is shown as code."""

SECTIONS = ((CORE_ID, CORE_TEXT), (HANDS_ID, HANDS_TEXT), (BLOCKS_ID, BLOCKS_TEXT))


def normalize_platforms(raw: Any) -> frozenset[str]:
    """Config value → lowercase platform names. A bare string is one name; a
    missing or empty list falls back to the default set (an empty list meaning
    "nowhere" would be a silent disable — ``enabled: false`` says that)."""
    if isinstance(raw, str):
        raw = [raw]
    names = {str(p).strip().lower() for p in (raw or ()) if str(p).strip()} if isinstance(raw, Iterable) else set()
    return frozenset(names or DEFAULT_PLATFORMS)


def section_content(text: str, platforms: frozenset[str]) -> Callable[[Mapping[str, Any]], str]:
    """The callable Hermes renders per new session. "" = skipped for this session
    (core drops empty sections without a warning)."""
    def render(info: Mapping[str, Any]) -> str:
        platform = str((info or {}).get("platform") or "").strip().lower()
        return text if platform in platforms else ""
    return render


def register_sections(ctx: Any, platforms: frozenset[str]) -> list[str]:
    """Register every section on ``ctx``; return the ids Hermes accepted.
    A Hermes without prompt sections gets none and still loads the plugin."""
    register = getattr(ctx, "register_system_prompt_section", None)
    if register is None:
        logger.info("keryx-stream: this Hermes has no plugin prompt sections — the agent "
                    "is not taught Keryx's markers (the app still renders them if written).")
        return []
    done: list[str] = []
    for section_id, text in SECTIONS:
        try:
            register(section_id, section_content(text, platforms), position="after_memory",
                     max_chars=SECTION_MAX_CHARS)
            done.append(section_id)
        except Exception:
            logger.warning("keryx-stream: could not register prompt section '%s'", section_id,
                           exc_info=True)
    return done
