import importlib.util
import sys
import types
from unittest.mock import MagicMock
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def aggressive_thread_switching():
    original = sys.getswitchinterval()
    sys.setswitchinterval(0.000001)
    yield
    sys.setswitchinterval(original)


# stub out all external dependencies before any plugin code is imported

_ctfd_models = types.ModuleType("CTFd.models")
_ctfd_models.db = MagicMock()
_ctfd_models.Users = MagicMock()

_stub_modules = [
    "CTFd",
    "CTFd.plugins",
    "CTFd.plugins.challenges",
    "CTFd.utils",
    "CTFd.utils.decorators",
    "CTFd.utils.user",
    "flask",
    "docker",
    "docker.errors",
    "paramiko",
    "paramiko.ssh_exception",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.background",
    "gevent",
    "gevent.monkey",
]

for mod_name in _stub_modules:
    sys.modules[mod_name] = types.ModuleType(mod_name)

sys.modules["CTFd.models"] = _ctfd_models

# flask stubs
_flask = sys.modules["flask"]
for attr in ("Blueprint", "request", "jsonify", "render_template", "Response", "stream_with_context", "current_app"):
    setattr(_flask, attr, MagicMock())

# CTFd decorator stubs
_decorators = sys.modules["CTFd.utils.decorators"]
_decorators.authed_only = lambda f: f
_decorators.admins_only = lambda f: f

_user_utils = sys.modules["CTFd.utils.user"]
_user_utils.get_current_user = MagicMock()

_plugins = sys.modules["CTFd.plugins"]
_plugins.bypass_csrf_protection = lambda f: f
_plugins.register_user_page_menu_bar = MagicMock()
_plugins.register_admin_plugin_menu_bar = MagicMock()

# docker stubs
_docker = sys.modules["docker"]
_docker.from_env = MagicMock()
_docker.DockerClient = MagicMock()
_docker_errors = sys.modules["docker.errors"]
_docker_errors.DockerException = type("DockerException", (Exception,), {})
_docker_errors.NotFound = type("NotFound", (Exception,), {})
_docker_errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
_docker.errors = _docker_errors

# paramiko stubs
_paramiko = sys.modules["paramiko"]
_paramiko_ssh = sys.modules["paramiko.ssh_exception"]
_paramiko_ssh.SSHException = type("SSHException", (Exception,), {})
_paramiko.ssh_exception = _paramiko_ssh

# apscheduler stubs
_apscheduler_sched = sys.modules["apscheduler.schedulers"]
_apscheduler_sched.SchedulerNotRunningError = type("SchedulerNotRunningError", (Exception,), {})
_apscheduler_bg = sys.modules["apscheduler.schedulers.background"]
_apscheduler_bg.BackgroundScheduler = MagicMock()

# gevent stubs
sys.modules["gevent.monkey"].get_original = lambda mod, attr: __import__(mod).__dict__[attr]
sys.modules["gevent"].spawn = MagicMock()

# register repo root as a package named "plugin" so relative imports resolve
repo_root = Path(__file__).resolve().parent.parent
repo_root_str = str(repo_root)

# DO NOT add repo_root to sys.path, the root __init__.py has relative imports
# that confuse pytest's collector, so load modules explicitly via importlib

# the plugin uses relative imports (from .event_logger import ...) which need
# a parent package, create one and load modules in dependency order
PKG = "_rd_plugin"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(repo_root / "src")]
pkg.__package__ = PKG
pkg.__file__ = str(repo_root / "__init__.py")
sys.modules[PKG] = pkg


def _load_module(name):
    full = f"{PKG}.{name}"
    filepath = repo_root / "src" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(full, filepath)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    sys.modules[full] = mod
    sys.modules[name] = mod
    setattr(pkg, name, mod)
    spec.loader.exec_module(mod)
    return mod


# load in dependency order (leaf modules first)
_load_module("models")
_load_module("event_logger")
_load_module("docker_host_manager")
_load_module("orchestrator")
_load_module("container_manager")
_load_module("routes")

# pytest tries to import __init__.py from the rootdir as a module named
# "__init__", pre-register a stub so it doesn't execute the real one
# (which has relative imports that fail outside CTFd)
sys.modules["__init__"] = types.ModuleType("__init__")
