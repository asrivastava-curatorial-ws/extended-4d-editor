// Extended 4D Editor - ESP32 firmware configuration (actuator bridge and/or effects node).
// Edit these values for each ESP32, then build and flash (see README.md in this folder).
// Values can also be overridden from platformio.ini with build_flags = -D NAME=value.
#pragma once

// ----------------------------------------------------------------- identity
#ifndef FDVR_PLATFORM_ID
#define FDVR_PLATFORM_ID 1  // must equal link_platform_id in the platform TOML (1..250); also this ESP32's effects-node id
#endif

// ------------------------------------------------------- what this ESP32 has
// The motion controller asks every ESP32 what it has and uses just that (docs/ESP32_EFFECTS_LINK.md):
//   FDVR_HAS_ACTUATORS 1   it moves a platform (motion link, UDP 47000).  0: an effects-only node
//   FDVR_HAS_DMX 1         a DMX512 line for effect devices (RS-485 transceiver on FDVR_DMX_TX_PIN)
//   FDVR_HAS_AUDIO 1       an I2S DAC (PCM5102A, UDA1334A, MAX98357A ...) feeding the amplifier of a
//                          bass shaker or subwoofer
// With neither DMX nor audio the firmware is the plain actuator bridge and does not open the effects port.
#ifndef FDVR_HAS_ACTUATORS
#define FDVR_HAS_ACTUATORS 1
#endif
#ifndef FDVR_HAS_DMX
#define FDVR_HAS_DMX 0
#endif
#ifndef FDVR_HAS_AUDIO
#define FDVR_HAS_AUDIO 0
#endif

// ------------------------------------------------------------------ network
// Exactly one transport is the primary link; USB serial is always available as well.
//   FDVR_NET_ETHERNET 1 : wired LAN (LAN8720 PHY, e.g. WT32-ETH01, Olimex ESP32-POE)  [recommended]
//   FDVR_NET_WIFI     1 : Wi-Fi (only for bench tests; Wi-Fi latency jitter is not suitable for shows)
#ifndef FDVR_NET_ETHERNET
#define FDVR_NET_ETHERNET 1
#endif
#ifndef FDVR_NET_WIFI
#define FDVR_NET_WIFI 0
#endif
#define FDVR_UDP_PORT 47000

// Static addressing (dedicated theatre LAN, no DHCP dependency). One address per platform.
#define FDVR_IP 192, 168, 10, 101
#define FDVR_GATEWAY 192, 168, 10, 1
#define FDVR_SUBNET 255, 255, 255, 0

#define FDVR_WIFI_SSID "theatre-motion"
#define FDVR_WIFI_PASSWORD "change-me"

// LAN8720 wiring (WT32-ETH01 defaults)
#define FDVR_ETH_PHY_ADDR 1
#define FDVR_ETH_PHY_POWER 16
#define FDVR_ETH_PHY_MDC 23
#define FDVR_ETH_PHY_MDIO 18

#define FDVR_SERIAL_BAUD 921600  // USB serial link (COBS framed); also used for log output when idle

// --------------------------------------------------------------- actuators
// Stroke of each actuator in micrometres (0 = fully retracted). Must match the platform TOML.
#define FDVR_STROKE_UM {300000, 300000, 300000, 300000, 300000, 300000}
// Park position (micrometres) used after a communication timeout or on PARK.
#define FDVR_PARK_UM {90000, 90000, 90000, 90000, 90000, 90000}
#define FDVR_MAX_SPEED_UM_S 480000   // slew limit while tracking (slightly above the controller limit)
#define FDVR_PARK_SPEED_UM_S 60000   // ramp speed to the park position
#define FDVR_TIMEOUT_MS 200          // communication watchdog
#define FDVR_STALE_MS 100            // commands older than this are discarded
#define FDVR_INVALID_BURST 20        // invalid packets per second that latch a fault

// ------------------------------------------------------------------ outputs
// Generic position-command output: one LEDC PWM channel per actuator. Duty is mapped linearly
// from 0..stroke to FDVR_PWM_DUTY_MIN..FDVR_PWM_DUTY_MAX. Many servo drives accept a PWM or
// (RC-filtered) analogue position command; adapt the mapping to your drive's input. No specific
// drive, feedback or fieldbus is implemented (the hardware has not been selected yet).
#define FDVR_OUTPUT_PWM 1
#define FDVR_PWM_PINS {2, 4, 12, 14, 15, 32}
#define FDVR_PWM_FREQ_HZ 1000
#define FDVR_PWM_BITS 14
#define FDVR_PWM_DUTY_MIN 1638   // 10 % of 16383
#define FDVR_PWM_DUTY_MAX 14745  // 90 % of 16383

// Optional "drive enable" output (high = enabled). Driven low on any fault, timeout or e-stop.
#define FDVR_ENABLE_PIN 33
// Optional safety-chain input (LOW = e-stop active). Informational only: the physical emergency
// stop must cut actuator and effect-device power independently of this firmware. While it is active
// the platform parks and the effects outputs are dark and silent. -1 disables the input.
// GPIO34-39 have no internal pull-up: wire the signal, or an external pull-up, before enabling it there.
#ifndef FDVR_ESTOP_PIN
#if FDVR_HAS_ACTUATORS
#define FDVR_ESTOP_PIN 35
#else
#define FDVR_ESTOP_PIN -1  // effects-only node: no input unless one is wired
#endif
#endif
#define FDVR_STATUS_LED_PIN -1  // -1: no LED

// ------------------------------------------------------------------ effects
#define FDVR_FX_UDP_PORT 47002  // the 4DFX link (DMX frames and bass sound from the motion controller)

// DMX512 output: this pin is the UART transmit line to the DI input of an RS-485 transceiver (MAX485,
// SN75176, or better an isolated one); A/B of the transceiver are DMX data + / data -. If the
// transceiver's DE and /RE are not wired to 3.3 V, name the pin that drives them (it is held high).
// One second without DMX packets from the controller and the line is sent as all zero.
#ifndef FDVR_DMX_TX_PIN
#define FDVR_DMX_TX_PIN 17
#endif
#ifndef FDVR_DMX_DE_PIN
#define FDVR_DMX_DE_PIN -1
#endif
#define FDVR_DMX_REFRESH_HZ 40

// Audio output: I2S, 16 bit stereo at 32 kHz (left = channel 1, right = channel 2 of the venue patch's
// [output.esp32.audio]). The controller sends the bass at 4 kHz; it is interpolated eight-fold here.
// The DAC's line output goes to the amplifier of the shaker / subwoofer - never connect a shaker to
// the DAC or to the ESP32 directly.
#ifndef FDVR_I2S_BCK_PIN
#define FDVR_I2S_BCK_PIN 32
#endif
#ifndef FDVR_I2S_WS_PIN
#define FDVR_I2S_WS_PIN 33
#endif
#ifndef FDVR_I2S_DOUT_PIN
#define FDVR_I2S_DOUT_PIN 4
#endif
#define FDVR_AUDIO_CHANNELS 2
#define FDVR_AUDIO_RATE_HZ 4000
#define FDVR_AUDIO_UPSAMPLE 8
// Sound buffered before it plays; absorbs network jitter. 100 ms suits Ethernet, 200 ms Wi-Fi.
// The controller is told and sends the sound that much earlier.
#ifndef FDVR_AUDIO_BUFFER_MS
#define FDVR_AUDIO_BUFFER_MS 100
#endif
// The default pins above suit an effects-only node (FDVR_HAS_ACTUATORS 0). On an ESP32 that also
// drives a platform they collide with FDVR_PWM_PINS / FDVR_ENABLE_PIN: choose free pins (the build
// stops with a message if a pin is used twice).
