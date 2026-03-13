from event_logger import EventLogger


def test_log_and_retrieve():
    el = EventLogger(max_events=10)
    event = el.log_event("test", "hello")
    assert event["type"] == "test"
    assert event["message"] == "hello"
    assert el.get_recent_events() == [event]


def test_deque_eviction():
    el = EventLogger(max_events=3)
    for i in range(5):
        el.log_event("t", f"msg {i}")
    events = el.get_recent_events()
    assert len(events) == 3
    assert events[0]["message"] == "msg 2"


def test_get_recent_with_limit():
    el = EventLogger(max_events=100)
    for i in range(10):
        el.log_event("t", f"msg {i}")
    events = el.get_recent_events(limit=3)
    assert len(events) == 3
    assert events[0]["message"] == "msg 7"


def test_listener_receives_events():
    el = EventLogger()
    received = []
    el.add_listener(lambda e: received.append(e))
    el.log_event("t", "hi")
    assert len(received) == 1
    assert received[0]["message"] == "hi"


def test_failing_listener_removed():
    el = EventLogger()

    def bad_listener(e):
        raise RuntimeError("boom")

    el.add_listener(bad_listener)
    el.log_event("t", "first")
    assert bad_listener not in el.listeners
    # second event shouldn't raise
    el.log_event("t", "second")


def test_remove_listener():
    el = EventLogger()
    received = []

    def cb(e):
        received.append(e)

    el.add_listener(cb)
    el.log_event("t", "before")
    el.remove_listener(cb)
    el.log_event("t", "after")
    assert len(received) == 1


def test_metadata_defaults_to_empty_dict():
    el = EventLogger()
    event = el.log_event("t", "no meta")
    assert event["metadata"] == {}


def test_event_has_timestamp_fields():
    el = EventLogger()
    event = el.log_event("t", "check fields")
    assert "timestamp" in event
    assert "datetime" in event
    assert isinstance(event["timestamp"], float)


def test_concurrent_log_events():
    import threading

    num_threads = 20
    events_per_thread = 50
    el = EventLogger(max_events=2000)
    barrier = threading.Barrier(num_threads + 1)

    def worker(thread_id):
        barrier.wait()
        for i in range(events_per_thread):
            el.log_event("t", f"thread {thread_id} event {i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    events = el.get_recent_events(limit=0)
    assert len(events) == num_threads * events_per_thread
