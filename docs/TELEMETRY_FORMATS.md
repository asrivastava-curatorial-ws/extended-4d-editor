# Telemetry formats of the live mode

What `fourdvr live` reads from each game, where that format is published, and — most important —
**which directions are certain and which are assumed**. Read [LIVE.md](LIVE.md) first; this file is
the reference behind its table of sources.

Three things are true of every format here:

* **The byte layout comes from the publisher's own document** (or, for two formats, from open
  community documentation, said below). Each decoder checks every packet's size and markers against
  it and refuses everything else; the tests build packets from the same layouts and check sizes
  against the published numbers.
* **No decoder was run against the real game.** There is no game, console or simulator in the test
  environment. What was tested is that packets built from the published layout come out as the
  motion that went in ([LIVE.md](LIVE.md), "What was tested").
* **Most publishers do not say which way a positive value points.** Where the document says it, the
  table says *stated*. Where a general convention applies (aeronautics), it says *convention*.
  Everywhere else it says *assumed*: the best reading of the document, to be checked once per game
  with `fourdvr live monitor` and turned in `[axes]` if it is the other way round.

The platform's directions, which every decoder converts to: **surge** + forwards, **sway** + to the
left, **heave** + upwards (m/s²); **roll** + right side down, **pitch** + nose down, **yaw** + nose
left (rad/s). These are the right-handed rotations about the axes x forward, y left, z up.

---

## Open live format — `open`

Published: [LIVE_FORMAT.md](LIVE_FORMAT.md) (this program). UDP, default port 47010, one JSON
object per datagram. Directions: *stated* (they are the platform's own).

## Forza Data Out — `forza`

Published: support.forza.net, "Forza Motorsport Data Out Documentation" and "Forza Horizon 6 Data
Out Documentation" (the Horizon page states the size of 324 bytes and the three values inserted
after `NumCylinders`). UDP, one way, 60 packets a second, to the address and port set in the game.
Little-endian.

| Packet | Size | Games |
|---|---|---|
| Sled | 232 | all (the "Sled" format) |
| Dash | 311 | Forza Motorsport 7 |
| Dash | 331 | Forza Motorsport (2023): Dash + tyre wear ×4 + track ordinal |
| Horizon | 324 | Forza Horizon 4, 5, 6: Sled + 12 bytes + the Dash part + 1 byte |

Read: `IsRaceOn` (0), `TimestampMS` (4), `EngineMaxRpm` / `EngineIdleRpm` / `CurrentEngineRpm` (8,
12, 16), `AccelerationX/Y/Z` (20), `VelocityX/Y/Z` (32), `AngularVelocityX/Y/Z` (44), `Yaw`, `Pitch`,
`Roll` (56), `WheelOnRumbleStrip` ×4 (116), `WheelInPuddleDepth` ×4 (132), `SurfaceRumble` ×4 (148),
`TireCombinedSlip` ×4 (180); from the Dash part `Speed` (244), `Accel`, `Brake`, `Gear` (303, 304,
307) — 12 bytes later in the Horizon packet.

| Value | From | Basis |
|---|---|---|
| surge, sway, heave | `AccelerationZ`, −`AccelerationX`, `AccelerationY` | axes *stated*: "X = right, Y = up, Z = forward" |
| roll, pitch, yaw | −`AngularVelocityZ`, `AngularVelocityX`, −`AngularVelocityY` | which axis is which is *stated* ("X = pitch, Y = yaw, Z = roll"); the direction of rotation is *assumed* (left-handed, as the axes are) |
| rest when not racing | `IsRaceOn` = 0 | *stated* |
| road feel | `SurfaceRumble`, rumble strips | *stated* |
| water spray | `WheelInPuddleDepth` | *stated* (0 to 1) |

## EA SPORTS F1 23 / 24 / 25 — `f1`

Published: EA forums, "F1 25 UDP Specification" (and the F1 24 and F1 23 documents). UDP, default
port 20777, packed little-endian structures, 22 cars per packet. The game's "UDP Format" must be
2023, 2024 or 2025.

Header, 29 bytes: packet format (0), packet id (6), session time (15), player car index (27).

| Packet | Id | Size | Read |
|---|---|---|---|
| Motion | 0 | 1349 | player's `gForceLateral`, `gForceLongitudinal`, `gForceVertical`, `yaw`, `pitch`, `roll` (car × 60 + 36) |
| Session | 1 | 644 / 753 | `m_weather` (byte 29): 3 light rain, 4 heavy rain, 5 storm → rain |
| Car telemetry | 6 | 1352 | speed, throttle, brake, gear, engine RPM, surface type under each wheel (car × 60) |
| Car status | 7 | 1239 | `m_maxRPM`, `m_idleRPM` (car × 55 + 17) |
| Motion ex | 13 | 217 / 237 / 273 | `m_angularVelocityX/Y/Z` (173) |

A sample is made at each motion packet; the document says that packet is "only sent while player
is in control", so a pause shows as the source stalling.

| Value | From | Basis |
|---|---|---|
| surge | `gForceLongitudinal` × g | *assumed* (positive = accelerating) |
| sway | −`gForceLateral` × g | *assumed* |
| heave | `gForceVertical` × g | *assumed*; a constant 1 g, if the game includes it, does not matter (only changes pass) |
| roll, pitch, yaw | −Z, X, −Y of the angular velocity | *assumed*: the document shows its axes in a picture only |
| road feel | surface type 1 rumble strip, 3 rock, 4 gravel … | *stated* (table of surface types) |

## Codemasters telemetry, "extradata 3" — `dirt`

Published: Codemasters' telemetry field list for DiRT Rally and later titles (66 values); the field
numbers used here are those of the open *dr2_logger* project, which cites that list. UDP, default
port 20777, 66 little-endian floats = 264 bytes.

Read: run time (0), speed (7), velocity (8–10), the car's "roll" = right vector (11–13) and "pitch"
= forward vector (14–16), throttle (29), brake (31), gear (33), lateral g (34), longitudinal g (35),
engine speed ÷ 10 (37), maximum and idle (63, 64).

| Value | From | Basis |
|---|---|---|
| surge, sway | field 35 × g, −field 34 × g | *assumed* |
| heave | change of velocity along the car's up direction | geometry |
| roll, pitch, yaw | how the right and forward vectors move between two packets | geometry: certain **if** fields 11–13 are the right vector and 14–16 the forward vector, as the list says |
| rest when paused | the run time stands still | observation of the format: *assumed* |

## Project CARS 2 / Automobilista 2 — `pcars2`

Published: Slightly Mad Studios, `SMS_UDP_Definitions.hpp` (Project CARS 2 API). UDP, port 5606,
packed structures with a 12-byte base; packet type at byte 10.

| Packet | Type | Size | Read |
|---|---|---|---|
| Telemetry | 0 | 559 | brake (29), throttle (30), speed (36), RPM and maximum (40, 42), gear (45), angular velocity (88), local acceleration (100), terrain under each wheel (140) |
| Game state | 4 | 24 | game state (14: 2 = playing), rain density (17), snow density (18) |

| Value | From | Basis |
|---|---|---|
| surge, sway, heave | −z, −x, y of the local acceleration | *assumed* (axes read as X right, Y up, Z backwards) |
| roll, pitch, yaw | −z, −x, y of the angular velocity | *assumed* |
| rest unless playing | game state | *stated* (table of game states) |
| rain, snow | densities ÷ 255 | scale *assumed* |

## Assetto Corsa remote telemetry — `assetto`

Published: Kunos Simulazioni, "AC UDP Remote Telemetry". The game listens on UDP port 9996; this
program sends the handshake (operation 0), then "subscribe update" (1), and "dismiss" (3) when it
ends. `RTCarInfo`: 328 bytes, begins with the letter `a` and its own size.

Read: speed in m/s (16), in-pit flag (24), `accG_vertical`, `accG_horizontal`, `accG_frontal` (28,
32, 36), gas, brake, RPM (56, 60, 68), gear (76), slip ratio ×4 (132).

| Value | From | Basis |
|---|---|---|
| surge, sway, heave | frontal × g, −horizontal × g, vertical × g | *assumed* |
| yaw | sway ÷ speed (above 5 m/s) | physics of a car that is not sliding |
| roll, pitch | — | not in this format; the tilt of the platform still follows the g-forces |

The original Assetto Corsa only; Assetto Corsa Competizione has a different interface, which this
program does not read.

## Live for Speed OutSim / OutGauge — `outsim`

Published: Live for Speed, `docs/InSim.txt`. `OutSimPack`: 64 bytes (68 with an id); `OutGaugePack`:
92 bytes (96). Both may be sent to the same port.

| Value | From | Basis |
|---|---|---|
| surge, sway, heave | the acceleration (world axes: "X and Y axes are on the ground, Z is up") turned into the car's by the heading | *stated* axes; that acceleration is in world axes is the usual reading, *assumed* |
| yaw, pitch, roll | changes of `Heading` ("anticlockwise from above"), −`Pitch` ("anticlockwise from right"), `Roll` ("anticlockwise from front") | *stated* |
| revs, pedals, gear | OutGauge | *stated* |

## BeamNG MotionSim / OutGauge — `beamng`

Published: documentation.beamng.com, Modding ▸ Protocols. MotionSim: the four characters `BNG1` and
21 floats (88 bytes): position, velocity, acceleration ("gravity not included"), the vehicle's up
vector, roll / pitch / yaw angles, their rates and accelerations. OutGauge as above.

| Value | From | Basis |
|---|---|---|
| surge, sway, heave | accY, −accX, accZ | *assumed*: the page gives no axes |
| roll, pitch, yaw | rollVel, −pitchVel, yawVel | *assumed* |

Of all sources this is the one whose directions are least certain. Check all six.

## X-Plane data output — `xplane`

Published: Laminar Research — the Data Output screen of X-Plane and "Exchanging Data with
X-Plane". `DATA` and one byte, then rows of 36 bytes: a row number and eight floats.

| Row | Used |
|---|---|
| 3 speeds | [0] indicated airspeed (knots) → wind |
| 4 Mach, VVI, g-load | [4] normal, [5] axial, [6] side |
| 16 angular velocities | [0] Q, [1] P, [2] R (rad/s) |
| 17 pitch, roll, headings | [0] pitch, [1] roll (degrees) |
| 37 engine RPM | [0] |

| Value | From | Basis |
|---|---|---|
| roll, pitch, yaw | P, −Q, −R | *convention* (aeronautics: P right wing down, Q nose up, R nose right) |
| heave | (normal − 1) × g | *convention* (1 g in level flight) |
| surge, sway | axial × g, −side × g | *assumed* |

## FlightGear generic protocol — `flightgear`

Published: FlightGear, the generic protocol (`README.protocol`, wiki "Generic protocol"). The
format is ours: `fourdvr live sender flightgear -o fourdvr.xml` writes the protocol file — one
comma-separated text line per frame, beginning with `4DFG1`. FlightGear is started with
`--generic=socket,out,60,HOST,47011,udp,fourdvr`.

Properties sent: `/accelerations/pilot/{x,y,z}-accel-fps_sec`, `/orientation/{roll,pitch,yaw}-rate-degps`,
`/orientation/{roll,pitch}-deg`, `/velocities/airspeed-kt`, `/velocities/groundspeed-kt`,
`/engines/engine/rpm`, `/gear/gear/wow`, `/sim/freeze/master`, `/environment/rain-norm`,
`/environment/snow-norm`.

| Value | From | Basis |
|---|---|---|
| surge, sway, heave | x, −y, −z − g (feet → metres) | *convention* (body axes X forward, Y right, Z down; the pilot acceleration includes the reaction to gravity) |
| roll, pitch, yaw | roll rate, −pitch rate, −yaw rate | *convention* |
| rest when paused | `/sim/freeze/master` | *stated* |
| rain, snow | `rain-norm`, `snow-norm` | *stated* (0 to 1) |

## Gran Turismo 7 — `gt7` (experimental)

**Not published by the game's maker.** The packet was worked out by the community (GTPlanet; the
open *gt7telemetry* script by Bornhall and those it credits). It may stop working with any update
of the game. This program asks the console for data by sending the letter `A` to its UDP port 33739
once a second and listens on port 33740. Packets are 296 bytes, encrypted with Salsa20 (the key
is the first 32 bytes of a fixed text; the nonce comes from four bytes of the packet); after
decryption the packet begins with the number 0x47375330.

Read: angular velocity (0x2C), engine speed (0x3C), speed in m/s (0x4C), frame counter (0x70), rev
limit (0x8A), flags (0x8E: bit 0 on track, bit 1 paused), gear (0x90), throttle and brake (0x91, 0x92).

| Value | From | Basis |
|---|---|---|
| surge | change of speed | physics |
| sway | speed × turning rate | physics of a car that is not sliding |
| heave | — | not in the packet |
| roll, pitch, yaw | −z, x, −y of the angular velocity | *assumed* |

The Salsa20 implementation here is checked against an independent one (PyCryptodome) in the tests.

---

## Not supported, and why

* **Shared-memory interfaces** (Assetto Corsa Competizione, iRacing, rFactor 2, Euro Truck
  Simulator 2 …): they exist only on the game's own Windows PC. A small relay there that sends the
  open live format is the way; none is included.
* **Microsoft Flight Simulator (SimConnect), DCS, War Thunder**: no documented UDP output that this
  program could read directly. The open live format is the bridge here too.
* **Any game without telemetry**: use the picture (`screen`) or the sound (`sound`).
