extends Node
## Autoload "FourD": this game's connection to the theatre (the interactive hub of Extended 4D
## Editor, "fourdvr play"). Switch the plug-in on in Project Settings > Plugins; then, anywhere:
##
##     FourD.event("hit", "A1")                    # the hub's logic blocks take it from here
##     FourD.input_received.connect(_on_button)    # a seat's buttons
##     FourD.react("A1", Vector3(-2.5, 0, 1), 0.25, {"vibration": 1.0})   # move a seat directly
##
## The address comes from Project Settings (fourd_theatre/url) or from the command line:
##     my_game -- --fourd-url=ws://192.168.10.2:47020/ws --fourd-key=...
## Directions are the seat's: x forward, y left, z up (metres, seconds); rates are roll (right side
## down), pitch (nose down) and yaw (nose left) in rad/s.

signal connected(welcome: Dictionary)
signal disconnected(code: int, reason: String)
signal refused(code: String, message: String)
signal phase_changed(phase: String, info: Dictionary)
signal seats_changed(seats: Array)
signal score_changed(seat: String, score: float, delta: float)
signal event_received(event_name: String, seat: String, info: Dictionary)
signal input_received(seat: String, button: String, pressed: bool)
signal prompt_opened(prompt: Dictionary)
signal prompt_closed(result: Dictionary)
signal state_changed(key: String, value: Variant)
signal results_received(results: Dictionary)
signal reaction(info: Dictionary)
signal hub_error(code: String, message: String)
signal message_received(type: String, message: Dictionary)

const Link := preload("res://addons/fourd_theatre/fourd_link.gd")

@export var url := "ws://127.0.0.1:47020/ws"
@export var role := "game"
@export var seat := ""
@export var player_name := ""
@export var key := ""
@export var auto_connect := true

var link := Link.new()
var phase := "lobby"
var held := false
var session_time := 0.0
var seats: Array = []
var teams: Dictionary = {}
var state: Dictionary = {}
var prompt: Dictionary = {}
var title := ""


func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS
	url = str(ProjectSettings.get_setting("fourd_theatre/url", url))
	role = str(ProjectSettings.get_setting("fourd_theatre/role", role))
	seat = str(ProjectSettings.get_setting("fourd_theatre/seat", seat))
	key = str(ProjectSettings.get_setting("fourd_theatre/key", key))
	auto_connect = bool(ProjectSettings.get_setting("fourd_theatre/auto_connect", auto_connect))
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--fourd-url="):
			url = arg.substr(12)
		elif arg.begins_with("--fourd-key="):
			key = arg.substr(12)
		elif arg.begins_with("--fourd-seat="):
			seat = arg.substr(13)
		elif arg.begins_with("--fourd-role="):
			role = arg.substr(13)
	link.welcomed.connect(_on_welcome)
	link.message_received.connect(_on_message)
	link.closed.connect(func(code: int, reason: String) -> void: disconnected.emit(code, reason))
	link.refused.connect(func(code: String, message: String) -> void: refused.emit(code, message))
	if auto_connect:
		open()


func _process(_delta: float) -> void:
	link.poll(Time.get_ticks_usec() / 1e6)


func _exit_tree() -> void:
	link.stop()


# ------------------------------------------------------------------ connection
## Connects (and keeps connecting) with the current url, role, seat, player_name and key.
func open() -> void:
	link.stop()
	link.url = url
	link.role = role
	link.seat = seat
	link.player_name = player_name
	link.key = key
	link.start(Time.get_ticks_usec() / 1e6)


func close() -> void:
	link.stop()


func is_online() -> bool:
	return link.online


func last_error() -> String:
	return link.last_error


## Any message of the protocol.
func send(type: String, fields: Dictionary = {}) -> bool:
	return link.send(type, fields)


## The hub's host name or address (for UDP telemetry).
func hub_host() -> String:
	var rest := url.substr(url.find("://") + 3) if url.contains("://") else url
	rest = rest.get_slice("/", 0)
	if rest.begins_with("["):
		return rest.get_slice("]", 0).trim_prefix("[")
	return rest.get_slice(":", 0)


func hub_port() -> int:
	var rest := url.substr(url.find("://") + 3) if url.contains("://") else url
	rest = rest.get_slice("/", 0)
	var colon := rest.rfind(":")
	if colon < 0 or rest.ends_with("]"):
		return 47020
	return int(rest.substr(colon + 1))


# ------------------------------------------------------------------ what a game says
## Something happened (to a seat, or to nobody in particular): the hub's blocks decide what follows.
func event(event_name: String, about_seat: String = "", value: float = 0.0) -> bool:
	return send("event", {"name": event_name, "seat": _or_null(about_seat), "value": value})


## A button. tap = pressed and let go at once.
func button(button_name: String, pressed: bool = true, of_seat: String = "", tap: bool = false) -> bool:
	var fields := {"button": button_name, "seat": _or_null(of_seat)}
	if not tap:
		fields["pressed"] = pressed
	return send("input", fields)


## An answer to the open question: its number (0 = first) or letter ("a").
func answer(choice: Variant, prompt_id: String = "", of_seat: String = "") -> bool:
	if typeof(choice) == TYPE_FLOAT:
		choice = int(choice)
	return send("answer", {"choice": choice, "prompt": _or_null(prompt_id), "seat": _or_null(of_seat)})


## A seat's own telemetry (the keys of the open live format: accel, rates, speed, rumble, effects ...).
## For a moving vehicle, the node FourDSeatMotion does this by itself.
func motion(of_seat: String, telemetry: Dictionary) -> bool:
	var fields := telemetry.duplicate()
	fields["seat"] = _or_null(of_seat)
	return send("motion", fields)


## Moves a seat (or "all") directly: a push in m/s^2 for some seconds, effect levels 0..1 for a while.
## What the seat really does is decided by its platform's limits.
func react(of_seat: String = "all", push: Vector3 = Vector3.ZERO, seconds: float = 0.3, effects: Dictionary = {},
		effect_seconds: float = 0.5, turn: Vector3 = Vector3.ZERO) -> bool:
	return send("react", {
		"seat": of_seat, "accel": [push.x, push.y, push.z], "rates": [turn.x, turn.y, turn.z],
		"seconds": seconds, "effects": effects, "effect_seconds": effect_seconds,
	})


## A value everybody sees (pages, other games, the blocks).
func set_state(state_key: String, value: Variant) -> bool:
	return send("state", {"key": state_key, "value": value})


func add_score(of_seat: String, points: float) -> bool:
	return send("score", {"seat": of_seat, "add": points})


## Asks a question in the headsets and on the phones. mode "each": everybody answers for themselves;
## "vote": the majority decides. The result arrives as prompt_closed.
func ask(text: String, choices: Array, seconds: float = 10.0, mode: String = "each", of_seat: String = "all",
		correct: String = "", points: float = 0.0) -> bool:
	return send("prompt", {
		"text": text, "choices": choices, "seconds": seconds, "mode": mode, "seat": of_seat,
		"correct": correct, "points": points,
	})


func set_ready(is_ready: bool = true, of_seat: String = "") -> bool:
	return send("ready", {"ready": is_ready, "seat": _or_null(of_seat)})


func start_session() -> bool:
	return send("phase", {"to": "start"})


func pause_session() -> bool:
	return send("phase", {"to": "pause"})


func resume_session() -> bool:
	return send("phase", {"to": "resume"})


func end_session() -> bool:
	return send("phase", {"to": "end"})


## The seat with this id, as the hub last described it ({} if there is none).
func seat_info(seat_id: String) -> Dictionary:
	for entry: Variant in seats:
		if typeof(entry) == TYPE_DICTIONARY and str(entry.get("id", "")) == seat_id:
			return entry
	return {}


func is_running() -> bool:
	return phase == "running" and not held


# ------------------------------------------------------------------ what the hub says
func _on_welcome(welcome: Dictionary) -> void:
	title = str(welcome.get("title", ""))
	seats = welcome.get("seats", [])
	teams = welcome.get("teams", {})
	state = welcome.get("state", {})
	var asked: Variant = welcome.get("prompt")
	prompt = asked if typeof(asked) == TYPE_DICTIONARY else {}
	_set_phase(welcome.get("phase", {}))
	connected.emit(welcome)
	seats_changed.emit(seats)
	if not prompt.is_empty():
		prompt_opened.emit(prompt)


func _on_message(type: String, message: Dictionary) -> void:
	match type:
		"phase":
			_set_phase(message)
		"seats":
			seats = message.get("seats", [])
			teams = message.get("teams", {})
			seats_changed.emit(seats)
		"score":
			score_changed.emit(str(message.get("seat", "")), float(message.get("score", 0.0)), float(message.get("delta", 0.0)))
		"event":
			var about: Variant = message.get("seat")
			event_received.emit(str(message.get("name", "")), "" if about == null else str(about), message)
		"input":
			input_received.emit(str(message.get("seat", "")), str(message.get("button", "")), bool(message.get("pressed", true)))
		"prompt":
			prompt = message
			prompt_opened.emit(message)
		"prompt_end":
			if str(message.get("id", "")) == str(prompt.get("id", "")):
				prompt = {}
			prompt_closed.emit(message)
		"state":
			if bool(message.get("reset", false)):
				state = {}
			else:
				state[str(message.get("key", ""))] = message.get("value")
				state_changed.emit(str(message.get("key", "")), message.get("value"))
		"results":
			results_received.emit(message)
		"react":
			reaction.emit(message)
		"error":
			hub_error.emit(str(message.get("code", "")), str(message.get("message", "")))
	message_received.emit(type, message)


func _set_phase(info: Variant) -> void:
	if typeof(info) != TYPE_DICTIONARY:
		return
	var details: Dictionary = info
	phase = str(details.get("phase", phase))
	held = bool(details.get("held", false))
	session_time = float(details.get("time", 0.0))
	phase_changed.emit(phase, details)


func _or_null(text: String) -> Variant:
	return null if text.is_empty() else text
