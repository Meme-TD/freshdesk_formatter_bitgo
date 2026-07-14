import os
import re #extracting article IDs
import sys
import csv
import time
import pathlib
import requests #http calls to freshdesk
from collections import Counter
from bs4 import BeautifulSoup #editing html


FRESHDESK_DOMAIN = "bitgo"
API_KEY = os.environ.get("FRESHDESK_API_KEY", "")
CSV_PATH = "CleanSheet.csv"

MODE = "dry_run" #defualt mode if i dont pass any arguments. dry run processes everything without applying stuff to freshdesk directly
INSPECT_COUNT = 3
LIMIT = None #number of articles processed at the same time
ONLY_ARTICLE_IDS = []       
REQUEST_PAUSE = 0.6

TITLE_BLUE_OTHER = "#1647DB" #rgb(22,71,219)
BLACK            = "#000000"
FONT_FAMILY      = "Arial, sans-serif"
TITLE_SIZE       = "30px"
SUBTITLE_SIZE    = "24px"
BODY_SIZE        = "16px"

REMOVE_EMPTY_SPACERS  = True
SUBTITLE_MAX_CHARS    = 120    
SUBTITLE_SIZE_MARGIN_PX = 0.5  

#FRESHDESK APIIIIIIII
BASE = f"https://{FRESHDESK_DOMAIN}.freshdesk.com/api/v2"
AUTH = (API_KEY, "X")


def fd_get_article(aid):
    return _request("GET", f"{BASE}/solutions/articles/{aid}") #get article


def fd_update_article(aid, html):
    return _request("PUT", f"{BASE}/solutions/articles/{aid}", json={"description": html}) #edit article


def _request(method, url, **kw):
    for _ in range(6):
        r = requests.request(method, url, auth=AUTH, timeout=30, **kw)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "10"))
            print(f"  rate limited {wait}s"); time.sleep(wait); continue
        r.raise_for_status(); return r.json()
    raise RuntimeError(f"Too many retries for {url}") #prevent infinite loop if rate limit keeps happening

#csv to a list of dictionaries
def read_articles():
    out = []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f: #strips encoding bom
        reader = csv.DictReader(f)
        for row in reader:
            category = row.get("KB Category")
            title    = row.get("KB Title")
            link     = row.get("Freshdesk Internal KB Hyperlink")
            if not any([category, title, link]): #blank rows
                continue
            m = re.search(r"/articles/(\d+)", str(link)) if link else None #gets article ID from link
            if not m:
                continue
            out.append({"id": m.group(1), "title": (title or "").strip(), #handles white space and None values
                        "category": str(category).strip(),
                        "is_user_guide": str(category).strip().lower() == "bitgo user guide"})
    return out

#thank you claude for the brilliant idea of using dictionaries instead of editing strings
def parse_style(s):
    d = {}
    for part in (s or "").split(";"): #turns "color: red; font-size: 16px" into {"color": "red", "font-size": "16px"}
        if ":" in part:
            k, v = part.split(":", 1)
            d[k.strip().lower()] = v.strip()
    return d


def dump_style(d):
    return "; ".join(f"{k}: {v}" for k, v in d.items())

#turns dictionary back itno string
def set_styles(el, **props):
    d = parse_style(el.get("style", ""))
    for k, v in props.items():
        d[k.replace("_", "-")] = v #turn into css property
    el["style"] = dump_style(d)


def norm(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower() #turns tabs, newlines, multiple spaces into 1 space

#converts points to pixels (idk if this is really needed but it's good to have)
def size_to_px(v):
    m = re.match(r"([\d.]+)\s*(px|pt)?", (v or "").strip())
    if not m:
        return None
    num = float(m.group(1)); unit = m.group(2) or "px"
    return num * (96.0 / 72.0) if unit == "pt" else num

#finds title in freshdesk article body
def find_title_element(soup, article_title):
    want = norm(article_title)
    if want:
        for tags in (["h1", "h2", "h3", "h4", "h5", "h6"],
                     ["p", "strong", "b", "span", "div"]):
            for el in soup.find_all(tags):
                if norm(el.get_text()) == want:
                    return el
    return None

#clear default Froala float-left and center the image
def center_image(img):
    set_styles(img, float="none", display="block",
               margin_left="auto", margin_right="auto")

#child and descendent elements inherit color of their parent
def override_descendant_color(el, color):
    for d in el.find_all(True):
        st = parse_style(d.get("style", ""))
        if "color" in st:
            st["color"] = color
            d["style"] = dump_style(st)


def restyle_ug_block(block, size, color, bold):
    set_styles(block, font_family=FONT_FAMILY, font_size=size)
    if bold:
        set_styles(block, font_weight="bold")
    if color is not None:
        set_styles(block, color=color)
    for d in block.find_all(True):
        st = parse_style(d.get("style", ""))
        st.pop("font-family", None) #remove if present, else none
        st.pop("font-size", None)
        if color is not None:
            st.pop("color", None)
        if bold:
            st.pop("font-weight", None)
        if st:
            d["style"] = dump_style(st)
        elif "style" in d.attrs:
            del d["style"]

HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
#removes line breaks and empty space
def is_spacer(el):
    return (el.name in ["p"] + HEADINGS
            and not el.get_text(strip=True)
            and el.find("img") is None)

#min size to find body text
def block_min_text_size_px(el):
    sizes = []
    for node in [el] + el.find_all(True): #for node in element and all its descendants
        st = parse_style(node.get("style", ""))
        if "font-size" in st and node.get_text(strip=True):
            px = size_to_px(st["font-size"])
            if px:
                sizes.append(px)
    return min(sizes) if sizes else None


#non user guide
def transform_other(html, article_title):
    soup = BeautifulSoup(html or "", "html.parser")
    title_el = find_title_element(soup, article_title)
    if title_el is not None:
        set_styles(title_el, color=TITLE_BLUE_OTHER)
        override_descendant_color(title_el, TITLE_BLUE_OTHER)
    for img in soup.find_all("img"):
        center_image(img)
    report = {"title": title_el.get_text(strip=True) if title_el else None, "subtitles": []}
    return str(soup), (title_el is not None), report #returns html, whether the title was found, and report of changes


#user guide
def transform_user_guide(html, article_title):
    soup = BeautifulSoup(html or "", "html.parser")

    if REMOVE_EMPTY_SPACERS:
        for el in soup.find_all(["p"] + HEADINGS):
            if is_spacer(el):
                el.decompose()

    title_el = find_title_element(soup, article_title)

    title_protect_ids = set()
    if title_el is not None:
        title_protect_ids.add(id(title_el))
        for _d in title_el.find_all(True):          #descendants
            title_protect_ids.add(id(_d))
        for _p in title_el.parents:                 #ancestors
            title_protect_ids.add(id(_p))

    #finds blocks of text
    blocks = [el for el in soup.find_all(["p", "li"] + HEADINGS)
              if el.get_text(strip=True) or el.find("img")]

    #finds most common font size among non-title blocks
    sizes = []
    for el in blocks:
        if id(el) in title_protect_ids:
            continue
        s = block_min_text_size_px(el)
        if s:
            sizes.append(round(s, 1))
    body_px = Counter(sizes).most_common(1)[0][0] if sizes else None

    #classify subtitles
    subtitle_ids = set()
    subtitle_texts = []
    for el in blocks:
        if id(el) in title_protect_ids:
            continue
        is_heading = el.name in HEADINGS #checks if it's actually heading or just bold text
        looks_like_subtitle = False
        if not is_heading and el.name != "li":
            s = block_min_text_size_px(el)
            txt = el.get_text(strip=True)
            if (s is not None and body_px is not None #if size exists
                    and s > body_px + SUBTITLE_SIZE_MARGIN_PX #meaningfully bigger than body text
                    and 0 < len(txt) <= SUBTITLE_MAX_CHARS #shorter than 120 characters
                    and el.find("br") is None): #no line breaks in the middle
                looks_like_subtitle = True
        if is_heading or looks_like_subtitle:
            subtitle_ids.add(id(el))
            subtitle_texts.append(el.get_text(strip=True))

    #restyle title + subtitles + body
    if title_el is not None:
        restyle_ug_block(title_el, TITLE_SIZE, color=None, bold=True)
    for el in blocks:
        if id(el) in subtitle_ids:
            restyle_ug_block(el, SUBTITLE_SIZE, color=BLACK, bold=True)
    for el in blocks:
        if id(el) in title_protect_ids or id(el) in subtitle_ids:
            continue
        restyle_ug_block(el, BODY_SIZE, color=BLACK, bold=False)

    #headers to paragraphs (aka body text classification)
    for h in soup.find_all(HEADINGS):
        h.name = "p"

    #recolor hyperlinks
    for a in soup.find_all("a"):
        set_styles(a, color=TITLE_BLUE_OTHER)
        override_descendant_color(a, TITLE_BLUE_OTHER)

    #center images + add 1 line break
    for img in soup.find_all("img"):                      
        center_image(img)
        nxt = img.find_next_sibling()
        if getattr(nxt, "name", None) != "br":            
            img.insert_after(soup.new_tag("br"))
 
    #2 line breaks before each subheader
    for el in soup.find_all("p"):                         
        if id(el) in subtitle_ids:
            prev = el.find_previous_sibling() #strip any existing leading <br>
            while prev is not None and prev.name == "br":
                stale = prev
                prev = prev.find_previous_sibling()
                stale.decompose()
            if el.find_previous_sibling() is not None: #makes sure it isnt first block in the article cuz why would u indent a subtitle at the top of an article lol
                el.insert_before(soup.new_tag("br"))
                el.insert_before(soup.new_tag("br"))
    
    #1 line break after each paragraph
    for p in [e for e in soup.find_all("p")           
              if id(e) not in title_protect_ids and id(e) not in subtitle_ids]:  
        nxt = p.find_next_sibling()
        if getattr(nxt, "name", None) == "p" and id(nxt) not in subtitle_ids \
                and id(nxt) not in title_protect_ids:
            p.insert_after(soup.new_tag("br"))
 
    while soup.contents:
        tail = soup.contents[-1]
        tail_name = getattr(tail, "name", None)
        if tail_name == "br":
            tail.extract()
        elif tail_name is None and not str(tail).strip(): #trailing whitespace
            tail.extract()
        else:
            break
    for _ in range(3): #3 line breaks at end of article
        soup.append(soup.new_tag("br"))

    #report for dry run
    report = {"title": title_el.get_text(strip=True) if title_el else None,
              "subtitles": subtitle_texts, "body_px": body_px}
    return str(soup), (title_el is not None), report
 

def main():
    global MODE
    if len(sys.argv) > 1: #sets default to mode if theres nothing after the script name in command line
        MODE = sys.argv[1] #sys.argv[0] is the name of the script, sys.argv[1] is the first arg
    if not API_KEY:
        sys.exit("ERROR: set the FRESHDESK_API_KEY environment variable first.")

    articles = read_articles()
    if ONLY_ARTICLE_IDS:
        keep = set(ONLY_ARTICLE_IDS)
        articles = [a for a in articles if a["id"] in keep]
    if LIMIT:
        articles = articles[:LIMIT]

    #split into user and non user guide
    ug = [a for a in articles if a["is_user_guide"]]
    other = [a for a in articles if not a["is_user_guide"]]
    print(f"MODE={MODE}  user-guide={len(ug)}  other={len(other)}  total={len(articles)}")

    #inspect mode takes a few articles from ug/non ug and puts raw html into fd_inspect folder for manual review
    if MODE == "inspect":
        outdir = pathlib.Path("fd_inspect"); outdir.mkdir(exist_ok=True) #creates folder called fd_inspect for outputs
        for a in ug[:INSPECT_COUNT] + other[:INSPECT_COUNT]:
            data = fd_get_article(a["id"]); tag = "UG" if a["is_user_guide"] else "OTHER"
            (outdir / f"{tag}_{a['id']}.html").write_text(data.get("description") or "", encoding="utf-8")
            print(f"  dumped {tag} {a['id']}  title={data.get('title')!r}") #puts title in quotes to check for whitespace before/after
            time.sleep(REQUEST_PAUSE)
        print(f"Raw HTML in {outdir}/")
        return

    #dry run output folder
    dryroot = pathlib.Path("fd_dryrun")
    report_rows = []
    if MODE == "dry_run":
        dryroot.mkdir(exist_ok=True)

    #claude wrote the stuff below
    missing = []
    no_size_signal = []
    for i, a in enumerate(articles, 1):
        try:
            data = fd_get_article(a["id"])
        except Exception as e:
            print(f"  [{i}/{len(articles)}] GET {a['id']} FAILED: {e}"); continue
        html = data.get("description") or ""
        fn = transform_user_guide if a["is_user_guide"] else transform_other
        new_html, found, rep = fn(html, a["title"])
        if not found:
            missing.append(a["id"])
        if a["is_user_guide"] and rep.get("body_px") is None:
            no_size_signal.append(a["id"])
        tag = "UG" if a["is_user_guide"] else "OTHER"

        if MODE == "dry_run":
            (dryroot / f"{tag}_{a['id']}.before.html").write_text(html, encoding="utf-8")
            (dryroot / f"{tag}_{a['id']}.after.html").write_text(new_html, encoding="utf-8")
            report_rows.append({"id": a["id"], "group": tag, "sheet_title": a["title"],
                                "detected_title": rep["title"],
                                "n_subtitles": len(rep["subtitles"]),
                                "subtitles": " | ".join(rep["subtitles"])})
            print(f"  [{i}/{len(articles)}] {tag} {a['id']} title-found={found} "
                  f"subtitles={len(rep['subtitles'])}")
        elif MODE == "apply":
            try:
                fd_update_article(a["id"], new_html)
                print(f"  [{i}/{len(articles)}] updated {a['id']}")
            except Exception as e:
                print(f"  [{i}/{len(articles)}] PUT {a['id']} FAILED: {e}")
        time.sleep(REQUEST_PAUSE)

    if MODE == "dry_run" and report_rows:
        with open(dryroot / "_classification.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(report_rows[0].keys()))
            w.writeheader(); w.writerows(report_rows)
        print(f"\nBefore/after HTML + _classification.csv in {dryroot}/ — review before apply.")
    if missing:
        print(f"\nWARNING: in-body title not found for {len(missing)} articles: {missing}")
    if no_size_signal:
        print(f"\nWARNING: no font-size signal in {len(no_size_signal)} user-guide articles; "
              f"subtitle detection fell back to heading tags only — verify these manually: {no_size_signal}")


if __name__ == "__main__":
    main() 