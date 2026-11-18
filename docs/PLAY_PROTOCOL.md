# The hub's protocol: "4DPLAY" 1

How a game, a page, a headset or any other program talks to the hub of an interactive show
(`fourdvr play run`, [PLAY.md](PLAY.md)). Everything is JSON; a client needs nothing but a
WebSocket. The Godot add-on, the pages and `fourdvr_play.client.PlayClient` are clients like any
other.

* **WebSocket** `ws://HUB:47020/ws` - text frames, one JSON object each, with a `"type"`.
  A message may be 16 KiB at most.
* **HTTP** on the same port: the pages (`/`, `/play`, `/operator`, `/scoreboard`), `/api/info`
  (title, seats, phase - no key) and `/api/status?key=OPERATOR_KEY`.
* **UDP** port 47020: a seat's telemetry, as an alternative to `motion` messages.

The traffic is not encrypted and is meant for the theatre's own network.

## 1. Joining

The first message of a connection is `hello`; the hub answers `welcome` or closes.

```json
{"type": "hello", "play": 1, "role": "game"}
{"type": "hello", "play": 1, "role": "player", "seat": "A1", "name": "Asha"}
{"type": "hello", "play": 1, "role": "operator", "key": "the-operator-key"}
```

| Role | Who | May |
|---|---|---|
| `game` | the game, a show controller, a script | speak for every seat (`"seat"` in each message), send events, reactions, questions, scores, shared values; needs `key` if the show has a `game_key` |
| `player` | a phone or tablet at a seat | speak for its own seat: buttons, answers, ready, its comfort settings |
| `headset` | the VR Player at a seat | the same as `player` |
| `display` | a scoreboard | listen only |
| `operator` | the operator page | everything, and the session itself; needs the operator `key` |

`welcome` carries what a client needs to draw itself: `play` (1), `version`, `title`, `client`,
`role`, `seat`, `phase` (as the `phase` message), `seats`, `teams`, `state`, `buttons`, `film`
(true: a film leads the session), `prompt` (the open question for this client, or null) and
`results` (while the results are shown).

A connection that is refused gets an `error` message and is closed:

| Close code | Meaning |
|---|---|
| 4000 | the first message was not a `hello` in time |
| 4001 | refused: wrong key, unknown seat or role, another protocol number - asking again will not help |
| 4002 | a frame was not JSON |
| 4003 | too slow: the client did not read, 512 messages were waiting |

## 2. What a client sends

A `game` or `operator` names the seat in each message (`"seat": "A2"`); a `player` or `headset`
speaks for its own seat and leaves it out.

| Message | Fields | Meaning |
|---|---|---|
| `event` | `name`, `seat`?, `value`? | something happened; `on_event` blocks react. Names: letters, digits, `. _ : -`, at most 40 |
| `input` | `button`, `pressed`?, `seat` | a button. Without `pressed` it is a tap (pressed and let go) |
| `answer` | `choice`, `prompt`?, `seat` | the answer to the open question: an index (0-3) or a letter (`"a"`-`"d"`) |
| `motion` | `seat`, the keys of the open live format | a seat's telemetry ([LIVE_FORMAT.md](LIVE_FORMAT.md)): `accel`, `rates`, `speed`, `rumble`, `effects` ... Not counted against the message limit |
| `ready` | `ready`?, `seat` | "I am ready" in the lobby |
| `seat` | `motion`?, `blocked`?, `name`?; operator: `enabled`?, `team`?, `seat` | a seat's settings: `motion` = `full` \| `gentle` \| `off`, `blocked` = effect names |
| `ping` | `t`? | answered with `pong` (`t` comes back, with the session's `time`) |

A `game` or `operator` may also send:

| Message | Fields | Meaning |
|---|---|---|
| `react` | `seat` (a seat or `"all"`), `accel`?, `rates`?, `seconds`?, `effects`?, `effect_seconds`? | move a seat and give it effects directly, as a `react` block does (inside `[play.limits]`) |
| `prompt` | `text`, `choices` (2-4), `seconds`?, `mode`?, `correct`?, `points`?, `seat`? | ask a question (everybody, or one seat) |
| `state` | `key`, `value` | set a shared value: a number, a short text or true/false |
| `score` | `seat` or `team`, `add` or `set` | change a score |
| `boards` | `platform`, `bits` (0-255) | a show service passes on the seat buttons of a platform's ESP32: bit 0 is input 1. The hub turns changes into `input`s of that platform's seat |

And an `operator` only:

| Message | Fields | Meaning |
|---|---|---|
| `phase` | `to` = `start` \| `pause` \| `resume` \| `end` \| `reset` | the session (refused with code `film` while a film leads) |
| `hold` | `on` | nothing moves / release (also: go on after a platform fault) |

A connection may send `max_events_per_s` messages a second (20 unless the show says otherwise;
`motion`, `boards` and `ping` are not counted). More are left out, and the sender gets an `error`
with code `slow_down` once a second.

## 3. What the hub sends

"Staff" below means games, operators and displays.

| Message | To | Fields |
|---|---|---|
| `phase` | everybody | `phase` = `lobby` \| `countdown` \| `running` \| `paused` \| `results`, `time` (s of the session), `held`, `seconds` (countdown left), `remaining`? (s, if the session has a length) |
| `seats` | everybody, when something changed (a few times a second at most) | `seats`: list of `{id, name, team, platform, enabled, motion, blocked, ready, clients, headset, score}`; `teams`: `{name: score}` |
| `score` | the seat | `seat`, `score`, `delta` |
| `state` | everybody | `key`, `value` - or `reset`: true (all shared values are gone) |
| `event` | the seats a `send` block names, and staff | `name`, `seat` (or null), `seats`, `value`, `choice`, `from`: `"logic"`. Operators also get every event that arrives, with `from` = who sent it |
| `input` | games, operators | `seat`, `button`, `pressed` |
| `react` | the seats concerned, staff | `seats`, `push` (true: the seat moves), `effects` (names), `seconds` - a phone may vibrate, a game may show it |
| `prompt` | the seats asked, staff | `id`, `text`, `choices`, `seconds`, `left`, `mode` |
| `answered` | the seat | `prompt`, `choice` (index) - "your answer is in" |
| `prompt_state` | staff | `id`, `answered`, `of` |
| `prompt_end` | the seats asked, staff | `id`, `text`, `choices`, `counts`, `winner` (index or null), `correct` (index or null), `mode`; to a seat also `yours`, `right`; to staff `answers` (`{seat: index}`); or `cancelled`: true |
| `results` | everybody, at the end | `title`, `ended`, `duration_s`, `seats` (place, id, name, team, score, events, inputs, answers, right, reactions), `teams`, `asked`, `state` |
| `status` | operators, twice a second | `session` (phase, seats, state, clients, logic counters, the open question, the film), `engine` (platforms, seats, effects), `network` |
| `log` | operators | `text` |
| `error` | the sender | `code`, `message` |
| `pong` | the sender | `t`, `time` |

A client should ignore message types and fields it does not know: later versions may add some.

Error codes: `hello`, `protocol`, `role`, `key`, `seat`, `unknown_seat`, `full` (too many
connections), `bad_message`, `not_allowed`, `slow_down`, `no_prompt` (there is no question to
answer), `phase` (not possible in this phase), `film` (the film leads the session).

## 4. Seat telemetry over UDP

One datagram is one JSON object of the open live format with the seat's id:

```json
{"4dlive": 1, "seat": "A1", "accel": [1.2, 0.0, 0.0], "rates": [0.0, 0.0, 0.1], "speed": 14.0}
```

Datagrams without a known seat, or that are not valid, are counted (`network.telemetry_bad` in the
status) and dropped. A seat whose telemetry stops comes to rest by itself.

## 5. A session, message by message

```
game      → {"type": "hello", "play": 1, "role": "game"}
hub       → {"type": "welcome", "play": 1, "title": "Asteroid run", "phase": {"phase": "lobby", ...}, ...}
operator  → {"type": "phase", "to": "start"}
hub       → {"type": "phase", "phase": "countdown", "seconds": 5.0, ...}
hub       → {"type": "phase", "phase": "running", "time": 0.0, "held": false}
game      → {"type": "event", "name": "hit", "seat": "A1"}
hub       → {"type": "react", "seats": ["A1"], "push": true, "effects": ["air", "vibration"], "seconds": 0.6}
hub       → {"type": "score", "seat": "A1", "score": -5, "delta": -5}          (to seat A1)
hub       → {"type": "prompt", "id": "p1", "text": "Which way?", "choices": ["Through the canyon", "Over the ridge"],
             "seconds": 8.0, "left": 8.0, "mode": "vote"}
player A1 → {"type": "answer", "choice": 0}
hub       → {"type": "answered", "prompt": "p1", "choice": 0}                  (to seat A1)
hub       → {"type": "prompt_end", "id": "p1", "counts": [2, 0], "winner": 0, "correct": null, "mode": "vote", ...}
hub       → {"type": "event", "name": "route.canyon", "value": 0}
operator  → {"type": "phase", "to": "end"}
hub       → {"type": "phase", "phase": "results", ...}
hub       → {"type": "results", "seats": [{"place": 1, "id": "A2", "score": 0, ...}, ...], "teams": [...], ...}
```

The messages a headset relies on are kept as a file by the hub's own tests
(`tests/play/fixtures/play_messages.json`); the VR Player's tests read the same file.
