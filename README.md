# Project1B internal recording library

Standalone static frontend for `https://internal.project1b.space/`. No production dependencies or build step. Source lives in `web/`; hosting configuration lives in `deploy/`.

## Access and downloads

Sign in with an existing Project1B **admin** account. Login uses the current API's secure, HttpOnly, host-only session cookie. This site stores no passwords or tokens in browser storage. Regular contributor accounts cannot browse this sample.

Browse the latest 500 recordings per status, search that loaded set, preview primary/reference video, inspect session files and MCAP presence, and download the complete finalized phone upload as a streamed ZIP. The ZIP preserves every file in the original upload manifest; it does not synthesize absent sensor data or include derived files. Incomplete uploads and external dataset videos do not have an original phone bundle.

Downloads go straight to the browser, without buffering the complete recording in JavaScript. The existing API checks permissions, manifest sizes, and checksums while streaming. If the server refuses a download, its error opens in a separate tab rather than being saved as a fake ZIP. If a stream fails, the browser may keep a partial ZIP; check that the download finished before using it.

The internal Nginx route permits only the needed read endpoints plus login/logout. It exposes no review, delete, upload, or role-management actions. Existing admin accounts retain their normal privileges on the main domain. No changes to the main repository or API are required.

## Deploy on the existing server

DNS: `A internal → 211.26.247.72`.

```bash
sudo bash /mnt/SSD5/project1B_internal/deploy/install.sh
```

The script installs a small static release under `/var/www/project1b-internal`, adds its own Nginx configuration, obtains a separate Let's Encrypt certificate using the server's existing ACME account, verifies Nginx, and reloads it. It restores the previous internal configuration on failure. It requires sudo; it does not modify the main site's configuration or source. Certificate renewal uses the existing certbot scheduler plus a reload hook for this certificate.

Run the same command to deploy updates. Source and Git checkout remain on SSD5; Nginx does not need access to that disk. `tho2` has shared ACL access to the checkout.

## Runnable check

The browser smoke check uses Node's built-in assertions, Playwright as a browser driver, and a local Nginx instance with a temporary certificate and fake API. It exercises login, staff access, file download, missing bundles, search, logout/CSRF, session expiry, mobile layout, and blocked write routes. It never contacts the production API.

```bash
npm install --prefix /mnt/SSD5/.codex-ui-validation playwright
PLAYWRIGHT_BROWSERS_PATH=/mnt/SSD5/.codex-ui-validation/browsers \
  /mnt/SSD5/.codex-ui-validation/node_modules/.bin/playwright install chromium
NODE_PATH=/mnt/SSD5/.codex-ui-validation/node_modules \
PLAYWRIGHT_BROWSERS_PATH=/mnt/SSD5/.codex-ui-validation/browsers \
  node tests/smoke.cjs
```
