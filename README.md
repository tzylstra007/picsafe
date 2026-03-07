# PicSafe v2

**A personal media management pipeline that syncs Apple Photos → Google Photos with intelligent face-tagging, quality filtering, and per-person album management.**

PicSafe bridges the gap between Apple's Photos app and Google Photos by:
- Scanning your library nightly with [osxphotos](https://github.com/RhetTbull/osxphotos)
- Auditing each photo for faces, GPS, and enhancement status
- Minting unique PicSafe IDs for shareable photos
- Exporting ready photos and uploading them to personal Google Photos albums
- Tracking everything in AppSheet + Smartsheet for visibility

---

## Architecture

```
Apple Photos Library
        │
        ▼
┌─────────────────────────────┐
│  picsafe_bridge_v2_appsheet │  ← Phase 1: Scan + audit + mint IDs
│         (7-Step Pipeline)   │    Creates PENDING records in AppSheet
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│    picsafe_export_v2        │  ← Phase 2: Export JPEG/MP4 by UUID
│                             │    Files → /Volumes/SharkTerra/zData/PicSafe_Exported/
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  Google Photos Publisher    │  ← Phase 3: Upload + album management
│   (Claude nightly task)     │    Updates AppSheet + Smartsheet
└─────────────────────────────┘
```

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

A photo is considered **PicSafe Ready** (eligible for export and upload) when:
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

### Required Smartsheet Dashboard Columns

The bridge and publisher expect these columns in your Smartsheet dashboard (sheet ID configurable in the scripts):

| Column | Type | Purpose |
|--------|------|---------|
| Person Name | TEXT_NUMBER (primary) | Apple Photos person name |
| Go Live | CHECKBOX | Enables upload for this person |
| Photos - AP | TEXT_NUMBER | Total photos in Apple Photos |
| Videos - AP | TEXT_NUMBER | Total videos in Apple Photos |
| Photos - PicSafe Ready | TEXT_NUMBER | Photos meeting Ready criteria |
| Videos - PicSafe Ready | TEXT_NUMBER | Videos meeting Ready criteria |
| Photos - Google | TEXT_NUMBER | Photos uploaded to Google Photos |
| Videos - Google | TEXT_NUMBER | Videos uploaded to Google Photos |
| Google Photos Link | TEXT_NUMBER | Shareable album URL |

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/tzylstra/picsafe-v2.git
cd picsafe-v2

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

**picsafe_export_v2.py**
```python
EXPORT_PATH  = "/Volumes/SharkTerra/zData/PicSafe_Exported"
LIBRARY_PATH = "/Volumes/SharkTerra/zData/Photos Library.photoslibrary"
```

---

## Usage

### Run the full pipeline manually

```bash
cd ~/PicSafe
source venv/bin/activate

# Phase 1: Scan Apple Photos and sync to AppSheet
python picsafe_bridge_v2_appsheet.py

# Phase 2: Export ready photos to local disk
./picsafe_export_v2.sh

# Or with dry-run to preview without writing:
./picsafe_export_v2.sh --dry-run
```

### Nightly automation

The pipeline runs automatically each night via a Claude scheduled task at 9 PM Pacific, which orchestrates all four phases (bridge → export → Google Photos upload → summary report).

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
| enhancement_status | `ENHANCED` / `UNENHANCED` |
| status_export | `PENDING` / `DONE` / `FAILED` |
| status_gphotos | `PENDING` / `DONE` / `FAILED` / `SKIPPED_SIZE` |
| is_public | `Yes` / `No` |
| last_audit_date | ISO date of last bridge scan |
| last_export_date | ISO date of last file export |
| last_upload_date | ISO date of last Google Photos upload |

---

## Files

| File | Purpose |
|------|---------|
| `picsafe_bridge_v2_appsheet.py` | Phase 1: Apple Photos scanner and AppSheet sync |
| `picsafe_export_v2.py` | Phase 2: Export PENDING assets to UUID-named files |
| `picsafe_export_v2.sh` | Shell wrapper for the export script |
| `picsafe_appsheet_client.py` | Reusable AppSheet REST API client |
| `picsafe_secrets_template.py` | Credentials template (copy to `picsafe_secrets.py`) |
| `requirements.txt` | Python dependencies |

---

## Contributing

PRs welcome! Key areas for community contribution:
- Support for additional cloud photo platforms
- Alternative face quality scoring approaches
- Cross-platform export (Windows/Linux with different photo libraries)
- Automated testing / dry-run CI

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [osxphotos](https://github.com/RhetTbull/osxphotos) by RhetTbull — the incredible library that makes Apple Photos scriptable
- [AppSheet](https://about.appsheet.com) — no-code database backend
- [Smartsheet](https://www.smartsheet.com) — dashboard and reporting
