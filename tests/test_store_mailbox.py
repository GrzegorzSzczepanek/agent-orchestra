from pathlib import Path

import pytest

from orchestra.store import EventStore


@pytest.fixture
def store(tmp_path: Path):
    s = EventStore(tmp_path / "e.db")
    yield s
    s.close()


def test_post_message_returns_increasing_ids_and_messages_in_order(store: EventStore):
    ids = [store.post_message("a", "b", f"m{i}") for i in range(3)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3
    msgs = store.messages()
    assert [m.id for m in msgs] == ids
    assert [m.body for m in msgs] == ["m0", "m1", "m2"]


def test_messages_since_id_returns_only_newer(store: EventStore):
    first = store.post_message("a", "b", "one")
    second = store.post_message("a", "b", "two")
    third = store.post_message("a", "b", "three")
    assert [m.id for m in store.messages(since_id=first)] == [second, third]
    assert [m.id for m in store.messages(since_id=second)] == [third]
    assert store.messages(since_id=third) == []


def test_messages_limit_truncates(store: EventStore):
    for i in range(5):
        store.post_message("a", "b", f"m{i}")
    assert [m.body for m in store.messages(limit=2)] == ["m0", "m1"]
    assert [m.body for m in store.messages(limit=0)] == []


def test_inbox_filters_by_recipient_since_id_and_thread(store: EventStore):
    m1 = store.post_message("planner", "impl", "pierwsza", thread="t1")
    store.post_message("planner", "impl", "druga", thread="t2")
    store.post_message("planner", "other", "nie dla impl", thread="t1")
    store.post_message("impl", "planner", "odpowiedź", thread="t1")

    assert [m.body for m in store.inbox("impl")] == ["pierwsza", "druga"]
    assert [m.body for m in store.inbox("impl", since_id=m1)] == ["druga"]
    assert [m.body for m in store.inbox("impl", thread="t1")] == ["pierwsza"]
    assert [m.body for m in store.inbox("impl", thread="t2")] == ["druga"]
    assert [m.body for m in store.inbox("impl", thread="t1", since_id=m1)] == []
    assert store.inbox("nobody") == []


def test_default_thread_is_empty(store: EventStore):
    mid = store.post_message("a", "b", "bez wątku")
    assert store.messages()[0].thread == ""
    assert store.inbox("b", thread="")[0].id == mid
    assert store.inbox("b")[0].thread == ""


def test_message_event_on_inbox_agent_has_payload(store: EventStore):
    store.post_message("planner", "impl", "zrób X", thread="t1")
    events = store.events(agent="inbox")
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "message"
    assert ev.agent == "inbox"
    assert ev.payload == {"from": "planner", "to": "impl", "thread": "t1", "body": "zrób X"}
    assert [e.kind for e in store.events(kind="message")] == ["message"]


def test_run_and_task_recipients_do_not_create_inbox_event(store: EventStore):
    run_id = store.post_message("planner", "run:abc", "sterowanie")
    task_id = store.post_message("planner", "task:x", "zadanie")
    assert store.events(agent="inbox") == []
    assert store.events(kind="message") == []

    all_ids = [m.id for m in store.messages()]
    assert all_ids == [run_id, task_id]
    assert [m.body for m in store.inbox("run:abc")] == ["sterowanie"]
    assert [m.body for m in store.inbox("task:x")] == ["zadanie"]


def test_message_persists_after_reopen(tmp_path: Path):
    path = tmp_path / "e.db"
    first = EventStore(path)
    mid = first.post_message("a", "b", "trwała", thread="t1")
    first.close()

    reopened = EventStore(path)
    try:
        msgs = reopened.messages()
        assert [m.id for m in msgs] == [mid]
        assert msgs[0].body == "trwała"
        assert msgs[0].thread == "t1"
        assert [m.body for m in reopened.inbox("b", thread="t1")] == ["trwała"]
    finally:
        reopened.close()
