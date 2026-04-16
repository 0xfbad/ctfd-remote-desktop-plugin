#!/usr/bin/env bash
set -euo pipefail

# setup script for ctfd-remote-desktop plugin
# adds required docker-compose volumes, group_add, permissions init service,
# and nginx websocket config

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
CTFD_ROOT="$(cd "$PLUGIN_DIR/../../.." && pwd)"
COMPOSE_FILE="$CTFD_ROOT/docker-compose.yml"
NGINX_CONF="$CTFD_ROOT/conf/nginx/http.conf"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}ok${NC}    $1"; }
added(){ echo -e "  ${GREEN}added${NC} $1"; }
skip() { echo -e "  ${YELLOW}skip${NC}  $1 (already present)"; }
err()  { echo -e "  ${RED}error${NC} $1"; }

echo "remote desktop plugin setup"
echo ""

# check files exist
if [ ! -f "$COMPOSE_FILE" ]; then
    err "docker-compose.yml not found at $COMPOSE_FILE"
    exit 1
fi

if [ ! -f "$NGINX_CONF" ]; then
    err "nginx config not found at $NGINX_CONF"
    exit 1
fi

# docker socket gid
DOCKER_SOCK="/var/run/docker.sock"
if [ -S "$DOCKER_SOCK" ]; then
    DOCKER_GID=$(stat -c '%g' "$DOCKER_SOCK")
    ok "docker socket gid: $DOCKER_GID"
else
    echo -e "  ${YELLOW}warn${NC}  docker socket not found, skipping group_add"
    DOCKER_GID=""
fi

# docker-compose.yml modifications
echo ""
echo "docker-compose.yml"

# group_add
if [ -n "$DOCKER_GID" ]; then
    if grep -q "group_add:" "$COMPOSE_FILE"; then
        skip "group_add"
    else
        # add group_add after the line containing 'build: .'
        sed -i "/^\s*build: \./a\\    group_add:\\n      - \"$DOCKER_GID\"" "$COMPOSE_FILE"
        added "group_add: $DOCKER_GID"
    fi
fi

# all three volume mounts go after the CTFd source mount
if grep -q "docker.sock" "$COMPOSE_FILE"; then
    skip "docker socket volume"
else
    sed -i '/\/opt\/CTFd/a\      - /var/run/docker.sock:/var/run/docker.sock' "$COMPOSE_FILE"
    added "docker socket volume"
fi

if grep -q "ctfd-ssh:/home/ctfd/.ssh" "$COMPOSE_FILE"; then
    skip "ssh volume"
else
    sed -i "/docker.sock/a\\      - ctfd-ssh:/home/ctfd/.ssh:ro" "$COMPOSE_FILE"
    added "ssh named volume"
fi

if grep -q "/home/ctfd/.docker" "$COMPOSE_FILE"; then
    skip "docker config volume"
else
    sed -i "/home\/ctfd\/.ssh/a\\      - ~/.docker:/home/ctfd/.docker:ro" "$COMPOSE_FILE"
    added "docker config volume"
fi

# permissions init service + named volume
# copies host ssh keys into a named volume with correct ownership so the
# ctfd container (uid 1001) can read them without loosening host permissions
echo ""
echo "permissions service"

if grep -q "ctfd-ssh:" "$COMPOSE_FILE" && grep -q "permissions:" "$COMPOSE_FILE"; then
    skip "permissions service and ctfd-ssh volume"
else
    # add permissions service before the nginx service
    PERMS_BLOCK='
  permissions:
    image: alpine:3.23
    user: root
    volumes:
      - ~/.ssh:/mnt/host-ssh:ro
      - ctfd-ssh:/mnt/ctfd-ssh
    command: >
      sh -c '"'"'
        cp -a /mnt/host-ssh/. /mnt/ctfd-ssh/ &&
        chown -R 1001:1001 /mnt/ctfd-ssh
      '"'"'
'
    if ! grep -q "permissions:" "$COMPOSE_FILE"; then
        sed -i "/^\s*nginx:/i\\$PERMS_BLOCK" "$COMPOSE_FILE"
        added "permissions service"
    else
        skip "permissions service"
    fi

    # add depends_on for ctfd service
    if ! grep -q "permissions:" <(sed -n '/^\s*ctfd:/,/^\s*[a-z]/p' "$COMPOSE_FILE" | grep "depends_on" -A5); then
        sed -i '/^\s*ctfd:/,/^\s*depends_on:/{/depends_on:/a\      permissions:\n        condition: service_completed_successfully
}' "$COMPOSE_FILE"
        added "ctfd depends_on permissions"
    fi

    # add named volume declaration
    if ! grep -q "^volumes:" "$COMPOSE_FILE" && ! grep -q "^  ctfd-ssh:" "$COMPOSE_FILE"; then
        echo -e "\nvolumes:\n  ctfd-ssh:" >> "$COMPOSE_FILE"
        added "ctfd-ssh named volume"
    elif ! grep -q "ctfd-ssh:" "$COMPOSE_FILE"; then
        sed -i '/^volumes:/a\  ctfd-ssh:' "$COMPOSE_FILE"
        added "ctfd-ssh named volume"
    else
        skip "ctfd-ssh named volume"
    fi
fi

# docker config permissions
echo ""
echo "file permissions"

if [ -d "$HOME/.docker" ]; then
    current=$(stat -c '%a' "$HOME/.docker")
    if [ "$((0$current & 0755))" = "$((0755))" ]; then
        skip "~/.docker ($current)"
    else
        chmod 755 "$HOME/.docker"
        added "~/.docker -> 755"
    fi
fi

# nginx config
echo ""
echo "nginx config"

if grep -q "remote-desktop/vnc/" "$NGINX_CONF"; then
    skip "vnc proxy location"
else
    cat > /tmp/_rd_nginx_block.conf << 'NGINXBLOCK'

    # VNC proxy with auth_request
    location ~ ^/remote-desktop/vnc/(?<vnc_user_id>\d+)/(?<vnc_path>.+)$ {
      resolver 127.0.0.11 valid=30s;
      auth_request /remote-desktop/vnc/auth;
      auth_request_set $vnc_host $upstream_http_x_vnc_host;
      auth_request_set $vnc_port $upstream_http_x_vnc_port;

      proxy_pass http://$vnc_host:$vnc_port/$vnc_path$is_args$args;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_read_timeout 86400s;
      proxy_send_timeout 86400s;
      proxy_buffering off;
      proxy_cache off;
      add_header Cache-Control "no-store";
    }

    # internal auth subrequest for VNC proxy
    location = /remote-desktop/vnc/auth {
      internal;
      proxy_pass http://app_servers;
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header X-VNC-User-ID $vnc_user_id;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header Cookie $http_cookie;
    }

NGINXBLOCK
    # find all nginx configs and insert the block before the catch-all location
    for conf in "$NGINX_CONF" "${NGINX_CONF%/*}/https.conf"; do
        [ -f "$conf" ] || continue
        if grep -q "remote-desktop/vnc/" "$conf"; then
            continue
        fi
        sed -i '/location \/ {/r /tmp/_rd_nginx_block.conf' "$conf"
    done
    rm -f /tmp/_rd_nginx_block.conf
    added "vnc proxy location"
fi

if grep -q "remote-desktop/terminal/" "$NGINX_CONF"; then
    skip "terminal proxy location"
else
    cat > /tmp/_rd_terminal_nginx_block.conf << 'NGINXBLOCK'

    # web terminal proxy with auth_request (ttyd)
    location ~ ^/remote-desktop/terminal/(?<terminal_user_id>\d+)/(?<terminal_path>.*)$ {
      resolver 127.0.0.11 valid=30s;
      auth_request /remote-desktop/terminal/auth;
      auth_request_set $terminal_host $upstream_http_x_terminal_host;
      auth_request_set $terminal_port $upstream_http_x_terminal_port;

      proxy_pass http://$terminal_host:$terminal_port/$terminal_path$is_args$args;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Accept-Encoding "";
      gunzip on;

      # inject nerd font so ttyd renders eza icons
      sub_filter '</head>' '<link rel="preload" href="/remote-desktop/static/fonts/JetBrainsMonoNerdFontMono-Regular.woff2" as="font" type="font/woff2" crossorigin><style>@font-face{font-family:JetBrainsMonoNerdFont;font-display:block;src:url(/remote-desktop/static/fonts/JetBrainsMonoNerdFontMono-Regular.woff2) format("woff2")}</style><script>document.fonts.ready.then(()=>{const i=setInterval(()=>{if(!window.term)return;clearInterval(i);Promise.all(Array.from(document.fonts).map(f=>f.load())).then(()=>{const o=window.term.options.fontFamily;window.term.options.fontFamily="monospace";window.term.options.fontFamily=o;if(window.term.fit)window.term.fit()})},50)})</script></head>';
      sub_filter_once on;

      proxy_read_timeout 86400s;
      proxy_send_timeout 86400s;
      proxy_buffering off;
      proxy_cache off;
      add_header Cache-Control "no-store";
    }

    # internal auth subrequest for terminal proxy
    location = /remote-desktop/terminal/auth {
      internal;
      proxy_pass http://app_servers;
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header X-Terminal-User-ID $terminal_user_id;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header Cookie $http_cookie;
    }

NGINXBLOCK
    for conf in "$NGINX_CONF" "${NGINX_CONF%/*}/https.conf"; do
        [ -f "$conf" ] || continue
        if grep -q "remote-desktop/terminal/" "$conf"; then
            continue
        fi
        sed -i '/location \/ {/r /tmp/_rd_terminal_nginx_block.conf' "$conf"
    done
    rm -f /tmp/_rd_terminal_nginx_block.conf
    added "terminal proxy location"
fi

if grep -q "location /remote-desktop/static/fonts/" "$NGINX_CONF"; then
    skip "font mime type location"
else
    cat > /tmp/_rd_font_nginx_block.conf << 'NGINXBLOCK'

    # serve nerd font with correct mime type
    location /remote-desktop/static/fonts/ {
      proxy_pass http://app_servers;
      proxy_hide_header Content-Type;
      add_header Content-Type "font/woff2";
      add_header Cache-Control "public, max-age=31536000";
      add_header Access-Control-Allow-Origin "*";
    }

NGINXBLOCK
    for conf in "$NGINX_CONF" "${NGINX_CONF%/*}/https.conf"; do
        [ -f "$conf" ] || continue
        if grep -q "location /remote-desktop/static/fonts/" "$conf"; then
            continue
        fi
        sed -i '/location \/ {/r /tmp/_rd_font_nginx_block.conf' "$conf"
    done
    rm -f /tmp/_rd_font_nginx_block.conf
    added "font mime type location"
fi

echo ""
echo -e "${GREEN}done${NC} - restart containers to apply: docker compose up -d"
