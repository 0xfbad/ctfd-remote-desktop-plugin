"""_clients is keyed by context_name and thread ident, one shared client pins the paramiko channel to one thread
conftest.py stubs docker out so no daemon is needed"""

import threading
from unittest.mock import MagicMock, patch

import pytest


def _make_manager():
    # conftest loads the plugin modules under the _rd_plugin package
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {"alpha": "unix:///fake.sock", "beta": "unix:///other.sock"}
    mgr._config_generation = 1
    return mgr


def test_same_thread_same_context_returns_cached():
    mgr = _make_manager()
    c1 = mgr._get_client("alpha")
    c2 = mgr._get_client("alpha")
    assert c1 is c2


def test_same_thread_different_contexts_distinct():
    mgr = _make_manager()
    mgr._get_client("alpha")
    mgr._get_client("beta")
    tid = threading.get_ident()
    assert ("alpha", tid) in mgr._clients
    assert ("beta", tid) in mgr._clients


def test_different_threads_get_different_clients_for_same_context():
    """the cache is inspected directly because docker.DockerClient is mocked and every instance compares equal"""
    mgr = _make_manager()

    # barrier timeouts so the test fails instead of deadlocking when a worker never arrives
    n = 2
    phase1 = threading.Barrier(n + 1, timeout=5.0)
    phase2 = threading.Barrier(n + 1, timeout=5.0)
    tids = []
    lock = threading.Lock()

    def worker():
        with lock:
            tids.append(threading.get_ident())
        mgr._get_client("alpha")
        try:
            phase1.wait()
            phase2.wait()
        except threading.BrokenBarrierError:
            pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()

    try:
        phase1.wait()
        alpha_keys = sorted(k for k in mgr._clients if k[0] == "alpha")
        assert len(alpha_keys) == 2, f"expected 2 thread-local entries, got {len(alpha_keys)}: {alpha_keys}"
        assert {k[1] for k in alpha_keys} == set(tids)
    finally:
        try:
            phase2.wait()
        except threading.BrokenBarrierError:
            pass
        for t in threads:
            t.join(timeout=2.0)


def test_clear_client_only_drops_calling_threads_entry():
    mgr = _make_manager()
    n = 2
    phase1 = threading.Barrier(n + 1, timeout=5.0)
    phase2 = threading.Barrier(n + 1, timeout=5.0)
    created: list[MagicMock] = []

    def new_client(**_kwargs):
        client = MagicMock(name=f"client-{len(created)}")
        created.append(client)
        return client

    def worker():
        mgr._get_client("alpha")
        try:
            phase1.wait()
            phase2.wait()
        except threading.BrokenBarrierError:
            pass

    with patch("_rd_plugin.docker_host_manager.docker.DockerClient", side_effect=new_client):
        main_client = mgr._get_client("alpha")
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
        for thread in threads:
            thread.start()

        try:
            phase1.wait()
            alpha_keys_before = [key for key in mgr._clients if key[0] == "alpha"]
            assert len(alpha_keys_before) == 3

            mgr._clear_client("alpha")

            alpha_keys_after = [key for key in mgr._clients if key[0] == "alpha"]
            assert len(alpha_keys_after) == 2
            assert all(key[1] != threading.get_ident() for key in alpha_keys_after)
            main_client.close.assert_called_once_with()
            for client in created:
                if client is not main_client:
                    client.close.assert_not_called()
        finally:
            try:
                phase2.wait()
            except threading.BrokenBarrierError:
                pass
            for thread in threads:
                thread.join(timeout=2.0)


def test_clear_client_doesnt_touch_other_contexts():
    mgr = _make_manager()

    mgr._get_client("alpha")
    mgr._get_client("beta")

    assert len([k for k in mgr._clients if k[0] == "alpha"]) == 1
    assert len([k for k in mgr._clients if k[0] == "beta"]) == 1

    mgr._clear_client("alpha")

    assert len([k for k in mgr._clients if k[0] == "alpha"]) == 0
    assert len([k for k in mgr._clients if k[0] == "beta"]) == 1


def test_context_invalidation_lazily_replaces_peer_threads_client():
    """invalidation marks peer clients stale without closing them while they may still be active"""
    mgr = _make_manager()
    ready = threading.Event()
    continue_worker = threading.Event()
    done = threading.Event()
    observed: dict[str, MagicMock] = {}
    created: list[MagicMock] = []

    def new_client(**_kwargs):
        client = MagicMock(name=f"client-{len(created)}")
        created.append(client)
        return client

    def worker():
        observed["first"] = mgr._get_client("alpha")
        ready.set()
        continue_worker.wait(timeout=5.0)
        observed["second"] = mgr._get_client("alpha")
        done.set()

    with patch("_rd_plugin.docker_host_manager.docker.DockerClient", side_effect=new_client):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert ready.wait(timeout=5.0)

        first = observed["first"]
        mgr._clear_client("alpha")
        first.close.assert_not_called()

        continue_worker.set()
        assert done.wait(timeout=5.0)
        thread.join(timeout=2.0)

    second = observed["second"]
    assert second is not first
    first.close.assert_called_once_with()
    second.close.assert_not_called()
    assert mgr._context_client_epochs["alpha"] == 1


def test_failed_fresh_ping_invalidates_threadpool_clients_lazily():
    mgr = _make_manager()
    ready = threading.Event()
    continue_worker = threading.Event()
    done = threading.Event()
    observed: dict[str, MagicMock] = {}

    def worker():
        observed["first"] = mgr._get_client("alpha")
        ready.set()
        continue_worker.wait(timeout=5.0)
        observed["second"] = mgr._get_client("alpha")
        done.set()

    clients = [MagicMock(name="first"), MagicMock(name="second")]

    with (
        patch(
            "_rd_plugin.docker_host_manager.docker.DockerClient",
            side_effect=clients,
        ),
        patch("_rd_plugin.docker_host_manager.ping_endpoint", return_value=False),
    ):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert ready.wait(timeout=5.0)

        assert mgr.ping("alpha") is False
        observed["first"].close.assert_not_called()

        continue_worker.set()
        assert done.wait(timeout=5.0)
        thread.join(timeout=2.0)

    assert observed["second"] is not observed["first"]
    observed["first"].close.assert_called_once_with()
    observed["second"].close.assert_not_called()


def test_generation_change_preserves_other_threads_active_clients():
    mgr = _make_manager()
    n = 2
    phase1 = threading.Barrier(n + 1, timeout=5.0)
    phase2 = threading.Barrier(n + 1, timeout=5.0)
    worker_clients: list[MagicMock] = []
    clients_lock = threading.Lock()

    def grab():
        client = mgr._get_client("alpha")
        with clients_lock:
            worker_clients.append(client)
        try:
            phase1.wait()
            phase2.wait()
        except threading.BrokenBarrierError:
            pass

    def new_client(**_kwargs):
        return MagicMock()

    with patch("_rd_plugin.docker_host_manager.docker.DockerClient", side_effect=new_client):
        threads = [threading.Thread(target=grab, daemon=True) for _ in range(n)]
        for thread in threads:
            thread.start()

        try:
            phase1.wait()
            mgr._config_generation += 1
            main_client = mgr._get_client("alpha")

            alpha_keys = [key for key in mgr._clients if key[0] == "alpha"]
            assert len(alpha_keys) == 3
            assert mgr._client_generations[("alpha", threading.get_ident())] == 2
            assert all(main_client is not client for client in worker_clients)
            for client in worker_clients:
                client.close.assert_not_called()
        finally:
            try:
                phase2.wait()
            except threading.BrokenBarrierError:
                pass
            for thread in threads:
                thread.join(timeout=2.0)


def test_generation_change_replaces_only_callers_stale_client():
    mgr = _make_manager()
    first = MagicMock(name="first")
    second = MagicMock(name="second")

    with patch(
        "_rd_plugin.docker_host_manager.docker.DockerClient",
        side_effect=[first, second],
    ):
        assert mgr._get_client("alpha") is first
        mgr._config_generation += 1
        assert mgr._get_client("alpha") is second

    first.close.assert_called_once_with()
    second.close.assert_not_called()
    assert mgr._client_generations[("alpha", threading.get_ident())] == 2


def test_generation_change_retires_callers_other_context_client():
    mgr = _make_manager()
    alpha = MagicMock(name="alpha")
    beta = MagicMock(name="beta")
    replacement_beta = MagicMock(name="replacement-beta")

    with patch(
        "_rd_plugin.docker_host_manager.docker.DockerClient",
        side_effect=[alpha, beta, replacement_beta],
    ):
        assert mgr._get_client("alpha") is alpha
        assert mgr._get_client("beta") is beta
        mgr._config_generation += 1
        assert mgr._get_client("beta") is replacement_beta

    alpha.close.assert_called_once_with()
    beta.close.assert_called_once_with()
    replacement_beta.close.assert_not_called()
    tid = threading.get_ident()
    assert ("alpha", tid) not in mgr._clients
    assert mgr._clients[("beta", tid)] is replacement_beta


def test_removed_context_client_is_retired_when_worker_uses_other_context():
    """a removed context must not leak a client until that exact name is reused"""
    mgr = _make_manager()
    alpha = MagicMock(name="alpha")
    beta = MagicMock(name="beta")
    replacement_beta = MagicMock(name="replacement-beta")

    with patch(
        "_rd_plugin.docker_host_manager.docker.DockerClient",
        side_effect=[alpha, beta, replacement_beta],
    ):
        assert mgr._get_client("alpha") is alpha
        assert mgr._get_client("beta") is beta
        del mgr._context_configs["alpha"]
        mgr._config_generation += 1
        assert mgr._get_client("beta") is replacement_beta

    alpha.close.assert_called_once_with()
    beta.close.assert_called_once_with()
    replacement_beta.close.assert_not_called()
    assert all(key[0] != "alpha" for key in mgr._clients)


def test_failed_replacement_still_closes_every_retired_client():
    mgr = _make_manager()
    alpha = MagicMock(name="alpha")
    beta = MagicMock(name="beta")

    with patch(
        "_rd_plugin.docker_host_manager.docker.DockerClient",
        side_effect=[alpha, beta, RuntimeError("invalid endpoint")],
    ):
        assert mgr._get_client("alpha") is alpha
        assert mgr._get_client("beta") is beta
        mgr._config_generation += 1
        with pytest.raises(RuntimeError, match="invalid endpoint"):
            mgr._get_client("beta")

    alpha.close.assert_called_once_with()
    beta.close.assert_called_once_with()
    assert mgr._clients == {}


def test_dead_thread_entries_get_pruned():
    mgr = _make_manager()

    def grab():
        mgr._get_client("alpha")

    t = threading.Thread(target=grab)
    t.start()
    t.join()

    pre = len(mgr._clients)
    assert pre == 1

    mgr._get_client("alpha")  # the pruner drops the dead thread entry and adds ours, net one
    post_keys = list(mgr._clients.keys())
    assert len(post_keys) == 1
    assert post_keys[0] == ("alpha", threading.get_ident())


def test_unknown_context_raises_hosts_unavailable():
    """callers map the typed exception to a 503"""
    from _rd_plugin.exceptions import HostsUnavailableException

    mgr = _make_manager()
    with pytest.raises(HostsUnavailableException):
        mgr._get_client("nonexistent")
