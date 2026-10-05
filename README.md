# Project1B internal recording library

Recording library at `https://internal.project1b.space/`, plus **captioning_data** at `/captioning_data/`. Frontend source lives in `web/`; hosting configuration lives in `deploy/`. A pinned copy of `data_prep/viz/datasets` lives in `viewer/`; imported caption/video helpers come from the VR-finetune-VLM deployment checkout. Manifests and videos stay outside this repository.

See [the Project1B captioning_data operations guide](docs/captioning_data.md) for production setup, login, database migration, role grants, video assignments, safe exports, and rollback. Local QA preview is stopped; production deployment and role migration have not been performed.

## Access and downloads

Sign in with an existing Project1B account. **All authenticated users can view and correct datasets; recording downloads require admin access.** Login uses the current API's secure, HttpOnly, host-only session cookie. This site stores no passwords or authentication tokens in browser storage.

Browse the latest 500 recordings per status, search that loaded set, preview primary/reference video, inspect session files and MCAP presence, and download the complete finalized phone upload as a streamed ZIP. The ZIP preserves every file in the original upload manifest; it does not synthesize absent sensor data or include derived files. Incomplete uploads and external dataset videos do not have an original phone bundle.

Downloads go straight to the browser, without buffering the complete recording in JavaScript. The existing API checks permissions, manifest sizes, and checksums while streaming. If the server refuses a download, its error opens in a separate tab rather than being saved as a fake ZIP. If a stream fails, the browser may keep a partial ZIP; check that the download finished before using it.

The internal Nginx route permits recording read endpoints, login/logout, and dataset save/revert. It exposes no recording review, delete, upload, or role-management actions. Caption access uses the existing main Project1B API; assigning the new caption roles requires the supplied backend allowlist patch and database migration.

## captioning_data flow

1. Open `/captioning_data/`. Without a session, return to the existing login screen, then back to `/captioning_data/`. Signed-in users enter directly. Episode links keep their hash through login.
2. Browse the existing train/val/test viewer: search, subtitles, timelines, QA flags, and instruction/subtask/atomic editing. Existing assignees remain useful filters; they do not restrict Project1B users' saves. Initial filter shows all samples.
3. Each HTML, asset, metadata, subtitle, video, save, and revert request validates the session against Project1B `/api/auth/me`. Video supports byte ranges; private responses are not cached. The adapter accepts cookies, not client-supplied identities or local QA tokens. Authentication outages deny access with 503.
4. Save/revert requires the site's exact Origin and the host-only CSRF cookie matching `X-CSRF-Token`. Server derives editor name and stable `editor_id` from Project1B. Existing caption validation and version checks reject invalid edits and stale writes.
5. Saves append full captions and sync history plus its directory before updating the viewer. Each record includes editor ID, authenticated roles, UTC time, version, and original manifest SHA-256. Manifests stay unchanged. Reset appends a revert; earlier corrections remain recoverable. One process holds the writer lock; request threads serialize writes. Exports share a per-append file lock. Storage failures block further saves until an administrator checks the log and restarts the service.
6. An expired session leaves unsaved edits onscreen. Sign in in another tab, click **Resume session**, then save again. Concurrent edits return 409; reopen the sample to load its current version before retrying.

Use **Before / after** beside the editing controls to inspect instruction, sub-task, and atomic captions. It compares original vs current captions by default, highlights removed/added words, and labels timing changes and added/removed cues. Unchanged captions are hidden until **Show unchanged captions** is checked. Timestamp buttons jump the video to either version's cue. On narrow screens, Before and After stack vertically, and opening the comparison hides the episode list; **☰ episodes** brings it back.

Choose saved versions in the Before/After selectors to inspect earlier reviewer changes, with editor and save time shown above the comparison. **Before selected change** compares each fix with the captions the reviewer started from; **Original captions** compares with that version's original. Unsaved changes display as **Unsaved draft** and update while typing; they become a saved version only after Save succeeds. Reset restores original captions while retaining all prior versions for inspection. New corrections store original and before-state snapshots in the durable audit log, so comparisons survive later source changes. Earlier-source versions are labelled; imported history without stored snapshots is unavailable when its source no longer matches. The comparison opens automatically for samples with saved history and stays read-only; it does not change the active editor or training export.

Dataset adapter binds only `127.0.0.1:8326`; Nginx exposes its allowlisted `/captioning_data/` routes. `/healthz` stays local. Dataset scripts are external and retain `script-src 'self'`; only dataset styles allow inline declarations, required by the existing timeline UI. Recording route CSP remains unchanged.

The shared dataset viewer keeps its existing QA login and assignments. Its small `window.DATASET_AUTH` hooks enable Project1B integration. The pinned UI/backend is recorded in `viewer/SOURCE_SNAPSHOT.json`; the original VR-finetune-VLM checkout remains unchanged by this repository's deployment.

## Local mock preview

```bash
python3 -B project1B_internal/local_preview.py
```

Open `https://127.0.0.1:9445/captioning_data/`. This uses a local self-signed certificate; accept the browser's local certificate prompt. Sign in as `admin`, `worker`, `caption_manager`, `caption_data_admin`, or `caption_data_reviewer`, password `preview-password` for all five. These are fictional preview accounts. The mock database includes a role catalog and uses the Project1B session/CSRF cookie contract; grants are loaded from SQLite on each request. Caption roles do not grant recording-library admin access. All authenticated users retain caption editing access.

The launcher starts its own loopback Nginx, mock auth API, and dataset adapter without sudo. It reuses the production route configuration and displays the actual dataset samples/videos. Account/session tables live in `project1B_internal/.preview/mock.sqlite3`; correction history lives in `.preview/edits.jsonl`, initially copied from the existing dataset history. Mock users, corrections, logs, and certificates stay inside this ignored directory. Production databases and services are not used.

Keep the command running; Ctrl+C stops the local servers. To run in the background:

```bash
mkdir -p project1B_internal/.preview
nohup python3 -B project1B_internal/local_preview.py \
  >project1B_internal/.preview/preview.log 2>&1 </dev/null &
# Stop later:
kill -TERM "$(cat project1B_internal/.preview/preview.pid)"
```

If browsing from another machine, forward the loopback port over SSH, then open the same URL:

```bash
ssh -N -L 9445:127.0.0.1:9445 YOUR_SERVER
```

For direct access over this server's Tailscale address, launch with:

```bash
python3 -B project1B_internal/local_preview.py --host 100.89.98.89
```

Open `https://100.89.98.89:9445/captioning_data/` from a device connected to the same tailnet. Accept the self-signed certificate prompt. The HTTPS listener binds that address; auth/data upstreams remain loopback-only. Origin checks, redirects, and certificate SAN use the selected address. Existing mock accounts and correction history are preserved. Pass `PREVIEW_URL=https://100.89.98.89:9445` when running the preview check below.

Check the running preview:

```bash
NODE_PATH=/mnt/SSD5/.codex-ui-validation/node_modules \
PLAYWRIGHT_BROWSERS_PATH=/mnt/SSD5/.codex-ui-validation/browsers \
  node project1B_internal/tests/preview.cjs
```

`--port` changes the HTTPS port; `--state-dir` chooses another isolated mock database/history. With custom values, pass `PREVIEW_URL` and `PREVIEW_STATE` to the check. The check verifies login return, grants/revocation within an existing session, real video playback, isolated corrections, and logout. The mock API is a preview substitute for the Project1B account API; it does not reproduce the entire production schema.

## Correction storage and training export

Local corrections persist at `project1B_internal/.preview/edits.jsonl` across preview restarts. The SQLite database holds accounts, roles, and sessions; the append-only correction log holds captions and their audit history. Keep both files when restarting. A successful save means the correction and file directory have been flushed to disk. Reset changes the current view while retaining prior versions in history.

Create a training snapshot without stopping the viewer:

```bash
python3 -B project1B_internal/datasets_server.py \
  --data-root "$PWD" --edits project1B_internal/.preview/edits.jsonl \
  --export /mnt/SSD5/captioning_data-snapshots/preview-2026-10-05
```

Use a new output directory for each snapshot. It contains corrected `data/splits/{train,val,test}.jsonl`, full `annotation_edits.jsonl`, and `snapshot.json` with source/export SHA-256 checksums and history counts. Training rows retain original caption fields in `before_edit` and include editor name/ID, time, and version. Export reads a consistent history snapshot, writes into a temporary directory, flushes files/directories, then publishes the completed directory. It refuses existing destinations, malformed history, and source manifests changed since a new correction was saved. Earlier history without source hashes is counted as `legacy_history_records`; inspect those imported edits before training. Original manifests and videos stay intact.

Keep an additional copy of completed snapshots on a separate disk or backup store. Local disk flush protects committed writes and restarts; a separate backup protects against disk loss. Export includes the entire train/val/test manifests, not only corrected samples, and does not introduce an approval filter.

```bash
python3 -B project1B_internal/tests/storage.py
```

## Caption roles in Project1B

The local mock database seeds `caption_data_admin` and `caption_data_reviewer` in its role catalog and grants them to matching fictional accounts. Existing users/grants remain unchanged. Authentication returns these roles directly. Caption administrators display as dataset admins; caption reviewers display as reviewers. Existing Project1B `admin` retains its current authority, and the current all-authenticated caption editing policy remains in effect.

For the main Project1B deployment, `deploy/caption_data_roles.sql` expands `ck_user_roles_role` transactionally, preserving existing grants. `deploy/project1b-caption-roles.patch` updates the backend model and existing admin role-assignment API allowlist. Apply both through the main Project1B deployment process, then assign roles through its existing admin endpoint or `scripts/grant_role.py`. These prepared files are not applied to the live database by the internal-site installer; no production users receive new grants automatically.

## Deploy on the existing server

DNS: `A internal → 211.26.247.72`.

```bash
sudo DATASETS_REPO=/home/tho2/VR-finetune-VLM \
  DATASETS_PYTHON=/home/tho2/miniconda3/bin/python3 DATASETS_USER=tho2 \
  bash /home/tho2/VR-finetune-VLM/project1B_internal/deploy/install.sh
```

Run this from the updated project1B_internal checkout; adjust paths to the target server. `DATASETS_REPO` must contain `data/splits/{train,val,test}.jsonl` and the existing viewer helper modules; reviewed viewer UI/backend comes from `viewer/`. `DATASETS_PYTHON` must already support the viewer's imports, including `python-dotenv`. `DATASETS_USER` must read the manifests and original/remapped videos. The default data root is the checkout's parent; a standalone checkout needs an explicit VR-finetune-VLM root.

The script snapshots frontend, adapter, and imported shared viewer modules under `/var/www/project1b-internal/releases`, installs `project1b-datasets.service`, verifies its local health, adds Nginx configuration, obtains a separate Let's Encrypt certificate using the existing ACME account, and reloads Nginx. On failure it restores the previous internal configuration, release, and service. It requires sudo and leaves the main site's configuration and source intact. Certificate renewal uses the existing certbot scheduler plus a reload hook.

Correction history lives at `/var/lib/project1b-datasets/annotation_edits.jsonl`, outside releases. First install seeds it from the checkout's existing `data/annotation_edits.jsonl` when present. Redeploy and rollback preserve it. The deployed site and standalone viewer subsequently have independent histories; do not run another writer against the deployed log. Export and back up deployed corrections using the installed adapter and shared exporter snapshot:

```bash
DATASETS_REPO=/var/www/project1b-internal/current/service \
  python /var/www/project1b-internal/current/datasets_server.py \
  --data-root /home/tho2/VR-finetune-VLM \
  --edits /var/lib/project1b-datasets/annotation_edits.jsonl \
  --export /mnt/SSD5/captioning_data-snapshots/production-2026-10-05
```

Run export as the service user, from the data checkout. Inspect service logs with `journalctl -u project1b-datasets.service`; restart after repairing any storage failure.

Run the same command from the updated checkout to deploy updates. Nginx serves the installed release; the dataset service reads manifests and videos from their existing locations.

## Runnable check

The browser smoke check uses Node's built-in assertions, Playwright, local Nginx, temporary dataset files, and a fake API. It verifies recording downloads and role restrictions; contributor dataset access; protected metadata/media/assets; CSRF/Origin; byte ranges; caption validation; save/revert; stale/concurrent writes; audit identity; history replay; singleton writer; and session recovery preserving drafts. It never contacts production.

```bash
npm install --prefix /mnt/SSD5/.codex-ui-validation playwright
PLAYWRIGHT_BROWSERS_PATH=/mnt/SSD5/.codex-ui-validation/browsers \
  /mnt/SSD5/.codex-ui-validation/node_modules/.bin/playwright install chromium
NODE_PATH=/mnt/SSD5/.codex-ui-validation/node_modules \
PLAYWRIGHT_BROWSERS_PATH=/mnt/SSD5/.codex-ui-validation/browsers \
  node project1B_internal/tests/smoke.cjs
```

Run from the VR-finetune-VLM root, or set `DATASETS_REPO` to that checkout and run `node tests/smoke.cjs` from a standalone internal checkout. The test packages the repository's pinned viewer and the checkout's existing helpers. Set `DATASETS_PYTHON` if `python3` lacks the existing viewer dependencies.
