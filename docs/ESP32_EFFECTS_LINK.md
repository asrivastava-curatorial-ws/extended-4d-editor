# DMX devices and bass shakers through ESP32 boards

The show service can send a venue's effects to **ESP32 boards** instead of (or beside) the PC's
own outputs: each board puts DMX levels on a DMX512 line and bass sound on an audio output. It can
be the same board that moves a platform's actuators, or a small second board that only does
effects. This document is the reference for that: what goes where, the wiring, the firmware
variants, the first power-on, and the packets of the link ("4DFX" 1.0).

For the venue patch that uses it (`protocol = "esp32"`) see [EFFECTS_GUIDE.md](EFFECTS_GUIDE.md)
§3.1; for the motion link of the same board [ESP32_PROTOCOL.md](ESP32_PROTOCOL.md).

> **State of this part.** The link, the show service's side of it and the firmware's logic are
> tested on a PC, the firmware's portable core byte for byte against the Python side
> ([integration/README.md](../integration/README.md)). The firmware has **not been compiled for an
> ESP32 and not run on a board**, and no RS-485 driver, DMX device, audio converter, amplifier or
> shaker was connected. Treat the wiring and pin tables as a design to be proven with the
> first-power-on steps below, not as a tested product.

## 1. Who does what

```
 show PC: fourdvr controller run                            one ESP32 per platform (or per rack)
┌──────────────────────────────────────────┐   UDP 47000   ┌──────────────────────────────────────────┐
│ motion: 6 actuator positions, 100-200 /s ├──────────────►│ actuator bridge ─► 6 servo drives        │
│                                          │   UDP 47002   │                                          │
│ effects: the programme through the venue ├──────────────►│ DMX frames ─► UART ─► RS-485 ─► DMX line │
│   patch and its limits, 40 frames /s     │               │ bass sound ─► I2S ─► converter ─► amplifier ─► shaker
│ bass and vibration, 4 kHz, in step       │◄──────────────┤ "I have: a DMX line, 2 audio outputs"    │
└──────────────────────────────────────────┘   HELLO/INFO  └──────────────────────────────────────────┘
```

* **The PC plays, the boards output.** The programme (`.motion`, with its effects and its bass
  track) stays on the PC. The boards store nothing, need no file and no update when a programme
  or the venue patch changes, and can be exchanged without copying anything onto them.
* **All limits are applied on the PC**, by the same engine as for every other output: longest time
  on, pauses, duty, totals, flash rate. A board outputs the levels it is sent.
* **Two links, two ports.** Motion stays on its own link (port 47000, unchanged since 1.0). The
  effects link has its own port (47002). A board with the motion-only firmware never sees effects
  packets, and nothing on the effects link can delay or disturb a motion packet.
* **Ask, then send.** Once a second the show service asks every board it knows what it has. It
  sends DMX only to boards that report a DMX line, sound only to boards that report an audio
  output. A board that does not answer is asked again every second and otherwise left alone.
  Missing hardware is never an error (it is visible in the status, nothing more).

What a board does by itself, without the PC:

| Situation | The board |
|---|---|
| no DMX packets for 1 s | sends all 512 slots as zero (it keeps sending DMX frames: devices see a live, dark line) |
| sound packets stop | plays what is buffered (0.1 s), then silence, faded so that nothing clicks |
| its emergency-stop input is active | dark DMX line and silence, whatever arrives; says so in its status |
| power-on | dark line, silence, until the first packets arrive |

None of this is the safety function: power to devices that must stop in an emergency is switched
by the emergency-stop circuit (README.md, EFFECTS_GUIDE.md §8).

## 2. Hardware

| Part | For | Notes |
|---|---|---|
| ESP32 board with wired Ethernet (WT32-ETH01, Olimex ESP32-POE) | everything | the boards the actuator bridge already uses. Wi-Fi works for an effects-only board (below) |
| RS-485 transceiver for 3.3 V logic (MAX3485, SN65HVD72, THVD1410 …), better an isolated module | DMX line | a MAX485 / SN75176 needs 5 V supply; its input accepts the ESP32's 3.3 V level in practice, but check the data sheet |
| I2S audio converter with line output (PCM5102A, UDA1334A modules) | bass shakers, subwoofer | feeds an **amplifier**. A MAX98357A (I2S amplifier, 3 W) drives only a very small exciter. Never connect a shaker to the converter or the ESP32 |
| 5-pin (or 3-pin) XLR socket, 120 Ω terminator at the end of the line | DMX line | pin 1 screen, pin 2 data −, pin 3 data + |

DMX line (transmit only — the board never listens):

```
ESP32 FDVR_DMX_TX_PIN (GPIO17) ──► DI   transceiver   A ──► XLR pin 3 (data +)
                       3.3 V  ──► DE                  B ──► XLR pin 2 (data −)
                       3.3 V  ──► /RE (receiver off)  GND ─► XLR pin 1 (through the isolated side, if isolated)
```

If DE and /RE are not tied to 3.3 V, wire them together to a free pin and name it as
`FDVR_DMX_DE_PIN`; the firmware holds it high. Use an isolated transceiver (or a DMX isolator /
splitter after the board) whenever the line leaves the rack: DMX devices on other supplies can
carry mains-borne faults into the board that also moves a platform.

Audio (PCM5102A module as the example):

```
ESP32 FDVR_I2S_BCK_PIN ──► BCK      SCK ─► GND (the module makes its own master clock)
      FDVR_I2S_WS_PIN  ──► LCK      3.3 V / 5 V and GND as the module requires
      FDVR_I2S_DOUT_PIN ─► DIN      L, R line out ──► amplifier inputs ──► shakers / subwoofer
```

Left is channel 1, right is channel 2 of `[output.esp32.audio]` in the venue patch. The firmware
sends no master clock (it must not: GPIO0 is the Ethernet clock on these boards).

### Pins

| Variant | DMX TX | I2S BCK / WS / DATA | Remarks |
|---|---|---|---|
| effects-only board (`FDVR_HAS_ACTUATORS 0`), WT32-ETH01 | GPIO17 | GPIO32 / 33 / 4 | the defaults in `config.h` |
| platform + DMX, WT32-ETH01 | GPIO17 | — | the six position outputs, drive enable and the emergency-stop input use GPIO2, 4, 12, 14, 15, 32, 33, 35; GPIO17 is free. **There are not three more free output pins on this board for audio** |
| platform + DMX + audio on one plain ESP32 dev board over Wi-Fi (bench) | GPIO17 | GPIO26 / 25 / 27 | environment `esp32dev-wifi-all` |

The build stops with a message if one pin is given two jobs. On a board that also moves a
platform, sound therefore needs either a board with more pins or — simpler — a **second, small
ESP32 as an effects-only board** next to it, with the shaker's amplifier and, if you like, the
DMX line too. The same holds for a custom controller board whose pins are all in use. Give the
second board its own address and list it in `[output.esp32.platforms]`.

Wi-Fi: an effects-only board may use Wi-Fi (the firmware then buffers 0.2 s of sound instead of
0.1 s). Fog, wind and bass tolerate the jitter of a quiet Wi-Fi network; a strobe that must stay
in step with others, and above all the platform, belong on a cable.

## 3. Firmware

`firmware/esp32-actuator-bridge/` builds every variant from one source with PlatformIO:

| Environment (`pio run -e …`) | Board | Has |
|---|---|---|
| `wt32-eth01` | WT32-ETH01 | platform (the firmware as before; the effects port is not opened) |
| `wt32-eth01-dmx` | WT32-ETH01 | platform + DMX line |
| `wt32-eth01-fx` | WT32-ETH01 | DMX line + audio, no platform |
| `esp32dev-wifi-fx` | ESP32 dev board, Wi-Fi | DMX line + audio, no platform |
| `esp32dev-wifi-all` | ESP32 dev board, Wi-Fi | platform + DMX + audio (bench tests only) |
| `olimex-esp32-poe`, `esp32dev-wifi`, `esp32dev-serial` | as before | platform |

Settings in `include/config.h` (or as `-D NAME=value` in `platformio.ini`):

| Setting | Default | Meaning |
|---|---|---|
| `FDVR_HAS_ACTUATORS`, `FDVR_HAS_DMX`, `FDVR_HAS_AUDIO` | 1, 0, 0 | what this board has |
| `FDVR_IP` … | 192.168.10.101 | the board's fixed address (one per board) |
| `FDVR_FX_UDP_PORT` | 47002 | the effects link |
| `FDVR_DMX_TX_PIN`, `FDVR_DMX_DE_PIN` | 17, −1 | DMX output pin; driver-enable pin (−1: tied to 3.3 V) |
| `FDVR_DMX_REFRESH_HZ` | 40 | DMX frames per second on the line |
| `FDVR_I2S_BCK_PIN`, `FDVR_I2S_WS_PIN`, `FDVR_I2S_DOUT_PIN` | 32, 33, 4 | audio converter |
| `FDVR_AUDIO_CHANNELS` | 2 | 1 or 2 outputs |
| `FDVR_AUDIO_RATE_HZ`, `FDVR_AUDIO_UPSAMPLE` | 4000, 8 | sound arrives at 4 kHz and is interpolated to 32 kHz for the converter |
| `FDVR_AUDIO_BUFFER_MS` | 100 | sound buffered before it plays (200 for Wi-Fi); the PC is told and sends that much earlier |
| `FDVR_ESTOP_PIN` | 35 with a platform, −1 without | low = emergency stop active: dark and silent. Pins 34–39 have no pull-up: wire the signal before enabling the input on an effects-only board |

The firmware has two parts. `lib/fourdvr_fx/` is the node itself — packets, the DMX frame, the
sound buffer with its clock-drift correction, time-outs — in portable C++17 without any Arduino
call; it is what the PC tests exercise, and the simulator `test/host/fxnode_sim` is this code
behind a UDP socket. `src/main.cpp` is the board-specific rest: network, a task that receives and
sends DMX through UART1 (250 000 baud, 8N2; the break is made by inverting the transmit line for
180 µs, the mark after it lasts 20 µs), and a task that feeds the I2S driver. Both tasks run on
the processor core that does not run the motion loop.

```bash
cd firmware/esp32-actuator-bridge/test/host
make            # host tests (motion link 645 checks, effects node 150), compile checks of main.cpp in 11 variants,
                # two pin-conflict builds that must fail, and the simulator
make clean
```

### First power-on of a board

Nothing below was done here; these are the checks the design needs before it is trusted.

1. **Build and flash** the variant (`pio run -e wt32-eth01-fx -t upload`). If the build fails in
   `src/main.cpp`, the installed ESP32 Arduino core differs from the ones the code was written
   against (ESP-IDF 4.4 and 5.1 declarations): report the message.
2. **Does it answer?** `fourdvr fx nodes 192.168.10.150` must print the board with what it has.
   No answer: address, cable, port 47002, firewall of the PC.
3. **DMX without devices.** Connect a DMX tester (or one dimmer with a lamp). Run
   `fourdvr fx fire <fixture> --patch patch.toml --seconds 2`: that channel, and only that one,
   must rise and fall. An oscilloscope on the line should show a break of about 180 µs, 513 slots,
   40 frames a second. Pull the network cable: after one second the line must be dark, not frozen.
4. **Devices**, one at a time, as in EFFECTS_GUIDE.md §7. Check that each device switches off
   when the DMX cable is pulled at the board.
5. **Sound.** Amplifier volume at zero. Play a programme with a bass track (`fourdvr fx play …`),
   raise the volume slowly. Listen for clicks or drop-outs over several minutes;
   `fourdvr fx nodes` shows the board's counters (underruns, gaps) — they should stay at zero on a
   cabled network.
6. **In step?** Compare a clear bass hit with the headset's own sound and with the platform's
   motion; correct with `latency_offset_s` in `[output.esp32.audio]`.
7. **Emergency stop.** With the input wired: open the safety chain — line dark, sound off, and
   `fourdvr fx nodes` says so. Then check that the devices' *power* is off as well: that is the
   part that counts.
8. **With the platform moving** (a board that does both): the motion link's statistics on the
   Control Server (late and lost packets) must not get worse when effects play.

## 4. Trying it without a board

```bash
# a simulated board on this PC (Python; the same rules as the firmware)
fourdvr controller esp32-sim --platform-id 1 --dmx-ports 1 --audio-channels 2 &
fourdvr fx nodes 127.0.0.1
fourdvr fx example-patch --esp32 | sed 's/192.168.10.101/127.0.0.1/' > /tmp/patch.toml
fourdvr fx fire fog-main --patch /tmp/patch.toml --seconds 1

# the complete chain with the Control Server and three platforms, boards = the firmware's own code
python integration/run_esp32_integration.py --server /path/to/fourdvr-server
```

## 5. The link: 4DFX 1.0

UDP, one packet per datagram, little-endian, at most 1306 bytes (inside one Ethernet frame). The
controller sends from one socket to port 47002 of each board; the board answers to the address
and port a packet came from.

Every packet: a 12-byte header, a payload, and a CRC-32 (as in zlib / Ethernet) over everything
before it.

| Offset | Type | Field |
|---|---|---|
| 0 | 4 bytes | `"4DFX"` |
| 4 | u8 | major version: 1 |
| 5 | u8 | minor version: 0 |
| 6 | u8 | packet type |
| 7 | u8 | node id the packet is for; 0 = whichever node receives it. A board's id is its `FDVR_PLATFORM_ID` (give an effects-only board a number of its own) |
| 8 | u32 | sequence number of the sender |

| Type | Name | Direction | Payload |
|---|---|---|---|
| 1 | HELLO | PC → board | u64 time of the PC in µs (echoed in INFO) |
| 2 | DMX | PC → board | u8 port (0 = first line), u8 flags (0), u16 first slot (1…512), u16 count, then `count` bytes |
| 3 | AUDIO | PC → board | u8 channels (1 or 2), u8 flags (bit 0: start of a stream), u16 sample rate (Hz), u32 position (number of the first frame), u16 frames (1…320), then `frames × channels` signed 16-bit samples, interleaved |
| 4 | STOP | PC → board | u8 what (bit 0: DMX dark, bit 1: sound silent), u8 reserved |
| 129 | INFO | board → PC | below (38 bytes) |

INFO, the answer to every HELLO:

| Type | Field |
|---|---|
| u64 | the time from the HELLO |
| u32 | uptime in ms |
| u16 | firmware version of the effects node (major × 256 + minor; 1.0) |
| u8 | capabilities: bit 0 a platform, bit 1 DMX, bit 2 audio |
| u8 | number of DMX lines |
| u8 | number of audio outputs |
| u8 | state: bit 0 DMX is being sent, bit 1 sound is playing, bit 2 inhibited (emergency stop) |
| u16 | sample rate the board wants sound at (Hz) |
| u16 | time from a sound packet's arrival to the output, ms: buffer + output stage. The PC sends sound that much ahead |
| u16 | sound in the buffer right now, ms |
| u8 | DMX frames per second on the line |
| u8 | reserved |
| u32 | packets received |
| u16 | invalid packets |
| u16 | times the DMX line went dark because packets stopped |
| u16 | times the sound buffer ran empty |
| u16 | sound packets that were lost (the gap was filled with silence) |

Rules for a node:

* A packet with a wrong magic, major version or CRC, an unknown type, a wrong length, or another
  node's id is counted as invalid and ignored. A newer minor version is accepted.
* **DMX**: the slots named in a packet are replaced, all others keep their value. A packet for a
  line the node does not have is ignored (not invalid). The controller sends all used slots of a
  line in every frame, so a lost packet costs 25 ms.
* **Sound** is a stream of numbered frames. A packet with the start flag empties the buffer; the
  node then waits until its buffer time is filled before it plays. A packet that continues later
  than expected: the gap is filled with silence (up to 1 024 frames, a quarter of a second at
  4 kHz; a larger jump restarts the stream). A packet from the past is dropped. PC and board have different clocks:
  when the buffer level drifts to more than 1.5 times or less than half of its target, the node
  drops or repeats one frame in 64 until it is back.
* **STOP** takes effect at once. **Inhibit** (emergency stop) overrides everything.
* The controller counts a node as absent after 3 s without INFO and stops sending to it; it keeps
  sending HELLO.

Sound format on the wire: mono or stereo, 16 bit, at the rate the node asks for (4 kHz as built:
the bass track is low-passed at 120 Hz and the vibration tone lies below 200 Hz). On the wire a
stereo stream at 4 kHz is about 150 kbit/s, and DMX for one line at 40 frames a second with 64
slots in use about 40 kbit/s — together a fraction of a percent of a 100 Mbit/s link.

The link carries no authentication and no encryption — like DMX itself. It belongs on the venue's
closed show network.

Test vectors for both sides: `firmware/esp32-actuator-bridge/test/fx_vectors.txt`
(`fourdvr fx test-vectors` writes them).
