from __future__ import annotations

from hermes_state import SessionDB


def test_explicit_reset_blocks_recovery_of_an_older_live_peer(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_key = "agent:main:telegram:group:chat-1:topic-1"
    peer = {
        "user_id": "user-1",
        "session_key": session_key,
        "chat_id": "chat-1",
        "chat_type": "group",
        "thread_id": "topic-1",
    }
    try:
        db.create_session("old-live-session", "telegram", **peer)
        db.append_message("old-live-session", "user", "old task")

        db.create_session("new-reset-session", "telegram", **peer)
        db.append_message("new-reset-session", "user", "new task")
        db.end_session("new-reset-session", "session_reset")

        recovered = db.find_latest_gateway_session_for_peer(
            source="telegram",
            user_id="user-1",
            session_key=session_key,
            chat_id="chat-1",
            chat_type="group",
            thread_id="topic-1",
        )
    finally:
        db.close()

    assert recovered is None
