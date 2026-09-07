"""记忆系统测试"""
import pytest

from memory.memory_manager import MemoryManager
from memory.short_term import ShortTermMemory


class _FakePipeline:
    def __init__(self, client):
        self.client = client

    def delete(self, key):
        self.client.values.pop(key, None)
        return self

    def rpush(self, key, *values):
        self.client.values.setdefault(key, []).extend(values)
        return self

    def expire(self, key, ttl):
        return self

    def execute(self):
        return []


class _FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, ex=None):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def lrange(self, key, start, end):
        return self.values.get(key, [])

    def pipeline(self):
        return _FakePipeline(self)


def test_short_term_fallback_is_flushed_after_reconnect(monkeypatch):
    memory = ShortTermMemory()
    memory._available = False
    memory._next_retry_at = float("inf")
    memory.save_state_snapshot("session-1", {"current_question_idx": 3})
    memory.append_turn("session-1", "user", "answer")

    fake_redis = _FakeRedis()
    memory._client = fake_redis
    memory._available = True
    memory._flush_fallback()

    assert "session-1" not in memory._mem_store
    assert memory.get_state_snapshot("session-1")["current_question_idx"] == 3
    assert memory.get_turns("session-1") == [{"role": "user", "content": "answer"}]


def test_memory_manager_loads_graph_checkpoint_and_warms_cache(monkeypatch):
    class FakeShortTerm:
        saved = None

        def get_state_snapshot(self, session_id):
            return None

        def save_state_snapshot(self, session_id, state):
            self.saved = state

    manager = MemoryManager()
    manager._short_term = FakeShortTerm()
    monkeypatch.setattr(
        manager,
        "_load_graph_checkpoint",
        lambda session_id: {
            "current_question_idx": "4",
            "awaiting_answer": "true",
        },
    )

    state = manager.load_session("session-2")

    assert state["current_question_idx"] == 4
    assert state["awaiting_answer"] is True
    assert manager._short_term.saved["current_question_idx"] == 4


def test_memory_manager_saves_redis_and_graph_checkpoint(monkeypatch):
    class FakeShortTerm:
        saved = None

        def save_state_snapshot(self, session_id, state):
            self.saved = (session_id, state)

    graph_saved = []
    manager = MemoryManager()
    manager._short_term = FakeShortTerm()
    monkeypatch.setattr(
        manager,
        "_save_graph_checkpoint",
        lambda session_id, state: graph_saved.append((session_id, state)) or True,
    )

    manager.save_session("session-3", {"current_question_idx": 2})

    assert manager._short_term.saved == (
        "session-3",
        {"current_question_idx": 2},
    )
    assert graph_saved == [("session-3", {"current_question_idx": 2})]


@pytest.mark.skip(reason="需要 Redis + MySQL")
def test_short_term():
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    print("Testing short-term memory...")
    print(stm.get_turns("test_session"))  # 应该是空的

    stm.append_turn("test_session", "user", "hello")
    turns = stm.get_turns("test_session")
    assert len(turns) >= 1
    print("Turns after appending:", turns)

    stm.clear("test_session")
    print("Turns after clearing:", stm.get_turns("test_session"))  # 应该是空的

    print("Short-term memory test passed!")

if __name__ == "__main__":
    test_short_term()

    # python -m tests.test_memory
    # pytest -s tests/test_memory.py
