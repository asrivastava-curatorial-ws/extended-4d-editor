class_name FourDLink
extends RefCounted
## Client of the interactive hub of Extended 4D Editor ("fourdvr play"), protocol "4DPLAY" 1:
## JSON messages over one WebSocket (docs/PLAY_PROTOCOL.md of the Editor).
##
## This class does the connection and nothing else: call poll(now) every frame (now = seconds on any
## steady clock). It connects, says hello, keeps trying when the hub is away and emits what arrives.
## The autoload "FourD" (fourd.gd) wraps it with signals and methods for games.

signal welcomed(welcome: Dictionary)
signal message_received(type: String, message: Dictionary)
signal closed(code: int, reason: String)
signal refused(code: String, message: String)

const PROTOCOL := 1
const CONNECT_TIMEOUT_S := 6.0
const MAX_BACKOFF_S := 8.0
const REFUSED_RETRY_S := 15.0
const CLOSE_REFUSED := 4001

var url := ""
var role := "game"
var seat := ""
var player_name := ""
var key := ""
var reconnect := true
var online := false
var last_error := ""
var messages_in := 0
var messages_out := 0
var connects := 0
var _socket_factory: Callable
var _socket: Variant = null
var _running := false
var _greeted := false
var _was_refused := false
var _deadline := 0.0
var _next_attempt := 0.0
var _backoff := 0.5


## socket_factory: a Callable that returns a new socket (for tests); by default a WebSocketPeer.
func _init(socket_factory: Callable = Callable()) -> void:
	_socket_factory = socket_factory


func start(now: float) -> void:
	_running = true
	_next_attempt = now
	_backoff = 0.5


func stop() -> void:
	_running = false
	if _socket != null:
		_socket.close(1000, "leaving")
	_socket = null
	if online:
		online = false
		closed.emit(1000, "leaving")


func is_running() -> bool:
	return _running


## Sends one message; returns false while the hub is not connected (the message is not kept).
func send(type: String, fields: Dictionary = {}) -> bool:
	if _socket == null or not online:
		return false
	var message := {"type": type}
	for field: Variant in fields:
		if fields[field] != null:
			message[str(field)] = fields[field]
	if _socket.send_text(JSON.stringify(message)) != OK:
		return false
	messages_out += 1
	return true


func poll(now: float) -> void:
	if not _running:
		return
	if _socket == null:
		if now >= _next_attempt and not url.is_empty():
			_open(now)
		return
	_socket.poll()
	var state: int = _socket.get_ready_state()
	if state == WebSocketPeer.STATE_CLOSED:
		_on_closed(now, int(_socket.get_close_code()), str(_socket.get_close_reason()))
		return
	if state == WebSocketPeer.STATE_CONNECTING:
		if now > _deadline:
			_drop(now, "the hub does not answer")
		return
	if state != WebSocketPeer.STATE_OPEN:
		return
	if not _greeted:
		_greeted = true
		_deadline = now + CONNECT_TIMEOUT_S
		_socket.send_text(JSON.stringify(hello()))
	while _socket != null and int(_socket.get_available_packet_count()) > 0:
		var packet: PackedByteArray = _socket.get_packet()
		_handle(packet.get_string_from_utf8())
	if _socket != null and not online and now > _deadline:
		_drop(now, "no welcome from the hub")


## The first message of a connection.
func hello() -> Dictionary:
	var message := {"type": "hello", "play": PROTOCOL, "role": role}
	if not seat.is_empty():
		message["seat"] = seat
	if not player_name.is_empty():
		message["name"] = player_name
	if not key.is_empty():
		message["key"] = key
	return message


func _open(now: float) -> void:
	_socket = _socket_factory.call() if _socket_factory.is_valid() else WebSocketPeer.new()
	_greeted = false
	_was_refused = false
	_deadline = now + CONNECT_TIMEOUT_S
	connects += 1
	var result: int = _socket.connect_to_url(url)
	if result != OK:
		_socket = null
		last_error = "cannot connect to %s (error %d)" % [url, result]
		_retry(now, _backoff)


func _handle(text: String) -> void:
	var parsed: Variant = JSON.parse_string(text)
	if typeof(parsed) != TYPE_DICTIONARY:
		return
	var message: Dictionary = parsed
	var type: Variant = message.get("type")
	if typeof(type) != TYPE_STRING:
		return
	messages_in += 1
	if type == "welcome":
		online = true
		_backoff = 0.5
		last_error = ""
		welcomed.emit(message)
	elif type == "error" and not online:
		_was_refused = true
		last_error = str(message.get("message", "refused by the hub"))
		refused.emit(str(message.get("code", "")), last_error)
	elif online:
		message_received.emit(str(type), message)


func _drop(now: float, reason: String) -> void:
	last_error = reason
	if _socket != null:
		_socket.close(1000, reason.left(100))
	_socket = null
	var was_online := online
	online = false
	_retry(now, _backoff)
	if was_online:
		closed.emit(0, reason)


func _on_closed(now: float, code: int, reason: String) -> void:
	var was_online := online
	_socket = null
	online = false
	if code == CLOSE_REFUSED or _was_refused:
		_retry(now, REFUSED_RETRY_S)  # e.g. no such seat: asking again at once would not help
	else:
		if last_error.is_empty() or was_online:
			last_error = "connection closed (%d %s)" % [code, reason]
		_retry(now, _backoff)
	if was_online:
		closed.emit(code, reason)


func _retry(now: float, delay: float) -> void:
	if not reconnect:
		_running = false
		return
	_next_attempt = now + delay
	_backoff = minf(maxf(_backoff, 0.5) * 2.0, MAX_BACKOFF_S)
