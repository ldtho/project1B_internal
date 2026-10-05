# captioning_data

Dataset video review and caption correction at **https://internal.project1b.space/captioning_data/**.

## Access

Sign in on the internal site with an existing Project1B account. Accounts and roles use Project1B's authentication API; the internal hostname requires its own login because session cookies are host-only.

- All authenticated users can view, correct, save, and reset captions.
- `caption_data_reviewer` identifies caption reviewers.
- `caption_data_admin` identifies caption administrators; it does not grant account-management or recording-library access.
- Existing Project1B `admin` controls account roles and recording-library access.
- Sample assignments organize workload; they do not restrict editing permissions.

## Deployment and database setup

Requirements: existing Project1B API at `127.0.0.1:8903`, internal-host DNS/HTTPS, Nginx, certbot, systemd, sudo, and Python 3.10+ with `python-dotenv`. The VR-finetune-VLM checkout must provide viewer helpers, `data/splits/{train,val,test}.jsonl`, and readable video paths.

Back up the database before changing roles. The following commands assume a configured PostgreSQL connection service named `project1b`.

From the **main Project1B source checkout**:

```bash
git apply --check /path/to/project1B_internal/deploy/project1b-caption-roles.patch
git apply /path/to/project1B_internal/deploy/project1b-caption-roles.patch

psql 'service=project1b' -X -v ON_ERROR_STOP=1 \
  -f /path/to/project1B_internal/deploy/caption_data_roles.sql
```

The patch adds both caption roles to the backend model and API allowlist. The SQL expands `ck_user_roles_role` transactionally without changing existing users or grants. Apply the SQL, then release the patched backend through Project1B's normal process before granting new roles. No caption-table migration is needed.

From the **project1B_internal checkout**, adjusting paths and service user:

```bash
sudo DATASETS_REPO=/path/to/VR-finetune-VLM \
  DATASETS_PYTHON=/path/to/python3 DATASETS_USER=tho2 \
  bash deploy/install.sh
```

The installer snapshots the viewer and imported helpers, installs `project1b-datasets.service`, configures the internal Nginx site and certificate, and checks readiness. The main website remains separate. Correction history stays outside releases at `/var/lib/project1b-datasets/annotation_edits.jsonl`; redeployment preserves it.

First installation seeds history from `DATASETS_REPO/data/annotation_edits.jsonl` if present. Inspect that file first so development edits do not become production corrections.

```bash
sudo systemctl status project1b-datasets.service --no-pager
curl --fail http://127.0.0.1:8325/healthz
sudo nginx -t
```

Verify login, video playback, save, Before / after, and reload with an ordinary Project1B account.

## Assign reviewer and administrator roles

Use an existing Project1B `admin` account on **the main Project1B site**. The internal site does not expose account-management endpoints. The supplied backend patch enables the new roles in the API; the existing role selector may still show only the original roles.

Role updates use `PUT /api/admin/workers/{user_id}/roles`. **This replaces the complete role set; preserve unrelated grants.** In the main site's browser console, read the account and add the desired role:

```javascript
const userId = 'REPLACE_WITH_USER_UUID';
const captionRole = 'caption_data_reviewer'; // or 'caption_data_admin'
const url = `/api/admin/workers/${encodeURIComponent(userId)}`;
const accountResponse = await fetch(url, { credentials: 'same-origin' });
if (!accountResponse.ok) throw new Error(await accountResponse.text());
const account = await accountResponse.json();
const roles = [...new Set([...account.roles, captionRole])];
console.log({ user: account.username, before: account.roles, after: roles });
```

Check the account and proposed roles, then submit:

```javascript
const cookie = document.cookie.split('; ')
  .find(value => value.startsWith('__Host-rbt_csrf='));
if (!cookie) throw new Error('Sign in with a Project1B web session first');
const response = await fetch(`${url}/roles`, {
  method: 'PUT', credentials: 'same-origin',
  headers: {
    'Content-Type': 'application/json',
    'X-CSRF-Token': decodeURIComponent(cookie.slice(cookie.indexOf('=') + 1))
  },
  body: JSON.stringify({ roles })
});
if (!response.ok) throw new Error(await response.text());
console.log(await response.json());
```

To remove a role, reread the account and use `account.roles.filter(role => role !== captionRole)` for the proposed set. Role changes are audited and revoke the user's sessions; ask them to sign in again. Use the API rather than editing grants directly in SQL.

## Assign and reassign videos

Account roles and video assignments are separate. Assignments live under the configured data root, not in Project1B's recording/cutting assignment tables.

`data/egoverse/qa/roster.json`:

```json
{
  "reviewers": ["Reviewer One", "Reviewer Two"],
  "admins": ["Caption Lead"]
}
```

`data/egoverse/qa/assignment.json`:

```json
{
  "episodes": {
    "EgoVerse:train:egoverse/example-a": "Reviewer One",
    "EgoVerse:val:egoverse/example-b": "Reviewer Two"
  }
}
```

Use exact sample `id` values from the authenticated `/captioning_data/api/episodes` response. Assignees are unique display labels, ideally matching Project1B full names; include each label in the roster. Audit records independently store stable authenticated user IDs.

To reassign samples, back up both JSON files, update only the relevant entries, then restart the service and refresh the browser. Ask reviewers to save drafts before restart. Use the **QA assignee** filter or progress panel to inspect workload.

For an initial automatic EgoVerse assignment, create the roster and run from the internal checkout:

```bash
python3 -B - <<'PY'
from pathlib import Path
from datasets_server import datasets

datasets.REPO = Path('/path/to/VR-finetune-VLM')
qa = datasets.REPO / datasets.QA_DIR
roster = datasets.read_json(qa / 'roster.json', {'reviewers': [], 'admins': []})
datasets.write_assignment(qa / 'assignment.json', datasets.load(datasets.SOURCES), roster)
PY
```

The helper refuses to overwrite existing assignments. It assigns EgoVerse only, with reviewers receiving roughly twice an administrator's share. Assign other datasets explicitly in the same `episodes` map.

New Project1B uploads do not automatically appear here: prepare video files and manifest rows through the data-preparation pipeline. Active corrections pin the entire source manifest's SHA-256. Before replacing source manifests, export a verified snapshot and retain the original sources; use a reviewed migration or a new immutable batch with separate history. Do not clear history or bypass source validation.

## Review and compare captions

1. Filter samples, select a video, and inspect instruction, sub-task, and atomic captions.
2. Edit text/timing and open **Before / after** to inspect the draft.
3. Click **Save**, wait for success, then reload to verify persistence.

Comparison highlights text changes, timing changes, and added/removed cues. Timestamp buttons seek the video. Select saved revisions to inspect reviewer/time metadata; **Before selected change** compares against the state that reviewer started from. **Show unchanged captions** includes untouched cues.

An unchanged save marks the sample **confirmed**; a changed save marks it **corrected**. **Reset** restores original captions by appending a revert and retains earlier revisions. Comparison is read-only; restoring an older correction requires reapplying it and saving a new revision.

On session expiry, keep the editor open, sign in in another tab, then use **Resume session**. Drafts are not durable until saved. A `409` means another reviewer saved first: preserve your intended text, reopen the sample, reconcile, and retry.

## Store corrections and export for training

Production history: `/var/lib/project1b-datasets/annotation_edits.jsonl`. Each save records full corrected captions, original/before states, editor ID/name/roles, UTC time, version, and source hash. Writes are appended and synced before success; reset keeps history. Schedule verified backups and copy them to separate storage.

Run export as the service user, using the deployed code and actual data root:

```bash
snapshot_name="production-$(date -u +%Y%m%dT%H%M%SZ)"
sudo -u tho2 env DATASETS_REPO=/var/www/project1b-internal/current/service \
  /path/to/python3 -B /var/www/project1b-internal/current/datasets_server.py \
  --data-root /path/to/VR-finetune-VLM \
  --edits /var/lib/project1b-datasets/annotation_edits.jsonl \
  --export "/path/to/backups/$snapshot_name"
```

The destination must be new. Export publishes an atomic package containing corrected train/val/test manifests, the captured audit log, and `snapshot.json` provenance/checksums. It rejects corrupt history and source drift. Changed rows include original fields and editor/version metadata; videos are referenced, not copied.

Verify checksums before training:

```bash
python3 -B - /path/to/exported-snapshot <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
metadata = json.loads((root / 'snapshot.json').read_text())
for relative, expected in metadata['file_sha256'].items():
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected, relative
print('Snapshot checksums verified')
PY
```

Train from the exported manifests and retain the package plus accessible video roots. Export includes unchanged samples too; check review coverage, legacy hashless edits, and split policy before training.

## Operate and update

```bash
sudo journalctl -u project1b-datasets.service -n 100 --no-pager
sudo systemctl restart project1b-datasets.service
```

For code updates, back up history and rerun the installer from the reviewed checkout with the same settings. For rollback, restore the matching release and systemd unit together; preserve correction history.

Storage failures block further writes until the log is inspected and the service restarted. Stop the service and preserve the damaged log before recovery. Only one writer may use a history file.
