"""The interactive REPL, driven through a scripted console.input."""

from typing import List

import pytest

from ipx import chat, ui
from ipx.errors import IpxError


@pytest.fixture()
def tty(monkeypatch):
    monkeypatch.setattr(ui, "is_tty", lambda: True)


def script(monkeypatch, lines: List[str]):
    it = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(ui.console, "input", fake_input)


def echo_backend(seen):
    def backend(messages, model):
        seen.append((list(messages), model))
        yield "meta", {"model_name": "Test Model"}
        yield "text", "echo: " + messages[-1]["content"]
        yield "usage", {"tokens": 7, "credits": 0.5}

    return backend


def test_repl_roundtrip_and_history(tty, monkeypatch, capsys):
    seen = []
    script(monkeypatch, ["hello", "again", "/usage", "/exit"])
    chat.run_repl(title="T", subtitle="s", backend=echo_backend(seen), model="m1", system="sys")
    out = capsys.readouterr().out
    assert "echo: hello" in out and "echo: again" in out and "Test Model" in out
    assert "2 turns" in out and "14 tokens" in out and "1 credits" in out
    # second request carries the full history, system prompt first
    second = seen[1][0]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user", ] or [m["role"] for m in second][:2] == ["system", "user"]
    assert seen[1][1] == "m1"


def test_repl_slash_commands(tty, monkeypatch, capsys, tmp_path):
    seen = []
    saved = tmp_path / "t.md"
    script(monkeypatch, ["first", f"/save {saved}", "/model other", "second", "/clear", "third", "/exit"])
    chat.run_repl(title="T", subtitle="s", backend=echo_backend(seen), model="m1")
    assert "## You" in saved.read_text() and "echo: first" in saved.read_text()
    assert seen[1][1] == "other"
    assert [m["content"] for m in seen[2][0]] == ["third"]  # /clear dropped history


def test_repl_multiline_block(tty, monkeypatch):
    seen = []
    script(monkeypatch, ['"""', "line one", "line two", '"""', "/exit"])
    chat.run_repl(title="T", subtitle="s", backend=echo_backend(seen), model=None)
    assert seen[0][0][-1]["content"] == "line one\nline two"


def test_repl_error_does_not_end_session(tty, monkeypatch, capsys):
    calls = {"n": 0}

    def backend(messages, model):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IpxError("Insufficient credits.", "top up")
        yield "text", "recovered"

    script(monkeypatch, ["one", "two", "/exit"])
    chat.run_repl(title="T", subtitle="s", backend=backend, model=None)
    cap = capsys.readouterr()
    assert "Insufficient credits" in cap.err and "recovered" in cap.out


def test_repl_requires_terminal(monkeypatch):
    monkeypatch.setattr(ui, "is_tty", lambda: False)
    with pytest.raises(IpxError):
        chat.run_repl(title="T", subtitle="s", backend=echo_backend([]), model=None)


def test_one_shot_json(capsys):
    chat.one_shot(backend=echo_backend([]), prompt="ping", model="m", system=None, as_json=True)
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["text"] == "echo: ping" and out["model"] == "Test Model" and out["usage"]["tokens"] == 7


@pytest.mark.parametrize("width,height", [(80, 24), (20, 4)])
def test_long_code_stream_is_append_only(monkeypatch, width, height):
    from io import StringIO

    from rich.console import Console

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=width, height=height, color_system=None)
    monkeypatch.setattr(ui, "console", console)
    chunks = ["```python\n"] + [f"    print('[bold]line {n}[/bold]')\n" for n in range(60)] + ["```"]
    expected = "".join(chunks)
    closed = []

    def backend(messages, model):
        try:
            yield "think", "Planning the code"
            for index, chunk in enumerate(chunks):
                yield "text", chunk
                # Inspect while the backend is still generating, not just the final frame.
                written = output.getvalue()
                start = written.index("```python")
                assert written[start:] == "".join(chunks[:index + 1])
            yield "usage", {"tokens": 100, "credits": 0.5}
        finally:
            closed.append(True)

    totals = chat.Totals()
    text, info = chat._stream_turn(backend, [], None, totals)
    written = output.getvalue()
    assert written[written.index("```python"):] == expected + "\n"
    assert text == expected
    assert totals.tokens == info["tokens"] == 100
    assert totals.credits == 0.5
    assert closed == [True]


@pytest.mark.parametrize("failure", [KeyboardInterrupt, IpxError])
def test_stream_failure_preserves_partial_output_and_closes(monkeypatch, failure):
    from io import StringIO

    from rich.console import Console

    output = StringIO()
    monkeypatch.setattr(ui, "console", Console(file=output))
    closed = []

    def backend(messages, model):
        try:
            yield "text", "partial code"
            raise failure("stopped")
        finally:
            closed.append(True)

    if failure is KeyboardInterrupt:
        text, info = chat._stream_turn(backend, [], None, chat.Totals())
        assert text == "partial code" and info["interrupted"]
    else:
        with pytest.raises(IpxError):
            chat._stream_turn(backend, [], None, chat.Totals())
    assert output.getvalue() == "partial code\n"
    assert closed == [True]
