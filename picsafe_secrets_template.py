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

# Google OAuth credentials file (shared by Photos and Docs)
# https://developers.google.com/photos/library/guides/get-started
GOOGLE_CREDENTIALS = "/path/to/picsafe_google_credentials.json"

# Google Photos OAuth token (scope: photoslibrary)
# Run picsafe_google_auth.py once to generate this file.
GOOGLE_TOKEN       = "/path/to/picsafe_google_token.json"

# Google Docs OAuth token (scope: documents)
# Run picsafe_google_docs_auth.py once to generate this file.
# Requires the Google Docs API to be enabled in Google Cloud Console.
GOOGLE_DOCS_TOKEN  = "/path/to/picsafe_google_docs_token.json"

# Netlify vanity URL sync is handled via git push — no Netlify credentials needed.
# The publisher writes _redirects to the repo root and pushes; Netlify deploys
# automatically via its GitHub integration.
# (NETLIFY_SITE_ID is no longer used by the v2 publisher.)

# Legacy / optional (not used in v2 pipeline)
FLICKR_API_KEY = ""
FLICKR_SECRET  = ""
