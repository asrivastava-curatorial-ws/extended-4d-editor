# Effects at the venue

How a theatre gets wind, water, rain, air, fog, heat, bubbles, snow, strobe, scent, lighting and
bass out of a programme: the **venue patch**, the outputs (network DMX, USB DMX, ESP32 boards,
sound), the limits, commissioning, and the safety of effect devices.
For making programmes see [USER_GUIDE.md](USER_GUIDE.md); for the file format
[EFFECTS_FORMAT.md](EFFECTS_FORMAT.md).

Effects are optional. A show PC without an `[effects]` section plays motion only.

## 1. How it fits together

```
programme (.motion)                 venue patch (patch.toml)                    devices
 "wind 60 % from 0:12"       ─►  wind = dimmer, universe 2, address 1 + 8·k  ─►  fan of platform k
 "water burst at 0:31"       ─►  water = switch, lead 0 s, at most 2 s        ─►  valve
 "fog 8 s at 1:10"           ─►  fog = level, lead 1 s, at most 4 s, 10 s off ─►  fog machine
 "scent cartridge 2 at 1:40" ─►  scent-sea = switch, only slot 2, lead 2 s    ─►  dispenser, channel 2
 bass track                  ─►  [audio] device, channels                      ─►  amplifier ─► shakers
```

The programme never mentions DMX addresses (except raw *DMX channel* tracks). The patch belongs
to the venue and is the only file that changes when a device is added, replaced or re-addressed.
An effect type for which the venue has no device is simply not played there; that is not an error.

At show time the show service evaluates the patch 40 times per second:

* a **platform** fixture (wind, water, rain, air, heat — one per seat group) follows *its own*
  platform and is off whenever that platform is not playing: before the start, paused, stopped,
  faulted;
* a **theatre** fixture (fog, bubbles, snow, strobe, scent, room lighting) and the bass follow the reference
  platform's timeline, or the first platform that is playing if the reference is not;
* on PAUSE, STOP, a fault, and when the service ends, every fixture goes to its off value (fans
  follow their ramp), and three dark frames are sent before the output closes.

## 2. Getting a patch

```bash
fourdvr fx example-patch -o patch.toml          # the commented example (network DMX, sound card)
fourdvr fx example-patch --esp32 -o patch.toml  # ... or the one with everything through ESP32 boards
fourdvr fx check-patch patch.toml --platforms PLATFORM-01,PLATFORM-02
```

`check-patch` validates the file and prints the address table — keep a printout in the rack:

```
output sacn, 40 frames/s -> multicast
  U1   ch 1       fog-main                   fog        level        on ≤ 4 s, off ≥ 10 s, duty ≤ 25 % / 60 s; lead 1 s
  U1   ch 10      strobe-front               strobe     level        on ≤ 5 s, off ≥ 1 s; ≤ 3 flashes/s (software flash)
  U1   ch 20      scent-pine                 scent      switch       on ≤ 6 s, off ≥ 15 s; lead 2 s; only slot=1
  U2   ch 1       wind@PLATFORM-01           wind       level        ramp 0.5/0.5 s; lead 0.4 s
  U2   ch 2       water@PLATFORM-01          water      switch       on ≤ 2 s, off ≥ 1 s, total ≤ 20 s per show
  U2   ch 9       wind@PLATFORM-02           wind       level        ramp 0.5/0.5 s; lead 0.4 s
  ...
```

The show service uses it through the controller configuration:

```toml
[effects]
patch = "effects/patch.toml"          # relative to the controller configuration file
enabled = true                        # false: keep everything configured, switch all effects off
reference_platform = "PLATFORM-01"
```

`fourdvr controller check-config -c controller.toml` prints the same table for the platforms of
that controller. A patch with errors does not stop the service: it starts, plays motion only and
reports the error in every platform's status on the Control Server.

## 3. Outputs

```toml
[output]
protocol = "sacn"          # sacn | artnet | enttec-pro | esp32 | none
refresh_hz = 40            # 1..44 frames per second and universe
```

| Protocol | Section | Keys |
|---|---|---|
| **sACN** (streaming ACN, ANSI E1.31) | `[output.sacn]` | `source_name` (shown by nodes and consoles), `priority` (0–200, default 100), `multicast` (default true: 239.255.*hi*.*lo* per universe), `unicast` (list of node addresses, in addition to or instead of multicast), `interface_ip` (local address of the network card to send from), `port` (5568) |
| **Art-Net** | `[output.artnet]` | `targets` (node addresses; default broadcast), `universe_base` (Art-Net port-address of DMX universe 1, default 0: universe 1 → port-address 0), `port` (6454), `interface_ip` |
| **USB DMX** (Enttec DMX USB Pro compatible) | `[output.enttec]` | `port` (`/dev/ttyUSB0`, `COM3`), `universe` (the one universe it carries). Needs the `pyserial` package (`pip install ".[show]"`; in the AppImage) and the service user in group `dialout` |
| **ESP32 boards** | `[output.esp32]` | section 3.1 |
| none | — | nothing is sent (the bass output can still be used) |

A USB interface that is missing when the service starts, or is unplugged during a show, is
reported in the status and looked for again every five seconds.

Notes for the network:

* Universes are numbered from 1. Unused universes are not sent.
* sACN: the service announces its universes (universe discovery) and sends *stream terminated*
  when it stops, so that nodes release at once. With multicast, switches should have IGMP snooping
  with a querier; with a single node unicast is simpler.
* Put effect devices on their own network or VLAN where possible, and give the show PC a fixed
  address there (`interface_ip`).
* Set every node and DMX device to **switch off on loss of signal** ("hold last look" off). The
  service sends dark frames when it stops, but a pulled cable sends nothing.
* `fourdvr fx monitor` on any PC of that network prints what arrives (sACN or `--protocol artnet`).

### 3.1 Through ESP32 boards

With `protocol = "esp32"` the show service sends the DMX levels — and, if wanted, the bass — to
ESP32 boards, which put them on a DMX line and an audio output of their own. Typically that is
the board that already drives a platform's actuators; a board without actuators, only for effect
devices, works the same way. Wiring, firmware and the packets: [ESP32_EFFECTS_LINK.md](ESP32_EFFECTS_LINK.md).

```toml
[output]
protocol = "esp32"

[output.esp32]
port = 47002                       # UDP port of the effects link in the firmware
theatre_platform = "PLATFORM-01"   # the theatre's devices are on the DMX line of this platform's board ...
# theatre = "192.168.10.150"       # ... or on a board of their own: its address instead (not both)

[output.esp32.platforms]           # the board of each platform - see below who needs this table
PLATFORM-01 = "192.168.10.101"
PLATFORM-02 = "192.168.10.102"

[output.esp32.audio]               # bass shakers / subwoofer amplifiers on the boards' audio outputs
enabled = true
bass_channels = [1, 2]             # 1 = left output, 2 = right output
vibration_channels = [1]
vibration_frequency_hz = 45
bass_gain_db = 0.0
vibration_gain_db = -6.0
latency_offset_s = 0.0
```

* **Every platform's board has a DMX line of its own.** A platform fixture is on the line of its
  platform's board, at the same address on every platform; `universe`, `address_step` and
  `universe_step` are not needed. Theatre fixtures are on line 1 (`universe = 1`) of the theatre's
  board, or on its second line (`universe = 2`) if the board has one. Where the theatre's devices
  share a line with a platform's, their addresses must not overlap — `check-patch` says so.
* **The show service finds the boards by itself:** a platform's board is the address of that
  platform's motion link (`udp://…` in the controller configuration). List a platform in
  `[output.esp32.platforms]` only if its effects are on another board. The Editor's LIVE mode and
  the `fourdvr fx` tools know no motion links: for them, list every board.
* **What is not there is left out.** Once a second the service asks every board what it has: a DMX
  line or two, one or two audio outputs. A board that does not answer, a board without a DMX line
  or without an audio output, a platform without a board address: its part of the effects is not
  sent, nothing is reported as an error, and the platforms and all other boards run as always. A
  board that is switched on later is found and driven within three seconds.
  `fourdvr fx nodes --patch patch.toml --platforms …` lists what answers.
* **Sound.** A platform's board plays the bass and the seat vibration of *that platform's* show (so
  a shaker stops with its platform); a board for theatre devices only plays the theatre's. The
  board's output is 16-bit sound through an I2S converter at the audio rate of the firmware (4 kHz
  as built: enough for everything a shaker or subwoofer reproduces, and little network traffic).
  `[audio]` (the PC's own sound card) and `[output.esp32.audio]` can be used together.
* **If a board falls silent or the PC does:** a board that receives no DMX data for one second
  switches its line dark; sound stops when its buffer (0.1 s) runs empty. A board whose
  emergency-stop input is active sends a dark line and silence and says so in its status.
* **Seats without a motion platform.** Effects follow a platform's show. A seat group that has
  effect devices but no moving platform is still a `[[platforms]]` entry of the controller — with a
  platform profile whose `driver` is `"simulation"` (`config/platforms/simulation-6dof.toml`), so
  that nothing waits for actuators — and its board is listed in `[output.esp32.platforms]`. (A platform
  that *should* move and whose actuator board does not answer is a fault, as it always was: it is
  shown on the Control Server, its own effects stay off with it, and all other platforms and the
  theatre's effects carry on.)
* The link is plain UDP on the venue's own network, like the motion link: no encryption, no
  authentication. Keep that network separate from visitors' networks.

## 4. Fixtures

One `[[fixtures]]` table per device type:

| Key | Default | Meaning |
|---|---|---|
| `id` | — | name of the fixture (a–z, 0–9, `-`, `_`); shown in status, tools and the Editor |
| `effect` | — | the effect type it reacts to: `wind`, `water`, `rain`, `air`, `fog`, `heat`, `bubbles`, `snow`, `strobe`, `scent`, `vibration`, or any other name used by programmes |
| `scope` | `theatre` | `theatre`: one device for the room. `platform`: one per platform |
| `universe`, `address` | 1, 1 | DMX universe and start address (1–512) |
| `profile` | `dimmer` | channel layout: `dimmer` (one level channel), `switch` (one on/off channel), `level-rate` (level + flash-rate channel). Or give `channels` |
| `channels` | — | list of channel tables (below) instead of a profile |
| `lead_s` | 0 | send this much *earlier* than the timeline (0–30 s): the time the device needs until the audience notices it |
| `match` | — | react only to cues with these parameter values, e.g. `{ slot = 2 }` for scent cartridge 2 |
| `flash` | `fixture` | strobe: `fixture` = the device flashes itself (send level and rate); `software` = the service pulses the level, all such strobes in step (below) |
| `flash_on_s` | 0.03 | software flash: length of one flash (0.01–0.5 s; at least one DMX frame, at most half a period) |
| `label` | — | free text |
| `[fixtures.limits]` | per type | section 5 |

Platform fixtures — where each platform's copy is:

| Key | Meaning |
|---|---|
| `address_step` | platform *k* (0, 1, 2 … in the order of the controller's `[[platforms]]`) is at `address + k × address_step` |
| `universe_step` | … and/or in universe `universe + k × universe_step` (one universe per platform) |
| `addresses` | explicit address per platform id: `{ "PLATFORM-01" = 1, "PLATFORM-07" = 49 }` |
| `platforms` | only these platforms have the device (default: all) |

With `platforms`, *k* is the position in that list. **If the platforms are split over several
show-service processes** (several controller configurations), list all platform ids in `platforms`
— or use `addresses` — so that every process computes the same address for the same platform; put
the theatre fixtures and `[audio]` in the patch of **one** process only, and never let two
processes send the same universe.

Channels (`channels = [ { … }, { … } ]`), each with `offset` from the fixture address (default 0):

| `role` | DMX value | Keys |
|---|---|---|
| `level` | `off` while the effect is off, otherwise `min` + (`max` − `min`) × level^`gamma` | `min` (0), `max` (255), `off` (0), `gamma` (1.0) |
| `switch` | `on` while the level is above `threshold`, otherwise `off` | `on` (255), `off` (0), `threshold` (0.01) |
| `rate` | the cue's flash rate, `hz_min`…`hz_max` mapped to `min`…`max` (strobe) | `hz_min` (1), `hz_max` (20), `min`, `max`, `off` |
| `static` | a fixed `value` while the effect is active (`when = "always"`: permanently) | `value`, `when`, `off` |

Typical devices:

```toml
# Fan on a DMX dimmer pack or DMX-to-0-10 V converter; it stalls below DMX 40
[[fixtures]]
id = "wind"
effect = "wind"
scope = "platform"
universe = 2
address = 1
address_step = 8
lead_s = 0.4
channels = [ { role = "level", min = 40, max = 255 } ]

# Solenoid valve on a DMX relay
[[fixtures]]
id = "water"
effect = "water"
scope = "platform"
universe = 2
address = 2
address_step = 8
profile = "switch"

# Fog machine with one output channel
[[fixtures]]
id = "fog-main"
effect = "fog"
universe = 1
address = 1
profile = "dimmer"
lead_s = 1.0

# Strobe with its own rate channel: level on channel 10, rate on 11 (1-20 flashes/s over the DMX range)
[[fixtures]]
id = "strobe-rig"
effect = "strobe"
universe = 1
address = 10
profile = "level-rate"

# LED flood used as lightning, pulsed by the service: dimmer on channel 30, white on 31-33
[[fixtures]]
id = "lightning"
effect = "strobe"
universe = 1
address = 30
flash = "software"
channels = [ { role = "level" },
             { role = "static", offset = 1, value = 255 }, { role = "static", offset = 2, value = 255 },
             { role = "static", offset = 3, value = 255 } ]

# Scent dispenser with one relay channel per cartridge
[[fixtures]]
id = "scent-pine"
effect = "scent"
universe = 1
address = 20
profile = "switch"
match = { slot = 1 }
lead_s = 2.0

# Overhead rain nozzles of a seat group (a valve on a DMX relay); rain has default limits, see section 5
[[fixtures]]
id = "rain"
effect = "rain"
scope = "platform"
universe = 2
address = 3
address_step = 8
profile = "switch"

# Warm air for fire scenes: a fan heater on a DMX dimmer, never driven above 80 %
[[fixtures]]
id = "heat"
effect = "heat"
scope = "platform"
universe = 2
address = 4
address_step = 8
lead_s = 0.5
[fixtures.limits]
max_level = 0.8

# Bubble machine and snow machine with one relay channel each
[[fixtures]]
id = "bubble-machine"
effect = "bubbles"
universe = 1
address = 40
profile = "switch"

[[fixtures]]
id = "snow-machine"
effect = "snow"
universe = 1
address = 41
profile = "switch"

# A venue-specific effect: programmes use a lane of type "confetti" (Other effect type… in the Editor)
[[fixtures]]
id = "confetti-cannon"
effect = "confetti"
universe = 1
address = 42
profile = "switch"
[fixtures.limits]
max_on_s = 1.0
min_off_s = 60.0
max_total_on_s = 2.0
```

Raw *DMX channel* tracks of a programme are sent to the universe and channel written in the
programme, underneath the fixtures (a fixture on the same channel wins). `allow_raw_dmx = false`
at the top of the patch ignores them.

## 5. Limits

Every fixture has activation limits. A programme (or a programming mistake, or a stuck cue) can
never drive a device harder than its patch allows.

| Key | Meaning | 0 means |
|---|---|---|
| `max_level` | highest level (0–1) | — |
| `max_on_s` | longest continuous activation; then the fixture is switched off | unlimited |
| `min_off_s` | pause enforced after every activation | none |
| `duty_window_s`, `max_duty` | at most this fraction of any window of that length | off |
| `max_total_on_s` | total on-time per show run (a loop restart starts a new run) | unlimited |
| `ramp_up_s`, `ramp_down_s` | time for 0 → 100 % and back (soft start / stop of fans) | immediate |
| `max_rate_hz` | strobe: highest flash rate, whatever the cue asks for | — |

Defaults by effect type (a fixture's `[fixtures.limits]` overrides single values):

| Effect | Default limits |
|---|---|
| wind | ramp 0.5 s up and down |
| water | on ≤ 2 s, off ≥ 1 s, total ≤ 30 s per show |
| rain | on ≤ 5 s, off ≥ 5 s, total ≤ 40 s per show |
| air | on ≤ 1 s, off ≥ 0.3 s |
| fog | on ≤ 5 s, off ≥ 10 s, duty ≤ 25 % of any minute |
| heat | on ≤ 6 s, off ≥ 10 s, duty ≤ 25 % of any minute |
| bubbles | on ≤ 10 s, off ≥ 5 s, total ≤ 60 s per show |
| snow | on ≤ 10 s, off ≥ 5 s, total ≤ 60 s per show |
| strobe | on ≤ 5 s, off ≥ 1 s, at most **3 flashes per second** (hard ceiling of the software: 25) |
| scent | on ≤ 6 s, off ≥ 15 s |
| others | none — set what the device needs |

These defaults are cautious starting values chosen for this software. Apart from the strobe rate
(section 8) they are **not taken from a standard**: the right numbers depend on the device, its
distance from the visitors and the room, and are for the venue to decide (section 7, step 5).
`check-patch` warns about a fog, water, rain, heat, bubble, snow, scent or strobe fixture that has
no activation limit at all.

**While a programme is being made** the Editor checks it against these defaults (button
**3 Check**, and by itself after every edit) and marks in red what they would cut — as soon as the
venue's patch is loaded in the Editor, against that venue's limits instead. `fourdvr fx check`
does the same on the command line.

**Software flash.** Flashes are counted along the programme from the start of the cue, so all
strobes of a venue flash in step (on different show PCs: as closely as the PCs' clock
synchronisation and one DMX frame allow). Each flash is shown once: a DMX frame that comes a few
milliseconds late delays a flash by that frame instead of dropping it, two flashes are always
separated by a dark frame, and a new flash never starts sooner after the previous one than
`max_rate_hz` allows (less one and a half DMX frames of timing tolerance) — in a show, and just as
much when somebody seeks, loops or drags the playhead in the Editor with LIVE on. A playhead that
stands still on a strobe cue gives one flash. Software flashes are made of whole DMX frames: with
a ceiling above a quarter of `refresh_hz` (10 per second at 40 frames) they become uneven,
`check-patch` says so, and the device should flash itself.

With `flash = "fixture"` the software only sends level and (capped) rate; when and how evenly the
device flashes is the device's business, and it keeps flashing for as long as the level is held.

Levels below 1 % count as off. A fixture held off by a limit is listed in the platform status on
the Control Server (`effects.limited`), in the Editor's *Live* tab, and by `fourdvr fx play`.

What a venue's limits do to a particular programme is shown before the show:

```bash
fourdvr fx check ride.motion --patch patch.toml --platforms PLATFORM-01,PLATFORM-02
  fog-main      fog    on  7.0 s of 11.0 s requested, peak 100 %  - LIMITED: maximum on-time 4.0 s
                         01:14.03 - 01:18.00  held off: maximum on-time
  no fixture for effect type 'vibration': its cues are ignored at this venue
```

Operator switches — an effect type can be switched off or turned down for the whole venue
(a cold day: less wind; a school class: no water):

```toml
[types.water]
enabled = false
[types.wind]
master = 0.7
```

The service reads the patch when it starts: `sudo systemctl restart fourdvr-motion` after a change
(between shows — the platforms return to park).

## 6. Bass and seat vibration

Subwoofers and bass shakers are driven from a sound card or USB audio interface of the show PC,
locked to the show timeline (the headsets keep their own audio).

```toml
[audio]
enabled = true
device = "USB Audio"           # part of the device name (fourdvr fx devices); empty = system default
sample_rate_hz = 48000
channels = 2
bass_channels = [1, 2]         # outputs that carry the programme's bass track (1 = left, 2 = right, ...)
vibration_channels = [2]       # outputs that also carry the synthesised seat vibration
vibration_frequency_hz = 45    # tone of the vibration effect (10-200 Hz; shakers are strongest near their resonance)
bass_gain_db = 0.0
vibration_gain_db = -6.0
latency_offset_s = 0.0         # positive: play earlier
buffer_ms = 40
```

* **Bass track**: the low-passed sound of the programme, embedded in the `.motion` file by the
  Editor. Programmes without one are silent on these outputs.
* **Seat vibration**: the *Seat vibration* track of a programme becomes a tone of
  `vibration_frequency_hz` whose loudness follows the track. Route it to the shaker channel.
* The sum is soft-limited, never hard-clipped. Set the level at the amplifier with the loudest
  programme; use the amplifier's high-pass and limiter to protect the shakers.
* `latency_offset_s`: play a programme with a clear bass hit, compare with the headset audio, and
  enter the difference (positive if the bass is late).
* The service user must be in group `audio` (the supplied unit does that) so that the sound card
  can be opened without a desktop session. `fourdvr fx devices` lists the outputs it sees.
* Needs the `miniaudio` package (`pip install ".[show]"`; in the AppImage). Without it, or without
  the device, the status says so and everything else runs.

A seat-vibration *DMX* device can be patched instead (a fixture with `effect = "vibration"`).

Shakers on the audio outputs of ESP32 boards are configured in `[output.esp32.audio]`
(section 3.1); the keys mean the same as here.

## 7. Commissioning

Do this with the room closed to visitors. Have the emergency stop within reach.

1. **Wiring and addresses.** Set node and device addresses as in the address table. Set every
   device to switch off on DMX loss.
2. **See the data** before connecting consumers: `fourdvr fx monitor -u 1 -u 2` on a laptop in the
   effects network, then on the show PC
   `fourdvr fx fire fog-main --patch patch.toml --seconds 1` — the monitor must show that channel
   and nothing else.
3. **One device at a time**: `fourdvr fx fire <fixture> --patch patch.toml [--level 0.5] [--seconds 2]`
   (platform fixtures: `wind@PLATFORM-03`). Check that the right device at the right seat reacts.
   The Editor's *Live* tab does the same with a button per fixture.
4. **Lead times**: fire each device and time how long it takes until a person in a seat notices
   it (fan: until the air arrives; fog: until it is visible from the seats; scent: until it is
   smelled). Enter that as `lead_s`.
5. **Limits**: decide `max_on_s`, `min_off_s`, totals and the fog duty with the device manuals
   and with what the room tolerates (fog density, wet seats, scent build-up). Check the strobe
   rate with the considerations in section 8.
6. **Levels**: `min` of fans (the lowest DMX value at which they turn reliably), `max_level` where
   full power is too much.
7. **A programme**: `fourdvr fx play ride.motion --patch patch.toml --platforms PLATFORM-01,PLATFORM-02`
   plays the effects of a programme without the server and without motion; one line per second
   shows the levels and what is held by a limit. Ctrl+C blacks out.
   `fourdvr fx blackout --patch patch.toml` sends zero to every universe of the patch.
8. **Bass**: `fourdvr fx devices`, enable `[audio]`, play a programme with a bass track, set the
   amplifier, measure `latency_offset_s`.
   **ESP32 boards**: `fourdvr fx nodes --patch patch.toml --platforms …` must list every board with
   what it has; then steps 2 to 8 work as described (the monitor of step 2 is replaced by a DMX
   tester or the first device on the board's line). First power-on of a board:
   [ESP32_EFFECTS_LINK.md](ESP32_EFFECTS_LINK.md).
9. **With the show service**: add `[effects]` to the controller configuration, restart, run
   `fourdvr controller check-config`, play a programme from the Control Server with nobody in the
   seats, then with staff. The platform details on the server show the effects state.
10. Write down the result: address table, limits, lead times, the date, who tested.

The whole chain can be rehearsed on one PC without any device:
`python integration/run_effects_integration.py` (network DMX) and
`python integration/run_esp32_integration.py` (ESP32 boards, simulated by the firmware's own code)
— [integration/README.md](../integration/README.md).

## 8. Safety of effect devices

**DMX, Art-Net, sACN, the ESP32 effects link and audio are not safety functions.** They carry no guarantee of delivery
and no monitoring that would satisfy a safety requirement. This software's limits reduce the
consequences of programming and software errors; they do not make a device safe. Everything in
this section is the venue's responsibility and needs the venue's own risk assessment.

* **Emergency stop.** Power to valves, pumps, fog, snow and bubble machines, heaters, scent units,
  strobes and air supply that must stop in an emergency is removed by the emergency-stop circuit
  (contactor in the supply of the effect devices), independent of the show PC and of any ESP32.
  After an emergency stop the show service has stopped the timeline as well, and a board whose
  emergency-stop input is active sends a dark line, but neither is what makes the devices safe.
* **Loss of signal.** Devices must fail to *off*. A valve that stays open when the DMX cable is
  pulled is a flood.
* **Strobe and flashing light.** Flashing light can provoke seizures in people with
  photosensitive epilepsy. The Epilepsy Society (UK) gives 3–30 flashes per second as the range
  that most commonly triggers seizures and quotes the Health and Safety Executive's advice to keep
  strobe lighting at or below four flashes per second
  (<https://epilepsysociety.org.uk/sites/default/files/2025-04/PhotosensitiveepilepsyApril2025.pdf>).
  The default ceiling here is 3 per second. In a headset the visitor cannot look away. Keep all
  flashing sources of the room synchronised (`flash = "software"` does that), keep bursts short,
  post a warning at the entrance and in the briefing, and do not raise `max_rate_hz` without a
  documented assessment under the rules that apply to your venue. `check-patch` prints a warning
  for every fixture above 3 per second. The ceiling is a property of each fixture: two strobes
  that are not flashed in step, or a strobe together with flashing picture content, add up.
* **Water.** Fine mist near headsets, electrical equipment and floors; slip hazard; water quality
  and stagnation in lines and tanks (Legionella risk: follow the applicable water-hygiene rules,
  flush and disinfect as the manufacturer prescribes); `max_total_on_s` limits how wet a visitor gets.
* **Fog and haze.** Fluid suitable for the machine; visibility of escape routes; fire detection
  (smoke detectors react to fog — coordinate with the fire-safety officer); people with asthma;
  heat of the machine's nozzle.
* **Heat.** The "heat" effect is warm air or a heat lamp for fire scenes — never open flame, never
  pyrotechnics. The device needs its own thermal cut-out and a guard; mount it so far from skin,
  hair, headsets, seat covers and fog fluid that nothing can be burnt or ignited even if it stays
  on (a relay can weld shut): the software limit is no protection against that. Mind the
  temperature of the air at the visitor's face, children, and people who cannot move away.
* **Rain, snow and bubbles.** Like water: wet and slippery floors and steps, fluid in eyes and on
  headset lenses, hygiene of tanks and lines; snow and bubble fluid as the manufacturer specifies;
  cleaning between shows.
* **Scent.** Allergies and sensitivities; ventilation rate of the room decides how long a scent
  stays; announce it.
* **Wind and air blasts.** Guarded fans; loose objects, long hair and scarves; eyes and ears —
  no nozzle pointed at the face at short distance; compressed-air pressure limited at the source.
* **Bass shakers and subwoofers.** Sound-pressure limits for visitors and staff; manufacturer's
  advice for people with implants; mounting that cannot work loose.
* **Operation.** Give operators a way to switch each effect type off without stopping the show
  (`[types.<name>] enabled = false`), a visible status (Control Server device details), and a
  routine for checking the devices before the first show of the day.

## 9. Status and logs

Every platform's STATUS to the Control Server carries an `effects` block when effects are
configured:

| Field | Meaning |
|---|---|
| `running` | the effects thread is alive |
| `playing` | this platform's effects are being output now |
| `programme_has_effects` | the loaded programme has an effects section |
| `programme_error` | the effects section of the programme is damaged (it is ignored; motion plays) |
| `output`, `output_errors` | DMX transport and its send errors |
| `errors`, `last_error` | internal errors of the effects thread (the outputs went dark each time) |
| `overruns` | DMX frames that could not be sent on time (an overloaded PC) |
| `limited` | fixtures currently held off by a limit |
| `theatre_follows` | the platform whose timeline the theatre fixtures and the bass follow |
| `audio` | sound output: running, device, error, re-synchronisations |
| `nodes` | effects through ESP32 boards: every board with its address, whether it answers, what it has (`dmx`, `audio`), DMX frames sent, time-outs, sound buffer, emergency-stop state. A board that does not answer is listed with `present: false`; that is information, not an error |
| `error` | why effects are not running at all (disabled, patch error) |

The service log (`journalctl -u fourdvr-motion`) records the same events.
