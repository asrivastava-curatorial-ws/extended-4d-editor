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
// With neither DMX nor audio (and no seat buttons, FDVR_INPUT_COUNT below) the firmware is the plain
// actuator bridge and does not open the effects port.
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
// How many actuators this board drives: 6 for a Stewart platform, 3 or 2 for a motion seat - the
// number in the platform profile (the controller checks that both say the same). The first
// FDVR_AXES values of the lists below are used.
#ifndef FDVR_AXES
#define FDVR_AXES 6
#endif
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
// What goes to each actuator's controller - exactly one of (docs/ACTUATORS.md):
//   FDVR_OUTPUT_PWM 1     a PWM position signal per actuator on FDVR_OUTPUT_PINS: servo drives with a PWM
//                         or (RC-filtered) analog input, or a PWM-to-0-10 V converter module   [the default]
//   FDVR_OUTPUT_DAC 1     an analog 0-10 V set-point per actuator from GP8403 I2C DACs (two channels each,
//                         e.g. DFRobot DFR0971, addresses FDVR_DAC_ADDR_FIRST, +1, +2 set on the modules): for
//                         servo drives, hydraulic valve amplifiers with a position card and servo-pneumatic
//                         positioners - their own loop holds the actuator. +-10 V or 4-20 mA: a signal converter.
//   FDVR_OUTPUT_VALVES 1  plain solenoid valves (control = "onoff" in the profile): an output per cylinder on
//                         FDVR_OUTPUT_PINS, on = extend, through a valve driver or relay module. Power the valves
//                         through FDVR_ENABLE_PIN (several ESP32 pins pulse briefly at boot).
#ifndef FDVR_OUTPUT_DAC
#define FDVR_OUTPUT_DAC 0
#endif
#ifndef FDVR_OUTPUT_VALVES
#define FDVR_OUTPUT_VALVES 0
#endif
#ifndef FDVR_OUTPUT_PWM
#define FDVR_OUTPUT_PWM (!FDVR_OUTPUT_DAC && !FDVR_OUTPUT_VALVES)
#endif
#ifndef FDVR_OUTPUT_PINS
#ifdef FDVR_PWM_PINS
#define FDVR_OUTPUT_PINS FDVR_PWM_PINS  // the name before firmware 1.3
#else
#define FDVR_OUTPUT_PINS {2, 4, 12, 14, 15, 32}  // PWM or valves: one pin per actuator
#endif
#endif
#define FDVR_PWM_FREQ_HZ 1000
#define FDVR_PWM_BITS 14
#define FDVR_PWM_DUTY_MIN 1638   // 10 % of 16383
#define FDVR_PWM_DUTY_MAX 14745  // 90 % of 16383
#define FDVR_VALVE_ACTIVE_HIGH 1  // 1: a valve output is high to extend; 0: low
#ifndef FDVR_DAC_ADDR_FIRST
#define FDVR_DAC_ADDR_FIRST 0x58  // GP8403 of actuators 1-2; actuators 3-4 at +1, 5-6 at +2
#endif
#define FDVR_DAC_CODE_RETRACTED 0      // 12-bit code (0 = 0 V, 4095 = 10 V) when fully retracted ...
#define FDVR_DAC_CODE_EXTENDED 4095    // ... and fully extended (swap them for a reversed input)

// Position sensors (optional): 4-20 mA or 0-10 V transducers in the actuators, read by ADS1115 I2C
// ADCs (four inputs each; actuators 1-4 at FDVR_ADC_ADDR_FIRST, 5-6 at +1). The positions go to the
// controller, which stops and parks a platform whose actuator does not follow its command; a sensor
// reading outside its valid range three times in a row is a FEEDBACK fault here.
// 4-20 mA through a 150 Ohm precision resistor to GND: 0.6-3.0 V; with FDVR_ADC_PGA 1 (+-4.096 V,
// 0.125 mV a step) that is 4800-24000; below 3.6 mA (4320) is a broken wire.
// 0-10 V: a divider to at most 3.3 V; set FDVR_SENSOR_RAW_MIN_VALID below 0 (no broken-wire check).
#ifndef FDVR_FEEDBACK
#define FDVR_FEEDBACK 0
#endif
#define FDVR_ADC_ADDR_FIRST 0x48
#define FDVR_ADC_PGA 1
#define FDVR_SENSOR_RAW_RETRACTED {4800, 4800, 4800, 4800, 4800, 4800}  // measure each one at commissioning
#define FDVR_SENSOR_RAW_EXTENDED {24000, 24000, 24000, 24000, 24000, 24000}
#define FDVR_SENSOR_RAW_MIN_VALID 4320
#define FDVR_SENSOR_RAW_MAX_VALID 24600

// The I2C bus of the DACs and ADCs (only with FDVR_OUTPUT_DAC or FDVR_FEEDBACK). On a WT32-ETH01
// with DAC outputs, 14 and 15 are free; with PWM outputs choose two other free pins (5 and 13).
#ifndef FDVR_I2C_SDA_PIN
#define FDVR_I2C_SDA_PIN 14
#endif
#ifndef FDVR_I2C_SCL_PIN
#define FDVR_I2C_SCL_PIN 15
#endif

// Supply / drive-ready input (optional): a pressure switch, "pump running", "drive ready". Lost for
// 50 ms while the platform runs or holds: a SUPPLY fault and the platform parks. GPIO36/39 suit it
// (input only, no internal pull-up: wire a defined level). -1: none.
#ifndef FDVR_READY_PIN
#define FDVR_READY_PIN -1
#endif
#define FDVR_READY_ACTIVE_HIGH 1  // 1: high = supply ok; 0: low = ok

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

// Seat buttons for interactive shows ("fourdvr play"): up to 8 push buttons, each between its pin
// and GND (the internal pull-up is used; a pressed button reads low). They are reported over the
// 4DFX link as the buttons "a", "b", "c", "d" ... of this platform's seat ([play] buttons in
// play.toml). Each button needs a pin that nothing else uses (the build stops if a pin is used twice
// or belongs to the platform outputs, DMX, I2S or Ethernet). On an ESP32 with Ethernet that also
// drives a platform, few pins are left: 5 and 13 have a pull-up; 36 and 39 are free too, but
// GPIO34-39 have NO internal pull-up - fit a 10 kOhm resistor from the pin to 3.3 V for a button
// there. An effects-only node has more free pins (2, 12, 14, 15 ...; avoid 12, a boot strapping pin).
#ifndef FDVR_INPUT_COUNT
#define FDVR_INPUT_COUNT 0  // 0: no buttons
#endif
#ifndef FDVR_INPUT_PINS
#define FDVR_INPUT_PINS {5, 36, 39, 13}  // as many pins as FDVR_INPUT_COUNT says (the first ones are used)
#endif
#define FDVR_INPUT_ACTIVE_LOW 1  // 1: button to GND with pull-up; 0: button to 3.3 V with an external pull-down

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
