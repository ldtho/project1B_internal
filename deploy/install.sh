#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash $0" >&2
    exit 1
fi

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
datasets_root=${DATASETS_REPO:-$(dirname "$repo")}
datasets_python=${DATASETS_PYTHON:-$(command -v python3)}
datasets_user=${DATASETS_USER:-${SUDO_USER:-tho2}}
datasets_state=/var/lib/project1b-datasets
service=/etc/systemd/system/project1b-datasets.service
if [[ ! -f $datasets_root/data/splits/train.jsonl ]]; then
    echo "Set DATASETS_REPO to the VR-finetune-VLM checkout containing data/splits and viewer helpers." >&2
    exit 1
fi
for path in "$datasets_root" "$datasets_python"; do
    if [[ $path != /* || $path == *[\"%]* || $path == *$'\n'* ]]; then
        echo "Dataset paths must be absolute and contain no quote, percent, or newline." >&2
        exit 1
    fi
done
id "$datasets_user" >/dev/null
DATASETS_REPO="$datasets_root" "$datasets_python" "$repo/datasets_server.py" --help >/dev/null
domain=internal.project1b.space
available=/etc/nginx/sites-available/$domain
enabled=/etc/nginx/sites-enabled/$domain
base=/var/www/project1b-internal
release=$base/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$
backup=$(mktemp -d)
had_config=false
had_enabled=false
old_release=$(readlink "$base/current" || true)
had_service=false
service_enabled=false
service_active=false
if [[ -e $service ]]; then
    if ! grep -q '^# Project1B dataset viewer' "$service"; then
        echo "Existing $service is not owned by this project; leaving it untouched." >&2
        rmdir "$backup"
        exit 1
    fi
    cp -a "$service" "$backup/service"
    had_service=true
fi
if systemctl is-enabled --quiet project1b-datasets.service; then service_enabled=true; fi
if systemctl is-active --quiet project1b-datasets.service; then service_active=true; fi

# Refuse a first install if the dedicated adapter port belongs to another process.
if ! $had_service; then
    "$datasets_python" - <<'PYTHON_PORT'
import socket
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 8326))
PYTHON_PORT
fi

if [[ -e $available ]] && ! grep -q '^# Project1B internal recording library' "$available"; then
    echo "Existing $available is not owned by this project; leaving it untouched." >&2
    rmdir "$backup"
    exit 1
fi
if [[ -e $enabled || -L $enabled ]] && [[ $(readlink -f "$enabled") != "$available" ]]; then
    echo "Existing $enabled points to another configuration; leaving it untouched." >&2
    rmdir "$backup"
    exit 1
fi
if [[ -e $available ]]; then cp -a "$available" "$backup/config"; had_config=true; fi
if [[ -e $enabled || -L $enabled ]]; then had_enabled=true; fi

rollback() {
    trap - ERR
    set +e
    if $had_config; then cp -a "$backup/config" "$available"; else rm -f "$available"; fi
    if ! $had_enabled; then rm -f "$enabled"; fi
    if [[ -n $old_release ]]; then
        ln -sfn "$old_release" "$base/current"
    else
        rm -f "$base/current"
    fi
    systemctl stop project1b-datasets.service
    if $had_service; then cp -a "$backup/service" "$service"; else rm -f "$service"; fi
    if ! $service_enabled; then systemctl disable project1b-datasets.service; fi
    systemctl daemon-reload
    if $service_active; then systemctl start project1b-datasets.service; fi
    nginx -t && systemctl reload nginx
    echo "Deployment failed; previous internal site configuration restored." >&2
    exit 1
}
trap rollback ERR
trap 'rm -rf -- "$backup"' EXIT

install -d -m 755 "$release" /var/www/letsencrypt
for asset in index.html styles.css app.js favicon.svg; do
    install -m 644 "$repo/web/$asset" "$release/$asset"
done
install -d -m 755 "$release/web"
install -m 644 "$repo/web/datasets-auth.js" "$release/web/datasets-auth.js"
install -m 644 "$repo/datasets_server.py" "$release/datasets_server.py"
install -d -m 755 "$release/viewer"
install -m 644 "$repo/viewer/server.py" "$release/viewer/server.py"
install -m 644 "$repo/viewer/index.html" "$release/viewer/index.html"
# Snapshot only the viewer's imported local modules; rollback restores the same code and UI.
"$datasets_python" - "$datasets_root" "$release/service" <<'PYTHON'
import shutil
import sys
from pathlib import Path
source, target = map(Path, sys.argv[1:])
source = source.resolve()
sys.path.insert(0, str(source))
from data_prep.viz.datasets import server
files = set()
for module in tuple(sys.modules.values()):
    if file := getattr(module, '__file__', None):
        file = Path(file).resolve()
        if file.is_file() and file.is_relative_to(source):
            files.add(file)
files.add(server.UI)
for file in files:
    out = target / file.relative_to(source)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(file, out)
PYTHON
install -d -m 700 -o "$datasets_user" "$datasets_state"
if [[ ! -e $datasets_state/annotation_edits.jsonl ]]; then
    initial_edits=$datasets_root/data/annotation_edits.jsonl
    if [[ ! -f $initial_edits ]]; then initial_edits=/dev/null; fi
    install -m 600 -o "$datasets_user" "$initial_edits" "$datasets_state/annotation_edits.jsonl"
fi
cat > "$service" <<UNIT
# Project1B dataset viewer — managed by project1B_internal/deploy/install.sh.
[Unit]
Description=Project1B authenticated dataset viewer
After=network.target

[Service]
User=$datasets_user
Environment="DATASETS_REPO=$release/service"
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart="$datasets_python" "$release/datasets_server.py" --data-root "$datasets_root" --edits "$datasets_state/annotation_edits.jsonl"
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$datasets_state
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable project1b-datasets.service
systemctl restart project1b-datasets.service
dataset_ready=false
for attempt in {1..30}; do
    if systemctl is-active --quiet project1b-datasets.service && curl --noproxy '*' --fail --silent --connect-timeout 1 --max-time 2 \
        http://127.0.0.1:8326/healthz -o "$backup/datasets-health.json"; then
        dataset_ready=true
        break
    fi
    sleep 1
done
if ! $dataset_ready; then
    journalctl -u project1b-datasets.service -n 15 --no-pager >&2
    false
fi

# Give ACME its own hostname before obtaining the separate certificate.
if [[ ! -f /etc/letsencrypt/live/$domain/fullchain.pem ]]; then
    cat > "$available" <<'NGINX'
# Project1B internal recording library — certificate bootstrap.
server {
    listen 80;
    listen [::]:80;
    server_name internal.project1b.space;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type text/plain;
    }
    location / { return 503; }
}
NGINX
    ln -sfn "$available" "$enabled"
    nginx -t
    systemctl reload nginx
fi

certbot certonly --webroot --webroot-path /var/www/letsencrypt \
    --domain "$domain" --cert-name "$domain" --keep-until-expiring --non-interactive

install -m 644 "$repo/deploy/nginx.conf" "$available"
ln -sfn "$available" "$enabled"
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
nginx -t
systemctl reload nginx
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/project1b-internal-reload <<'HOOK'
#!/bin/sh
if [ "$RENEWED_LINEAGE" = /etc/letsencrypt/live/internal.project1b.space ]; then
    /usr/sbin/nginx -t && /bin/systemctl reload nginx
fi
HOOK
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/project1b-internal-reload
# Wait for the new workers: systemctl reload only sends a signal to Nginx.
ready=false
for attempt in {1..15}; do
    if curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
        --resolve "$domain:443:127.0.0.1" "https://$domain/" \
        -o "$backup/health.html" 2>"$backup/health.error" \
        && cmp -s "$repo/web/index.html" "$backup/health.html"; then
        ready=true
        break
    fi
    sleep 1
done
if ! $ready; then
    cat "$backup/health.error" >&2
    echo "Nginx did not serve the expected internal site before the readiness deadline." >&2
    false
fi
trap - ERR
echo "DEPLOYED https://$domain/"
echo "CAPTIONING_DATA https://$domain/captioning_data/"
