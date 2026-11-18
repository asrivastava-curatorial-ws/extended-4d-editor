class_name FourDSeatMotion
extends Node3D
## Gives one seat of the theatre the motion of this node.
##
## Attach it to the vehicle or to the rider's seat in the game (or make it a child of that node): it
## sends what a passenger would feel - acceleration and rotation in the vehicle's own axes - to the
## hub as that seat's telemetry, 50 times a second. The hub turns it into motion of the seat's
## platform within that platform's limits; wind, vibration and the other effects follow from speed,
## rumble and the levels given in `effects`.
## Godot: -Z is forward, +X right, +Y up. The theatre: x forward, y left, z up.

## The seat this node moves ("A1"). Empty: nothing is sent.
@export var seat := ""
## Messages a second.
@export_range(10.0, 120.0) var rate_hz := 50.0
## 0..1: rough ground, engine, turbulence - set it from the game.
@export_range(0.0, 1.0) var rumble := 0.0
## Effect levels 0..1 that the game states itself, e.g. {"rain": 0.6, "fog": 0.2}.
@export var effects := {}
## Engine speed for the seat's shaker (0: none) and its top value.
@export var rpm := 0.0
@export var rpm_max := 8000.0
## false: the telemetry goes over the FourD autoload's WebSocket instead of UDP.
@export var use_udp := true

var _udp := PacketPeerUDP.new()
var _udp_ready := false
var _last_velocity := Vector3.ZERO
var _last_basis := Basis.IDENTITY
var _last_position := Vector3.ZERO
var _started := false
var _wait := 0.0
var _span := 0.0


func _physics_process(delta: float) -> void:
	if seat.is_empty() or delta <= 0.0:
		return
	_wait += delta
	_span += delta
	if _wait < 1.0 / rate_hz:
		return
	_wait = fmod(_wait, 1.0 / rate_hz)
	var message := sample(_span)
	_span = 0.0
	var hub: Node = get_node_or_null("/root/FourD")
	if hub == null:
		return
	if not use_udp:
		hub.call("motion", seat, message)
		return
	if not _udp_ready:
		_udp_ready = _udp.set_dest_address(str(hub.call("hub_host")), int(hub.call("hub_port"))) == OK
	if _udp_ready:
		message["seat"] = seat
		_udp.put_packet(JSON.stringify(message).to_utf8_buffer())


## What a passenger of this node felt during the last `seconds`, as a message of the open live format.
func sample(seconds: float) -> Dictionary:
	var b := global_transform.basis.orthonormalized()
	var velocity := (global_position - _last_position) / seconds
	if not _started:
		velocity = Vector3.ZERO
		_last_basis = b
		_started = true
	var accel := (velocity - _last_velocity) / seconds
	# the rotation since the last message, as a vector along its axis (world axes, rad/s)
	var dq := (b * _last_basis.inverse()).get_rotation_quaternion()
	var omega := Vector3.ZERO
	if absf(dq.w) < 0.999999:
		omega = dq.get_axis() * dq.get_angle() / seconds
	var forward := -b.z
	var right := b.x
	var up := b.y
	_last_velocity = velocity
	_last_basis = b
	_last_position = global_position
	return {
		"4dlive": 1,
		"accel": [accel.dot(forward), -accel.dot(right), accel.dot(up)],  # surge, sway (+ left), heave
		"rates": [omega.dot(forward), -omega.dot(right), omega.dot(up)],  # roll, pitch (+ nose down), yaw (+ left)
		"speed": velocity.length(),
		"rumble": rumble,
		"rpm": rpm,
		"rpm_max": rpm_max,
		"effects": effects,
		"active": not get_tree().paused,
	}
