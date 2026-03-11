"""
picsafe_gphotos_publisher_v1.py
--------------------------------
PicSafe v2 publisher: exports from Apple Photos and syncs to Google Photos.

Replaces picsafe_publisher_v69.py (Flickr) with Google Photos Library API.

Flow for each Go Live person:
  1. Local export via osxphotos (--update, --exiftool, metadata baked in)
  2. Get or create Google Photos album for person
  3. Share album → get link → update Smartsheet
  4. Upload files not yet in Google Photos (identified by PicSafe ID in description)
  5. Handle "PicSafe Public" album (two-tier model, face-guard applied)
  6. Prune album of deleted photos (--cleanup handles local; API handles GPhotos)
  7. Update Smartsheet photo counts
  8. Batch-flush all status changes to AppSheet

Run order: picsafe_preflight.py → picsafe_bridge_v2.py → THIS SCRIPT

Key design decisions (from architecture doc):
  - Videos < 5 GB sync; videos >= 5 GB skipped (SKIPPED_SIZE)
  - Public album eligibility: PicSafe Public keyword + zero named faces
  - Google Photos API: 2-step upload (bytes → token → mediaItems:batchCreate)
  - All metadata baked in by exiftool at export time (API cannot edit post-upload)
  - AuthorizedSession handles silent token refresh (no more Flickr-style expiry)
"""

import os
import sys
import time
import subprocess
import json
import requests
import smartsheet

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import picsafe_appsheet_client as as_db

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession, Request
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("❌ Missing Google libraries. Run:")
    print("   pip install google-auth google-auth-oauthlib google-api-python-client requests")
    sys.exit(1)

try:
    import picsafe_secrets
    SMARTSHEET_TOKEN     = picsafe_secrets.SMARTSHEET_ACCESS_TOKEN.strip()
    GOOGLE_CREDS_FILE    = picsafe_secrets.GOOGLE_CREDENTIALS_FILE
    GOOGLE_TOKEN_FILE    = picsafe_secrets.GOOGLE_TOKEN_FILE
except (ImportError, AttributeError) as e:
    print(f"❌ CREDENTIAL ERROR: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_NAME            = "picsafe_gphotos_publisher_v1"
SMARTSHEET_ID          = 8077434218827652
EXPORT_ROOT            = "/Volumes/SharkTerra/zData/PicSafe_Exported"
PHOTOS_LIBRARY_DB      = "/Volumes/SharkTerra/zData/Photos Library.photoslibrary"
VIDEO_SIZE_LIMIT_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB

PUBLIC_ALBUM_TITLE     = "PicSafe Public"

GPHOTOS_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",                         # full read/write incl. batchRemoveMediaItems
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.sharing",
]
GPHOTOS_BASE   = "https://photoslibrary.googleapis.com/v1"

# Supported photo/video extensions to upload
PHOTO_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.gif', '.tif', '.tiff', '.webp', '.bmp'}
VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.wmv', '.3gp'}
UPLOAD_EXTS = PHOTO_EXTS | VIDEO_EXTS

# Netlify vanity URL redirects — written in-repo, deployed via git push
# netlify.toml (repo root) sets publish="." so Netlify serves _redirects from here.
PICSAFE_REPO_DIR  = os.path.dirname(os.path.abspath(__file__))
NETLIFY_REDIRECTS = os.path.join(PICSAFE_REPO_DIR, "_redirects")


# ---------------------------------------------------------------------------
# Google Photos authentication
# ---------------------------------------------------------------------------

def get_google_session() -> AuthorizedSession:
    """
    Load or refresh Google OAuth2 credentials and return an AuthorizedSession.
    First run: opens browser for one-time authorization.
    Subsequent runs: silently refreshes the saved token.
    """
    creds = None

    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GPHOTOS_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("   🔑 Refreshing Google OAuth token silently...")
            creds.refresh(Request())
        else:
            print("   🔑 No valid token — launching one-time browser auth...")
            flow  = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDS_FILE, GPHOTOS_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(GOOGLE_TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
        print("   ✅ Token saved.")

    return AuthorizedSession(creds)


# ---------------------------------------------------------------------------
# Google Photos: album management
# ---------------------------------------------------------------------------

def list_all_albums(session: AuthorizedSession) -> dict:
    """Returns dict of {title: album_id} for all albums in the library."""
    albums = {}
    page_token = None
    while True:
        params = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = session.get(f"{GPHOTOS_BASE}/albums", params=params)
        resp.raise_for_status()
        data = resp.json()
        for a in data.get("albums", []):
            albums[a["title"]] = a["id"]
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return albums


def create_album(session: AuthorizedSession, title: str) -> str:
    """Create a new album and return its ID."""
    resp = session.post(
        f"{GPHOTOS_BASE}/albums",
        json={"album": {"title": title}}
    )
    resp.raise_for_status()
    return resp.json()["id"]


def share_album(session: AuthorizedSession, album_id: str) -> str:
    """
    Share an album (link-based access — anyone with link can view).
    Returns the shareable URL.
    """
    resp = session.post(
        f"{GPHOTOS_BASE}/albums/{album_id}:share",
        json={"sharedAlbumOptions": {"isCollaborative": False, "isCommentable": False}}
    )
    resp.raise_for_status()
    return resp.json()["shareInfo"]["shareableUrl"]


def get_album_media_ids(session: AuthorizedSession, album_id: str) -> dict:
    """
    Returns dict of {description: media_item_id} for all items in the album.
    We store PicSafe ID in the description field at upload time.
    """
    items = {}
    page_token = None
    while True:
        body = {"albumId": album_id, "pageSize": 100}
        if page_token:
            body["pageToken"] = page_token
        resp = session.post(f"{GPHOTOS_BASE}/mediaItems:search", json=body)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("mediaItems", []):
            desc = item.get("description", "")
            if desc:
                items[desc] = item["id"]
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def upload_file_to_gphotos(session: AuthorizedSession, fpath: str) -> str | None:
    """
    Step 1 of 2-step Google Photos upload: upload raw bytes, get upload token.
    Returns upload_token string, or None on failure.
    """
    filename = os.path.basename(fpath)
    headers  = {
        "X-Goog-Upload-File-Name":   filename,
        "X-Goog-Upload-Protocol":    "raw",
        "Content-Type":              "application/octet-stream",
    }
    try:
        with open(fpath, "rb") as f:
            resp = session.post(
                f"{GPHOTOS_BASE}/uploads",
                data=f.read(),
                headers=headers
            )
        resp.raise_for_status()
        return resp.text  # upload token
    except Exception as e:
        print(f"      ❌ Upload bytes failed for {filename}: {e}")
        return None


def create_media_item(
    session:      AuthorizedSession,
    upload_token: str,
    picsafe_id:   str,
    album_id:     str
) -> str | None:
    """
    Step 2 of 2-step Google Photos upload: create the media item in an album.
    The PicSafe ID is stored in the description for future idempotency checks.
    Returns the Google Photos media item ID, or None on failure.
    """
    body = {
        "albumId": album_id,
        "newMediaItems": [{
            "description": picsafe_id,
            "simpleMediaItem": {"uploadToken": upload_token}
        }]
    }
    try:
        resp = session.post(f"{GPHOTOS_BASE}/mediaItems:batchCreate", json=body)
        resp.raise_for_status()
        result = resp.json()["newMediaItemResults"][0]
        if result.get("status", {}).get("message", "").lower() == "ok" or "mediaItem" in result:
            return result["mediaItem"]["id"]
        else:
            print(f"      ⚠️  Media create non-OK status: {result.get('status')}")
            return None
    except Exception as e:
        print(f"      ❌ Create media item failed for {picsafe_id}: {e}")
        return None


def add_to_album(session: AuthorizedSession, album_id: str, media_item_id: str) -> bool:
    """Add an existing media item to an album."""
    try:
        resp = session.post(
            f"{GPHOTOS_BASE}/albums/{album_id}:batchAddMediaItems",
            json={"mediaItemIds": [media_item_id]}
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"      ❌ Add to album failed: {e}")
        return False


def remove_from_album(session: AuthorizedSession, album_id: str, media_item_id: str) -> bool:
    """Remove a media item from an album (item stays in library)."""
    try:
        resp = session.post(
            f"{GPHOTOS_BASE}/albums/{album_id}:batchRemoveMediaItems",
            json={"mediaItemIds": [media_item_id]}
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"      ❌ Remove from album failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Local export via osxphotos
# ---------------------------------------------------------------------------

def export_person(person_name: str) -> tuple[bool, int]:
    """
    Run osxphotos export for one person.
    Returns (success, file_count).
    --update: only export new/modified files
    --exiftool: bakes all metadata into files BEFORE Google Photos upload
    --cleanup: removes locally exported files that no longer exist in Apple Photos
    """
    person_dir = os.path.join(EXPORT_ROOT, person_name)
    os.makedirs(person_dir, exist_ok=True)

    cmd = [
        "osxphotos", "export", person_dir,
        "--person",            person_name,
        "--keyword",           "PicSafe Ready",
        "--download-missing",
        "--exiftool",
        "--sidecar",           "xmp",
        "--update",
        "--cleanup",
        "--filename",          "{title}",       # Use PicSafe ID as filename
        "--overwrite",
    ]

    print(f"   🖥️  Exporting '{person_name}'...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            # Count files in export dir (photos + videos, no sidecars)
            count = sum(
                1 for f in os.listdir(person_dir)
                if os.path.splitext(f)[1].lower() in UPLOAD_EXTS
            )
            print(f"      ✅ Export complete. {count} files in {person_dir}")
            return True, count
        else:
            print(f"      ❌ osxphotos export failed: {result.stderr[:500]}")
            return False, 0
    except subprocess.TimeoutExpired:
        print(f"      ❌ osxphotos export timed out for '{person_name}'")
        return False, 0
    except FileNotFoundError:
        print("      ❌ osxphotos not found. Install with: pip install osxphotos")
        return False, 0


# ---------------------------------------------------------------------------
# Smartsheet helpers
# ---------------------------------------------------------------------------

def get_smartsheet_data(ss_client) -> dict:
    """
    Returns dict of {person_name: {row_id, go_live, google_photos_link_col_id, ...}}
    for all rows in the control sheet.
    """
    sheet = ss_client.Sheets.get_sheet(SMARTSHEET_ID)
    # Map column titles to IDs
    col_map = {c.title: c.id for c in sheet.columns}

    rows = {}
    for row in sheet.rows:
        cells = {c.column_id: c.value for c in row.cells}
        name  = cells.get(col_map.get("Person Name"))
        if not name:
            continue
        rows[str(name).strip()] = {
            "row_id":        row.id,
            "go_live":       bool(cells.get(col_map.get("Go Live"), False)),
            "col_map":       col_map,
        }
    return rows, col_map


def update_smartsheet_row(
    ss_client,
    row_id:       int,
    col_map:      dict,
    gphotos_link: str = None,
    photo_count:  int = None,
    last_sync:    str = None,
) -> bool:
    """Update Smartsheet cells for one person's row."""
    cells = []
    if gphotos_link and "Google Photos Link" in col_map:
        cells.append({"columnId": col_map["Google Photos Link"], "value": gphotos_link})
    if photo_count is not None and "Photos - Google" in col_map:
        cells.append({"columnId": col_map["Photos - Google"], "value": photo_count})
    if last_sync and "Last Sync" in col_map:
        cells.append({"columnId": col_map["Last Sync"], "value": last_sync})

    if not cells:
        return True

    try:
        updated_row = ss_client.models.Row()
        updated_row.id = row_id
        updated_row.cells = [
            ss_client.models.Cell({"columnId": c["columnId"], "value": c["value"]})
            for c in cells
        ]
        ss_client.Sheets.update_rows(SMARTSHEET_ID, [updated_row])
        return True
    except Exception as e:
        print(f"      ❌ Smartsheet update failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Extract PicSafe ID from filename
# ---------------------------------------------------------------------------

def picsafe_id_from_filename(filename: str) -> str:
    """
    The export uses {title} as filename, so filename stem IS the PicSafe ID.
    e.g. "PicSafe_001234.jpg" → "PicSafe_001234"
    """
    return os.path.splitext(filename)[0]


# ---------------------------------------------------------------------------
# Netlify vanity URL sync
# ---------------------------------------------------------------------------

def sync_netlify_redirects(ss_client) -> int:
    """
    Build a fresh Netlify _redirects file from Smartsheet, write it to the
    PicSafe git repo, and push — Netlify's GitHub integration deploys automatically.

    For every row where 'Go Live' is checked, 'Smell Adjectives' is non-empty,
    and 'Google Photos Share Link' is non-empty, write a redirect:
        /<smell_adjective_slug>   <google_photos_share_link>   301

    The _redirects file is fully regenerated on every run (idempotent).
    Deployment is event-driven: this function fires the git push event; Netlify
    handles the actual deploy asynchronously via its GitHub integration.

    Returns the count of redirects written.
    """
    print("\n🌐  NETLIFY VANITY URL SYNC...")
    sheet   = ss_client.Sheets.get_sheet(SMARTSHEET_ID)
    col_map = {c.title: c.id for c in sheet.columns}

    required = ["Go Live", "Smell Adjectives", "Google Photos Share Link"]
    missing  = [c for c in required if c not in col_map]
    if missing:
        print(f"   ❌ Missing columns: {missing} — skipping Netlify sync")
        return 0

    redirects = []
    for row in sheet.rows:
        cells     = {c.column_id: c.value for c in row.cells}
        go_live   = bool(cells.get(col_map["Go Live"], False))
        slug      = str(cells.get(col_map["Smell Adjectives"]) or "").strip().lower()
        share_url = str(cells.get(col_map["Google Photos Share Link"]) or "").strip()
        if go_live and slug and share_url:
            redirects.append(f"/{slug}   {share_url}   301")

    if not redirects:
        print("   ℹ️  No rows qualify for vanity URLs — nothing to deploy")
        return 0

    # Write _redirects to the repo root (netlify.toml sets publish="." so Netlify picks it up)
    with open(NETLIFY_REDIRECTS, "w") as f:
        f.write("# PicSafe vanity URL redirects — auto-generated, do not edit manually\n")
        f.write("# Format: /<slug>   <destination>   <status>\n\n")
        f.write("\n".join(redirects) + "\n")

    print(f"   📝 Written {len(redirects)} redirect(s) to {NETLIFY_REDIRECTS}")

    # Clear any stale git lock files before running git commands (can be left
    # behind if a previous run was interrupted mid-commit).
    for lock_file in [".git/HEAD.lock", ".git/index.lock"]:
        lock_path = os.path.join(PICSAFE_REPO_DIR, lock_file)
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
                print(f"   🔓 Removed stale git lock: {lock_file}")
            except OSError as e:
                print(f"   ⚠️  Could not remove git lock {lock_file}: {e}")

    # Commit and push — Netlify's GitHub integration deploys on the push event
    git_steps = [
        (["git", "-C", PICSAFE_REPO_DIR, "add", "_redirects"],
         "add"),
        (["git", "-C", PICSAFE_REPO_DIR, "commit", "-m",
          f"chore: update {len(redirects)} vanity URL redirect(s) [publisher]"],
         "commit"),
        (["git", "-C", PICSAFE_REPO_DIR, "push"],
         "push"),
    ]

    for cmd, label in git_steps:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                combined = (result.stdout + result.stderr).lower()
                if label == "commit" and "nothing to commit" in combined:
                    print("   ℹ️  _redirects unchanged — no new commit needed")
                    return len(redirects)
                print(f"   ⚠️  git {label} failed (exit {result.returncode}): "
                      f"{(result.stderr or result.stdout).strip()[:200]}")
                return len(redirects)
        except subprocess.TimeoutExpired:
            print(f"   ❌ git {label} timed out after 30s")
            return len(redirects)

    print(f"   ✅ Pushed _redirects ({len(redirects)} redirect(s)) — "
          f"Netlify will deploy automatically via GitHub integration.")
    return len(redirects)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"🎬  STARTING {SCRIPT_NAME}...")
    start_time = time.time()

    # -----------------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------------
    print("   🔑 Authenticating with Google Photos...")
    session   = get_google_session()
    ss_client = smartsheet.Smartsheet(SMARTSHEET_TOKEN)
    print("   ✅ Authenticated.\n")

    # -----------------------------------------------------------------------
    # Load existing album map from Google Photos
    # -----------------------------------------------------------------------
    print("   📚 Loading existing Google Photos albums...")
    album_map = list_all_albums(session)  # {title: album_id}
    print(f"      Found {len(album_map)} existing albums.")

    # Ensure "PicSafe Public" album exists
    if PUBLIC_ALBUM_TITLE not in album_map:
        print(f"   ➕ Creating '{PUBLIC_ALBUM_TITLE}' album...")
        album_map[PUBLIC_ALBUM_TITLE] = create_album(session, PUBLIC_ALBUM_TITLE)

    public_album_id = album_map[PUBLIC_ALBUM_TITLE]

    # -----------------------------------------------------------------------
    # Get Go Live people from Smartsheet
    # -----------------------------------------------------------------------
    print("   ☁️  Loading Go Live people from Smartsheet...")
    ss_rows, col_map = get_smartsheet_data(ss_client)
    go_live_people   = [name for name, data in ss_rows.items() if data["go_live"]]
    print(f"      ✅ {len(go_live_people)} Go Live: {go_live_people}\n")

    # -----------------------------------------------------------------------
    # Accumulators for AppSheet batch writes
    # -----------------------------------------------------------------------
    pending_asset_updates: list = []
    pending_log_entries:   list = []

    total_stats = {
        "uploaded":         0,
        "skipped_size":     0,
        "already_synced":   0,
        "public_added":     0,
        "errors":           0,
        "refresh_uploads":  0,   # edited versions uploaded alongside their original
    }

    sync_time = as_db.now_ts()

    # -----------------------------------------------------------------------
    # Per-person loop
    # -----------------------------------------------------------------------
    for person_name in go_live_people:
        print(f"👤  Processing: {person_name}")

        # --- Local export ---
        export_ok, local_count = export_person(person_name)
        if not export_ok:
            total_stats["errors"] += 1
            pending_log_entries.append(
                as_db.make_log_entry(f"PERSON:{person_name}", "EXPORT_FAIL",
                                     "osxphotos export failed", SCRIPT_NAME)
            )
            continue

        person_dir = os.path.join(EXPORT_ROOT, person_name)

        # --- Ensure album exists ---
        if person_name not in album_map:
            print(f"   ➕ Creating album '{person_name}'...")
            album_map[person_name] = create_album(session, person_name)

        album_id = album_map[person_name]

        # --- Share album → get link ---
        try:
            share_link = share_album(session, album_id)
            print(f"   🔗 Share link: {share_link}")
        except Exception as e:
            share_link = ""
            print(f"   ⚠️  Could not get share link: {e}")

        # --- Get items already in this album (by PicSafe ID in description) ---
        print(f"   🔍 Checking existing items in album...")
        existing_in_album = get_album_media_ids(session, album_id)  # {picsafe_id: media_id}
        print(f"      {len(existing_in_album)} items already in album.")

        # --- Upload new files ---
        local_files = [
            f for f in os.listdir(person_dir)
            if os.path.splitext(f)[1].lower() in UPLOAD_EXTS
        ]

        # Deduplicate: prefer the edited version over the original for each photo.
        # osxphotos exports both PicSafe_XXXXXX.jpg AND PicSafe_XXXXXX_edited.jpeg
        # when a photo has been edited in Apple Photos. We only want one copy in
        # Google Photos — the edited version wins; original is the fallback.
        _best: dict = {}  # base_id → filename
        for _fname in local_files:
            _stem = os.path.splitext(_fname)[0]           # e.g. "PicSafe_012640_edited"
            _base = _stem.replace("_edited", "")           # e.g. "PicSafe_012640"
            _is_edited = "_edited" in _stem
            if _base not in _best or _is_edited:
                _best[_base] = _fname
        local_files = list(_best.values())

        person_media_ids = dict(existing_in_album)  # We'll add to this as we upload

        person_uploaded    = 0
        person_skipped_sz  = 0

        for filename in local_files:
            fpath      = os.path.join(person_dir, filename)
            picsafe_id = picsafe_id_from_filename(filename)

            if not picsafe_id.startswith("PicSafe_"):
                continue  # Skip files without proper PicSafe ID names

            # Already in this album?
            if picsafe_id in existing_in_album:
                total_stats["already_synced"] += 1
                continue

            # Detect "refresh" case: an _edited version is being uploaded but the
            # original (base) is already in the album.  We CANNOT remove the original
            # via batchRemoveMediaItems (blocked for unverified OAuth apps), so both
            # versions will coexist in the album.  Log a warning so the user knows
            # manual cleanup is needed in the Google Photos UI.
            _base_id   = picsafe_id.replace("_edited", "")
            _is_refresh = "_edited" in picsafe_id and _base_id in existing_in_album
            if _is_refresh:
                print(f"      🔄  REFRESH: uploading {picsafe_id} (enhanced version).")
                print(f"          ⚠️  Original {_base_id} already in album — manual removal")
                print(f"              needed in Google Photos UI (API removal blocked).")
                total_stats["refresh_uploads"] += 1

            # Video size check
            file_size = os.path.getsize(fpath)
            ext       = os.path.splitext(filename)[1].lower()
            is_video  = ext in VIDEO_EXTS

            if is_video and file_size >= VIDEO_SIZE_LIMIT_BYTES:
                size_gb = file_size / (1024 ** 3)
                print(f"      ⏭️  SKIP (oversized): {filename} — {size_gb:.1f} GB")
                total_stats["skipped_size"]  += 1
                person_skipped_sz            += 1
                pending_asset_updates.append({
                    "picsafe_id":    picsafe_id,
                    "status_gphotos": "SKIPPED_SIZE",
                })
                pending_log_entries.append(
                    as_db.make_log_entry(picsafe_id, "SKIPPED_SIZE",
                                         f"Video {size_gb:.1f} GB >= 5 GB limit", SCRIPT_NAME)
                )
                continue

            # Upload
            print(f"      ⬆️  Uploading: {filename} ({file_size / (1024*1024):.1f} MB)...")
            upload_token = upload_file_to_gphotos(session, fpath)
            if not upload_token:
                total_stats["errors"]   += 1
                pending_asset_updates.append({"picsafe_id": picsafe_id, "status_gphotos": "FAILED"})
                pending_log_entries.append(
                    as_db.make_log_entry(picsafe_id, "FAIL", "Upload bytes failed", SCRIPT_NAME)
                )
                continue

            media_id = create_media_item(session, upload_token, picsafe_id, album_id)
            if not media_id:
                total_stats["errors"]   += 1
                pending_asset_updates.append({"picsafe_id": picsafe_id, "status_gphotos": "FAILED"})
                pending_log_entries.append(
                    as_db.make_log_entry(picsafe_id, "FAIL", "Create media item failed", SCRIPT_NAME)
                )
                continue

            # Success
            person_media_ids[picsafe_id] = media_id
            person_uploaded              += 1
            total_stats["uploaded"]      += 1
            pending_asset_updates.append({
                "picsafe_id":        picsafe_id,
                "status_gphotos":    "DONE",
                "gphotos_media_id":  media_id,
                "gphotos_album_id":  album_id,
                "last_upload_date":  sync_time,
            })
            pending_log_entries.append(
                as_db.make_log_entry(picsafe_id, "GPHOTOS_UPLOAD",
                                     f"Uploaded to album '{person_name}'", SCRIPT_NAME)
            )

            time.sleep(0.1)  # Gentle rate limiting

        # --- "PicSafe Public" album: check for public-eligible photos ---
        # Re-scan local export dir for files tagged PicSafe Public (no named faces)
        # We rely on osxphotos having exported these files because we use keywords
        # to filter. Public eligibility = PicSafe Public keyword AND facesfree.
        # The bridge already ensures facesfree photos in the export have been tagged.
        #
        # Practical approach: check AppSheet assets where is_public=Yes AND person
        # maps to this person's export folder → add to public album if not there.
        #
        # For simplicity in v1, we check for any file in the export that has
        # "facesfree" in its AppSheet record (is_public = Yes). We query AppSheet
        # by scanning the pending_asset_updates we just built.

        # Get existing items in public album
        existing_in_public = get_album_media_ids(session, public_album_id)

        for picsafe_id, media_id in person_media_ids.items():
            # Find whether this was marked is_public=Yes by the bridge
            # Check our pending updates first (most recent), then look at AppSheet
            is_public = False
            for upd in pending_asset_updates:
                if upd.get("picsafe_id") == picsafe_id:
                    is_public = upd.get("is_public") == "Yes"
                    break

            if is_public and picsafe_id not in existing_in_public:
                print(f"      🌍 Adding to Public album: {picsafe_id}")
                if add_to_album(session, public_album_id, media_id):
                    total_stats["public_added"] += 1
                    existing_in_public[picsafe_id] = media_id
                    pending_log_entries.append(
                        as_db.make_log_entry(picsafe_id, "GPHOTOS_PUBLIC_ADD",
                                             "Added to PicSafe Public album", SCRIPT_NAME)
                    )

        # --- Prune deleted photos from album ---
        # Files removed by osxphotos --cleanup are no longer in person_dir.
        # Any media_id in existing_in_album but NOT in current local_files → remove.
        current_picsafe_ids = {picsafe_id_from_filename(f) for f in local_files
                               if os.path.splitext(f)[1].lower() in UPLOAD_EXTS}

        for pid, mid in existing_in_album.items():
            if pid not in current_picsafe_ids:
                print(f"      🗑️  Pruning deleted: {pid}")
                if remove_from_album(session, album_id, mid):
                    pending_log_entries.append(
                        as_db.make_log_entry(pid, "GPHOTOS_REMOVE",
                                             f"Removed from album '{person_name}'", SCRIPT_NAME)
                    )

        # --- Update Smartsheet ---
        if person_name in ss_rows:
            row_id = ss_rows[person_name]["row_id"]
            update_smartsheet_row(
                ss_client,
                row_id    = row_id,
                col_map   = col_map,
                gphotos_link = share_link,
                photo_count  = len(person_media_ids),
                last_sync    = sync_time,
            )

        # --- Update AppSheet album record ---
        as_db.upsert_album({
            "album_id":    album_id,
            "person_name": person_name,
            "share_link":  share_link,
            "photo_count": len(person_media_ids),
            "last_synced": sync_time,
        })

        print(f"   ✅ {person_name}: {person_uploaded} uploaded, "
              f"{person_skipped_sz} skipped (size), {len(existing_in_album)} already synced.\n")

    # -----------------------------------------------------------------------
    # Flush all AppSheet updates
    # -----------------------------------------------------------------------
    print(f"   📤 Flushing {len(pending_asset_updates)} asset status updates to AppSheet...")
    as_db.upsert_assets(pending_asset_updates)  # upsert_assets handles Row ID lookup

    print(f"   📤 Flushing {len(pending_log_entries)} log entries to AppSheet...")
    as_db.add_audit_log(pending_log_entries)

    # -----------------------------------------------------------------------
    # Netlify vanity URL sync
    # -----------------------------------------------------------------------
    sync_netlify_redirects(ss_client)

    # -----------------------------------------------------------------------
    # Run summary
    # -----------------------------------------------------------------------
    elapsed = time.time() - start_time
    refresh_count = total_stats.get("refresh_uploads", 0)
    refresh_note  = f" Refresh uploads (manual cleanup needed): {refresh_count}." if refresh_count else ""
    summary = (
        f"Publisher complete in {elapsed:.0f}s. "
        f"Uploaded: {total_stats['uploaded']}. "
        f"Already synced: {total_stats['already_synced']}. "
        f"Public album additions: {total_stats['public_added']}. "
        f"Skipped oversized: {total_stats['skipped_size']}. "
        f"Errors: {total_stats['errors']}."
        f"{refresh_note}"
    )
    status = "FAILED" if total_stats["errors"] > total_stats["uploaded"] else (
             "PARTIAL" if total_stats["errors"] > 0 else "SUCCESS")

    print(f"\n✅  PUBLISHER COMPLETE. {summary}")

    as_db.add_run_history(as_db.make_run_row(
        script_name     = SCRIPT_NAME,
        photos_uploaded = total_stats["uploaded"],
        errors_count    = total_stats["errors"],
        status          = status,
        summary         = summary,
    ))


if __name__ == "__main__":
    main()
