from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from agent.a2a_context import bind_a2a_root_task_id, get_a2a_root_task_id


def test_a2a_root_task_context_is_scoped_and_propagates_to_tool_thread():
    assert get_a2a_root_task_id() is None

    with bind_a2a_root_task_id("root-123"):
        assert get_a2a_root_task_id() == "root-123"
        with ThreadPoolExecutor(max_workers=1) as pool:
            observed = pool.submit(copy_context().run, get_a2a_root_task_id).result()
        assert observed == "root-123"

    assert get_a2a_root_task_id() is None


def test_empty_a2a_root_task_is_not_authoritative():
    with bind_a2a_root_task_id("  "):
        assert get_a2a_root_task_id() is None
