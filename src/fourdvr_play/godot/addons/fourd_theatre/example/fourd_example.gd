extends Control
## A first test of the 4D Theatre add-on: attach this script to an empty Control node and run the
## scene while "fourdvr play run -c play.toml" runs (on this computer, or set fourd_theatre/url).
##
##   Enter  start the session        H  the event "hit" for the first seat
##   P      pause / resume           J  move the first seat directly (a jolt and a shake)
##   X      end the session          Q  ask everybody a question (a vote)
##
## It shows the seats and their scores and writes what the hub says to the output panel.

var _label := Label.new()


func _ready() -> void:
	_label.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT, Control.PRESET_MODE_MINSIZE, 16)
	add_child(_label)
	FourD.connected.connect(func(_welcome: Dictionary) -> void: _redraw())
	FourD.disconnected.connect(func(_code: int, _reason: String) -> void: _redraw())
	FourD.refused.connect(func(_code: String, message: String) -> void: print("refused by the hub: ", message))
	FourD.phase_changed.connect(func(_phase: String, _info: Dictionary) -> void: _redraw())
	FourD.seats_changed.connect(func(_seats: Array) -> void: _redraw())
	FourD.event_received.connect(_on_event)
	FourD.input_received.connect(_on_button)
	FourD.prompt_closed.connect(_on_answers)
	FourD.hub_error.connect(func(_code: String, message: String) -> void: print("the hub says: ", message))
	_redraw()


func _unhandled_key_input(event: InputEvent) -> void:
	var key := event as InputEventKey
	if key == null or not key.pressed or key.echo:
		return
	var first := "" if FourD.seats.is_empty() else str(FourD.seats[0].get("id", ""))
	match key.keycode:
		KEY_ENTER:
			FourD.start_session()
		KEY_P:
			if FourD.phase == "paused":
				FourD.resume_session()
			else:
				FourD.pause_session()
		KEY_X:
			FourD.end_session()
		KEY_H:
			FourD.event("hit", first)
		KEY_J:
			FourD.react(first, Vector3(0.0, 0.0, 2.5), 0.25, {"vibration": 0.8}, 0.4)
		KEY_Q:
			FourD.ask("Which way?", ["Left", "Right"], 8.0, "vote")


func _on_event(event_name: String, seat: String, _info: Dictionary) -> void:
	print("event '%s'%s" % [event_name, "" if seat.is_empty() else " for seat " + seat])


func _on_button(seat: String, button: String, pressed: bool) -> void:
	print("seat %s: button %s %s" % [seat, button, "pressed" if pressed else "let go"])


func _on_answers(result: Dictionary) -> void:
	if bool(result.get("cancelled", false)):
		return
	print("answers to '%s': %s -> %s" % [result.get("text", ""), result.get("counts", []), result.get("winner")])


func _redraw() -> void:
	var lines: PackedStringArray = []
	if FourD.is_online():
		lines.append("%s - %s" % [FourD.title, FourD.phase])
	else:
		lines.append("not connected to %s  %s" % [FourD.url, FourD.last_error()])
	for entry: Variant in FourD.seats:
		var info: Dictionary = entry
		lines.append("%-8s %-16s %6s   %s" % [
			info.get("id", ""), info.get("name", ""), info.get("score", 0),
			"connected" if int(info.get("clients", 0)) > 0 else ""])
	lines.append("")
	lines.append("Enter start   P pause   X end   H hit   J jolt   Q question")
	_label.text = "\n".join(lines)
