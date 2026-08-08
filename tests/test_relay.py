"""Relay registry: per-user routing, single-flight, and free retries.

These are the properties that stop the relay from either spending the wrong
person's subscription or billing twice for one answer.
"""
import asyncio

import pytest

from apps.api.relay import (
    RelayConnection,
    RelayRegistry,
    RelayTimeout,
    RelayUnavailable,
    fingerprint,
)


def run(coro):
    return asyncio.run(coro)


class _Helper:
    """Fake helper: records what it was sent, answers on demand."""

    def __init__(self, *, answer=None, delay=0.0, auto=True):
        self.sent: list[dict] = []
        self.answer = answer if answer is not None else {"status": 200, "body": {"ok": True}}
        self.delay = delay
        self.auto = auto
        self.conn: RelayConnection | None = None
        self.calls = 0

    async def send_json(self, message):
        self.sent.append(message)
        if message.get("type") != "call" or not self.auto:
            return
        self.calls += 1

        async def reply():
            if self.delay:
                await asyncio.sleep(self.delay)
            self.conn.resolve(message["id"], self.answer)

        asyncio.get_running_loop().create_task(reply())


def _wire(registry, user="u1", **kw):
    helper = _Helper(**kw)
    conn = RelayConnection(user, helper.send_json)
    helper.conn = conn
    registry.register(conn)
    return helper, conn


BODY = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hi"}]}


# ---------- fingerprint ----------


def test_fingerprint_is_stable_for_equivalent_bodies():
    a = fingerprint("u1", "codex", {"b": 1, "a": 2})
    b = fingerprint("u1", "codex", {"a": 2, "b": 1})
    assert a == b


def test_fingerprint_separates_users():
    """A bare content hash would let one user's cached response satisfy
    another's identical request."""
    assert fingerprint("u1", "codex", BODY) != fingerprint("u2", "codex", BODY)


def test_fingerprint_separates_providers():
    assert fingerprint("u1", "codex", BODY) != fingerprint("u1", "openai", BODY)


def test_fingerprint_separates_different_bodies():
    other = {**BODY, "messages": [{"role": "user", "content": "different"}]}
    assert fingerprint("u1", "codex", BODY) != fingerprint("u1", "codex", other)


# ---------- routing ----------


def test_dispatch_reaches_the_registered_helper():
    async def scenario():
        reg = RelayRegistry()
        helper, _ = _wire(reg)
        out = await reg.dispatch("u1", "codex", BODY)
        assert out == {"status": 200, "body": {"ok": True}}
        assert helper.sent[0]["type"] == "call"
        assert helper.sent[0]["provider"] == "codex"

    run(scenario())


def test_dispatch_without_a_connection_fails_loudly():
    """Never fall back to 'any connected helper' — that would spend a
    stranger's subscription."""
    async def scenario():
        reg = RelayRegistry()
        _wire(reg, user="someone-else")
        with pytest.raises(RelayUnavailable):
            await reg.dispatch("u1", "codex", BODY)

    run(scenario())


def test_each_user_reaches_only_their_own_helper():
    async def scenario():
        reg = RelayRegistry()
        h1, _ = _wire(reg, user="u1", answer={"who": "one"})
        h2, _ = _wire(reg, user="u2", answer={"who": "two"})
        assert await reg.dispatch("u1", "codex", BODY) == {"who": "one"}
        assert await reg.dispatch("u2", "codex", BODY) == {"who": "two"}
        assert h1.calls == 1 and h2.calls == 1

    run(scenario())


def test_reconnect_displaces_the_stale_connection():
    """A laptop waking must not be locked out by a registration the server has
    not yet noticed is dead."""
    reg = RelayRegistry()
    _, first = _wire(reg)
    _, second = _wire(reg)
    assert reg.get("u1").connection_id == second.connection_id
    # Unregistering the displaced one must not evict the live connection.
    reg.unregister(first)
    assert reg.is_connected("u1")


def test_unregister_removes_the_current_connection():
    reg = RelayRegistry()
    _, conn = _wire(reg)
    reg.unregister(conn)
    assert not reg.is_connected("u1")


def test_disconnect_fails_in_flight_calls_rather_than_hanging():
    async def scenario():
        reg = RelayRegistry()
        _, conn = _wire(reg, auto=False)
        task = asyncio.create_task(reg.dispatch("u1", "codex", BODY))
        await asyncio.sleep(0.05)
        reg.unregister(conn)
        with pytest.raises(RelayUnavailable):
            await task

    run(scenario())


# ---------- single-flight and the result cache ----------


def test_identical_concurrent_calls_hit_the_helper_once():
    """Issuing a second identical call would bill twice for one answer."""
    async def scenario():
        reg = RelayRegistry()
        helper, _ = _wire(reg, delay=0.05)
        results = await asyncio.gather(*(reg.dispatch("u1", "codex", BODY) for _ in range(5)))
        assert helper.calls == 1
        assert all(r == {"status": 200, "body": {"ok": True}} for r in results)

    run(scenario())


def test_a_retry_after_the_answer_is_free():
    """The dropped-socket-then-retry path: already paid for."""
    async def scenario():
        reg = RelayRegistry()
        helper, _ = _wire(reg)
        first = await reg.dispatch("u1", "codex", BODY)
        second = await reg.dispatch("u1", "codex", BODY)
        assert first == second
        assert helper.calls == 1

    run(scenario())


def test_a_different_body_is_not_served_from_cache():
    async def scenario():
        reg = RelayRegistry()
        helper, _ = _wire(reg)
        await reg.dispatch("u1", "codex", BODY)
        await reg.dispatch("u1", "codex", {**BODY, "messages": [{"role": "user", "content": "z"}]})
        assert helper.calls == 2

    run(scenario())


def test_another_user_never_receives_a_cached_response():
    async def scenario():
        reg = RelayRegistry()
        h1, _ = _wire(reg, user="u1", answer={"who": "one"})
        h2, _ = _wire(reg, user="u2", answer={"who": "two"})
        await reg.dispatch("u1", "codex", BODY)
        assert await reg.dispatch("u2", "codex", BODY) == {"who": "two"}
        assert h2.calls == 1

    run(scenario())


def test_expired_results_are_not_served():
    async def scenario():
        reg = RelayRegistry(result_ttl_s=0.01)
        helper, _ = _wire(reg)
        await reg.dispatch("u1", "codex", BODY)
        await asyncio.sleep(0.05)
        await reg.dispatch("u1", "codex", BODY)
        assert helper.calls == 2

    run(scenario())


def test_a_failed_call_is_not_cached():
    """Caching a failure would make one blip permanent for the TTL."""
    async def scenario():
        reg = RelayRegistry()
        _, conn = _wire(reg, auto=False)
        task = asyncio.create_task(reg.dispatch("u1", "codex", BODY))
        await asyncio.sleep(0.05)
        conn.fail_all("boom")
        with pytest.raises(RelayUnavailable):
            await task
        assert reg.cached_count() == 0

    run(scenario())


def test_single_flight_propagates_failure_to_all_waiters():
    async def scenario():
        reg = RelayRegistry()
        _, conn = _wire(reg, auto=False)
        tasks = [asyncio.create_task(reg.dispatch("u1", "codex", BODY)) for _ in range(3)]
        await asyncio.sleep(0.05)
        conn.fail_all("gone")
        for t in tasks:
            with pytest.raises(RelayUnavailable):
                await t

    run(scenario())


# ---------- timeout and cancellation ----------


def test_timeout_cancels_the_helper_side_work():
    """Otherwise the helper keeps streaming and keeps billing for a result
    nobody will read."""
    async def scenario():
        reg = RelayRegistry()
        helper, _ = _wire(reg, auto=False)
        with pytest.raises(RelayTimeout):
            await reg.dispatch("u1", "codex", BODY, timeout_s=0.05)
        assert [m["type"] for m in helper.sent] == ["call", "cancel"]
        assert helper.sent[1]["id"] == helper.sent[0]["id"]

    run(scenario())


def test_in_flight_count_returns_to_zero_after_a_timeout():
    async def scenario():
        reg = RelayRegistry()
        _, conn = _wire(reg, auto=False)
        with pytest.raises(RelayTimeout):
            await reg.dispatch("u1", "codex", BODY, timeout_s=0.05)
        assert conn.in_flight == 0

    run(scenario())


def test_late_resolution_after_timeout_is_harmless():
    async def scenario():
        reg = RelayRegistry()
        _, conn = _wire(reg, auto=False)
        with pytest.raises(RelayTimeout):
            await reg.dispatch("u1", "codex", BODY, timeout_s=0.05)
        conn.resolve("whatever", {"late": True})  # must not raise

    run(scenario())
