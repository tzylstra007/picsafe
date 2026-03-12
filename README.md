# PicSafe v2

**A personal media management pipeline that syncs Apple Photos → Google Photos with intelligent face-tagging, quality filtering, and per-person album management.**

PicSafe bridges the gap between Apple's Photos app and Google Photos by:
- Scanning your library nightly with [osxphotos](https://github.com/RhetTbull/osxphotos)
- Auditing each photo for faces, GPS, and enhancement status
- Minting unique PicSafe IDs for shareable photos
- Uploading ready photos to personal Google Photos albums
- Writing Netlify vanity redirect URLs (e.g. `picsafe.net/daffodil`) for each Go Live person
- Tracking everything in AppSheet + Smartsheet for visibility

---

## Architecture

```
Apple Photos Library
        │
        ▼
┌─────────────────────────────┐
│  picsafe_bridge_v2_appsheet │  ← Phase 1: Scan + audit + mint IDs
│       (7-Step Pipeline)     │    Creates/updates records in AppSheet
│                             │    Updates "Photos - AP" / "Videos - AP"
│                             │    in Smartsheet dashboard
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  picsafe_gphotos_publisher  │  ← Phase 2: Upload + album management
│                             │    AppSheet PENDING → Google Photos
│                             │    Writes Netlify _redirects vanity URLs
│                             │    Updates Smartsheet dashboard:
│                             │      Photos/Videos - Google
│                             │      Google Photos Share Link
│                             │      Last Album Update
│                             │      DNS Redirected
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│    picsafe_git_sync.sh      │  ← Phase 3: Commit + push _redirects
│                             │    Deploys vanity URL changes to Netlify
└─────────────────────────────┘
```

### Nightly Automation

The full pipeline runs automatically each night via macOS launchd:

| Time | What | Where |
|------|------|--------|
| 11:00 PM | `picsafe_nightly.sh` (bridge → publisher → git sync) | Mac (launchd) |
| 7:00 AM | Morning health check: run history + asset stats + dashboard review | Claude scheduled task |

The launchd plist is at `com.picsafe.nightly.plist`. Install with:
```bash
cp com.picsafe.nightly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.picsafe.nightly.plist
```

Logs go to `~/PicSafe/logs/nightly_YYYY-MM-DD.log`.

---

## The 7-Step Pipeline (Bridge)

Each photo in Apple Photos passes through:

1. **Heart ↔ Keyword Sync** — Syncs Apple Photos heart (favorite) to a `Favorite` keyword
2. **Face Recognition Bridge** — Reads Apple's face recognition data
3. **Missing Faces Detector** — Flags unnamed faces above a quality threshold (0.4) as `facesmissing`
4. **AI Scene Analysis** — Syncs Apple's ML-generated scene labels as keywords
5. **GPS Audit** — Checks for missing location data; adds `!Audit: Missing GPS` blocker tag
6. **Enhancement Audit** — Checks for missing edits on 3+ Star photos; adds `!Audit: Not Enhanced` blocker tag
7. **Transaction Logging / Write-back** — Mints PicSafe IDs, writes keywords back to Apple Photos via AppleScript, syncs to AppSheet

### PicSafe Ready Criteria

A photo is considered **PicSafe Ready** (eligible for upload) when:
- ✅ 3 Star or higher rating
- ✅ At least one "Go Live" person is tagged
- ✅ No blocker tags (`facesmissing`, `!Audit: Missing GPS`, `!Audit: Not Enhanced`)
- ✅ Not an excluded date

### Face Quality Filtering

PicSafe uses osxphotos' face quality score to filter out blurry or background faces. Only faces with `quality >= 0.4` are considered "significant" — faces below this threshold won't trigger a `facesmissing` flag. This prevents false positives from crowd shots and background people.

---

## Prerequisites

- **macOS** with Apple Photos
- **Python 3.11+**
- **osxphotos** (requires Full Disk Access for the terminal app)
- An **AppSheet** account with the PicSafe app
- A **Smartsheet** account with the PicSafe dashboard sheet
- **Google Photos API** credentials (OAuth 2.0)
- A **Netlify** site connected to this repo (for vanity redirect URLs)

### Required Smartsheet Dashboard Columns

The bridge and publisher expect these columns in your Smartsheet dashboard (sheet ID configurable in the scripts):

| Column | Type | Written by | Purpose |
|--------|------|-----------|---------|
| Person Name | TEXT_NUMBER (primary) | — | Apple Photos person name |
| Go Live | CHECKBOX | Manual | Enables upload for this person |
| Smell Adjectives | TEXT_NUMBER | Manual | Vanity URL slug (e.g. `daffodil`) |
| Photos - AP | TEXT_NUMBER | Bridge | PicSafe Ready photo count in Apple Photos |
| Videos - AP | TEXT_NUMBER | Bridge | PicSafe Ready video count in Apple Photos |
| Photos - Google | TEXT_NUMBER | Publisher | Photos in Google Photos album |
| Videos - Google | TEXT_NUMBER | Publisher | Videos in Google Photos album |
| Google Photos Share Link | TEXT_NUMBER | Publisher | Shareable album URL |
| Last Album Update | DATE | Publisher | Date of last upload or prune |
| DNS Redirected | CHECKBOX | Publisher | True when vanity URL is live |

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/tzylstra007/picsafe.git
cd picsafe

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up credentials
cp picsafe_secrets_template.py picsafe_secrets.py
# Edit picsafe_secrets.py with your API keys (this file is git-ignored)

# 5. Grant Full Disk Access to Terminal.app
# System Settings → Privacy & Security → Full Disk Access → add Terminal

# 6. Install nightly launchd job
cp com.picsafe.nightly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.picsafe.nightly.plist
```

---

## Configuration

Edit the `CONFIGURATION` block at the top of each script:

**picsafe_bridge_v2_appsheet.py**
```python
LIBRARY_PATH  = "/Volumes/SharkTerra/zData/Photos Library.photoslibrary"
SEQUENCE_FILE = os.path.expanduser("~/PicSafe/picsafe_sequence.txt")
FACE_QUALITY_THRESHOLD = 0.4
MINTING_RATING_KEYWORDS = {"2 Star", "3 Star", "4 Star", "5 Star"}
READY_RATING_KEYWORDS   = {"3 Star", "4 Star", "5 Star"}
```

**picsafe_gphotos_publisher_v1.py**
```python
DASHBOARD_SHEET_ID = 8077434218827652   # Smartsheet sheet ID
PICSAFE_REPO_DIR   = os.path.dirname(os.path.abspath(__file__))
```

---

## Usage

### Run the full pipeline manually

```bash
cd ~/PicSafe
source venv/bin/activate

# Full nightly pipeline (bridge → publisher → git sync)
bash picsafe_nightly.sh

# Or run individual phases:
python picsafe_bridge_v2_appsheet.py
python picsafe_gphotos_publisher_v1.py
bash picsafe_git_sync.sh
```

### Makefile shortcuts

```bash
make bridge    # Run bridge only
make publish   # Run publisher only
make check     # Health check
make help      # List all targets
```

---

## AppSheet Data Model

The `assets` table tracks every PicSafe-managed photo:

| Field | Description |
|-------|-------------|
| picsafe_id | Unique ID (e.g., `PicSafe_042731`) |
| apple_uuid | Apple Photos UUID (primary key for dedup) |
| capture_date | Photo date |
| people_list | Comma-separated person names |
| keywords | Comma-separated keywords |
| face_status | `facesfree` / `facesmissing` / `facescomplete` |
| gps_status | `OK` / `MISSING` |
| enhancement_status | `Enhanced` / `Not Enhanced` |
| status_export | `Pending` / `Done` / `Failed` |
| status_gphotos | `Pending` / `Done` / `Failed` / `Skipped Size` |
| is_public | `Yes` / `No` |
| last_audit_date | ISO date of last bridge scan |
| last_export_date | ISO date of last file export |
| last_upload_date | ISO date of last Google Photos upload |

---

## Netlify Vanity URLs

The publisher writes a `_redirects` file (Netlify format) in the repo root when a Go Live person has a `Smell Adjectives` slug and a Google Photos share link. `netlify.toml` sets `publish = "."` so Netlify serves the repo root directly.

Example redirect:
```
/daffodil  https://photos.google.com/share/...  302
```

The `DNS Redirected` checkbox in Smartsheet is set to `true` once the redirect is live.

---

## Core Files

| File | Purpose |
|------|---------|
| `picsafe_bridge_v2_appsheet.py` | Phase 1: Apple Photos scanner and AppSheet sync |
| `picsafe_gphotos_publisher_v1.py` | Phase 2: Google Photos upload and album management |
| `picsafe_nightly.sh` | Orchestrator: runs bridge → publisher → git sync |
| `com.picsafe.nightly.plist` | macOS launchd plist for 11 PM nightly execution |
| `picsafe_git_sync.sh` | Commits and pushes tracked file changes (e.g. `_redirects`) |
| `picsafe_appsheet_client.py` | Reusable AppSheet REST API client |
| `picsafe_secrets_template.py` | Credentials template (copy to `picsafe_secrets.py`) |
| `netlify.toml` | Sets Netlify publish directory to repo root |
| `_redirects` | Netlify vanity URL redirects (auto-updated by publisher) |
| `Makefile` | Convenience shortcuts: `make bridge`, `make publish`, etc. |
| `requirements.txt` | Python dependencies |

---

## Acknowledgements

- [osxphotos](https://github.com/RhetTbull/osxphotos) by RhetTbull — the incredible library that makes Apple Photos scriptable
- [AppSheet](https://about.appsheet.com) — no-code database backend
- [Smartsheet](https://www.smartsheet.com) — dashboard and reporting
- [Netlify](https://netlify.com) — hosts the vanity redirect layer
