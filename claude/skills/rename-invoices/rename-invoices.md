---
name: rename-invoices
description: Rename supplier invoice PDFs in bulk based on their contents, using the pattern "YYYY-MM-DD [Company] - [Description] - Invoice [Number].pdf" (with an optional seller name after marketplace hosts like Amazon/eBay). Use this skill whenever Kevin asks to rename, tidy, sort, organise or "process" a folder of invoices — whether the request mentions Amazon specifically or just says things like "rename the invoices in _TO SORT", "tidy up my invoice folder", "the monthly accounts pile", "process the batch of receipts", or points at a folder full of supplier PDFs. Also use this when a single invoice PDF is dropped in and needs renaming to the same convention. Trigger even when the user doesn't mention the word "skill" — this is the default behaviour for any invoice-renaming task against Kevin's `_TO SORT` / accounts workflow.
---

# Rename invoices

## What this skill does

Walks every PDF in a folder (default: the user's `_TO SORT` accounts folder) and renames each one to a consistent, chronologically-sortable form by reading its contents. Non-PDF files (notably `.txt` notes) are ignored. Already-correctly-named files are skipped. Files the user has flagged as incomplete (prefixes like `TO GET`, `GET-INVOICE`, `TODO`) are left alone because they aren't ready yet.

## Naming pattern

```
YYYY-MM-DD [Company] - [Short description of item(s)] - Invoice [Invoice number].pdf
```

With one variation: when the invoice is from a **marketplace host** (Amazon, eBay, Etsy, AliExpress, etc.) and the item was **sold by a third party** rather than the host itself, append the seller short-name after the host:

```
YYYY-MM-DD [Host] [Seller] - [Description] - Invoice [Invoice number].pdf
```

So:
- Amazon-direct (Amazon Business EU S.à.r.l., Amazon.co.uk, Amazon EU S.à.r.l., Amazon Services Europe, etc.) → `Amazon` alone.
- Amazon third-party seller (e.g. UGREEN Group Ltd.) → `Amazon UGREEN`.
- eBay listing sold by a private seller or shop → `eBay [SellerShortName]`.
- Any non-marketplace supplier (Anthropic, DigiKey, RS Components, Farnell, NorthridgeFix, …) → just the company short name.

The `[Company]` portion is a **short, recognisable brand name**, not the full legal entity. Examples:
- `Amazon Business EU S.à.r.l., UK Branch` → `Amazon`
- `Anthropic Ireland, Limited` → `Anthropic`
- `RS Components Ltd` → `RS`
- `Premier Farnell UK Limited` → `Farnell`
- `Digi-Key Electronics` → `DigiKey`

If the company genuinely trades under a multi-word name, keep it concise but readable (e.g. `Mouser`, `LCSC`, `JLCPCB`).

## Description guidance

Aim for **3–8 words** that Kevin would recognise at a glance six months from now. Trim noise like pack quantities, colour codes, and marketing fluff unless essential. Combine multiple items with ` & ` (space-ampersand-space). Examples:

- `Plant Label Markers`
- `USB Sound Card Adapter`
- `Decaf Coffee & SanDisk 128GB USB Drive`
- `Whiteboard Marker Pens`
- `Claude Pro Monthly Subscription`
- `Hot Air Nozzles & Flux & Solder Mask`

If there are many line items (>4), summarise by category rather than listing everything: e.g. `PCB Rework Supplies (7 items)`.

## Invoice number

Use the **primary invoice / document / order number** printed on the PDF, unchanged (preserve case, hyphens and slashes — but replace `/` with `-` because filesystems). Common labels to look for: "Invoice number", "Invoice No.", "Document number", "Order number" (only if no invoice number is present). Examples: `GB639V1QABEI`, `9BF0758D-634125`, `INV-2026-0412`.

## Date

Use the **invoice/issue date**, not the payment or due date. Output it as `YYYY-MM-DD`. Handle common UK/US/ISO formats (`12 April 2026`, `April 11, 2026`, `11/04/2026`, `2026-04-11`).

## Filesystem-safe characters

The final filename must be valid on Windows, macOS and Linux. Strip or replace these characters from any field you interpolate:
- `/ \ : * ? " < > |` → remove or replace with ` - `
- Leading/trailing whitespace → trim
- Collapse runs of spaces to a single space
- Preserve non-ASCII letters (é, à, etc.) — they're fine on modern filesystems

## Workflow

Follow these steps in order.

### 1. Determine the target folder

Default to the user's accounts inbox. In this Cowork environment, that is the mounted folder `/sessions/wonderful-confident-thompson/mnt/_TO SORT` (displayed to the user as "the folder you selected" or "your _TO SORT folder" — never expose the internal `/sessions/...` path). If the user names a different folder, use that instead.

### 2. Scan the folder

Use the bundled scanner to produce a JSON report of every PDF, its current name, whether it already matches the full naming pattern, whether it's flagged as "not ready" (e.g. `TO GET…`, `GET-INVOICE…`, `TODO…` prefixes), and the extracted text of its first 2 pages.

The scanner sits alongside this SKILL.md at `scripts/scan_pdfs.py`. Invoke it by resolving its path relative to this file — don't hard-code an absolute path. A robust one-liner that works wherever the skill is installed:

```bash
SKILL_DIR="$(dirname "$(find /sessions /home /root -name 'SKILL.md' -path '*rename-invoices*' 2>/dev/null | head -1)")"
python3 "$SKILL_DIR/scripts/scan_pdfs.py" "<folder>"
```

Or more simply, if you already know where you read the SKILL.md from, just use `<that-dir>/scripts/scan_pdfs.py` — no searching needed.

The scanner writes its JSON to stdout. Keys per file: `path`, `filename`, `already_renamed` (bool), `not_ready` (bool — true when the filename starts with a "not yet ready" marker), `text` (first ~6000 chars of extracted text), `error` (non-null on extraction failure).

### 3. Decide what to do per PDF

For each entry in the scanner output:

| Scanner flags | Action |
|---|---|
| `not_ready = true` | **Skip.** The user has explicitly marked this file as "not ready". |
| `already_renamed = true` **and** all five parts look complete (valid date, non-empty company, non-empty description, non-empty invoice number, `.pdf` extension) | **Skip.** |
| `already_renamed = true` **but** a field is missing/empty/obviously wrong | **Re-process.** Extract from the PDF contents and rename. |
| `already_renamed = false` | **Process.** Extract from the PDF contents and rename. |
| `error` non-null | **Skip and report.** Don't guess names from filenames alone. |

The "completeness" check exists because Kevin wants partial renames fixed automatically, but fully-correct names to be left alone — re-renaming a correct file wastes time and risks introducing drift.

### 4. Extract fields from the PDF text

From the `text` returned by the scanner, identify:

1. **Issuer / host company** — usually the entity at the top of the invoice, or after "From:" or in the "Sold by" field for marketplace direct sales. Normalise to a short brand name (see earlier examples).
2. **Third-party seller** (marketplace case only) — look for "Sold by [Seller]" on Amazon/eBay invoices where the seller is not the marketplace itself. If host and seller are the same legal family ("Sold by Amazon Business EU" on an Amazon Business invoice), treat as direct and omit the seller.
3. **Invoice date** — labels: "Invoice date", "Date of issue", "Issue date", "Tax date", "Invoice/order date".
4. **Invoice number** — labels: "Invoice number", "Invoice #", "Invoice No.", "Document number". Fall back to "Order number" only if no invoice number exists at all (be sure to note this in the summary).
5. **Description** — the item(s) purchased, summarised per the guidance above.

If any of these cannot be determined confidently, **do not rename** — add the file to the "needs attention" list in the summary with a short reason. It is better to skip one than to guess wrong.

### 5. Apply the renames

Use `mv` (via the Bash tool) one at a time. Before each rename, check that the target filename doesn't already exist in the folder; if it does, append ` (2)`, ` (3)`, … to disambiguate. Quote paths properly — filenames contain spaces and ampersands.

```bash
mv "/.../<old>.pdf" "/.../2026-04-12 Amazon - Plant Label Markers - Invoice GB639V1QABEI.pdf"
```

### 6. Report results

Summarise concisely at the end. Kevin prefers concise technical output; no excessive postamble. Use a format like:

```
Renamed (N):
  <old filename>
    → <new filename>
  …

Skipped – already named correctly (M):
  <filename>
  …

Skipped – flagged not-ready (K):
  <filename>

Needs attention (L):
  <filename>  — <reason, e.g. "no invoice number found, only order number">
```

Each renamed file should then be linked individually with `computer://` URLs so Kevin can open them from the chat. The link format the desktop app accepts is `computer://<absolute-windows-path-with-%20-for-spaces>` — the scanner outputs a `windows_path` field derived from the symlink target (when available) that you can URL-encode and drop straight into the link. If the mount doesn't expose the Windows path, fall back to the Cowork-mounted path; the app rewrites it.

## Marketplace-vs-direct decision tree

Amazon invoices are especially fiddly — the user's last batch had three direct and one third-party. Use this logic:

1. Look for a "Sold by" line (Amazon invoices: left column, near the top; eBay: on the order summary).
2. If "Sold by" contains "Amazon" (any Amazon legal entity) → direct → `Amazon` only.
3. If "Sold by" is a different company name → third-party → `Amazon [ShortSellerName]`.
4. If the invoice is *from* a company that happens to sell on Amazon but the PDF you have is the supplier's own VAT invoice (not the Amazon invoice), treat it as a direct supplier invoice with just that company's short name — **not** `Amazon X`. Kevin occasionally downloads both the Amazon record and the seller's own tax invoice; only the Amazon-branded PDF gets `Amazon [Seller]`.

Seller short-names: strip "Ltd", "Inc", "GmbH", "Group", "Co.", "Limited", "Technology Co.", etc. Keep the recognisable brand. `UGREEN Group Limited` → `UGREEN`. `Anker Innovations Limited` → `Anker`.

## Worked examples

**Example 1 — Amazon direct, single item**
Sold by: `Amazon Business EU S.à.r.l., UK Branch`
Invoice date: `12 April 2026`
Invoice number: `GB639V1QABEI`
Item: `Plant label markers, 100 pack, copper-look`
→ `2026-04-12 Amazon - Plant Label Markers - Invoice GB639V1QABEI.pdf`

**Example 2 — Amazon third-party, single item**
Sold by: `UGREEN Group Limited`
Invoice date: `12 April 2026`
Invoice number: `GB601FRJDXVSJI`
Item: `UGREEN USB External Stereo Sound Card Adapter`
→ `2026-04-12 Amazon UGREEN - USB Sound Card Adapter - Invoice GB601FRJDXVSJI.pdf`

**Example 3 — Amazon direct, multiple items**
Sold by: `Amazon EU S.à.r.l.`
Invoice date: `12 April 2026`
Invoice number: `GB639V1UABEI`
Items: Taylors of Harrogate decaf coffee; SanDisk 128GB USB drive
→ `2026-04-12 Amazon - Decaf Coffee & SanDisk 128GB USB Drive - Invoice GB639V1UABEI.pdf`

**Example 4 — Non-marketplace supplier**
Issuer: `Anthropic Ireland, Limited`
Invoice date: `April 11, 2026`
Invoice number: `9BF0758D-634125`
Item: `Claude Pro` (subscription line `Apr 11–May 11, 2026`)
→ `2026-04-11 Anthropic - Claude Pro Monthly Subscription - Invoice 9BF0758D-634125.pdf`

**Example 5 — Not ready / missing invoice number**
Filename: `GET-INVOICE Order received – NorthridgeFix.pdf`
→ **Skip.** The `GET-INVOICE` prefix is Kevin's "still need the real invoice" marker — leave untouched and mention it in the summary under "flagged not-ready".

## Common pitfalls (earned the hard way)

- **Don't trust the filename alone.** Amazon's default filenames are often random hashes; derive every field from the PDF's contents.
- **Don't confuse billing date with invoice date.** Some vendors print both. The invoice/issue date is what we want.
- **Don't use the "Sold by" block as the description source.** That's the seller; descriptions come from the line items.
- **Beware reverse-charge VAT invoices** (e.g. Anthropic): the supplier is in a different country from the billing address. The issuer is still the supplier — don't use the billing entity.
- **Amazon sometimes uses "Order number" without an "Invoice number" on dispatch notes.** If you have a dispatch note rather than a VAT invoice, the correct invoice PDF is a separate download. Flag this as "needs attention – dispatch note only, no VAT invoice" rather than renaming with the order number.
- **When in doubt, skip and flag.** The cost of a wrong rename (hunting down what happened) vastly exceeds the cost of a one-line warning.

## Bundled scripts

- `scripts/scan_pdfs.py` — scans a folder, returns JSON with per-PDF metadata and extracted text. Run with `python3 scripts/scan_pdfs.py "<folder>"`. Uses `pdftotext -layout` if available (fastest/cleanest) with a pdfplumber fallback.
