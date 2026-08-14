import pytest

from gateway.session_events import SessionEventHub


@pytest.mark.asyncio
async def test_session_event_hub_fans_out_without_consuming_other_subscribers():
    hub = SessionEventHub()
    first = hub.subscribe("session-1")
    second = hub.subscribe("session-1")

    hub.publish("session-1", "run.started", {"run_id": "run-1", "seq": 1})
    hub.publish("session-1", "assistant.delta", {"delta": "hello", "seq": 2})

    assert (await first.get(0.1))["event"] == "run.started"
    assert (await second.get(0.1))["event"] == "run.started"
    assert (await first.get(0.1))["data"]["delta"] == "hello"
    assert (await second.get(0.1))["data"]["delta"] == "hello"
    assert hub.is_running("session-1") is True

    hub.publish("session-1", "run.completed", {"seq": 3})
    assert (await first.get(0.1))["event"] == "run.completed"
    assert (await second.get(0.1))["event"] == "run.completed"
    assert hub.is_running("session-1") is False


@pytest.mark.asyncio
async def test_session_event_hub_replays_only_live_frames_after_sequence():
    hub = SessionEventHub()
    hub.publish("session-1", "run.started", {"run_id": "run-1", "seq": 1})
    hub.publish(
        "session-1",
        "assistant.delta",
        {"message_id": "message-1", "delta": "a", "seq": 2},
    )
    hub.publish(
        "session-1",
        "assistant.delta",
        {"message_id": "message-1", "delta": "b", "seq": 3},
    )

    subscriber = hub.subscribe("session-1", after_seq=1)
    replay = await subscriber.get(0.1)
    assert replay["event"] == "assistant.delta"
    assert replay["data"]["delta"] == "ab"
    assert replay["data"]["seq"] == 3

    hub.publish("session-1", "done", {"seq": 4})
    assert (await subscriber.get(0.1))["event"] == "done"
    assert await hub.subscribe("session-1").get(0.01) is None
