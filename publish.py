import os
import csv
import sys
import time
import requests
import re

FRESHDESK_DOMAIN = os.environ.get("FRESHDESK_DOMAIN", "bitgo.freshdesk.com")
API_KEY = os.environ.get("FRESHDESK_API_KEY")
CSV_PATH = "CleanSheet.csv"
PAUSE = 0.5

if not API_KEY:
    sys.exit("ERROR: set the FRESHDESK_API_KEY environment variable first.")

#Read CleanSheet CSV to get all the article IDs
records = []
ARTICLE_ID_RE = re.compile(r"/articles/(\d+)")

with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    headers = {(h or "").strip().lower(): h for h in reader.fieldnames or []}
    
    key = headers.get("freshdesk internal kb hyperlink")
    for row in reader:
        url = (row.get(key, "") or "").strip() if key else ""
        m = ARTICLE_ID_RE.search(url)
        if m:
            records.append(m.group(1))

print(f"Loaded {len(records)} articles to publish.")

#Loop through each article and update its status to 2 (Published)
session = requests.Session()
auth = (API_KEY, "X")

print("Starting publishing...")
for i, aid in enumerate(records, 1):
    url = f"https://{FRESHDESK_DOMAIN}/api/v2/solutions/articles/{aid}"
    while True:
        resp = session.request("PUT", url, auth=auth, json={"status": 2}, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5") or "5")
            print(f"  Rate limited, sleeping {wait}s...")
            time.sleep(wait + 1)
            continue
        break
    
    if resp.status_code == 200:
        print(f"  [{i}/{len(records)}] Published article {aid} successfully!")
    else:
        print(f"  [{i}/{len(records)}] FAILED to publish article {aid}. Status: {resp.status_code}")
    
    time.sleep(PAUSE)

print("\nAll articles have been successfully published live!")