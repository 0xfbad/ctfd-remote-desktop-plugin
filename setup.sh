#!/usr/bin/env bash
set -euo pipefail

# setup script for ctfd-remote-desktop plugin
# adds required docker-compose volumes, group_add, nginx websocket config,
# and fixes file permissions so the ctfd container user can read them

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

if grep -q "/home/ctfd/.ssh" "$COMPOSE_FILE"; then
    skip "ssh volume"
else
    sed -i "/docker.sock/a\\      - ~/.ssh:/home/ctfd/.ssh:ro" "$COMPOSE_FILE"
    added "ssh volume"
fi

if grep -q "/home/ctfd/.docker" "$COMPOSE_FILE"; then
    skip "docker config volume"
else
    sed -i "/home\/ctfd\/.ssh/a\\      - ~/.docker:/home/ctfd/.docker:ro" "$COMPOSE_FILE"
    added "docker config volume"
fi

# file permissions
echo ""
echo "file permissions"

fix_perms() {
    local path="$1"
    local target="$2"
    local desc="$3"

    if [ ! -e "$path" ]; then
        echo -e "  ${YELLOW}warn${NC}  $path not found, skipping"
        return
    fi

    current=$(stat -c '%a' "$path")
    if [ "$current" = "$target" ] || [ "$((0$current & 0$target))" = "$((0$target))" ]; then
        skip "$desc ($current)"
    else
        chmod "$target" "$path"
        added "$desc -> $target"
    fi
}

fix_perms "$HOME/.docker" "755" "~/.docker"
fix_perms "$HOME/.ssh" "755" "~/.ssh"

if [ -f "$HOME/.ssh/known_hosts" ]; then
    fix_perms "$HOME/.ssh/known_hosts" "644" "~/.ssh/known_hosts"
fi

# ssh keys: the .ssh dir is bind-mounted into the ctfd container which runs
# as a different uid. paramiko (used by the docker SDK for ssh tunnels) needs
# to read the key but doesn't enforce permissions. openssh does enforce 600.
# solution: keep the standard key names at 644 so paramiko can read them,
# and make a 600 copy for CLI ssh usage.
for key in "$HOME/.ssh/id_"*; do
    [ -f "$key" ] || continue
    case "$key" in
        *.pub|*_cli) continue ;;
    esac

    cli_copy="${key}_cli"
    if [ ! -f "$cli_copy" ]; then
        cp "$key" "$cli_copy"
        chmod 600 "$cli_copy"
        added "$cli_copy (600, for CLI ssh)"
    else
        skip "$cli_copy"
    fi

    fix_perms "$key" "644" "$key (container-readable)"
done

# ssh config for CLI usage that points at the 600 copies. kept outside the
# standard config path so the container user doesn't try to read it
CLI_CONFIG="$HOME/.ssh/cli_config"
if [ ! -f "$CLI_CONFIG" ]; then
    {
        echo "Host *"
        for key in "$HOME/.ssh/id_"*_cli; do
            [ -f "$key" ] || continue
            echo "    IdentityFile $key"
        done
        echo "    StrictHostKeyChecking no"
    } > "$CLI_CONFIG"
    chmod 600 "$CLI_CONFIG"
    added "~/.ssh/cli_config (use: ssh -F ~/.ssh/cli_config)"
else
    skip "~/.ssh/cli_config"
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

echo ""
echo -e "${GREEN}done${NC} - restart containers to apply: docker compose up -d"
