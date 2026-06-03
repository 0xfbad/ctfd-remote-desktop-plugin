"""Verify the thread-local _clients cache fix.

Baseline (pre-patch) keys cache by context_name only. Two threads asking the
same context get the same DockerClient -> paramiko Channel pinned to one
Hub -> InvalidThreadUseError when the other thread uses it.

Patched version keys by (context_name, threading.get_ident()), so each thread
gets its own cached client.

These tests don't need a real docker daemon; conftest.py stubs docker out.
"""
import threading

import pytest


def _make_manager():
    # use the test-fix src path established by conftest
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    # populate config so _get_client can find the endpoint
    mgr._context_configs = {"alpha": "unix:///fake.sock", "beta": "unix:///other.sock"}
    mgr._config_generation = 1  # advance past initial -1 so generation invalidation doesn't fire
    mgr._client_generation = 1
    return mgr


def test_same_thread_same_context_returns_cached():
    """Sanity: within one thread, two calls to _get_client for the same context
    return the same cached client. No regression."""
    mgr = _make_manager()
    c1 = mgr._get_client("alpha")
    c2 = mgr._get_client("alpha")
    assert c1 is c2


def test_same_thread_different_contexts_distinct():
    """Sanity: same thread, different contexts -> distinct cache entries."""
    mgr = _make_manager()
    mgr._get_client("alpha")
    mgr._get_client("beta")
    tid = threading.get_ident()
    assert ("alpha", tid) in mgr._clients
    assert ("beta", tid) in mgr._clients


def _run_concurrent_grabs(mgr, contexts, n_threads):
    """Make n_threads call _get_client(ctx) concurrently for each ctx in
    contexts. Uses a barrier so all threads hit _get_client simultaneously and
    none have died yet when others run the dead-thread pruner."""
    barrier = threading.Barrier(n_threads)
    results = {}
    lock = threading.Lock()

    def worker(idx):
        tid = threading.get_ident()
        barrier.wait()
        local = [(ctx, mgr._get_client(ctx)) for ctx in contexts]
        with lock:
            results[idx] = (tid, local)
        # keep thread alive until all done so pruner doesn't drop our entries
        # while another worker is still running
        barrier.wait()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads: t.start()
    for t in threads: t.join()
    return results


def test_different_threads_get_different_clients_for_same_context():
    """The bug fix: thread A and thread B asking for the same context create
    DISTINCT cache entries keyed by thread_ident. We verify by inspecting the
    cache directly because docker.DockerClient is mocked (all instances ==).

    On baseline (cache keyed by context only), the second thread reuses the
    first thread's entry and we get only 1 cache key. The fix makes it 2.
    """
    mgr = _make_manager()

    # barriers with timeouts so test fails gracefully on baseline instead of
    # deadlocking when worker threads are stuck waiting for main thread
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
    for t in threads: t.start()

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
        for t in threads: t.join(timeout=2.0)


def test_clear_client_drops_all_threads_entries():
    """_clear_client(ctx) must drop the cached entry for EVERY thread, so that
    the next call from any thread builds a fresh client."""
    mgr = _make_manager()
    n = 3
    phase1 = threading.Barrier(n + 1, timeout=5.0)
    phase2 = threading.Barrier(n + 1, timeout=5.0)

    def worker():
        mgr._get_client("alpha")
        try:
            phase1.wait()
            phase2.wait()
        except threading.BrokenBarrierError:
            pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads: t.start()

    try:
        phase1.wait()
        alpha_keys_before = [k for k in mgr._clients if k[0] == "alpha"]
        assert len(alpha_keys_before) == 3, f"expected 3 entries, got {alpha_keys_before}"

        mgr._clear_client("alpha")

        alpha_keys_after = [k for k in mgr._clients if k[0] == "alpha"]
        assert alpha_keys_after == []
    finally:
        try:
            phase2.wait()
        except threading.BrokenBarrierError:
            pass
        for t in threads: t.join(timeout=2.0)


def test_clear_client_doesnt_touch_other_contexts():
    """Clearing one context must not evict cached clients for other contexts."""
    mgr = _make_manager()

    def grab():
        mgr._get_client("alpha")
        mgr._get_client("beta")

    t = threading.Thread(target=grab)
    t.start(); t.join()

    assert len([k for k in mgr._clients if k[0] == "alpha"]) == 1
    assert len([k for k in mgr._clients if k[0] == "beta"]) == 1

    mgr._clear_client("alpha")

    assert len([k for k in mgr._clients if k[0] == "alpha"]) == 0
    assert len([k for k in mgr._clients if k[0] == "beta"]) == 1


def test_generation_change_drops_all_entries():
    """When _config_generation advances past _client_generation, every cached
    client (across all contexts and threads) must be dropped."""
    mgr = _make_manager()

    def grab():
        mgr._get_client("alpha")
        mgr._get_client("beta")

    threads = [threading.Thread(target=grab) for _ in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(mgr._clients) > 0

    # bump config generation
    mgr._config_generation += 1

    # next _get_client call should trigger full drain
    mgr._get_client("alpha")

    # alpha for the calling thread is back, everything else should be gone
    remaining = [k for k in mgr._clients if k[0] != "alpha" or k[1] != threading.get_ident()]
    assert remaining == [], f"stale entries survived generation bump: {remaining}"


def test_dead_thread_entries_get_pruned():
    """Thread terminates, its cached entry sits until the next _get_client call
    which prunes dead threads against threading.enumerate()."""
    mgr = _make_manager()

    def grab():
        mgr._get_client("alpha")

    t = threading.Thread(target=grab)
    t.start(); t.join()  # thread is dead now

    # one entry from the dead thread
    pre = len(mgr._clients)
    assert pre == 1

    # now call from the main thread; pruner should remove the dead-thread entry
    # AND add the main-thread entry, net should be 1
    mgr._get_client("alpha")
    post_keys = list(mgr._clients.keys())
    assert len(post_keys) == 1
    assert post_keys[0] == ("alpha", threading.get_ident())


def test_unknown_context_raises_hosts_unavailable():
    """Unchanged behavior: asking for a context with no config raises typed
    HostsUnavailableException so callers can map to 503."""
    from _rd_plugin.exceptions import HostsUnavailableException

    mgr = _make_manager()
    with pytest.raises(HostsUnavailableException):
        mgr._get_client("nonexistent")
