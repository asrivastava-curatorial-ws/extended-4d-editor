# The open live format ("4DLIVE")

For anything that has no telemetry of its own: a VR experience made in Godot, Unity or Unreal, a
simulator of your own, a ride film player, a show controller. The experience sends what the
audience should feel; `fourdvr live run --source open` turns it into platform motion, effects and
bass — through the same cueing, the same limits and the same safety as game telemetry.

* **Transport:** UDP, one message per datagram, to the computer that runs the live show, port
  **47010** (changeable). Send 30 to 120 messages a second; 60 is a good number.
* **Message:** one JSON object, UTF-8, at most 4096 bytes.
* **Every key but `"4dlive"` is optional.** A message that is not valid is refused as a whole and
  counted; the show carries on with the last good one and rests if nothing good arrives for a
  quarter of a second.

Ready senders: `fourdvr live sender godot`, `fourdvr live sender unity`, `fourdvr live sender python`
(add `-o FOLDER` to write the file). Check with `fourdvr live monitor --source open`.

## Keys

| Key | Value | Meaning |
|---|---|---|
| `4dlive` | `1` | **required**: the format's version |
| `accel` | three numbers, m/s² | what a passenger feels beyond resting weight: **surge** (+ forwards), **sway** (+ to the left), **heave** (+ upwards). Zero when standing still or cruising straight on level ground |
| `accel_g` | three numbers, g | the same in g (instead of `accel`) |
| `velocity` | three numbers, m/s | instead of `accel`: the vehicle's velocity in its own axes (forwards, to the left, upwards). The acceleration is worked out here, including what turning adds (speed × turning rate) |
| `rates` | three numbers, rad/s | rotation: **roll** (+ right side down), **pitch** (+ nose down), **yaw** (+ nose left) |
| `rates_deg` | three numbers, °/s | the same in degrees (instead of `rates`) |
| `attitude` | two numbers, rad | roll and pitch angle (same signs); shown, not used for motion |
| `speed` | m/s | for the wind; taken from `velocity` if that is given |
| `rpm`, `rpm_max`, `rpm_idle` | revolutions a minute | engine tone of the shakers and seat vibration; without `rpm_max` 8000 is assumed |
| `throttle`, `brake` | 0…1 | the engine tone gets louder with the throttle |
| `gear` | integer | −1 reverse, 0 neutral (shown only) |
| `rumble` | 0…1 | rough ground under the vehicle: seat vibration and road noise in the shakers |
| `slip` | 0…1 | tyres sliding (a little vibration) |
| `impact` | 0…1 | a hit **in this message**: a thump in the shakers, an air blast from 0.35 on. If the key is missing, hits are recognised from jumps of `accel` |
| `airborne` | true / false | shown only |
| `active` | true / false | `false` while paused or in a menu: motion and effects fade to rest and come back when it is `true` again |
| `effects` | object of levels 0…1 | effects the experience asks for itself: `rain`, `fog`, `heat`, `snow`, `bubbles`, `water`, `air`, `scent`, `wind`, `vibration`, `strobe`, or a venue's own effect type. The venue's limits apply to every one |
| `scent` | integer | which cartridge a stated `scent` level means |
| `strobe_hz` | number | flash rate of a stated `strobe` (the venue's ceiling applies) |
| `t` | seconds | the sender's own clock; used for `velocity` and for hit detection instead of the arrival time |

A stated `strobe` is ignored unless the venue has switched it on (`[rules] strobe = true`): a
program outside the venue's control does not get to flash lights at an audience by default.
`wind` and `vibration` are normally made from `speed`, `rpm` and `rumble`; a stated level is used
if it is higher.

## Example

```json
{"4dlive": 1, "t": 12.35,
 "accel": [1.2, -0.4, 0.0], "rates": [0.0, 0.02, 0.31],
 "speed": 14.0, "rpm": 3200, "rpm_max": 7000, "throttle": 0.8,
 "rumble": 0.2, "effects": {"rain": 0.6}, "active": true}
```

## Getting the directions right

The platform's rotations are the right-handed rotations about **x forward, y left, z up**. Game
engines use other axes:

| Engine | Its axes | surge, sway, heave from an acceleration **a** in the object's own axes | roll, pitch, yaw from its angular velocity **w** |
|---|---|---|---|
| Godot 4 | −Z forward, +X right, +Y up (right-handed) | −a.z, −a.x, a.y | −w.z, −w.x, w.y |
| Unity | +Z forward, +X right, +Y up (left-handed) | a.z, −a.x, a.y | −w.z, w.x, −w.y |
| Unreal | +X forward, +Y right, +Z up (left-handed) | a.x, −a.y, a.z | −w.x, w.y, −w.z  (check: Unreal's rotation signs differ between its own functions) |

The printed Godot and Unity senders do this conversion. They follow the engines' documented
conventions and were **not run inside the engines** for this release: check each direction once
with `fourdvr live monitor --source open` (it says "braking", "pushed left", "nose turning left" …),
and turn what is the other way round with `--axis NAME=-1`.

## What the format deliberately cannot do

It cannot command a platform pose, a DMX value or an actuator. A sender describes what happens to
the vehicle; what the seat does with it is decided by the cueing, the platform's limiter and the
venue's patch — the same guards that a recorded programme passes. That is what makes it safe to
accept from a program nobody at the venue wrote.
