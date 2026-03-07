"""
picsafe_secrets_template.py
============================
Copy this file to picsafe_secrets.py and fill in your credentials.
picsafe_secrets.py is git-ignored and should NEVER be committed.
"""

# Smartsheet API token
# https://help.smartsheet.com/articles/2482389-generate-API-key
SMARTSHEET_ACCESS_TOKEN = "YOUR_SMARTSHEET_TOKEN_HERE"

# AppSheet App credentials
# https://support.google.com/appsheet/answer/10105398
APPSHEET_APP_ID  = "YOUR_APPSHEET_APP_ID_HERE"
APPSHEET_API_KEY = "YOUR_APPSHEET_API_KEY_HERE"

# Google Photos OAuth credentials (JSON file paths)
# https://developers.google.com/photos/library/guides/get-started
GOOGLE_CREDENTIALS = "/path/to/picsafe_google_credentials.json"
GOOGLE_TOKEN       = "/path/to/picsafe_google_token.json"

# Netlify site ID for vanity URL redirect sync (picsafe.net/<slug> → Google Photos)
# Found at: Netlify dashboard → Site settings → General → Site details → Site ID
# Leave blank to skip Netlify sync entirely.
NETLIFY_SITE_ID = "YOUR_NETLIFY_SITE_ID_HERE"

# Legacy / optional (not used in v2 pipeline)
FLICKR_API_KEY = ""
FLICKR_SECRET  = ""
