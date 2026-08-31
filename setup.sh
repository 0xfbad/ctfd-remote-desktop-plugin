#!/usr/bin/env bash
set -euo pipefail

# setup script for ctfd-remote-desktop plugin
# adds required docker-compose volumes, group_add, permissions init service,
# and nginx websocket config

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
CTFD_ROOT="${CTFD_ROOT_OVERRIDE:-$(cd "$PLUGIN_DIR/../../.." && pwd)}"
COMPOSE_FILE="$CTFD_ROOT/docker-compose.yml"
NGINX_CONF="$CTFD_ROOT/conf/nginx/http.conf"
NGINX_HTTPS_CONF="${NGINX_CONF%/*}/https.conf"

# Serialize setup against the CTFd workspace directory itself.  Holding an
# open directory descriptor avoids leaving a lock artifact in the checkout,
# while flock -n makes a concurrent invocation fail before either process
# snapshots or changes shared configuration.
if ! command -v flock >/dev/null 2>&1; then
    echo "error: flock is required to run setup safely" >&2
    exit 1
fi
if ! exec 9<"$CTFD_ROOT"; then
    echo "error: could not open CTFd root for setup locking: $CTFD_ROOT" >&2
    exit 1
fi
if ! flock -n 9; then
    echo "error: another remote desktop setup is already running for $CTFD_ROOT" >&2
    exit 1
fi

SETUP_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ctfd-rd-setup.XXXXXXXX")"
SETUP_OK=0

cleanup() {
    status=$?
    if [ "$status" -ne 0 ] && [ "$SETUP_OK" -eq 0 ]; then
        echo "setup failed; restoring configuration files" >&2
        cp -p "$SETUP_TMP_DIR/docker-compose.yml" "$COMPOSE_FILE" 2>/dev/null || true
        [ ! -f "$SETUP_TMP_DIR/http.conf" ] || cat "$SETUP_TMP_DIR/http.conf" >"$NGINX_CONF" 2>/dev/null || true
        if [ -f "$SETUP_TMP_DIR/https.conf" ]; then
            cat "$SETUP_TMP_DIR/https.conf" >"$NGINX_HTTPS_CONF" 2>/dev/null || true
        fi
    fi
    rm -rf -- "$SETUP_TMP_DIR"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

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

ctfd_service_block() {
    awk '
        /^  ctfd:[[:space:]]*$/ { in_ctfd=1 }
        in_ctfd && /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ && !/^  ctfd:/ { exit }
        in_ctfd { print }
    ' "$COMPOSE_FILE"
}

# Local Docker uses only the daemon socket. Remote context material is copied
# only after an explicit opt-in, and each requested source must already exist;
# this avoids Compose creating empty root-owned credential directories.
STAGE_SSH="${CTFD_RD_STAGE_SSH:-0}"
STAGE_DOCKER_CONFIG="${CTFD_RD_STAGE_DOCKER_CONFIG:-0}"
for credential_flag in "$STAGE_SSH" "$STAGE_DOCKER_CONFIG"; do
    case "$credential_flag" in
        0|1) ;;
        *) err "credential staging flags must be 0 or 1"; exit 1 ;;
    esac
done
if [ "$STAGE_SSH" -eq 0 ] && ctfd_service_block | grep -Eq '/home/ctfd/\.ssh'; then
    err "existing SSH credential staging requires CTFD_RD_STAGE_SSH=1 on every setup run"
    exit 1
fi
if [ "$STAGE_DOCKER_CONFIG" -eq 0 ] && ctfd_service_block | grep -Eq '/home/ctfd/\.docker'; then
    err "existing Docker credential staging requires CTFD_RD_STAGE_DOCKER_CONFIG=1 on every setup run"
    exit 1
fi
if [ "$STAGE_SSH" -eq 1 ] && { [ ! -d "$HOME/.ssh" ] || [ ! -r "$HOME/.ssh" ] || [ ! -x "$HOME/.ssh" ]; }; then
    err "CTFD_RD_STAGE_SSH=1 requires a readable $HOME/.ssh directory"
    exit 1
fi
if [ "$STAGE_DOCKER_CONFIG" -eq 1 ] && { [ ! -d "$HOME/.docker" ] || [ ! -r "$HOME/.docker" ] || [ ! -x "$HOME/.docker" ]; }; then
    err "CTFD_RD_STAGE_DOCKER_CONFIG=1 requires a readable $HOME/.docker directory"
    exit 1
fi

cp -p "$COMPOSE_FILE" "$SETUP_TMP_DIR/docker-compose.yml"
cp -p "$NGINX_CONF" "$SETUP_TMP_DIR/http.conf"
if [ -f "$NGINX_HTTPS_CONF" ]; then
    cp -p "$NGINX_HTTPS_CONF" "$SETUP_TMP_DIR/https.conf"
fi

# docker socket gid
DOCKER_SOCK="${DOCKER_SOCK_OVERRIDE:-/var/run/docker.sock}"
case "$DOCKER_SOCK" in
    /*) ;;
    *) err "Docker socket path must be absolute: $DOCKER_SOCK"; exit 1 ;;
esac
case "$DOCKER_SOCK" in
    *:*|*$'\n'*) err "Docker socket path contains unsupported characters: $DOCKER_SOCK"; exit 1 ;;
esac
if [ ! -S "$DOCKER_SOCK" ]; then
    err "Docker socket not found at $DOCKER_SOCK"
    exit 1
fi
DOCKER_GID=$(stat -c '%g' "$DOCKER_SOCK")
ok "docker socket gid: $DOCKER_GID"

# docker-compose.yml modifications
echo ""
echo "docker-compose.yml"

ctfd_group_add_block() {
    ctfd_service_block | awk '
        /^    group_add:/ { in_group=1 }
        in_group && /^    [a-zA-Z0-9_-]+:/ && !/^    group_add:/ { exit }
        in_group { print }
    '
}

add_ctfd_volume() {
    volume_line=$1
    awk -v addition="$volume_line" '
        /^  ctfd:[[:space:]]*$/ { in_ctfd=1 }
        in_ctfd && /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ && !/^  ctfd:/ { in_ctfd=0 }
        { print }
        in_ctfd && /\/opt\/CTFd/ && !added { print addition; added=1 }
        END { if (!added) exit 42 }
    ' "$COMPOSE_FILE" >"$SETUP_TMP_DIR/docker-compose.next.yml" || {
        err "could not find the /opt/CTFd source mount inside the ctfd service"
        exit 1
    }
    chmod --reference="$COMPOSE_FILE" "$SETUP_TMP_DIR/docker-compose.next.yml"
    mv -f -- "$SETUP_TMP_DIR/docker-compose.next.yml" "$COMPOSE_FILE"
}

ensure_ctfd_dependency() {
    dependency=$1
    if ctfd_service_block | grep -q "^      ${dependency}:"; then
        skip "ctfd depends_on $dependency"
        return
    fi
    if ctfd_service_block | grep -q '^    depends_on:'; then
        # The upstream CTFd file uses the mapping form because it already has
        # service-completion conditions. Refuse a list instead of producing a
        # subtly invalid mixture of sequence and mapping entries.
        first_dependency_line=$(ctfd_service_block | awk '/^    depends_on:/{found=1; next} found && NF {print; exit}')
        case "$first_dependency_line" in
            "      - "*) err "ctfd depends_on uses list form; convert it to mapping form before setup"; exit 1 ;;
        esac
        awk -v dependency="$dependency" '
            /^  ctfd:[[:space:]]*$/ { in_ctfd=1 }
            in_ctfd && /^    depends_on:[[:space:]]*$/ && !added {
                print
                print "      " dependency ":"
                print "        condition: service_completed_successfully"
                added=1
                next
            }
            { print }
            END { if (!added) exit 42 }
        ' "$COMPOSE_FILE" >"$SETUP_TMP_DIR/docker-compose.next.yml" || {
            err "could not update ctfd depends_on"
            exit 1
        }
    else
        awk -v dependency="$dependency" '
            /^  ctfd:[[:space:]]*$/ && !added {
                print
                print "    depends_on:"
                print "      " dependency ":"
                print "        condition: service_completed_successfully"
                added=1
                next
            }
            { print }
            END { if (!added) exit 42 }
        ' "$COMPOSE_FILE" >"$SETUP_TMP_DIR/docker-compose.next.yml" || {
            err "could not add ctfd depends_on"
            exit 1
        }
    fi
    chmod --reference="$COMPOSE_FILE" "$SETUP_TMP_DIR/docker-compose.next.yml"
    mv -f -- "$SETUP_TMP_DIR/docker-compose.next.yml" "$COMPOSE_FILE"
    added "ctfd depends_on $dependency"
}

# These names are owned by this plugin. Replace the complete service block on
# every run so a current installation is deterministic and idempotent.
install_owned_service() {
    service_name=$1
    service_block_file=$2
    awk -v service_line="  ${service_name}:" -v block="$service_block_file" '
        function emit_block(  line) {
            while ((getline line < block) > 0) print line
            close(block)
            emitted=1
        }
        $0 == service_line {
            if (!emitted) emit_block()
            replacing=1
            found=1
            next
        }
        replacing && (/^  [a-zA-Z0-9_-]+:[[:space:]]*$/ || /^[^[:space:]#]/) {
            replacing=0
        }
        replacing { next }
        /^  nginx:[[:space:]]*$/ && !emitted {
            emit_block()
        }
        { print }
        END { if (!emitted) exit 42 }
    ' "$COMPOSE_FILE" >"$SETUP_TMP_DIR/docker-compose.next.yml" || {
        err "could not install the canonical $service_name service"
        exit 1
    }
    chmod --reference="$COMPOSE_FILE" "$SETUP_TMP_DIR/docker-compose.next.yml"
    mv -f -- "$SETUP_TMP_DIR/docker-compose.next.yml" "$COMPOSE_FILE"
}

# group_add
if [ -n "$DOCKER_GID" ]; then
    docker_group_marker='ctfd-remote-desktop docker socket'
    marker_count=$(ctfd_group_add_block | grep -c "$docker_group_marker" || true)
    if [ "$marker_count" -gt 1 ]; then
        err "ctfd group_add contains multiple plugin-managed Docker socket entries"
        exit 1
    elif [ "$marker_count" -eq 1 ]; then
        awk -v docker_gid="$DOCKER_GID" -v marker="$docker_group_marker" '
            /^  ctfd:[[:space:]]*$/ { in_ctfd=1 }
            in_ctfd && /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ && !/^  ctfd:/ { in_ctfd=0 }
            in_ctfd && /^    group_add:/ { in_group=1 }
            in_group && /^    [a-zA-Z0-9_-]+:/ && !/^    group_add:/ { in_group=0 }
            in_group && /^      - / && index($0, marker) {
                print "      - \"" docker_gid "\" # " marker
                updated=1
                next
            }
            { print }
            END { if (!updated) exit 42 }
        ' "$COMPOSE_FILE" >"$SETUP_TMP_DIR/docker-compose.next.yml" || {
            err "could not update the plugin-managed Docker socket group"
            exit 1
        }
        chmod --reference="$COMPOSE_FILE" "$SETUP_TMP_DIR/docker-compose.next.yml"
        mv -f -- "$SETUP_TMP_DIR/docker-compose.next.yml" "$COMPOSE_FILE"
        ok "docker socket group: $DOCKER_GID"
    elif ctfd_group_add_block | grep -Eq '^    group_add:[[:space:]]*\['; then
        err "ctfd group_add uses inline-list form; convert it to block-list form before setup"
        exit 1
    elif ctfd_group_add_block | grep -Eq "^      - [\"']?${DOCKER_GID}[\"']?[[:space:]]*$"; then
        err "ctfd group_add already contains Docker GID $DOCKER_GID without the plugin marker; fresh setup will not adopt it"
        exit 1
    elif ctfd_group_add_block | grep -q '^    group_add:[[:space:]]*$'; then
        awk -v docker_gid="$DOCKER_GID" -v marker="$docker_group_marker" '
            /^  ctfd:[[:space:]]*$/ { in_ctfd=1 }
            in_ctfd && /^    group_add:[[:space:]]*$/ && !added {
                print
                print "      - \"" docker_gid "\" # " marker
                added=1
                next
            }
            { print }
            END { if (!added) exit 42 }
        ' "$COMPOSE_FILE" >"$SETUP_TMP_DIR/docker-compose.next.yml" || {
            err "could not add the Docker socket group to ctfd group_add"
            exit 1
        }
        chmod --reference="$COMPOSE_FILE" "$SETUP_TMP_DIR/docker-compose.next.yml"
        mv -f -- "$SETUP_TMP_DIR/docker-compose.next.yml" "$COMPOSE_FILE"
        added "docker socket group: $DOCKER_GID"
    else
        awk -v docker_gid="$DOCKER_GID" '
            /^  ctfd:[[:space:]]*$/ { in_ctfd=1 }
            in_ctfd && /^    build:[[:space:]]/ && !added {
                print
                print "    group_add:"
                print "      - \"" docker_gid "\" # ctfd-remote-desktop docker socket"
                added=1
                next
            }
            { print }
            END { if (!added) exit 42 }
        ' "$COMPOSE_FILE" >"$SETUP_TMP_DIR/docker-compose.next.yml" || {
            err "could not find the ctfd build field for group_add insertion"
            exit 1
        }
        chmod --reference="$COMPOSE_FILE" "$SETUP_TMP_DIR/docker-compose.next.yml"
        mv -f -- "$SETUP_TMP_DIR/docker-compose.next.yml" "$COMPOSE_FILE"
        added "group_add: $DOCKER_GID"
    fi
fi

# Runtime mounts go after the CTFd source mount.
if ctfd_service_block | grep -q ':/var/run/docker.sock'; then
    skip "docker socket volume"
else
    add_ctfd_volume "      - ${DOCKER_SOCK}:/var/run/docker.sock"
    added "docker socket volume"
fi

if [ "$STAGE_SSH" -eq 1 ]; then
    if ctfd_service_block | grep -q 'ctfd-ssh:/home/ctfd/.ssh:ro'; then
        skip "ssh volume"
    elif ctfd_service_block | grep -q '/home/ctfd/.ssh'; then
        err "ctfd already has an unmanaged /home/ctfd/.ssh mount; fresh setup will not replace it"
        exit 1
    else
        add_ctfd_volume '      - ctfd-ssh:/home/ctfd/.ssh:ro'
        added "ssh named volume"
    fi
fi

if [ "$STAGE_DOCKER_CONFIG" -eq 1 ]; then
    if ctfd_service_block | grep -q 'ctfd-docker:/home/ctfd/.docker:ro'; then
        skip "docker config volume"
    elif ctfd_service_block | grep -q '/home/ctfd/.docker'; then
        err "ctfd already has an unmanaged /home/ctfd/.docker mount; fresh setup will not replace it"
        exit 1
    else
        add_ctfd_volume '      - ctfd-docker:/home/ctfd/.docker:ro'
        added "docker config named volume"
    fi
fi

if [ "$STAGE_SSH" -eq 1 ] || [ "$STAGE_DOCKER_CONFIG" -eq 1 ]; then
    echo ""
    echo "credential init services"
fi

if [ "$STAGE_SSH" -eq 1 ]; then
    # Do not reuse CTFd's generic `permissions` service: upstream owns it for
    # uploads/logs. Copy without weakening the host credential modes.
    cat >"$SETUP_TMP_DIR/ssh-permissions.yml" <<'COMPOSESERVICE'
  ssh-permissions:
    image: alpine:3.23
    user: root
    volumes:
      - ~/.ssh:/mnt/host-ssh:ro
      - ctfd-ssh:/mnt/ctfd-ssh
    command: >
      sh -c '
        set -eu;
        stage=/mnt/ctfd-ssh/.rd-next; backup=/mnt/ctfd-ssh/.rd-previous;
        rm -rf -- "$stage" "$backup"; mkdir -p "$stage" "$backup";
        cp -a /mnt/host-ssh/. "$stage"/; chown -R 1001:1001 "$stage";
        rollback() { find /mnt/ctfd-ssh -mindepth 1 -maxdepth 1 ! -name .rd-next ! -name .rd-previous \
          -exec rm -rf -- {} +; find "$backup" -mindepth 1 -maxdepth 1 \
          -exec sh -c '\''for entry do mv -- "$entry" "$0"/; done'\'' /mnt/ctfd-ssh {} +; };
        trap rollback HUP INT TERM EXIT;
        find /mnt/ctfd-ssh -mindepth 1 -maxdepth 1 ! -name .rd-next ! -name .rd-previous \
          -exec sh -c '\''for entry do mv -- "$entry" "$0"/; done'\'' "$backup" {} +;
        find "$stage" -mindepth 1 -maxdepth 1 \
          -exec sh -c '\''for entry do mv -- "$entry" "$0"/; done'\'' /mnt/ctfd-ssh {} +;
        trap - HUP INT TERM EXIT; rm -rf -- "$stage" "$backup"
      '
COMPOSESERVICE
    install_owned_service ssh-permissions "$SETUP_TMP_DIR/ssh-permissions.yml"
    added "canonical ssh permissions service"
    ensure_ctfd_dependency ssh-permissions
fi

# Docker context metadata, TLS material, and registry config can contain 0600
# files. Copy the complete directory as root, then transfer ownership inside a
# dedicated named volume; never loosen traversal or file modes on the host.
if [ "$STAGE_DOCKER_CONFIG" -eq 1 ]; then
    cat >"$SETUP_TMP_DIR/docker-permissions.yml" <<'COMPOSESERVICE'
  docker-permissions:
    image: alpine:3.23
    user: root
    volumes:
      - ~/.docker:/mnt/host-docker:ro
      - ctfd-docker:/mnt/ctfd-docker
    command: >
      sh -c '
        set -eu;
        stage=/mnt/ctfd-docker/.rd-next; backup=/mnt/ctfd-docker/.rd-previous;
        rm -rf -- "$stage" "$backup"; mkdir -p "$stage" "$backup";
        cp -a /mnt/host-docker/. "$stage"/; chown -R 1001:1001 "$stage";
        rollback() { find /mnt/ctfd-docker -mindepth 1 -maxdepth 1 ! -name .rd-next ! -name .rd-previous \
          -exec rm -rf -- {} +; find "$backup" -mindepth 1 -maxdepth 1 \
          -exec sh -c '\''for entry do mv -- "$entry" "$0"/; done'\'' /mnt/ctfd-docker {} +; };
        trap rollback HUP INT TERM EXIT;
        find /mnt/ctfd-docker -mindepth 1 -maxdepth 1 ! -name .rd-next ! -name .rd-previous \
          -exec sh -c '\''for entry do mv -- "$entry" "$0"/; done'\'' "$backup" {} +;
        find "$stage" -mindepth 1 -maxdepth 1 \
          -exec sh -c '\''for entry do mv -- "$entry" "$0"/; done'\'' /mnt/ctfd-docker {} +;
        trap - HUP INT TERM EXIT; rm -rf -- "$stage" "$backup"
      '
COMPOSESERVICE
    install_owned_service docker-permissions "$SETUP_TMP_DIR/docker-permissions.yml"
    added "canonical docker permissions service"
    ensure_ctfd_dependency docker-permissions
fi

top_level_volume_exists() {
    volume_name=$1
    awk -v volume_name="$volume_name" '
        /^volumes:[[:space:]]*$/ { in_volumes=1; next }
        in_volumes && /^[^[:space:]#]/ { in_volumes=0 }
        in_volumes && $0 ~ "^  " volume_name ":[[:space:]]*" { found=1 }
        END { exit(found ? 0 : 1) }
    ' "$COMPOSE_FILE"
}

if [ "$STAGE_SSH" -eq 1 ] || [ "$STAGE_DOCKER_CONFIG" -eq 1 ]; then
    if grep -q '^volumes:' "$COMPOSE_FILE" && ! grep -q '^volumes:[[:space:]]*$' "$COMPOSE_FILE"; then
        err "top-level volumes uses inline form; fresh setup cannot add credential volumes"
        exit 1
    elif ! grep -q '^volumes:[[:space:]]*$' "$COMPOSE_FILE"; then
        printf '\nvolumes:\n' >>"$COMPOSE_FILE"
    fi
    if [ "$STAGE_SSH" -eq 1 ]; then
        if top_level_volume_exists ctfd-ssh; then
            skip "ctfd-ssh named volume"
        else
            sed -i '/^volumes:[[:space:]]*$/a\  ctfd-ssh:' "$COMPOSE_FILE"
            added "ctfd-ssh named volume"
        fi
    fi
    if [ "$STAGE_DOCKER_CONFIG" -eq 1 ]; then
        if top_level_volume_exists ctfd-docker; then
            skip "ctfd-docker named volume"
        else
            sed -i '/^volumes:[[:space:]]*$/a\  ctfd-docker:' "$COMPOSE_FILE"
            added "ctfd-docker named volume"
        fi
    fi
fi

# nginx config
echo ""
echo "nginx config"

NGINX_MANAGED_BEGIN="# BEGIN CTFD-REMOTE-DESKTOP MANAGED LOCATIONS v1"
NGINX_MANAGED_PREFIX="# BEGIN CTFD-REMOTE-DESKTOP MANAGED LOCATIONS v"
NGINX_MANAGED_END="# END CTFD-REMOTE-DESKTOP MANAGED LOCATIONS"

cat > "$SETUP_TMP_DIR/nginx-managed.conf" << 'NGINXBLOCK'
    # BEGIN CTFD-REMOTE-DESKTOP MANAGED LOCATIONS v1
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

    # serve nerd font with correct mime type
    location /remote-desktop/static/fonts/ {
      proxy_pass http://app_servers;
      proxy_hide_header Content-Type;
      add_header Content-Type "font/woff2";
      add_header Cache-Control "public, max-age=31536000";
      add_header Access-Control-Allow-Origin "*";
    }
    # END CTFD-REMOTE-DESKTOP MANAGED LOCATIONS
NGINXBLOCK

fixed_count() {
    needle=$1
    file=$2
    grep -F -c -- "$needle" "$file" || true
}

exact_marker_count() {
    needle=$1
    file=$2
    awk -v needle="$needle" '
        {
            line=$0
            sub(/^[[:space:]]*/, "", line)
            sub(/[[:space:]]*$/, "", line)
            if (line == needle) count++
        }
        END { print count + 0 }
    ' "$file"
}

validate_managed_nginx_config() {
    candidate=$1
    [ "$(fixed_count "$NGINX_MANAGED_BEGIN" "$candidate")" -eq 1 ] &&
        [ "$(fixed_count "$NGINX_MANAGED_END" "$candidate")" -eq 1 ] &&
        [ "$(fixed_count 'location ~ ^/remote-desktop/vnc/' "$candidate")" -eq 1 ] &&
        [ "$(fixed_count 'location = /remote-desktop/vnc/auth' "$candidate")" -eq 1 ] &&
        [ "$(fixed_count 'location ~ ^/remote-desktop/terminal/' "$candidate")" -eq 1 ] &&
        [ "$(fixed_count 'location = /remote-desktop/terminal/auth' "$candidate")" -eq 1 ] &&
        [ "$(fixed_count 'location /remote-desktop/static/fonts/' "$candidate")" -eq 1 ]
}

prepare_nginx_config() {
    conf=$1
    label=$(basename "$conf")
    candidate="$SETUP_TMP_DIR/${label}.next"
    begin_count=$(fixed_count "$NGINX_MANAGED_PREFIX" "$conf")
    current_begin_count=$(exact_marker_count "$NGINX_MANAGED_BEGIN" "$conf")
    end_count=$(fixed_count "$NGINX_MANAGED_END" "$conf")

    if [ "$begin_count" -ne 0 ] || [ "$end_count" -ne 0 ]; then
        if [ "$begin_count" -ne 1 ] || [ "$current_begin_count" -ne 1 ] || [ "$end_count" -ne 1 ]; then
            err "unsupported or ambiguous managed nginx region in $label; fresh setup only accepts v1"
            return 1
        fi
        awk -v begin="$NGINX_MANAGED_BEGIN" -v end="$NGINX_MANAGED_END" \
            -v block="$SETUP_TMP_DIR/nginx-managed.conf" '
            index($0, begin) {
                while ((getline line < block) > 0) print line
                close(block)
                skipping=1
                replaced=1
                next
            }
            skipping && index($0, end) { skipping=0; next }
            !skipping { print }
            END { if (skipping || !replaced) exit 42 }
        ' "$conf" >"$candidate" || {
            err "could not replace managed nginx region in $label"
            return 1
        }
    else
        if grep -Eq 'remote-desktop/(vnc|terminal|static/fonts)' "$conf"; then
            err "unmanaged remote-desktop nginx configuration in $label; fresh setup will not replace it"
            return 1
        fi

        anchor_count=$(grep -Ec '^[[:space:]]*location /[[:space:]]*\{' "$conf" || true)
        if [ "$anchor_count" -ne 1 ]; then
            err "expected exactly one catch-all nginx location in $label, found $anchor_count"
            return 1
        fi
        awk -v block="$SETUP_TMP_DIR/nginx-managed.conf" '
            /^[[:space:]]*location \/[[:space:]]*\{/ && !inserted {
                while ((getline line < block) > 0) print line
                close(block)
                inserted=1
            }
            { print }
            END { if (!inserted) exit 42 }
        ' "$conf" >"$candidate" || {
            err "could not insert managed nginx region in $label"
            return 1
        }
    fi

    if ! validate_managed_nginx_config "$candidate"; then
        err "generated nginx configuration failed managed-region validation: $label"
        return 1
    fi
    # Preserve the bind-mounted inode and its SELinux/xattr metadata so a
    # running Nginx container validates the candidate we just generated.
    cat "$candidate" >"$conf"
    added "canonical managed nginx region in $label"
}

for conf in "$NGINX_CONF" "$NGINX_HTTPS_CONF"; do
    [ -f "$conf" ] || continue
    prepare_nginx_config "$conf"
done

docker compose -f "$COMPOSE_FILE" config -q
ok "docker compose configuration validates"

if docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null | grep -qx nginx; then
    docker compose -f "$COMPOSE_FILE" exec -T nginx nginx -t
    ok "running nginx configuration validates"
else
    echo -e "  ${YELLOW}warn${NC}  nginx is not running; run 'docker compose exec -T nginx nginx -t' after startup"
fi

SETUP_OK=1

echo ""
echo -e "${GREEN}done${NC} - restart containers to apply: docker compose up -d"
