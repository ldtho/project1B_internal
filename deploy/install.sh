#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash $0" >&2
    exit 1
fi

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
domain=internal.project1b.space
available=/etc/nginx/sites-available/$domain
enabled=/etc/nginx/sites-enabled/$domain
base=/var/www/project1b-internal
release=$base/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$
backup=$(mktemp -d)
had_config=false
had_enabled=false
old_release=$(readlink "$base/current" || true)

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
curl --fail --silent --show-error --resolve "$domain:443:127.0.0.1" "https://$domain/" -o /dev/null
trap - ERR
echo "DEPLOYED https://$domain/"
