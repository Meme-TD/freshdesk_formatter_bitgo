#!/usr/bin/env python3
"""
freshdesk_kb_formatter.py

Normalize formatting of BitGo Freshdesk KB articles via the Freshdesk Solutions API.

Two article groups (decided from the sheet's KB Category):
  * User Guide      -> KB Category equals "bitgo user guide" (case-insensitive)
  * Non-User-Guide  -> everything else

Non-User-Guide  : minimal changes only
  - recolor the body title (heading whose text matches the sheet KB Title) to
    blue rgb(22, 71, 219), forcing the color onto every nested run so the whole
    title turns blue; if the title is not found, skip it and report it.
  - center all images.
  - nothing else.

User Guide      : normalize formatting from a clean slate
  - convert all headings to <p> (no <h1>-<h6> in output), preserving role via
    size/weight.
  - all fonts -> Arial.
  - title    : 30px bold Arial, keep its text color.
  - subtitle : 24px bold Arial black.
  - body     : 16px normal Arial black, but keep runs that are already bold.
  - hyperlinks recolored to rgb(22, 71, 219).
  - clear inherited cruft (margin/line-height/box-sizing/stray colors/13px/pt),
    but keep list indentation (padding / padding-inline-start on <ol>/<ul>).
  - spacing (empty <p><br></p> spacers): 2 before each subtitle, 1 between body
    paragraphs, 1 after an image, 3 at the very end. Pre-existing empty spacer
    paragraphs are removed first so old spacing doesn't compound.

Modes: inspect | dry_run (default) | apply | restore

Read the API key from the FRESHDESK_API_KEY environment variable.
Requires: requests, beautifulsoup4
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

FONT = "Arial, Helvetica, sans-serif"
BLUE = "rgb(22, 71, 219)"          # brand blue #1647DB
BLACK = "rgb(0, 0, 0)"

USER_GUIDE_CATEGORY = "bitgo user guide"   # compared case-insensitively

# A <p> is treated as a subtitle by size when its text is larger than body.
# Body target is 16px; observed subtitles are ~16pt (21.3px) / 24px, and the
# smallest real subtitle seen is 13pt (17.3px). 16.5 cleanly separates them.
SUBTITLE_MIN_PX = 16.5

# Bold-standalone-paragraph subheader heuristic thresholds.
BOLD_SUBHEAD_MAX_WORDS = 8
BOLD_SUBHEAD_MAX_CHARS = 70

# Text-run tags we normalize inside a block.
RUN_TAGS = ("span", "a", "strong", "b", "font", "u", "em", "i")

# Trailing/leading punctuation stripped when matching titles.
_MATCH_PUNCT = " \t\r\n :;.,!?-–—\"'`"

DEFAULT_PAUSE = 0.5   # seconds between API requests


# --------------------------------------------------------------------------- #
# Small style helpers
# --------------------------------------------------------------------------- #

def parse_style(style_str):
    """Parse an inline style string into an ordered dict of {prop: value}."""
    out = {}
    if not style_str:
        return out
    for part in style_str.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def render_style(d):
    """Render an ordered dict of style props back to a string (drops empties)."""
    return "; ".join(f"{k}: {v}" for k, v in d.items() if v != "")


def set_style(tag, d):
    s = render_style(d)
    if s:
        tag["style"] = s
    elif tag.has_attr("style"):
        del tag["style"]


def size_to_px(value, unit):
    """Convert a font-size value to px. pt uses the CSS 96/72 ratio."""
    if unit == "px":
        return value
    if unit == "pt":
        return value * 96.0 / 72.0
    return None  # em / other: unknown


def font_size_px(style_str):
    """Return the font-size of a style string in px, or None (em is unknown)."""
    m = re.search(r"font-size\s*:\s*([0-9.]+)\s*(px|pt|em)", style_str or "", re.I)
    if not m:
        return None
    return size_to_px(float(m.group(1)), m.group(2).lower())


def style_is_bold(style_str):
    m = re.search(r"font-weight\s*:\s*([a-z0-9]+)", style_str or "", re.I)
    if not m:
        return False
    v = m.group(1).lower()
    if v in ("bold", "bolder"):
        return True
    return v.isdigit() and int(v) >= 600


# --------------------------------------------------------------------------- #
# Title matching
# --------------------------------------------------------------------------- #

def norm_title(s):
    """Normalize a title for comparison: lowercase, nbsp->space, collapse
    whitespace, strip surrounding punctuation."""
    if not s:
        return ""
    s = s.replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.strip(_MATCH_PUNCT)


# --------------------------------------------------------------------------- #
# Element/text helpers
# --------------------------------------------------------------------------- #

def block_text(el):
    return el.get_text()


def is_empty_block(el):
    """A block that renders as blank space: only <br>/whitespace and no image."""
    if el.find("img") is not None:
        return False
    return block_text(el).replace(" ", " ").strip() == ""


def wraps_image(span):
    """True if this run element exists only to wrap an <img> (skip scrubbing)."""
    return span.find("img") is not None


def run_is_bold(run, stop):
    """Walk from run up to (and including) `stop` checking for bold markup."""
    node = run
    while node is not None:
        if isinstance(node, Tag):
            if node.name in ("strong", "b"):
                return True
            if style_is_bold(node.get("style", "")):
                return True
        if node is stop:
            break
        node = node.parent
    return False


def run_is_link(run, block):
    if run.name == "a":
        return True
    p = run.parent
    while p is not None and p is not block.parent:
        if isinstance(p, Tag) and p.name == "a":
            return True
        if p is block:
            break
        p = p.parent
    return False


def representative_px(p):
    """Representative rendered font-size (px) of a <p>: max size across its
    text-bearing runs; falls back to the block's own font-size."""
    sizes = []
    for run in p.find_all(RUN_TAGS):
        if wraps_image(run):
            continue
        if run.get_text().replace(" ", " ").strip() == "":
            continue
        px = font_size_px(run.get("style", ""))
        if px is not None:
            sizes.append(px)
    if sizes:
        return max(sizes)
    return font_size_px(p.get("style", ""))


def paragraph_fully_bold(p):
    """True if every non-empty text node in the paragraph is bold."""
    found = False
    for node in p.descendants:
        if isinstance(node, NavigableString):
            if node.strip() == "":
                continue
            found = True
            if not run_is_bold(node.parent, p):
                return False
    return found


# --------------------------------------------------------------------------- #
# NON-USER-GUIDE transform
# --------------------------------------------------------------------------- #

def transform_non_user_guide(html, sheet_title):
    """Recolor the matching title blue and center images. Nothing else."""
    soup = BeautifulSoup(html, "html.parser")
    report = {"title_found": False, "detected_title": "", "subtitle_count": "",
              "uncertain": False, "notes": []}

    target = norm_title(sheet_title)

    # Find the first block-level element whose text matches the sheet title.
    title_el = None
    if target:
        for el in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "div"]):
            if norm_title(el.get_text()) == target:
                title_el = el
                break

    if title_el is not None:
        report["title_found"] = True
        report["detected_title"] = re.sub(r"\s+", " ", title_el.get_text()).strip()
        # Force blue onto the block and every text run (incl. those carrying
        # their own color), so the whole title turns blue.
        st = parse_style(title_el.get("style", ""))
        st["color"] = BLUE
        set_style(title_el, st)
        for run in title_el.find_all(RUN_TAGS):
            rs = parse_style(run.get("style", ""))
            rs["color"] = BLUE
            set_style(run, rs)
    else:
        report["notes"].append("title not found")

    # Center all images.
    for img in soup.find_all("img"):
        ist = parse_style(img.get("style", ""))
        ist["display"] = "block"
        ist["margin-left"] = "auto"
        ist["margin-right"] = "auto"
        if "float" in ist:
            ist["float"] = "none"
        set_style(img, ist)

    return str(soup), report


# --------------------------------------------------------------------------- #
# USER-GUIDE transform
# --------------------------------------------------------------------------- #

def _scrub_runs(container, role, block_for_bold):
    """Normalize every text run inside `container` for the given role.

    Reads bold/link/color from the ORIGINAL markup first (two-pass) so that
    rewriting one run cannot change what a sibling/child sees.
    """
    runs = [r for r in container.find_all(RUN_TAGS) if not wraps_image(r)]
    facts = [(r, run_is_bold(r, block_for_bold), run_is_link(r, block_for_bold))
             for r in runs]
    for run, is_bold, is_link in facts:
        old = parse_style(run.get("style", ""))
        new = {}
        if is_link:
            new["color"] = BLUE
        elif role == "title" and "color" in old:
            new["color"] = old["color"]          # title: keep its color
        # subtitle/body: drop color so the block's color cascades.
        if role == "body" and is_bold:
            new["font-weight"] = "bold"           # preserve real bold runs
        set_style(run, new)


def _is_blank_text(node):
    return isinstance(node, NavigableString) and node.replace("\xa0", " ").strip() == ""


def _meaningful_children(p):
    """Direct children of p, ignoring whitespace-only text nodes."""
    return [c for c in p.contents if not _is_blank_text(c)]


def _is_edge_break(node):
    """A bare <br>, or a run wrapping only <br> (no text, no image)."""
    if isinstance(node, Tag) and node.name == "br":
        return True
    return (isinstance(node, Tag) and node.name in RUN_TAGS
            and node.find("img") is None
            and node.find("br") is not None
            and node.get_text().replace("\xa0", " ").strip() == "")


def _is_empty_wrapper(tag):
    """A tag left with no text, no <img>, and no <br> inside it."""
    return (isinstance(tag, Tag)
            and tag.name not in ("img", "br")
            and tag.find("img") is None
            and tag.find("br") is None
            and tag.get_text().replace("\xa0", " ").strip() == "")


def _last_meaningful_node(node):
    """Deepest last descendant that is a <br>, an <img>, or non-empty text.
    Whitespace-only text and empty wrapper tags are skipped, so this sees
    through nesting like <span><strong><img><br></strong></span>."""
    for child in reversed(list(node.children)):
        if isinstance(child, NavigableString):
            if _is_blank_text(child):
                continue
            return child
        if isinstance(child, Tag):
            if child.name in ("br", "img"):
                return child
            found = _last_meaningful_node(child)
            if found is not None:
                return found
            continue  # empty wrapper -> keep scanning left
    return None


def _trim_edge_breaks(p):
    """Remove leading and trailing <br> spacers inside a block.

    Trailing removal is DEEP: a <br> at the very end of the block is removed
    even when nested (e.g. <span><strong><img><br></strong></span>), as long
    as no meaningful text or image follows it. Any wrapper tags left empty by
    the removal are cleaned up too. Internal <br> line breaks that have real
    content after them are preserved.
    """
    # Trailing: repeatedly drop the last <br> wherever it is nested.
    while True:
        leaf = _last_meaningful_node(p)
        if isinstance(leaf, Tag) and leaf.name == "br":
            parent = leaf.parent
            leaf.decompose()
            node = parent
            while node is not None and node is not p and _is_empty_wrapper(node):
                nxt = node.parent
                node.decompose()
                node = nxt
            continue
        break

    # Leading: drop bare <br> / empty-<br>-wrapper spacers at the very start.
    while True:
        kids = _meaningful_children(p)
        if not kids or not _is_edge_break(kids[0]):
            break
        kids[0].extract()


def _style_paragraph(p, role, soup, prev_role=None):
    """Convert a heading to <p> if needed, scrub runs, set canonical block
    style for the role.

    All vertical spacing is carried by margin-top/margin-bottom on the block
    itself (never by empty spacer paragraphs, which Freshdesk strips into
    parentless <br><br> that collapse to size 13):
      * body / list / table paragraph : margin-top 0px,  margin-bottom 16px
      * title                         : margin-top 0px,  margin-bottom 16px
      * subtitle                      : margin-top 24px, margin-bottom 16px
        - but 0px on top when it sits directly under the main title (snug).
    This makes every inter-paragraph break render at size 16px.
    """
    keep_color = None
    if role == "title":
        keep_color = parse_style(p.get("style", "")).get("color")
    if p.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        p.name = "p"
    _scrub_runs(p, role, p)
    _trim_edge_breaks(p)
    if role == "title":
        d = {"font-family": FONT, "font-size": "30px", "font-weight": "bold",
             "margin-top": "0px", "margin-bottom": "16px"}
        if keep_color:
            d["color"] = keep_color
    elif role == "subtitle":
        # snug (0) directly under the title, and at the very top of the body
        top = "0px" if prev_role in (None, "title") else "24px"
        d = {"font-family": FONT, "font-size": "24px",
             "font-weight": "bold", "color": BLACK,
             "margin-top": top, "margin-bottom": "16px"}
    else:
        d = {"font-family": FONT, "font-size": "16px",
             "font-weight": "normal", "color": BLACK,
             "margin-top": "0px", "margin-bottom": "16px"}
    set_style(p, d)


def _style_list(el, soup):
    """Normalize an <ol>/<ul> as body content, keeping indentation."""
    old = parse_style(el.get("style", ""))
    keep = {}
    for prop in ("padding", "padding-inline-start", "padding-left",
                 "list-style", "list-style-type"):
        if prop in old:
            keep[prop] = old[prop]
    base = {"font-family": FONT, "font-size": "16px",
            "font-weight": "normal", "color": BLACK}
    base.update(keep)
    set_style(el, base)

    for li in el.find_all("li", recursive=False):
        lold = parse_style(li.get("style", ""))
        lkeep = {}
        if "list-style-type" in lold:
            lkeep["list-style-type"] = lold["list-style-type"]
        lbase = {"font-family": FONT, "font-size": "16px",
                 "font-weight": "normal", "color": BLACK}
        lbase.update(lkeep)
        set_style(li, lbase)

        # Writers sometimes nest heading tags inside a list item. Demote each
        # to a plain span and strip ALL its inline styling (Helvetica, grey
        # color, pt size, etc.) so it inherits the list's 16px Arial black
        # styling instead of rendering as a grey subheading. Links keep their
        # style (recolored blue by the run scrub below).
        for h in li.find_all(("h1", "h2", "h3", "h4", "h5", "h6")):
            h.name = "span"
            for node in [h] + h.find_all(True):
                if node.name != "a" and node.has_attr("style"):
                    del node["style"]

        for child in li.find_all(recursive=False):
            if child.name in ("ol", "ul"):
                _style_list(child, soup)
            elif child.name == "p":
                _style_paragraph(child, "body", soup)
        # Scrub any runs sitting directly in the <li> (no wrapping <p>),
        # without descending into nested lists / paragraphs.
        for run in li.find_all(RUN_TAGS):
            if wraps_image(run):
                continue
            if run.find_parent(["p", "ol", "ul"]) not in (None, li) or \
               run.find_parent("p") is not None:
                continue
            is_bold = run_is_bold(run, li)
            is_link = run_is_link(run, li)
            new = {}
            if is_link:
                new["color"] = BLUE
            if is_bold:
                new["font-weight"] = "bold"
            set_style(run, new)


def _style_table_block(el, soup):
    """Normalize text inside a table (or a div wrapping a table) as body,
    keeping table structure/borders."""
    table = el if el.name == "table" else el.find("table")
    if el.name == "div":
        set_style(el, {})  # drop wrapper cruft (margins, flex, etc.)
    if table is not None:
        tstyle = parse_style(table.get("style", ""))
        tstyle["font-family"] = FONT
        set_style(table, tstyle)
        for p in table.find_all("p"):
            _style_paragraph(p, "body", soup)
        for cell in table.find_all(["td", "th"]):
            for run in cell.find_all(RUN_TAGS):
                if wraps_image(run):
                    continue
                if run.find_parent("p") is not None:
                    continue
                is_bold = run_is_bold(run, cell)
                is_link = run_is_link(run, cell)
                new = {}
                if is_link:
                    new["color"] = BLUE
                if is_bold:
                    new["font-weight"] = "bold"
                set_style(run, new)


def _is_marker_char(ch):
    """A leading 'box marker' char: whitespace/nbsp, a literal '?', the Unicode
    replacement char, an emoji variation selector / ZWJ, or any symbol/emoji
    codepoint (>= U+2190: arrows, dingbats, pictographs, emoji)."""
    if ch in " \t\r\n ?�️‍​":
        return True
    return ord(ch) >= 0x2190


def _strip_leading_markers(root):
    """Strip a leading run of marker chars (unrendered emoji / '?') from the
    start of an element's text. Only touches the very beginning; a '?' later in
    the text is left alone."""
    for text in list(root.strings):
        s = str(text)
        if s.strip("\xa0 \t\r\n") == "":
            continue                       # pure whitespace node: keep scanning
        i = 0
        while i < len(s) and _is_marker_char(s[i]):
            i += 1
        new = s[i:].lstrip("\xa0 ")
        if new.strip() == "":
            text.extract()                 # node was only marker(s): drop it
            continue                       # look at the next text node
        if new != s:
            text.replace_with(new)
        break                              # reached the first real content


def _table_is_box(table):
    """A single-column table used as a callout/note box (one <td> per row).
    A real data table has at least one row with two or more cells."""
    rows = table.find_all("tr")
    if not rows:
        return False
    for tr in rows:
        if len(tr.find_all(["td", "th"], recursive=False)) > 1:
            return False   # multi-column -> genuine table, keep it
    return True            # every row single-column -> box


def _unwrap_box_tables(soup):
    """Remove single-column 'box' wrappers by lifting each cell's content up
    to where the box was. Multi-column tables are left untouched."""
    BLOCK = ("p", "ol", "ul", "table", "div", "blockquote",
             "h1", "h2", "h3", "h4", "h5", "h6")
    for box in list(soup.children):
        if not isinstance(box, Tag):
            continue
        if box.name == "table":
            table = box
        elif box.name == "div":
            table = box.find("table")
        else:
            table = None
        if table is None or not _table_is_box(table):
            continue
        for cell in table.find_all(["td", "th"]):
            _strip_leading_markers(cell)   # drop the leading '?'/emoji marker
            block_children = [c for c in cell.find_all(recursive=False)
                              if getattr(c, "name", None) in BLOCK]
            if block_children:
                for c in block_children:
                    box.insert_before(c.extract())
            else:  # loose inline content -> wrap it in a paragraph
                new_p = soup.new_tag("p")
                for c in list(cell.contents):
                    new_p.append(c.extract())
                box.insert_before(new_p)
        box.decompose()


def transform_user_guide(html, sheet_title):
    soup = BeautifulSoup(html, "html.parser")
    report = {"title_found": False, "detected_title": "", "subtitle_count": 0,
              "uncertain": False, "notes": []}
    target = norm_title(sheet_title)

    # Pass 1: drop comments, unwrap single-column callout "boxes", and remove
    # pre-existing empty spacer paragraphs / empty heading blocks so old ad-hoc
    # spacing can't compound.
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    _unwrap_box_tables(soup)   # lift note-box content out; keep real tables
    top_blocks = []
    for child in list(soup.children):
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in ("p", "h1", "h2", "h3", "h4", "h5", "h6") and is_empty_block(child):
            continue  # pre-existing spacer -> drop
        top_blocks.append(child)

    # Pass 2: classify each retained block (reads ORIGINAL styles).
    classified = []          # list of (element, role)
    title_assigned = False
    for i, el in enumerate(top_blocks):
        name = el.name
        role = "body"
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            txt = norm_title(el.get_text())
            if not title_assigned and target and txt == target:
                role = "title"
                title_assigned = True
                report["title_found"] = True
                report["detected_title"] = re.sub(r"\s+", " ", el.get_text()).strip()
            else:
                role = "subtitle"           # heading tag -> subtitle
        elif name == "p":
            txt = norm_title(el.get_text())
            if not title_assigned and target and txt == target:
                role = "title"
                title_assigned = True
                report["title_found"] = True
                report["detected_title"] = re.sub(r"\s+", " ", el.get_text()).strip()
            else:
                px = representative_px(el)
                if px is not None and px > SUBTITLE_MIN_PX:
                    role = "subtitle"       # larger-than-body <p>
                elif paragraph_fully_bold(el):
                    raw = re.sub(r"\s+", " ", el.get_text()).strip()
                    words = raw.split()
                    short = (len(words) <= BOLD_SUBHEAD_MAX_WORDS and
                             len(raw) <= BOLD_SUBHEAD_MAX_CHARS)
                    if short and raw.rstrip().endswith(":"):
                        # Bold colon lead-in. If it introduces a list it is a
                        # lead-in (keep as body, no flag); otherwise flag it.
                        role = "body"
                        nxt = top_blocks[i + 1] if i + 1 < len(top_blocks) else None
                        if not (nxt is not None and nxt.name in ("ol", "ul")):
                            report["uncertain"] = True
                            report["notes"].append(
                                f"bold short paragraph kept as body (ends with ':'): {raw!r}")
                    elif short:
                        role = "subtitle"    # bold-standalone subheader
                        report["uncertain"] = True
                        report["notes"].append(
                            f"bold-standalone paragraph treated as subtitle: {raw!r}")
                    else:
                        role = "body"
                else:
                    role = "body"
        elif name in ("ol", "ul"):
            role = "body"
        elif name == "table":
            role = "body"
        elif name == "div":
            role = "body"
        else:
            role = "body"

        if role == "subtitle":
            report["subtitle_count"] += 1
        classified.append((el, role))

    # Pass 3: style each block. prev_role lets a subtitle sit snug (margin-top
    # 0) when it immediately follows the main title.
    prev_role = None
    for el, role in classified:
        if el.name in ("ol", "ul"):
            _style_list(el, soup)
        elif el.name == "table" or (el.name == "div" and el.find("table") is not None):
            _style_table_block(el, soup)
        else:
            _style_paragraph(el, role, soup, prev_role=prev_role)
        prev_role = role

    # Pass 4: flatten to the classified blocks in document order. No empty
    # spacer paragraphs (Freshdesk strips them into parentless <br><br> that
    # collapse to size 13) -- all vertical spacing is carried by each block's
    # margin-top / margin-bottom set in Pass 3.
    blocks_only = [el for el, _role in classified]
    soup.clear()
    for el in blocks_only:
        soup.append(el)

    return str(soup), report


# --------------------------------------------------------------------------- #
# Sheet parsing
# --------------------------------------------------------------------------- #

ARTICLE_ID_RE = re.compile(r"/articles/(\d+)")


def read_sheet(path):
    """Read the 4 named columns; ignore any others. Returns a list of records:
    {id, category, folder, title, url, group}."""
    records = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Map headers case/space-insensitively to the exact names we need.
        headers = {(h or "").strip().lower(): h for h in reader.fieldnames or []}

        def col(row, name):
            key = headers.get(name.lower())
            return (row.get(key, "") or "").strip() if key else ""

        for row in reader:
            url = col(row, "Freshdesk Internal KB Hyperlink")
            m = ARTICLE_ID_RE.search(url)
            if not m:
                continue
            category = col(row, "KB Category")
            group = ("user_guide"
                     if category.strip().lower() == USER_GUIDE_CATEGORY
                     else "non_user_guide")
            records.append({
                "id": m.group(1),
                "category": category,
                "folder": col(row, "Folder"),
                "title": col(row, "KB Title"),
                "url": url,
                "group": group,
            })
    return records


# --------------------------------------------------------------------------- #
# Freshdesk API client
# --------------------------------------------------------------------------- #

class Freshdesk:
    def __init__(self, domain, api_key, pause=DEFAULT_PAUSE):
        self.base = f"https://{domain}/api/v2"
        self.auth = (api_key, "X")
        self.pause = pause
        self.session = requests.Session()

    def _request(self, method, url, **kwargs):
        while True:
            resp = self.session.request(method, url, auth=self.auth,
                                        timeout=60, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "5") or "5")
                print(f"  429 rate limited; sleeping {wait}s", flush=True)
                time.sleep(wait + 1)
                continue
            time.sleep(self.pause)  # small courtesy pause between requests
            return resp

    def get_article(self, article_id):
        url = f"{self.base}/solutions/articles/{article_id}"
        resp = self._request("GET", url)
        resp.raise_for_status()
        return resp.json()

    def update_article(self, article_id, description):
        url = f"{self.base}/solutions/articles/{article_id}"
        resp = self._request("PUT", url, json={"description": description})
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------- #
# Selection / orchestration
# --------------------------------------------------------------------------- #

def select_records(records, ids, limit):
    if ids:
        wanted = set(str(i) for i in ids)
        records = [r for r in records if r["id"] in wanted]
    if limit is not None:
        records = records[:limit]
    return records


def transform_record(description, record):
    if record["group"] == "user_guide":
        return transform_user_guide(description, record["title"])
    return transform_non_user_guide(description, record["title"])


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def print_warnings(rows):
    not_found = [r for r in rows if r.get("title_found") is False]
    uncertain = [r for r in rows if r.get("uncertain")]
    print("\n" + "=" * 60)
    print("WARNINGS")
    print("=" * 60)
    if not_found:
        print(f"\nTitle NOT found ({len(not_found)}):")
        for r in not_found:
            print(f"  - {r['id']}  [{r['group']}]  {r['kb_title']!r}")
    else:
        print("\nTitle not found: none")
    if uncertain:
        print(f"\nUncertain subtitle detection ({len(uncertain)}):")
        for r in uncertain:
            print(f"  - {r['id']}  {r['group']}")
            for n in r.get("notes", []):
                if n != "title not found":
                    print(f"        {n}")
    else:
        print("\nUncertain subtitle detection: none")
    print()


def classification_row(record, report):
    return {
        "article_id": record["id"],
        "group": record["group"],
        "kb_title": record["title"],
        "url": record["url"],
        "title_found": report["title_found"],
        "detected_title": report["detected_title"],
        "subtitle_count": report["subtitle_count"],
        "uncertain": report["uncertain"],
        "notes": " | ".join(report["notes"]),
    }


def write_classification_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = ["article_id", "group", "kb_title", "url", "title_found",
              "detected_title", "subtitle_count", "uncertain", "notes"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def mode_inspect(fd, records, out_dir, limit):
    n = limit if limit is not None else 3
    sel = records[:n]
    dest = os.path.join(out_dir, "inspect")
    print(f"INSPECT: dumping {len(sel)} raw article(s) to {dest}")
    for r in sel:
        art = fd.get_article(r["id"])
        write_text(os.path.join(dest, f"{r['id']}.html"), art.get("description") or "")
        write_text(os.path.join(dest, f"{r['id']}.json"),
                   json.dumps(art, indent=2, ensure_ascii=False))
        print(f"  {r['id']}  [{r['group']}]  {r['title']!r}")
    print("Done.")


def mode_dry_run(fd, records, out_dir):
    dest = os.path.join(out_dir, "dry_run")
    rows = []
    print(f"DRY RUN: transforming {len(records)} article(s) -> {dest} (no live changes)")
    for r in records:
        art = fd.get_article(r["id"])
        before = art.get("description") or ""
        after, report = transform_record(before, r)
        write_text(os.path.join(dest, f"{r['id']}_before.html"), before)
        write_text(os.path.join(dest, f"{r['id']}_after.html"), after)
        row = classification_row(r, report)
        rows.append(row)
        print(f"  {r['id']}  [{r['group']}]  title_found={report['title_found']} "
              f"subtitles={report['subtitle_count']}")
    write_classification_csv(os.path.join(dest, "classification.csv"), rows)
    print(f"Wrote classification: {os.path.join(dest, 'classification.csv')}")
    print_warnings(rows)


def mode_apply(fd, records, out_dir):
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(out_dir, "backups", stamp)
    applied_dir = os.path.join(out_dir, "applied", stamp)
    rows = []
    manifest = []
    print(f"APPLY: {len(records)} article(s). Backups -> {backup_dir}")

    # 1) Back up current HTML for every selected article FIRST.
    for r in records:
        art = fd.get_article(r["id"])
        before = art.get("description") or ""
        write_text(os.path.join(backup_dir, f"{r['id']}.html"), before)
        manifest.append({"id": r["id"], "group": r["group"],
                         "title": r["title"], "url": r["url"]})
        r["_before"] = before  # cache so we don't re-fetch
    write_text(os.path.join(backup_dir, "manifest.json"),
               json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"  backed up {len(records)} article(s).")

    # 2) Transform + PUT.
    for r in records:
        before = r["_before"]
        after, report = transform_record(before, r)
        write_text(os.path.join(applied_dir, f"{r['id']}_after.html"), after)
        fd.update_article(r["id"], after)
        rows.append(classification_row(r, report))
        print(f"  updated {r['id']}  [{r['group']}]  "
              f"title_found={report['title_found']} subtitles={report['subtitle_count']}")

    write_classification_csv(os.path.join(applied_dir, "classification.csv"), rows)
    print(f"Applied. Backups: {backup_dir}")
    print(f"To roll back:  python freshdesk_kb_formatter.py restore --backup-dir {backup_dir}")
    print_warnings(rows)


def mode_restore(fd, backup_dir, ids, limit):
    manifest_path = os.path.join(backup_dir, "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            entries = json.load(f)
        id_list = [e["id"] for e in entries]
    else:
        id_list = [fn[:-5] for fn in os.listdir(backup_dir) if fn.endswith(".html")]
    if ids:
        wanted = set(str(i) for i in ids)
        id_list = [i for i in id_list if i in wanted]
    if limit is not None:
        id_list = id_list[:limit]

    print(f"RESTORE: re-uploading {len(id_list)} article(s) from {backup_dir}")
    for aid in id_list:
        path = os.path.join(backup_dir, f"{aid}.html")
        if not os.path.exists(path):
            print(f"  SKIP {aid}: no backup file")
            continue
        with open(path, encoding="utf-8") as f:
            html = f.read()
        fd.update_article(aid, html)
        print(f"  restored {aid}")
    print("Done.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="Normalize BitGo Freshdesk KB article formatting.")
    ap.add_argument("mode", nargs="?", default="dry_run",
                    choices=["inspect", "dry_run", "apply", "restore"],
                    help="default: dry_run")
    ap.add_argument("--sheet", help="path to the CSV export of the article sheet")
    ap.add_argument("--ids", nargs="+", help="limit run to these article IDs")
    ap.add_argument("--limit", type=int, help="cap the number of articles processed")
    ap.add_argument("--out", default="fd_out", help="output directory (default: fd_out)")
    ap.add_argument("--domain", default=os.environ.get("FRESHDESK_DOMAIN", "bitgo.freshdesk.com"),
                    help="Freshdesk API domain (default: bitgo.freshdesk.com)")
    ap.add_argument("--backup-dir", help="[restore] backup folder to re-upload from")
    ap.add_argument("--pause", type=float, default=DEFAULT_PAUSE,
                    help=f"seconds between API requests (default: {DEFAULT_PAUSE})")
    args = ap.parse_args(argv)

    api_key = os.environ.get("FRESHDESK_API_KEY")
    if not api_key:
        sys.exit("ERROR: set the FRESHDESK_API_KEY environment variable.")

    fd = Freshdesk(args.domain, api_key, pause=args.pause)

    if args.mode == "restore":
        if not args.backup_dir:
            sys.exit("ERROR: restore requires --backup-dir")
        mode_restore(fd, args.backup_dir, args.ids, args.limit)
        return

    if not args.sheet:
        sys.exit("ERROR: this mode requires --sheet <path to CSV>")
    records = read_sheet(args.sheet)
    print(f"Loaded {len(records)} article(s) from sheet "
          f"({sum(1 for r in records if r['group']=='user_guide')} user-guide, "
          f"{sum(1 for r in records if r['group']=='non_user_guide')} non-user-guide).")
    records = select_records(records, args.ids, args.limit)
    print(f"Selected {len(records)} for this run.")

    if args.mode == "inspect":
        mode_inspect(fd, records, args.out, args.limit)
    elif args.mode == "dry_run":
        mode_dry_run(fd, records, args.out)
    elif args.mode == "apply":
        mode_apply(fd, records, args.out)


if __name__ == "__main__":
    main()