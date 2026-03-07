"""
picsafe_gphotos_mcp_server.py
------------------------------
MCP server (stdio transport) wrapping the Google Photos Library API for PicSafe v2.

Tools (9 total):
  Read (readOnlyHint=True):
    picsafe_gphotos_list_albums       – list all albums with share URLs + counts
    picsafe_gphotos_get_album         – get one album by ID
    picsafe_gphotos_list_album_media  – paginated media items in an album (full objects)
    picsafe_gphotos_get_album_index   – {picsafe_id → media_item_id} map for an album
                                        (the fast idempotency check used before uploads)

  Write:
    picsafe_gphotos_create_album      – create an album with a given title
    picsafe_gphotos_share_album       – enable sharing, return shareableUrl
    picsafe_gphotos_upload_photo      – 2-step upload of a local file → media_item_id
    picsafe_gphotos_add_to_album      – batch add media item IDs to an album
    picsafe_gphotos_remove_from_album – batch remove media item IDs from an album

Auth:
    Reads GOOGLE_TOKEN path from picsafe_secrets.GOOGLE_TOKEN.
    Loads google.oauth2.credentials.Credentials, auto-refreshes when expired,
    saves refreshed token back to the same file.

Prerequisites:
    pip install mcp httpx google-auth google-auth-oauthlib

Claude Desktop config (add to claude_desktop_config.json):
    {
      "mcpServers": {
        "picsafe_gphotos": {
          "command": "/Users/tomz/PicSafe/venv/bin/python3",
          "args": ["/Users/tomz/PicSafe/picsafe_gphotos_mcp_server.py"]
        }
      }
    }

Claude Code / Cowork config (settings.local.json):
    {
      "mcpServers": {
        "picsafe_gphotos": {
          "command": "/Users/tomz/PicSafe/venv/bin/python3",
          "args": ["/Users/tomz/PicSafe/picsafe_gphotos_mcp_server.py"],
          "type": "stdio"
        }
      }
    }

Google Photos API notes:
  • Media items can only be added to albums created by this app (Library API limitation).
  • Media items cannot be deleted via API — only removed from albums.
  • The PicSafe ID is stored in the media item's 'description' field at upload time.
  • Upload is two steps: (1) POST raw bytes → uploadToken, (2) batchCreate with token.
"""

import sys
import os
import json
import mimetypes
import asyncio
import datetime
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Bootstrap: load credentials paths from picsafe_secrets.py
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import picsafe_secrets
    _TOKEN_FILE = getattr(picsafe_secrets, "GOOGLE_TOKEN", "").strip()
except ImportError:
    print("❌ picsafe_secrets.py not found — MCP server cannot start.", file=sys.stderr)
    sys.exit(1)

if not _TOKEN_FILE or not Path(_TOKEN_FILE).exists():
    print(
        f"❌ GOOGLE_TOKEN path '{_TOKEN_FILE}' does not exist. "
        "Run picsafe_gphotos_publisher_v1.py once to complete OAuth flow.",
        file=sys.stderr,
    )
    sys.exit(1)

# Google Photos OAuth scopes — must match what was granted during initial auth
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.sharing",
]

# API base URLs
PHOTOS_BASE   = "https://photoslibrary.googleapis.com/v1"
UPLOAD_URL    = "https://photos.googleapis.com/v1/uploads"

# Pagination / batch limits
PAGE_SIZE     = 50
MAX_PAGE_SIZE = 100   # Google Photos Library API maximum
BATCH_LIMIT   = 50   # batchAddMediaItems / batchRemoveMediaItems maximum


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("picsafe_gphotos_mcp")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _load_and_refresh_creds() -> str:
    """
    Synchronous helper: load credentials from token file, refresh if expired,
    save back, and return a valid access token string.
    Runs in a thread executor to avoid blocking the asyncio event loop.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    with open(_TOKEN_FILE) as f:
        creds_info = json.load(f)

    creds = Credentials.from_authorized_user_info(creds_info, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Persist refreshed token so we don't re-auth on every restart
            with open(_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError(
                "Google credentials are invalid and cannot be refreshed. "
                "Re-run the publisher script to re-authenticate."
            )

    return creds.token


async def _auth_headers() -> dict:
    """
    Async wrapper: get auth headers with a fresh access token.
    Delegates the synchronous google-auth refresh to a thread executor.
    """
    loop = asyncio.get_event_loop()
    token = await loop.run_in_executor(None, _load_and_refresh_creds)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Internal HTTP helpers
# ---------------------------------------------------------------------------

async def _get(path: str, params: Optional[dict] = None) -> dict:
    """GET request against the Photos Library API."""
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PHOTOS_BASE}/{path.lstrip('/')}",
            headers=headers,
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, body: dict, base_url: str = PHOTOS_BASE) -> dict:
    """POST JSON request against the Photos Library API."""
    headers = await _auth_headers()
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/{path.lstrip('/')}",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


def _err(msg: str) -> str:
    """Return a JSON error envelope."""
    return json.dumps({"error": msg})


def _paginate(items: list, offset: int, limit: int) -> dict:
    """Slice a list and attach pagination metadata."""
    total  = len(items)
    sliced = items[offset: offset + limit]
    return {
        "items":    sliced,
        "total":    total,
        "offset":   offset,
        "limit":    limit,
        "has_more": (offset + limit) < total,
    }


async def _collect_all_album_media(album_id: str) -> list:
    """
    Fetch ALL media items in an album by following nextPageToken.
    Returns a flat list of mediaItem dicts.
    Each item has: id, description (= picsafe_id), filename, mediaMetadata, productUrl.
    """
    items = []
    page_token = None

    while True:
        body: dict = {"albumId": album_id, "pageSize": MAX_PAGE_SIZE}
        if page_token:
            body["pageToken"] = page_token

        data = await _post("mediaItems:search", body)
        batch = data.get("mediaItems", [])
        items.extend(batch)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return items


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_list_albums
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_list_albums",
    description=(
        "List all Google Photos albums created by this app. "
        "Returns id, title, mediaItemsCount, and shareableUrl (if shared) for each album. "
        "Use to confirm albums exist before uploading, or to get share URLs for Smartsheet. "
        "Optionally filter by title substring."
    ),
    annotations={
        "readOnlyHint":   True,
        "destructiveHint": False,
        "idempotentHint":  True,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_list_albums(
    title_filter: Optional[str] = None,
    offset: int = 0,
    limit:  int = PAGE_SIZE,
) -> str:
    """
    Args:
        title_filter: Optional substring match on album title (case-insensitive).
        offset:       Zero-based pagination offset.
        limit:        Page size (default 50).
    Returns:
        JSON with keys: items, total, offset, limit, has_more.
        Each item: {id, title, mediaItemsCount, productUrl, shareableUrl, isShared}.
    """
    try:
        albums = []
        page_token = None

        while True:
            params: dict = {"pageSize": MAX_PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token

            data = await _get("albums", params=params)
            batch = data.get("albums", [])
            albums.extend(batch)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        # Normalize shape: extract shareableUrl from shareInfo if present
        result = []
        for a in albums:
            share_info = a.get("shareInfo", {})
            result.append({
                "id":               a.get("id", ""),
                "title":            a.get("title", ""),
                "mediaItemsCount":  a.get("mediaItemsCount", "0"),
                "productUrl":       a.get("productUrl", ""),
                "isShared":         bool(share_info),
                "shareableUrl":     share_info.get("shareableUrl", ""),
            })

        if title_filter:
            tf = title_filter.lower()
            result = [a for a in result if tf in a["title"].lower()]

        result.sort(key=lambda a: a["title"].lower())

        limit = max(1, min(limit, 500))
        return json.dumps(_paginate(result, offset, limit), indent=2)

    except httpx.HTTPStatusError as e:
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_get_album
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_get_album",
    description=(
        "Get a single Google Photos album by its ID. "
        "Returns id, title, mediaItemsCount, productUrl, and shareableUrl. "
        "Use when you already have the album ID from list_albums or from AppSheet."
    ),
    annotations={
        "readOnlyHint":   True,
        "destructiveHint": False,
        "idempotentHint":  True,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_get_album(album_id: str) -> str:
    """
    Args:
        album_id: Google Photos album ID string.
    Returns:
        JSON album object, or an error envelope.
    """
    try:
        data = await _get(f"albums/{album_id}")
        share_info = data.get("shareInfo", {})
        result = {
            "id":              data.get("id", ""),
            "title":           data.get("title", ""),
            "mediaItemsCount": data.get("mediaItemsCount", "0"),
            "productUrl":      data.get("productUrl", ""),
            "isShared":        bool(share_info),
            "shareableUrl":    share_info.get("shareableUrl", ""),
        }
        return json.dumps(result, indent=2)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return _err(f"Album not found: '{album_id}'")
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_list_album_media
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_list_album_media",
    description=(
        "List media items in a Google Photos album. Fetches all pages internally. "
        "Each item includes: id (media_item_id), description (= picsafe_id), "
        "filename, mediaMetadata (width/height/creationTime), productUrl. "
        "Returns paginated results; use offset + limit to page through large albums."
    ),
    annotations={
        "readOnlyHint":   True,
        "destructiveHint": False,
        "idempotentHint":  True,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_list_album_media(
    album_id: str,
    offset:   int = 0,
    limit:    int = PAGE_SIZE,
) -> str:
    """
    Args:
        album_id: Google Photos album ID.
        offset:   Zero-based pagination offset into the full item list.
        limit:    Page size (default 50).
    Returns:
        JSON with keys: items, total, offset, limit, has_more.
    """
    try:
        items = await _collect_all_album_media(album_id)
        limit = max(1, min(limit, 500))
        return json.dumps(_paginate(items, offset, limit), indent=2)

    except httpx.HTTPStatusError as e:
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_get_album_index
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_get_album_index",
    description=(
        "Return a {picsafe_id → media_item_id} mapping for every item in an album. "
        "This is the idempotency check used before uploads: if a picsafe_id is already "
        "in this index, the photo is already uploaded and can be skipped. "
        "Also returns count of items whose description is empty (not PicSafe-tagged). "
        "Fetches all pages internally — may be slow for albums with 1000+ photos."
    ),
    annotations={
        "readOnlyHint":   True,
        "destructiveHint": False,
        "idempotentHint":  True,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_get_album_index(album_id: str) -> str:
    """
    Args:
        album_id: Google Photos album ID.
    Returns:
        JSON with:
          index           – dict mapping picsafe_id → media_item_id
          total_items     – total items in album
          untagged_count  – items with no description (not uploaded by PicSafe)
    """
    try:
        items = await _collect_all_album_media(album_id)

        index: dict   = {}
        untagged: int = 0

        for item in items:
            desc = item.get("description", "").strip()
            if desc:
                index[desc] = item["id"]
            else:
                untagged += 1

        return json.dumps(
            {
                "index":          index,
                "total_items":    len(items),
                "indexed_count":  len(index),
                "untagged_count": untagged,
            },
            indent=2,
        )

    except httpx.HTTPStatusError as e:
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_create_album
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_create_album",
    description=(
        "Create a new Google Photos album with the given title. "
        "Returns the new album's id and productUrl. "
        "Note: the album is private until picsafe_gphotos_share_album is called. "
        "Call share_album immediately after if you need a shareableUrl for Smartsheet."
    ),
    annotations={
        "readOnlyHint":   False,
        "destructiveHint": False,
        "idempotentHint":  False,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_create_album(title: str) -> str:
    """
    Args:
        title: Album title, e.g. 'PicSafe – Alice' or 'PicSafe Public'.
    Returns:
        JSON with {id, title, productUrl} or an error envelope.
    """
    try:
        data = await _post("albums", {"album": {"title": title}})
        return json.dumps(
            {
                "id":         data.get("id", ""),
                "title":      data.get("title", ""),
                "productUrl": data.get("productUrl", ""),
            },
            indent=2,
        )

    except httpx.HTTPStatusError as e:
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_share_album
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_share_album",
    description=(
        "Enable link-sharing on an album and return the shareableUrl. "
        "The URL is suitable for pasting into Smartsheet's 'Google Photos Link' column. "
        "Sharing is idempotent — calling it on an already-shared album is safe "
        "and returns the existing URL."
    ),
    annotations={
        "readOnlyHint":   False,
        "destructiveHint": False,
        "idempotentHint":  True,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_share_album(album_id: str) -> str:
    """
    Args:
        album_id: Google Photos album ID to share.
    Returns:
        JSON with {album_id, shareableUrl, shareToken} or an error envelope.
    """
    try:
        body = {
            "sharedAlbumOptions": {
                "isCollaborative": False,
                "isCommentable":   False,
            }
        }
        data = await _post(f"albums/{album_id}:share", body)
        share_info = data.get("shareInfo", {})
        return json.dumps(
            {
                "album_id":     album_id,
                "shareableUrl": share_info.get("shareableUrl", ""),
                "shareToken":   share_info.get("shareToken", ""),
            },
            indent=2,
        )

    except httpx.HTTPStatusError as e:
        # 400 can mean already shared — attempt to read existing share info
        if e.response.status_code == 400:
            try:
                album_data = await _get(f"albums/{album_id}")
                share_info = album_data.get("shareInfo", {})
                if share_info.get("shareableUrl"):
                    return json.dumps(
                        {
                            "album_id":     album_id,
                            "shareableUrl": share_info["shareableUrl"],
                            "shareToken":   share_info.get("shareToken", ""),
                            "note":         "Album was already shared; returning existing URL.",
                        },
                        indent=2,
                    )
            except Exception:
                pass
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_upload_photo
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_upload_photo",
    description=(
        "Upload a local photo or video file to Google Photos in two steps: "
        "(1) POST raw bytes to get an uploadToken, "
        "(2) batchCreate the media item with the PicSafe ID stored in the description field. "
        "Returns the new media_item_id on success. "
        "Does NOT add to an album — call picsafe_gphotos_add_to_album separately. "
        "Files larger than 5 GB will be rejected before upload."
    ),
    annotations={
        "readOnlyHint":   False,
        "destructiveHint": False,
        "idempotentHint":  False,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_upload_photo(
    file_path:  str,
    picsafe_id: str,
) -> str:
    """
    Args:
        file_path:  Absolute path to the exported file on disk.
        picsafe_id: The PicSafe ID (e.g. 'PicSafe_000042') stored in description
                    for idempotency checking on future runs.
    Returns:
        JSON with {media_item_id, picsafe_id, filename, status} or an error envelope.
    """
    VIDEO_SIZE_LIMIT = 5 * 1024 * 1024 * 1024  # 5 GB

    p = Path(file_path)
    if not p.exists():
        return _err(f"File not found: '{file_path}'")
    if not p.is_file():
        return _err(f"Path is not a file: '{file_path}'")

    file_size = p.stat().st_size
    if file_size >= VIDEO_SIZE_LIMIT:
        size_gb = file_size / (1024 ** 3)
        return _err(
            f"File is {size_gb:.2f} GB — exceeds 5 GB upload limit. "
            f"Set status_gphotos='SKIPPED_SIZE' in AppSheet for '{picsafe_id}'."
        )

    # Determine MIME type
    mime_type, _ = mimetypes.guess_type(str(p))
    if not mime_type:
        mime_type = "application/octet-stream"

    try:
        auth_headers = await _auth_headers()

        # ── Step 1: Upload raw bytes ──────────────────────────────────────────
        upload_headers = {
            **auth_headers,
            "Content-Type":             mime_type,
            "X-Goog-Upload-Content-Type": mime_type,
            "X-Goog-Upload-Protocol":   "raw",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            with open(p, "rb") as fh:
                raw_bytes = fh.read()

            upload_resp = await client.post(
                UPLOAD_URL,
                headers=upload_headers,
                content=raw_bytes,
            )
            upload_resp.raise_for_status()
            upload_token = upload_resp.text.strip()

        if not upload_token:
            return _err("Upload step 1 returned empty uploadToken.")

        # ── Step 2: Create media item ─────────────────────────────────────────
        create_body = {
            "newMediaItems": [
                {
                    "description": picsafe_id,
                    "simpleMediaItem": {
                        "uploadToken": upload_token,
                        "fileName":    p.name,
                    },
                }
            ]
        }

        create_data = await _post("mediaItems:batchCreate", create_body)

        results = create_data.get("newMediaItemResults", [])
        if not results:
            return _err("batchCreate returned no results.")

        first = results[0]
        status = first.get("status", {})
        if status.get("message", "").upper() not in ("", "OK", "SUCCESS"):
            code = status.get("code", "?")
            msg  = status.get("message", "unknown")
            return _err(f"batchCreate status error {code}: {msg}")

        media_item = first.get("mediaItem", {})
        media_item_id = media_item.get("id", "")

        if not media_item_id:
            return _err("batchCreate succeeded but returned no media item ID.")

        return json.dumps(
            {
                "status":        "uploaded",
                "media_item_id": media_item_id,
                "picsafe_id":    picsafe_id,
                "filename":      p.name,
                "file_size_mb":  round(file_size / (1024 * 1024), 2),
            },
            indent=2,
        )

    except httpx.HTTPStatusError as e:
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_add_to_album
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_add_to_album",
    description=(
        "Add one or more media items to a Google Photos album. "
        "Pass media_item_ids as a JSON array (max 50 per call). "
        "Returns count of items added. "
        "Note: media items must have been created by this same Google account."
    ),
    annotations={
        "readOnlyHint":   False,
        "destructiveHint": False,
        "idempotentHint":  True,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_add_to_album(
    album_id:          str,
    media_item_ids_json: str,
) -> str:
    """
    Args:
        album_id:             Google Photos album ID.
        media_item_ids_json:  JSON array of media item ID strings.
                              Example: ["MEDIA_ID_1", "MEDIA_ID_2"]
                              Maximum 50 IDs per call.
    Returns:
        JSON with {status, album_id, items_added} or an error envelope.
    """
    try:
        ids = json.loads(media_item_ids_json)
    except json.JSONDecodeError as e:
        return _err(f"Invalid JSON in media_item_ids_json: {e}")

    if not isinstance(ids, list):
        return _err("media_item_ids_json must be a JSON array.")
    if not ids:
        return _err("media_item_ids_json is empty.")
    if len(ids) > BATCH_LIMIT:
        return _err(f"Too many IDs ({len(ids)}). Maximum is {BATCH_LIMIT} per call.")

    try:
        await _post(
            f"albums/{album_id}:batchAddMediaItems",
            {"mediaItemIds": ids},
        )
        return json.dumps(
            {"status": "added", "album_id": album_id, "items_added": len(ids)},
            indent=2,
        )

    except httpx.HTTPStatusError as e:
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool: picsafe_gphotos_remove_from_album
# ---------------------------------------------------------------------------

@mcp.tool(
    name="picsafe_gphotos_remove_from_album",
    description=(
        "Remove one or more media items from a Google Photos album. "
        "Pass media_item_ids as a JSON array (max 50 per call). "
        "This ONLY removes items from the album — it does NOT delete them from the library. "
        "Use when pruning photos that no longer meet the 'PicSafe Ready' criteria."
    ),
    annotations={
        "readOnlyHint":   False,
        "destructiveHint": False,
        "idempotentHint":  True,
        "openWorldHint":   False,
    },
)
async def picsafe_gphotos_remove_from_album(
    album_id:            str,
    media_item_ids_json: str,
) -> str:
    """
    Args:
        album_id:             Google Photos album ID.
        media_item_ids_json:  JSON array of media item ID strings to remove.
                              Maximum 50 IDs per call.
    Returns:
        JSON with {status, album_id, items_removed} or an error envelope.
    """
    try:
        ids = json.loads(media_item_ids_json)
    except json.JSONDecodeError as e:
        return _err(f"Invalid JSON in media_item_ids_json: {e}")

    if not isinstance(ids, list):
        return _err("media_item_ids_json must be a JSON array.")
    if not ids:
        return _err("media_item_ids_json is empty.")
    if len(ids) > BATCH_LIMIT:
        return _err(f"Too many IDs ({len(ids)}). Maximum is {BATCH_LIMIT} per call.")

    try:
        await _post(
            f"albums/{album_id}:batchRemoveMediaItems",
            {"mediaItemIds": ids},
        )
        return json.dumps(
            {"status": "removed", "album_id": album_id, "items_removed": len(ids)},
            indent=2,
        )

    except httpx.HTTPStatusError as e:
        return _err(f"Google Photos API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
