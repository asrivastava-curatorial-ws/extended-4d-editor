"""Messages of the hub, kept as a file that the Godot side is tested against.

The VR Player (tests/fixtures/play_messages.json there) and the add-on cannot be run where this
program is built, so their tests read what the hub really sends - this file. If a message changes,
this test fails until the file is written again (FOURDVR_WRITE_FIXTURES=1) and copied over.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from play_helpers import Session

FIXTURE = Path(__file__).parent / "fixtures" / "play_messages.json"
QUIZ = [{"id": "ask", "type": "on_event", "event": "quiz", "then": ["q"]},
        {"id": "q", "type": "prompt", "text": "2 + 2?", "choices": ["3", "4", "5"], "seconds": 10.0, "correct": "b", "points": 50.0},
        {"id": "fork", "type": "on_event", "event": "fork", "then": ["v"]},
        {"id": "v", "type": "prompt", "text": "Which way?", "choices": ["Left", "Right"], "seconds": 8.0, "mode": "vote"}]


def build() -> dict[str, Any]:
    s = Session(QUIZ, seats=3)
    h1, h2, h3, game = s.join("headset", "A1"), s.join("headset", "A2"), s.join("headset", "A3"), s.join("game")
    s.start()
    out: dict[str, Any] = {}
    s.hub.handle(game, {"type": "event", "name": "quiz"})
    out["prompt_each"] = s.messages("prompt", h1)[-1]
    s.run(2.0)
    _late, welcome = s.hub.join({"type": "hello", "play": 1, "role": "headset", "seat": "A1", "name": "VR-01"})
    welcome.pop("version")
    out["welcome_with_open_prompt"] = welcome
    s.hub.handle(h1, {"type": "answer", "prompt": "q1", "choice": 1})
    out["answered"] = s.messages("answered", h1)[-1]
    s.hub.handle(h2, {"type": "answer", "prompt": "q1", "choice": 0})
    s.run(8.1)
    out["prompt_end_right"] = s.messages("prompt_end", h1)[-1]
    out["prompt_end_wrong"] = s.messages("prompt_end", h2)[-1]
    out["prompt_end_no_answer"] = s.messages("prompt_end", h3)[-1]
    s.hub.handle(game, {"type": "event", "name": "fork"})
    out["prompt_vote"] = s.messages("prompt", h1)[-1]
    s.hub.handle(h1, {"type": "answer", "choice": 1})
    s.hub.handle(h2, {"type": "answer", "choice": 1})
    s.hub.handle(h3, {"type": "answer", "choice": 0})
    out["prompt_end_vote"] = s.messages("prompt_end", h3)[-1]
    s.hub.handle(game, {"type": "event", "name": "quiz"})
    s.hub.handle(game, {"type": "event", "name": "fork"})                      # replaces the open one
    out["prompt_end_cancelled"] = next(m for m in s.messages("prompt_end", h1) if m.get("cancelled"))
    s.clear()
    s.hub.handle(game, {"type": "score", "seat": "A1", "add": 5})
    out["score"] = s.messages("score", h1)[-1]
    s.hub.command("end")
    out["phase_results"] = s.messages("phase", h1)[-1]
    return json.loads(json.dumps(out))


def test_the_messages_the_godot_side_is_tested_against_are_what_the_hub_sends() -> None:
    messages = build()
    if os.environ.get("FOURDVR_WRITE_FIXTURES"):
        FIXTURE.write_text(json.dumps(messages, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == messages
    assert messages["prompt_end_right"]["right"] is True and messages["prompt_end_right"]["yours"] == 1
    assert messages["prompt_end_no_answer"]["yours"] is None and messages["prompt_end_vote"]["winner"] == 1
    assert messages["welcome_with_open_prompt"]["prompt"]["left"] == 8.0
