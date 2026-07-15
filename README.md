# Formatting stuff PMs send your way

This is a Python tool designed to standardize Help Center HTML drafts on Freshdesk via the Freshdesk Solutions API. Note that you must enable your Freshdesk API key (and obviously do not hard code it in)

---

## The Spacing & Cleaning Engine (CSV Triggers)

The script processes articles based on the **`KB Category`** column in your csv sheet. Refer to `Example.csv` You must set the entire **`KB Category`** column to "User Guide" (if the column is anything else, the program will only change the color of the title).

* **What it does:** Converts all headings to standard paragraphs, overrides all fonts to Arial, strips out custom background/font colors (except for links), flattens nested headings inside lists, unwraps visual text-boxes, and enforces strict CSS margin-driven vertical spacing. Refer to the formatting guide found [here](https://support.bitgo.com/support/solutions/articles/158000450243-formatting-guide-for-kb-articles-new-and-old): 

## Prerequisites

* **Python 3.11+**
* **Required Libraries:** `requests`, `beautifulsoup4`

### Quick Install
```bash
python3 -m pip install requests beautifulsoup4 --break-system-packages
```

---

## Configuration & Credentials

The script requires a private Freshdesk API key loaded as an environment variable to prevent hardcoding credentials in the repository:

* **Mac / Linux:**
  ```bash
  export FRESHDESK_API_KEY="your_freshdesk_api_key_here"
  ```
* **Windows (PowerShell):**
  ```powershell
  $env:FRESHDESK_API_KEY="your_freshdesk_api_key_here"
  ```

---

## Execution Workflow

Always follow this three-step workflow (**Dry Run** $\rightarrow$ **Verify** $\rightarrow$ **Apply**) to ensure no live content is modified unexpectedly.

### Step 1: Execute a Dry Run (Read-Only)
The default mode is `dry_run`. This is completely read-only. It downloads your target articles, cleans them locally, and outputs them as previews:
```bash
python3 "better formatter.py" dry_run --sheet Example.csv
```

### Step 2: Verify the Previews
1. Open the generated **`fd_out/dry_run`** folder in VS Code or your browser.
2. Review the `.html` files to verify that the messy styles, custom colors, and vertical breaks have been normalized.
3. Review `classification.csv` inside that folder to check for any `title_found = False` flags. 
   *(Note: If `title_found` is `False`, the title in the CSV did not match the text inside the article body. Update the title in the CSV to match the exact in-body text and re-run).*

### Step 3: Apply the Changes Live
Once the previews look correct, push the normalized articles live to Freshdesk:
```bash
python3 "better formatter.py" apply --sheet Example.csv
```

---

## What the Normalization Engine Does (Under the Hood)

When Normalization Mode is triggered (via `Bitgo User Guide` in the CSV) [1], the script applies the following cleanups:

* **Font Normalization:** Standardizes all elements and nested text runs to `Arial, Helvetica, sans-serif`.
* **Margin-Driven Spacing:** Wipes out empty spacer paragraphs (which are prone to getting stripped by Freshdesk's sanitizer into parentless `<br>` tags) [2]. Instead, it applies standard vertical spacing via inline CSS margins:
  - **Body Paragraphs:** `margin-top: 0px`, `margin-bottom: 16px` (forces exactly a size-16 equivalent line break between blocks).
  - **Subtitles:** `margin-top: 24px`, `margin-bottom: 16px`.
  - **Title:** `margin-top: 0px`, `margin-bottom: 16px`, `color: rgb(22, 71, 219)`.
  - **Title-Subtitle Nesting:** If a subtitle immediately follows the main page title, the script sets the subtitle's top margin to `0px` so they sit snug without an empty gap.
* **Single-Cell Table Unwrapping:** Finds tables acting strictly as visual layout containers (exactly 1 row and 1 column). It extracts their inner paragraphs and lists, places them cleanly in the main document flow, and deletes the table borders and wrappers [2]. Standard multi-row/multi-column comparison tables are left untouched.
* **Deep Edge-Break Trimming:** Scans deeply nested inline wrappers (like `<span>` and `<strong>`) to delete trailing `<br>` tags, preventing double-spaced gaps between consecutive images.
* **Nested Heading Fixes:** Demotes non-standard heading tags (`h1`-`h6`) found nested inside list items (`<li>`) to standard text, stripping their custom grey/Helvetica styles so they inherit the standard list formatting.

---

## Safety & Rollbacks

Whenever you run `apply` mode, the script performs an **automatic backup** first. It downloads the original, unmodified HTML of every targeted article and saves it inside `fd_out/backups/[timestamp]`.

If you publish the changes and need to revert, run the `restore` command and point it to your timestamped backup folder:
```bash
python3 "better formatter.py" restore --backup-dir "fd_out/backups/YYYYMMDD_HHMMSS"
```


