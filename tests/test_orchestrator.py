import pytest
from orchestrator import Orchestrator


class FakeHostManager:
    def get_pub_hostname(self, name):
        return f"{name}.example.com"

    def close(self):
        pass


def make_orchestrator(contexts):
    """contexts: list of (name, weight, healthy) tuples"""
    o = Orchestrator(FakeHostManager())
    for name, weight, healthy in contexts:
        o.health[name] = healthy
        o.weights[name] = weight
        o.container_counts[name] = 0
    return o


def test_single_healthy_context():
    o = make_orchestrator([("a", 1, True)])
    assert o.get_next_context() == "a"


def test_no_healthy_raises():
    o = make_orchestrator([("a", 1, False)])
    with pytest.raises(Exception, match="no healthy contexts"):
        o.get_next_context()


def test_empty_raises():
    o = Orchestrator(FakeHostManager())
    with pytest.raises(Exception, match="no healthy contexts"):
        o.get_next_context()


def test_higher_weight_preferred():
    o = make_orchestrator([("a", 1, True), ("b", 5, True)])
    assert o.get_next_context() == "b"


def test_weight_balanced_by_load():
    # weight 2 vs weight 1, both at 0 containers -> "a" wins (score 2 vs 1)
    o = make_orchestrator([("a", 2, True), ("b", 1, True)])
    assert o.get_next_context() == "a"

    # after reserving a slot on "a": score_a = 2/2 = 1, score_b = 1/1 = 1
    # tie broken alphabetically -> "a" still wins
    o.reserve_slot("a")
    assert o.get_next_context() == "a"

    # reserve another on "a": score_a = 2/3 = 0.67, score_b = 1/1 = 1
    o.reserve_slot("a")
    assert o.get_next_context() == "b"


def test_unhealthy_skipped():
    o = make_orchestrator([("a", 10, False), ("b", 1, True)])
    assert o.get_next_context() == "b"


def test_reserve_and_release():
    o = make_orchestrator([("a", 1, True)])
    assert o.container_counts["a"] == 0
    o.reserve_slot("a")
    assert o.container_counts["a"] == 1
    o.release_slot("a")
    assert o.container_counts["a"] == 0


def test_release_does_not_go_negative():
    o = make_orchestrator([("a", 1, True)])
    o.release_slot("a")
    assert o.container_counts["a"] == 0


def test_alphabetical_tiebreak():
    # equal weight, equal load -> alphabetical wins
    o = make_orchestrator([("zebra", 1, True), ("alpha", 1, True)])
    assert o.get_next_context() == "alpha"


def test_concurrent_reserve_release():
    import threading

    num_threads = 20
    o = make_orchestrator([("a", 1, True)])
    barrier = threading.Barrier(num_threads + 1)
    min_count = [0]
    lock = threading.Lock()

    def worker():
        barrier.wait()
        o.reserve_slot("a")
        o.release_slot("a")
        with lock:
            if o.container_counts["a"] < min_count[0]:
                min_count[0] = o.container_counts["a"]

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert o.container_counts["a"] == 0
    assert min_count[0] >= 0


def test_concurrent_get_next_context():
    import threading

    num_threads = 20
    o = make_orchestrator([("a", 2, True), ("b", 3, True), ("c", 1, True)])
    barrier = threading.Barrier(num_threads + 1)
    results = []
    errors = []
    results_lock = threading.Lock()
    valid_names = {"a", "b", "c"}

    def worker():
        barrier.wait()
        try:
            result = o.get_next_context()
            with results_lock:
                results.append(result)
        except Exception as e:
            with results_lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == num_threads
    assert all(r in valid_names for r in results)
