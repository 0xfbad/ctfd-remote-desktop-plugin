import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def loaded_plugin():
    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("_rd_plugin", repo_root / "src" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "_rd_plugin"
    sys.modules["_rd_plugin"] = mod
    spec.loader.exec_module(mod)

    # stub heavyweight collaborators so load() reaches the blueprint registration
    mod._seed_defaults = MagicMock()
    mod._seed_local_context = MagicMock()
    mod._reconcile_containers = MagicMock()
    mod._claim_scheduler_leader = MagicMock(return_value=False)
    mod.DockerHostManager = MagicMock()
    mod.Orchestrator = MagicMock()
    mod.ContainerManager = MagicMock()
    mod.event_bus = MagicMock()
    mod.event_logger = MagicMock()
    mod.register_user_page_menu_bar = MagicMock()
    mod.create_routes = MagicMock(return_value=MagicMock())

    app = MagicMock()
    app.overridden_templates = {}

    fake_open = MagicMock()
    fake_open.return_value.__enter__ = lambda s: MagicMock(read=lambda: "tpl")
    fake_open.return_value.__exit__ = lambda *a, **k: None

    with patch("builtins.open", fake_open):
        mod.load(app)

    bp = app.register_blueprint.call_args.args[0]
    return mod, app, bp


def _captured_handler(bp, name):
    for call in bp.after_request.call_args_list:
        fn = call.args[0]
        if getattr(fn, "__name__", None) == name:
            return fn
    raise LookupError(name)


class _Headers(dict):
    def setdefault(self, key, value):
        return super().setdefault(key, value)


class _Response:
    def __init__(self):
        self.headers = _Headers()


def test_after_request_handler_registered_on_blueprint(loaded_plugin):
    _, _, bp = loaded_plugin
    fn = _captured_handler(bp, "_add_frame_headers")
    assert callable(fn)


def test_frame_headers_set_on_response(loaded_plugin):
    _, _, bp = loaded_plugin
    fn = _captured_handler(bp, "_add_frame_headers")

    resp = _Response()
    out = fn(resp)

    assert out is resp
    assert out.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert out.headers["Content-Security-Policy"] == "frame-ancestors 'self'"


def test_frame_headers_respect_existing_values(loaded_plugin):
    _, _, bp = loaded_plugin
    fn = _captured_handler(bp, "_add_frame_headers")

    resp = _Response()
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "default-src 'none'"

    out = fn(resp)

    # setdefault preserves caller-provided values so a stricter policy isn't downgraded
    assert out.headers["X-Frame-Options"] == "DENY"
    assert out.headers["Content-Security-Policy"] == "default-src 'none'"
