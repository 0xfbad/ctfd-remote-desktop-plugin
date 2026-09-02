import html as _html
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
    "apscheduler.schedulers.gevent",
    "gevent",
    "gevent.monkey",
    "gevent.threadpool",
    "markupsafe",
    "sqlalchemy",
    "sqlalchemy.exc",
    "sqlalchemy.orm",
    "sqlalchemy.orm.exc",
    "docker.types",
]

for mod_name in _stub_modules:
    sys.modules[mod_name] = types.ModuleType(mod_name)

sys.modules["CTFd.models"] = _ctfd_models

_markupsafe = sys.modules["markupsafe"]
_markupsafe.escape = lambda s: _html.escape(str(s), quote=True)

_flask = sys.modules["flask"]
for attr in (
    "Flask",
    "Blueprint",
    "request",
    "jsonify",
    "render_template",
    "Response",
    "stream_with_context",
    "current_app",
):
    setattr(_flask, attr, MagicMock())

_decorators = sys.modules["CTFd.utils.decorators"]
_decorators.authed_only = lambda f: f
_decorators.admins_only = lambda f: f
_decorators.ratelimit = lambda **_kw: lambda f: f

_user_utils = sys.modules["CTFd.utils.user"]
_user_utils.get_current_user = MagicMock()
_user_utils.is_admin = MagicMock(return_value=False)
_user_utils.is_verified = MagicMock(return_value=True)
_user_utils.get_ip = MagicMock(return_value="127.0.0.1")

_plugins = sys.modules["CTFd.plugins"]
_plugins.register_user_page_menu_bar = MagicMock()
_plugins.register_admin_plugin_menu_bar = MagicMock()

_docker = sys.modules["docker"]
_docker.from_env = MagicMock()
_docker.DockerClient = MagicMock()
_docker_errors = sys.modules["docker.errors"]
_docker_errors.DockerException = type("DockerException", (Exception,), {})
_docker_errors.APIError = type("APIError", (_docker_errors.DockerException,), {})
_docker_errors.NotFound = type("NotFound", (Exception,), {})
_docker_errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
_docker.errors = _docker_errors


class _StubUlimit:
    def __init__(self, name=None, soft=None, hard=None):
        self.name = name
        self.soft = soft
        self.hard = hard

    def __eq__(self, other):
        return isinstance(other, _StubUlimit) and (self.name, self.soft, self.hard) == (
            other.name,
            other.soft,
            other.hard,
        )

    def __repr__(self):
        return f"Ulimit(name={self.name!r}, soft={self.soft}, hard={self.hard})"


_docker_types = sys.modules["docker.types"]
_docker_types.Ulimit = _StubUlimit


class _StubMount(dict):
    def __init__(self, target, source, type="volume", read_only=False, **_kwargs):
        super().__init__(
            Target=target,
            Source=source,
            Type=type,
            ReadOnly=read_only,
        )


_docker_types.Mount = _StubMount
_docker.types = _docker_types

_sqlalchemy_orm_exc = sys.modules["sqlalchemy.orm.exc"]
_sqlalchemy_orm_exc.ObjectDeletedError = type("ObjectDeletedError", (Exception,), {})
sys.modules["sqlalchemy.orm"].exc = _sqlalchemy_orm_exc
sys.modules["sqlalchemy"].orm = sys.modules["sqlalchemy.orm"]
_sqlalchemy_exc = sys.modules["sqlalchemy.exc"]
_sqlalchemy_exc.IntegrityError = type("IntegrityError", (Exception,), {})
_sqlalchemy_exc.OperationalError = type("OperationalError", (Exception,), {})
sys.modules["sqlalchemy"].exc = _sqlalchemy_exc
sys.modules["sqlalchemy"].text = lambda statement: statement

_paramiko = sys.modules["paramiko"]
_paramiko_ssh = sys.modules["paramiko.ssh_exception"]
_paramiko_ssh.SSHException = type("SSHException", (Exception,), {})
_paramiko.ssh_exception = _paramiko_ssh

_apscheduler_sched = sys.modules["apscheduler.schedulers"]
_apscheduler_sched.SchedulerNotRunningError = type("SchedulerNotRunningError", (Exception,), {})
_apscheduler_gevent = sys.modules["apscheduler.schedulers.gevent"]
_apscheduler_gevent.GeventScheduler = MagicMock()

sys.modules["gevent.monkey"].get_original = lambda mod, attr: __import__(mod).__dict__[attr]
sys.modules["gevent"].spawn = MagicMock()

_gevent = sys.modules["gevent"]
_gevent_threadpool = sys.modules["gevent.threadpool"]
_gevent_monkey = sys.modules["gevent.monkey"]


class _StubThreadPool:
    def __init__(self, maxsize=None):
        pass

    # runs the callable synchronously so tests exercise the wrapped paths without a real hub
    def apply(self, fn, args=None, kwds=None):
        return fn(*(args or ()), **(kwds or {}))


_gevent_threadpool.ThreadPool = _StubThreadPool
_gevent_monkey.is_module_patched = lambda name: True
_gevent.threadpool = _gevent_threadpool
_gevent.monkey = _gevent_monkey

repo_root = Path(__file__).resolve().parent.parent

# plugin modules use relative imports so they need a parent package to load under
PKG = "_rd_plugin"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(repo_root / "src")]
pkg.__package__ = PKG
pkg.__file__ = str(repo_root / "__init__.py")
sys.modules[PKG] = pkg


# never add repo_root to sys.path, relative imports in the root __init__.py break the pytest collector
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


# load order matters, leaf modules first
_load_module("settings")
_load_module("models")
_load_module("event_logger")
_load_module("event_bus")
_load_module("exceptions")
_load_module("docker_host_manager")
_load_module("orchestrator")
_load_module("container_manager")
_load_module("routes")

# pytest imports the rootdir __init__.py as a module named __init__, stub it so the real one never runs
sys.modules["__init__"] = types.ModuleType("__init__")


@pytest.fixture()
def container_manager():
    from container_manager import ContainerManager

    host_manager = MagicMock()
    # destructive paths need a strict state result, pause and unknown tests override this default
    host_manager.inspect_container_state.return_value = "running"
    return ContainerManager(host_manager, MagicMock(), MagicMock())
