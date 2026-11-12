"""Ready senders of the open live format (docs/LIVE_FORMAT.md) for your own experience.

They are printed by ``fourdvr live sender godot|unity|python`` so that they are at hand wherever the
program is installed. The Godot and Unity scripts follow those engines' documented axis conventions;
they were not run inside the engines here - check the directions once with ``fourdvr live monitor``.
"""

from __future__ import annotations

from .sources.flight import flightgear_protocol_xml

GODOT = '''# FourDLiveSender.gd - Extended 4D Editor, open live format ("4DLIVE"), Godot 4.
# Attach to the node that is the vehicle / the rider's seat (a RigidBody3D, CharacterBody3D or any
# Node3D that moves). It sends, 60 times a second, what a passenger would feel: acceleration and
# rotation in the vehicle's own axes. Godot: -Z is forward, +X right, +Y up (right-handed).
extends Node3D

@export var host := "127.0.0.1"      # the computer that runs "fourdvr live run"
@export var port := 47010
@export var rumble := 0.0            # 0..1: set from your game (rough ground, engine, ...)
@export var effects := {}            # e.g. {"rain": 0.6, "fog": 0.0, "wind": 0.3}

var _udp := PacketPeerUDP.new()
var _last_velocity := Vector3.ZERO
var _last_basis := Basis.IDENTITY
var _last_position := Vector3.ZERO
var _started := false

func _ready() -> void:
	_udp.set_dest_address(host, port)

func _physics_process(delta: float) -> void:
	var b := global_transform.basis.orthonormalized()
	var velocity := (global_position - _last_position) / delta
	if not _started:
		velocity = Vector3.ZERO
		_last_basis = b
		_started = true
	var accel := (velocity - _last_velocity) / delta
	# rotation since the last frame, as a vector along the axis of rotation (world axes, rad/s)
	var dq := (b * _last_basis.inverse()).get_rotation_quaternion()
	var omega := Vector3.ZERO
	if absf(dq.w) < 0.999999:
		omega = dq.get_axis() * dq.get_angle() / delta
	var forward := -b.z
	var right := b.x
	var up := b.y
	var msg := {
		"4dlive": 1,
		"accel": [accel.dot(forward), -accel.dot(right), accel.dot(up)],       # surge, sway (+ left), heave
		"rates": [omega.dot(forward), -omega.dot(right), omega.dot(up)],       # roll, pitch (+ nose down), yaw (+ left)
		"speed": velocity.length(),
		"rumble": rumble,
		"effects": effects,
		"active": not get_tree().paused,
	}
	_udp.put_packet(JSON.stringify(msg).to_utf8_buffer())
	_last_velocity = velocity
	_last_basis = b
	_last_position = global_position
'''

UNITY = '''// FourDLiveSender.cs - Extended 4D Editor, open live format ("4DLIVE"), Unity.
// Add to the GameObject that is the vehicle / the rider's seat. It sends, at every physics step,
// what a passenger would feel: acceleration and rotation in the vehicle's own axes.
// Unity: +Z is forward, +X right, +Y up (left-handed; rotations follow the left-hand rule).
using System.Globalization;
using System.Net.Sockets;
using System.Text;
using UnityEngine;

public class FourDLiveSender : MonoBehaviour
{
    public string host = "127.0.0.1";   // the computer that runs "fourdvr live run"
    public int port = 47010;
    [Range(0f, 1f)] public float rumble = 0f;   // set from your game (rough ground, engine, ...)
    [Range(0f, 1f)] public float rain = 0f;
    [Range(0f, 1f)] public float fog = 0f;

    UdpClient udp;
    Vector3 lastPosition, lastVelocity;
    Quaternion lastRotation;
    bool started;

    void Start() { udp = new UdpClient(); udp.Connect(host, port); }
    void OnDestroy() { udp?.Close(); }

    void FixedUpdate()
    {
        float dt = Time.fixedDeltaTime;
        Vector3 velocity = started ? (transform.position - lastPosition) / dt : Vector3.zero;
        Vector3 accel = started ? (velocity - lastVelocity) / dt : Vector3.zero;
        Vector3 omega = Vector3.zero;                       // world axes, rad/s, left-hand rule
        if (started)
        {
            Quaternion dq = transform.rotation * Quaternion.Inverse(lastRotation);
            dq.ToAngleAxis(out float angle, out Vector3 axis);
            if (angle > 180f) angle -= 360f;
            if (!float.IsNaN(axis.x)) omega = axis * (angle * Mathf.Deg2Rad / dt);
        }
        Vector3 a = transform.InverseTransformDirection(accel);   // x right, y up, z forward
        Vector3 w = transform.InverseTransformDirection(omega);
        var c = CultureInfo.InvariantCulture;
        string msg = string.Format(c,
            "{{\\"4dlive\\":1,\\"accel\\":[{0:F4},{1:F4},{2:F4}],\\"rates\\":[{3:F5},{4:F5},{5:F5}]," +
            "\\"speed\\":{6:F3},\\"rumble\\":{7:F3},\\"effects\\":{{\\"rain\\":{8:F3},\\"fog\\":{9:F3}}},\\"active\\":{10}}}",
            a.z, -a.x, a.y,          // surge, sway (+ left), heave
            -w.z, w.x, -w.y,         // roll (+ right side down), pitch (+ nose down), yaw (+ nose left)
            velocity.magnitude, rumble, rain, fog, Time.timeScale > 0f ? "true" : "false");
        byte[] data = Encoding.UTF8.GetBytes(msg);
        udp.Send(data, data.Length);
        lastPosition = transform.position; lastVelocity = velocity; lastRotation = transform.rotation;
        started = true;
    }
}
'''

PYTHON = '''#!/usr/bin/env python3
"""Minimal sender of the open live format ("4DLIVE") - Extended 4D Editor.

One JSON object per UDP datagram, 60 times a second. Directions as on the platform:
accel = [surge (+ forwards), sway (+ to the left), heave (+ upwards)] in m/s^2,
rates = [roll (+ right side down), pitch (+ nose down), yaw (+ nose left)] in rad/s.
Everything but "4dlive" is optional. Try it:   fourdvr live monitor --source open
"""
import json
import math
import socket
import time

HOST, PORT = "127.0.0.1", 47010
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
start = time.monotonic()
while True:
    t = time.monotonic() - start
    msg = {
        "4dlive": 1,
        "t": round(t, 3),
        "accel": [2.0 * math.sin(0.5 * t), 3.0 * math.sin(0.3 * t), 0.0],
        "rates": [0.0, 0.0, 0.25 * math.sin(0.3 * t)],
        "speed": 20.0 + 8.0 * math.sin(0.1 * t),
        "rpm": 3000 + 1500 * math.sin(0.5 * t), "rpm_max": 7000, "throttle": 0.6,
        "rumble": 0.2,
        "effects": {"rain": 0.5 if 10 < t % 30 < 14 else 0.0},
        "active": True,
    }
    sock.sendto(json.dumps(msg).encode(), (HOST, PORT))
    time.sleep(1 / 60)
'''

SENDERS = {
    "godot": ("FourDLiveSender.gd", GODOT),
    "unity": ("FourDLiveSender.cs", UNITY),
    "python": ("fourdlive_sender.py", PYTHON),
    "flightgear": ("fourdvr.xml", flightgear_protocol_xml()),
}
