import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent


def _fixture(
    tmp_path: Path,
    docker_config_ok: bool = True,
    nginx_running: bool = False,
    nginx_config_ok: bool = True,
    credentials: bool = False,
) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "CTFd"
    conf = root / "conf" / "nginx"
    conf.mkdir(parents=True)
    compose = root / "docker-compose.yml"
    compose.write_text(
        """services:
  ctfd:
    build: .
    restart: always
    ports:
      - "8000:8000"
    environment:
      - UPLOAD_FOLDER=/var/uploads
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
      - DATABASE_URL=mysql+pymysql://ctfd:ctfd@db/ctfd
      - REDIS_URL=redis://cache:6379
      - WORKERS=5
      - LOG_FOLDER=/var/log/CTFd
      - ACCESS_LOG=-
      - ERROR_LOG=-
      - REVERSE_PROXY=true
    volumes:
      - .data/CTFd/logs:/var/log/CTFd
      - .data/CTFd/uploads:/var/uploads
      - .:/opt/CTFd:ro
    depends_on:
      permissions:
        condition: service_completed_successfully
      db:
        condition: service_started
      cache:
        condition: service_started
    networks:
        default:
        internal:
  permissions:
    image: alpine:3.23
    user: root
    volumes:
      - .data/CTFd/logs:/var/log/CTFd
      - .data/CTFd/uploads:/var/uploads
    command: chown -R 1001:1001 /var/uploads /var/log/CTFd
  nginx:
    image: nginx:stable
    restart: always
    volumes:
      - ./conf/nginx/http.conf:/etc/nginx/nginx.conf
    ports:
      - 80:80
    depends_on:
      - ctfd
  db:
    image: mariadb:10.11
    restart: always
    environment:
      - MARIADB_ROOT_PASSWORD=ctfd
      - MARIADB_USER=ctfd
      - MARIADB_PASSWORD=ctfd
      - MARIADB_DATABASE=ctfd
    volumes:
      - .data/mysql:/var/lib/mysql
    networks:
        internal:
    command: [mysqld, --character-set-server=utf8mb4, --collation-server=utf8mb4_unicode_ci, --wait_timeout=28800, --log-warnings=0]
  cache:
    image: redis:4
    restart: always
    volumes:
    - .data/redis:/data
    networks:
        internal:
networks:
    default:
    internal:
        internal: true
"""
    )
    base_nginx = "server {\n  location / {\n    proxy_pass http://app_servers;\n  }\n}\n"
    http = conf / "http.conf"
    https = conf / "https.conf"
    http.write_text(base_nginx)
    https.write_text(base_nginx)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    config_status = "0" if docker_config_ok else "1"
    nginx_ps = "echo nginx; exit 0" if nginx_running else "exit 0"
    nginx_status = "0" if nginx_config_ok else "1"
    bash = shutil.which("bash")
    assert bash
    docker.write_text(
        f"""#!{bash}
if [[ "$*" == *" config -q" ]]; then exit {config_status}; fi
if [[ "$*" == *" ps --status running --services" ]]; then {nginx_ps}; fi
if [[ "$*" == *" exec -T nginx nginx -t" ]]; then exit {nginx_status}; fi
exit 0
"""
    )
    docker.chmod(0o755)
    docker_socket = tmp_path / "local-docker.sock"
    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_socket.bind(str(docker_socket))
    unix_socket.close()

    env = os.environ.copy()
    env.update(
        {
            "CTFD_ROOT_OVERRIDE": str(root),
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "DOCKER_SOCK_OVERRIDE": str(docker_socket),
        }
    )
    if credentials:
        docker_home = Path(env["HOME"]) / ".docker"
        docker_home.mkdir(parents=True)
        (docker_home / "config.json").write_text('{"auths": {}}')
        docker_home.chmod(0o700)
        ssh_home = Path(env["HOME"]) / ".ssh"
        ssh_home.mkdir()
        (ssh_home / "id_ed25519").write_text("test-private-key")
        ssh_home.chmod(0o700)
    return root, env


def _setup(env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(REPO / "setup.sh")], env=env, check=check, capture_output=True, text=True)


def test_setup_updates_http_and_https_independently_and_is_idempotent(tmp_path):
    root, env = _fixture(tmp_path)
    script = REPO / "setup.sh"
    config_paths = [root / "conf" / "nginx" / name for name in ("http.conf", "https.conf")]
    original_inodes = {path: path.stat().st_ino for path in config_paths}
    for _ in range(2):
        subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True, text=True)

    for path in config_paths:
        content = path.read_text()
        assert content.count("# BEGIN CTFD-REMOTE-DESKTOP MANAGED LOCATIONS v1") == 1
        assert content.count("# END CTFD-REMOTE-DESKTOP MANAGED LOCATIONS") == 1
        assert content.count("location ~ ^/remote-desktop/vnc/") == 1
        assert content.count("location ~ ^/remote-desktop/terminal/") == 1
        assert content.count('proxy_set_header Cookie "";') == 2
        assert content.count('proxy_set_header Authorization "";') == 1
        assert content.count('proxy_set_header Proxy-Authorization "";') == 2
        assert content.count("proxy_set_header Authorization $terminal_authorization;") == 1
        assert content.count("proxy_hide_header Set-Cookie;") == 2
        assert content.count("proxy_set_header Cookie $http_cookie;") == 2
        assert content.count("proxy_set_header Host $http_host;") == 1
        assert content.count("location /remote-desktop/static/fonts/") == 1
        assert content.index("# BEGIN CTFD-REMOTE-DESKTOP") < content.index("location / {")
        assert path.stat().st_ino == original_inodes[path]

    compose = (root / "docker-compose.yml").read_text()
    parsed_compose = yaml.safe_load(compose)
    assert isinstance(parsed_compose, dict)
    assert {"ctfd", "nginx"} <= set(parsed_compose["services"])
    assert "ssh-permissions" not in parsed_compose["services"]
    assert "docker-permissions" not in parsed_compose["services"]
    assert sum(line == "  permissions:" for line in compose.splitlines()) == 1
    assert "/home/ctfd/.ssh" not in compose
    assert "/home/ctfd/.docker" not in compose
    assert "ctfd-ssh:" not in compose
    assert "ctfd-docker:" not in compose
    assert f"{env['DOCKER_SOCK_OVERRIDE']}:/var/run/docker.sock" in compose
    assert "chown -R 1001:1001 /var/uploads /var/log/CTFd" in compose


def test_setup_stages_each_remote_credential_source_only_when_opted_in(tmp_path):
    root, env = _fixture(tmp_path, credentials=True)
    env["CTFD_RD_STAGE_SSH"] = "1"
    env["CTFD_RD_STAGE_DOCKER_CONFIG"] = "1"

    _setup(env)

    compose = (root / "docker-compose.yml").read_text()
    parsed_compose = yaml.safe_load(compose)
    assert {"ssh-permissions", "docker-permissions"} <= set(parsed_compose["services"])
    assert compose.count("~/.ssh:/mnt/host-ssh:ro") == 1
    assert compose.count("ctfd-ssh:/home/ctfd/.ssh:ro") == 1
    assert compose.count("~/.docker:/mnt/host-docker:ro") == 1
    assert compose.count("ctfd-docker:/home/ctfd/.docker:ro") == 1
    assert compose.count("ssh-permissions:") == 2
    assert compose.count("docker-permissions:") == 2
    assert compose.count("  ctfd-ssh:") == 1
    assert compose.count("  ctfd-docker:") == 1


def test_setup_requires_credential_opt_in_on_every_rerun(tmp_path):
    root, env = _fixture(tmp_path, credentials=True)
    env["CTFD_RD_STAGE_SSH"] = "1"
    env["CTFD_RD_STAGE_DOCKER_CONFIG"] = "1"
    _setup(env)

    paths = [
        root / "docker-compose.yml",
        root / "conf" / "nginx" / "http.conf",
        root / "conf" / "nginx" / "https.conf",
    ]
    before = {path: path.read_bytes() for path in paths}
    env.pop("CTFD_RD_STAGE_SSH")
    env.pop("CTFD_RD_STAGE_DOCKER_CONFIG")

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "requires CTFD_RD_STAGE_SSH=1 on every setup run" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_rejects_concurrent_invocation_before_second_process_mutates_files(tmp_path):
    root, env = _fixture(tmp_path)
    hold = tmp_path / "hold-compose-validation"
    ready = tmp_path / "compose-validation-started"
    hold.touch()
    env["SETUP_TEST_HOLD_FILE"] = str(hold)
    env["SETUP_TEST_READY_FILE"] = str(ready)

    docker = Path(env["PATH"].split(":", 1)[0]) / "docker"
    docker.write_text(
        docker.read_text().replace(
            'if [[ "$*" == *" config -q" ]]; then exit 0; fi',
            """if [[ "$*" == *" config -q" ]]; then
    if [[ -n "${SETUP_TEST_HOLD_FILE:-}" ]]; then
        : > "$SETUP_TEST_READY_FILE"
        while [[ -e "$SETUP_TEST_HOLD_FILE" ]]; do sleep 0.01; done
    fi
    exit 0
fi""",
        )
    )

    first = subprocess.Popen(
        ["bash", str(REPO / "setup.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), first.communicate(timeout=1)

        paths = [
            root / "docker-compose.yml",
            root / "conf" / "nginx" / "http.conf",
            root / "conf" / "nginx" / "https.conf",
        ]
        while_first_is_blocked = {path: path.read_bytes() for path in paths}
        second = _setup(env, check=False)

        assert second.returncode != 0
        assert "another remote desktop setup is already running" in second.stderr
        assert {path: path.read_bytes() for path in paths} == while_first_is_blocked
    finally:
        hold.unlink(missing_ok=True)

    stdout, stderr = first.communicate(timeout=10)
    assert first.returncode == 0, (stdout, stderr)


def test_setup_replaces_noncanonical_owned_initializers(tmp_path):
    root, env = _fixture(tmp_path, credentials=True)
    env["CTFD_RD_STAGE_SSH"] = "1"
    env["CTFD_RD_STAGE_DOCKER_CONFIG"] = "1"
    compose_path = root / "docker-compose.yml"
    compose_path.write_text(
        compose_path.read_text().replace(
            "  nginx:\n",
            """  ssh-permissions:
    image: alpine:old
    privileged: true
    command: cp -a /mnt/host-ssh/. /mnt/ctfd-ssh/
  docker-permissions:
    image: alpine:old
    privileged: true
    command: cp -a /mnt/host-docker/. /mnt/ctfd-docker/
  nginx:
""",
        )
    )

    subprocess.run(["bash", str(REPO / "setup.sh")], env=env, check=True, capture_output=True, text=True)
    once = compose_path.read_bytes()
    subprocess.run(["bash", str(REPO / "setup.sh")], env=env, check=True, capture_output=True, text=True)
    twice = compose_path.read_bytes()

    compose = twice.decode()
    assert twice == once
    assert "alpine:old" not in compose
    assert "privileged: true" not in compose
    assert compose.count("stage=/mnt/ctfd-ssh/.rd-next") == 1
    assert compose.count("stage=/mnt/ctfd-docker/.rd-next") == 1
    assert compose.count("trap rollback HUP INT TERM EXIT") == 2
    assert compose.count("ctfd-docker:/home/ctfd/.docker:ro") == 1


def test_setup_rolls_back_all_configs_when_preflight_fails(tmp_path):
    root, env = _fixture(tmp_path, docker_config_ok=False)
    paths = [
        root / "docker-compose.yml",
        root / "conf" / "nginx" / "http.conf",
        root / "conf" / "nginx" / "https.conf",
    ]
    before = {path: path.read_bytes() for path in paths}

    result = subprocess.run(["bash", str(REPO / "setup.sh")], env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert "restoring configuration files" in result.stderr
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_refuses_explicit_missing_host_credentials_without_mutating_files(tmp_path):
    root, env = _fixture(tmp_path)
    env["CTFD_RD_STAGE_DOCKER_CONFIG"] = "1"
    paths = [
        root / "docker-compose.yml",
        root / "conf" / "nginx" / "http.conf",
        root / "conf" / "nginx" / "https.conf",
    ]
    before = {path: path.read_bytes() for path in paths}
    result = subprocess.run(["bash", str(REPO / "setup.sh")], env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert "CTFD_RD_STAGE_DOCKER_CONFIG=1 requires" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_rejects_invalid_credential_staging_flag_without_mutation(tmp_path):
    root, env = _fixture(tmp_path)
    env["CTFD_RD_STAGE_SSH"] = "yes"
    paths = [
        root / "docker-compose.yml",
        root / "conf" / "nginx" / "http.conf",
        root / "conf" / "nginx" / "https.conf",
    ]
    before = {path: path.read_bytes() for path in paths}

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "credential staging flags must be 0 or 1" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_requires_existing_local_unix_socket_without_mutation(tmp_path):
    root, env = _fixture(tmp_path)
    Path(env["DOCKER_SOCK_OVERRIDE"]).unlink()
    paths = [
        root / "docker-compose.yml",
        root / "conf" / "nginx" / "http.conf",
        root / "conf" / "nginx" / "https.conf",
    ]
    before = {path: path.read_bytes() for path in paths}

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "Docker socket not found" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_refuses_unmarked_nginx_blocks_without_mutation(tmp_path):
    root, env = _fixture(tmp_path)
    _setup(env)

    configs = [root / "conf" / "nginx" / name for name in ("http.conf", "https.conf")]
    for path in configs:
        unmarked = (
            path.read_text()
            .replace("    # BEGIN CTFD-REMOTE-DESKTOP MANAGED LOCATIONS v1\n", "")
            .replace("    # END CTFD-REMOTE-DESKTOP MANAGED LOCATIONS\n", "")
        )
        path.write_text(unmarked)

    paths = [root / "docker-compose.yml", *configs]
    before = {path: path.read_bytes() for path in paths}

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "unmanaged remote-desktop nginx configuration" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_refuses_old_managed_nginx_version_without_mutation(tmp_path):
    root, env = _fixture(tmp_path)
    _setup(env)

    http = root / "conf" / "nginx" / "http.conf"
    # v10 starts with the v1 text so a prefix match would wrongly accept it
    http.write_text(http.read_text().replace("LOCATIONS v1", "LOCATIONS v10", 1))
    paths = [root / "docker-compose.yml", http, root / "conf" / "nginx" / "https.conf"]
    before = {path: path.read_bytes() for path in paths}

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "fresh setup only accepts v1" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_refuses_modified_unmarked_nginx_block_without_mutation(tmp_path):
    root, env = _fixture(tmp_path)
    _setup(env)

    http = root / "conf" / "nginx" / "http.conf"
    modified = (
        http.read_text()
        .replace("    # BEGIN CTFD-REMOTE-DESKTOP MANAGED LOCATIONS v1\n", "")
        .replace("    # END CTFD-REMOTE-DESKTOP MANAGED LOCATIONS\n", "")
    )
    modified = modified.replace("proxy_read_timeout 86400s;", "proxy_read_timeout 123s;", 1)
    http.write_text(modified)
    paths = [root / "docker-compose.yml", http, root / "conf" / "nginx" / "https.conf"]
    before = {path: path.read_bytes() for path in paths}

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "unmanaged remote-desktop nginx configuration" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_refuses_ambiguous_nginx_anchor_without_mutation(tmp_path):
    root, env = _fixture(tmp_path)
    http = root / "conf" / "nginx" / "http.conf"
    http.write_text(http.read_text() + "server {\n  location / { return 404; }\n}\n")
    paths = [root / "docker-compose.yml", http, root / "conf" / "nginx" / "https.conf"]
    before = {path: path.read_bytes() for path in paths}

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "expected exactly one catch-all nginx location" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_repairs_managed_docker_gid_and_scopes_named_volume_detection(tmp_path):
    root, env = _fixture(tmp_path)
    docker_socket = tmp_path / "docker.sock"
    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_socket.bind(str(docker_socket))
    env["DOCKER_SOCK_OVERRIDE"] = str(docker_socket)
    docker_gid = docker_socket.stat().st_gid
    stale_gid = docker_gid + 100000
    unmanaged_gid = stale_gid + 1

    compose_path = root / "docker-compose.yml"
    compose_path.write_text(
        compose_path.read_text().replace("  nginx:\n", "  ctfd-ssh:\n    image: busybox:stable\n  nginx:\n", 1)
    )

    try:
        _setup(env)
        installed = compose_path.read_text()
        assert f'- "{docker_gid}" # ctfd-remote-desktop docker socket' in installed
        compose_path.write_text(
            installed.replace(
                f'- "{docker_gid}" # ctfd-remote-desktop docker socket',
                f'- "{stale_gid}" # ctfd-remote-desktop docker socket\n'
                f'      - "{unmanaged_gid}" # externally managed group',
            )
        )
        once = compose_path.read_bytes()
        _setup(env)
        repaired = compose_path.read_bytes()
        _setup(env)
        twice = compose_path.read_bytes()
    finally:
        unix_socket.close()

    compose = twice.decode()
    assert twice == repaired
    assert twice != once
    assert f'- "{docker_gid}" # ctfd-remote-desktop docker socket' in compose
    assert f'- "{stale_gid}"' not in compose
    assert f'- "{unmanaged_gid}" # externally managed group' in compose
    assert compose.count("ctfd-remote-desktop docker socket") == 1
    # credential staging is off so the colliding ctfd-ssh service stays untouched and gains no volume entry
    assert sum(line == "  ctfd-ssh:" for line in compose.splitlines()) == 1


def test_setup_refuses_unmanaged_docker_gid_without_mutation(tmp_path):
    root, env = _fixture(tmp_path)
    docker_socket = tmp_path / "docker.sock"
    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_socket.bind(str(docker_socket))
    env["DOCKER_SOCK_OVERRIDE"] = str(docker_socket)
    docker_gid = docker_socket.stat().st_gid
    compose_path = root / "docker-compose.yml"
    compose_path.write_text(
        compose_path.read_text().replace("    build: .\n", f'    build: .\n    group_add:\n      - "{docker_gid}"\n', 1)
    )
    paths = [compose_path, root / "conf" / "nginx" / "http.conf", root / "conf" / "nginx" / "https.conf"]
    before = {path: path.read_bytes() for path in paths}

    try:
        result = _setup(env, check=False)
    finally:
        unix_socket.close()

    assert result.returncode != 0
    assert "fresh setup will not adopt it" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_refuses_unmanaged_credential_mount_without_mutation(tmp_path):
    root, env = _fixture(tmp_path, credentials=True)
    env["CTFD_RD_STAGE_DOCKER_CONFIG"] = "1"
    compose_path = root / "docker-compose.yml"
    compose_path.write_text(
        compose_path.read_text().replace(
            "      - .:/opt/CTFd:ro\n",
            "      - .:/opt/CTFd:ro\n      - ~/.docker:/home/ctfd/.docker:ro\n",
            1,
        )
    )
    paths = [compose_path, root / "conf" / "nginx" / "http.conf", root / "conf" / "nginx" / "https.conf"]
    before = {path: path.read_bytes() for path in paths}

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "unmanaged /home/ctfd/.docker mount" in result.stdout
    assert {path: path.read_bytes() for path in paths} == before


def test_setup_rolls_back_when_running_nginx_rejects_generated_config(tmp_path):
    root, env = _fixture(tmp_path, nginx_running=True, nginx_config_ok=False)
    paths = [
        root / "docker-compose.yml",
        root / "conf" / "nginx" / "http.conf",
        root / "conf" / "nginx" / "https.conf",
    ]
    before = {path: path.read_bytes() for path in paths}

    result = _setup(env, check=False)

    assert result.returncode != 0
    assert "restoring configuration files" in result.stderr
    assert {path: path.read_bytes() for path in paths} == before
