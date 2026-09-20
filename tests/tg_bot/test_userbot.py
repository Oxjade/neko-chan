import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

from userbot import _answer_once, _try_answer  # noqa: E402


class _Q:
    """Minimal CallbackQuery stand-in that records answer() attempts."""

    def __init__(self, qid):
        self.id = qid
        self.answers = []
        self.replies = []
        self.message = self

    async def answer(self, *args, **kwargs):
        self.answers.append(args)
        if self._raise:
            raise _AlreadyAnswered("Query is already answered")

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _AlreadyAnswered(Exception):
    pass


def test_answer_once_answers_exactly_once():
    q = _Q("cb-1")
    q._raise = False

    async def run():
        await _answer_once(q, "first")
        await _answer_once(q, "first")

    asyncio.run(run())
    assert len(q.answers) == 1
    assert q.answers[0] == ("first",)


def test_answer_once_delivers_reason_as_reply_on_repeat():
    q = _Q("cb-2")
    q._raise = False

    async def run():
        await _answer_once(q, "first")
        await _answer_once(q, "second-time")

    asyncio.run(run())
    assert len(q.answers) == 1
    assert q.replies == ["second-time"]


def test_answer_once_swallows_already_answered():
    q = _Q("cb-3")
    q._raise = True

    async def run():
        await _answer_once(q, "toast")

    asyncio.run(run())  # must not raise
    assert len(q.answers) == 1


class _OtherError(Exception):
    pass


def test_answer_once_re_raises_real_errors():
    q = _Q("cb-4")

    async def answer(*a, **k):
        raise _OtherError("connection reset")

    q.answer = answer

    async def run():
        await _try_answer(q, "toast")

    try:
        asyncio.run(run())
        assert False, "expected _OtherError to propagate"
    except _OtherError:
        pass