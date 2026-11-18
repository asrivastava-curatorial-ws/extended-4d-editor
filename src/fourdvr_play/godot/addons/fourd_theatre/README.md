# 4D Theatre - Godot 4 add-on

Connects a Godot 4 game to the interactive hub of **Extended 4D Editor** (`fourdvr play run`):
events and scores, questions, the seats' buttons, and every seat's own motion and effects.

Written for Godot 4.3 or newer. It was checked for syntax (gdtoolkit); it has **not been run in
Godot** by its author - please report what does not work.

## Set-up

1. This folder belongs at `addons/fourd_theatre` in your project
   (`fourdvr play addon -o MY_PROJECT` writes it there).
2. *Project > Project Settings > Plugins*: switch on **4D Theatre**. It adds the autoload `FourD`.
3. *Project Settings > General > Fourd Theatre*: set `url` to the hub, e.g.
   `ws://192.168.10.2:47020/ws` (the address `fourdvr play run` prints as "game"). Or start the game
   with `-- --fourd-url=ws://192.168.10.2:47020/ws`.
4. Try it: attach `example/fourd_example.gd` to an empty `Control` node and run the scene.

## The game's side in a few lines

```gdscript
func _ready() -> void:
    FourD.input_received.connect(_on_button)        # a seat's buttons (board, phone, headset)
    FourD.phase_changed.connect(_on_phase)          # lobby, countdown, running, paused, results
    FourD.event_received.connect(_on_event)         # events the hub's blocks send ("tell the game")

func _on_player_hit(seat: String) -> void:
    FourD.event("hit", seat)                        # the blocks decide: points, a jolt, effects ...

func _on_button(seat: String, button: String, pressed: bool) -> void:
    if button == "a" and pressed:
        fire(seat)
```

| Call | What it does |
|---|---|
| `FourD.event(name, seat = "", value = 0.0)` | something happened; the hub's logic blocks react |
| `FourD.react(seat, push, seconds, effects, effect_seconds, turn)` | moves a seat directly (`"all"`: every seat and the room) |
| `FourD.motion(seat, telemetry)` | a seat's telemetry by hand; the node `FourDSeatMotion` does it for a moving node |
| `FourD.ask(text, choices, seconds, mode, seat, correct, points)` | a question in the headsets and on the phones |
| `FourD.set_state(key, value)` | a value everybody sees |
| `FourD.add_score(seat, points)` | points |
| `FourD.start_session()`, `pause_session()`, `resume_session()`, `end_session()` | the session |
| `FourD.seats`, `FourD.teams`, `FourD.state`, `FourD.phase`, `FourD.prompt` | what the hub last said |

Signals: `connected`, `disconnected`, `refused`, `phase_changed`, `seats_changed`, `score_changed`,
`event_received`, `input_received`, `prompt_opened`, `prompt_closed`, `state_changed`,
`results_received`, `reaction`, `hub_error`, `message_received`.

## A seat that moves with the game

Add a `FourDSeatMotion` node as a child of each vehicle (or attach the script to it) and set its
`seat`. It sends what a passenger would feel; the hub makes the platform's motion of it, inside the
platform's own limits. Directions: the theatre uses x forward, y left, z up; the node converts from
Godot's axes.

Nothing a game sends can move a platform beyond its limits: a push is an acceleration that goes
through the Editor's cueing and the platform's limiter. Seats move only while the session runs.

## Roles

`fourd_theatre/role`: `game` (the default: may speak for every seat) or `player` / `headset` with
`fourd_theatre/seat` (speaks for that seat only - a game instance per seat). If the hub has a
`game_key`, put it into `fourd_theatre/key`.

Licence: MIT, as Extended 4D Editor.
