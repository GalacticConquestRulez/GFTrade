"""Multi-user authorization and alert broadcasting."""
from types import SimpleNamespace

import pytest

from gftrade import config
from gftrade.config import _parse_ids
from gftrade.tg import bot as tgbot
from gftrade.tg import handlers


def test_parse_ids_handles_lists_and_junk():
    assert _parse_ids("111111111,222222222") == [111111111, 222222222]
    assert _parse_ids(" 123 ; 456 ") == [123, 456]
    assert _parse_ids("123, notanid, 456") == [123, 456]
    assert _parse_ids("") == []
    assert _parse_ids(None) == []


def test_is_authorized_checks_the_live_list(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_IDS", [111, 222])
    assert handlers.is_authorized(111)
    assert handlers.is_authorized(222)
    assert not handlers.is_authorized(333)


async def test_authorized_only_blocks_strangers(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_IDS", [111, 222])
    calls = []

    @handlers.authorized_only
    async def probe(update, context):
        calls.append(update.effective_user.id)

    def update_from(user_id):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=user_id),
        )

    await probe(update_from(111), None)
    await probe(update_from(222), None)
    await probe(update_from(999), None)  # stranger: silently dropped
    assert calls == [111, 222]


async def test_key_export_restricted_to_primary_owner(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_IDS", [111, 222])
    monkeypatch.setattr(config, "OWNER_ID", 111)
    answers = []

    class FakeQuery:
        def __init__(self, user_id):
            self.from_user = SimpleNamespace(id=user_id)

        async def answer(self, text=None, show_alert=False):
            answers.append((text, show_alert))

    deps = SimpleNamespace(keypair=None)
    # second user: refused before any wallet logic runs
    await handlers.dispatch_callback(FakeQuery(222), None, deps, "we")
    assert "primary owner" in answers[-1][0] and answers[-1][1] is True
    await handlers.dispatch_callback(FakeQuery(222), None, deps, "wec")
    assert "primary owner" in answers[-1][0]
    # primary owner in dry-run: passes the owner gate, hits the no-wallet gate
    await handlers.dispatch_callback(FakeQuery(111), None, deps, "we")
    assert "dry-run" in answers[-1][0]


class RecordingBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_for:
            raise RuntimeError("user never pressed Start")
        self.sent.append((chat_id, text))


async def test_events_broadcast_to_all_authorized_users(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_IDS", [111, 222])
    bot = RecordingBot()
    app = SimpleNamespace(bot=bot)
    event = {"type": "exit", "reason": "take_profit", "symbol": "MOON",
             "dry_run": True, "pnl_sol": 0.05, "pnl_pct": 25.0}
    await tgbot.publish_events(app, deps=None, events=[event])
    assert [chat_id for chat_id, _ in bot.sent] == [111, 222]
    assert all("MOON" in text for _, text in bot.sent)


async def test_one_unreachable_user_does_not_block_the_other(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_IDS", [111, 222])
    bot = RecordingBot(fail_for=[111])
    app = SimpleNamespace(bot=bot)
    event = {"type": "exit", "reason": "stop_loss", "symbol": "MOON",
             "dry_run": True, "pnl_sol": -0.02, "pnl_pct": -10.0}
    await tgbot.publish_events(app, deps=None, events=[event])
    assert [chat_id for chat_id, _ in bot.sent] == [222]
