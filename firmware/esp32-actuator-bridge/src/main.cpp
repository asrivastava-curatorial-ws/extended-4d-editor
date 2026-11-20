// Extended 4D Editor - ESP32 firmware (Arduino framework, PlatformIO): actuator bridge and effects node.
//
// Actuator bridge (FDVR_HAS_ACTUATORS): receives COMMAND packets from the motion controller (UDP over
// Ethernet/Wi-Fi, or COBS-framed USB serial), validates them with the portable LinkGuard
// (fourdvr_link), drives one position command output per actuator and answers every command with a
// STATUS packet.
//
// Effects node (FDVR_HAS_DMX and/or FDVR_HAS_AUDIO): receives DMX slot values and a low-rate stream of
// bass sound from the motion controller on a second UDP port, with the portable Node (fourdvr_fx),
// and puts them out on a DMX512 line (UART + RS-485 transceiver) and an I2S DAC. It tells the
// controller what it has; without DMX and audio the effects port is not even opened. The effects run
// in tasks of their own on the other processor core and never wait for, or hold up, the motion loop.
//
// SAFETY: this firmware is NOT a safety system. It parks the actuators on communication loss and
// sends dark DMX frames and silence when the controller stops talking or the emergency-stop input is
// active - conveniences, not safety functions. An independent, hard-wired emergency stop that removes
// the power of the actuators and of every effect device that must stop is mandatory.
#include <Arduino.h>

#include "config.h"

#if FDVR_NET_ETHERNET
#include <ETH.h>
#endif
#if FDVR_NET_WIFI
#include <WiFi.h>
#endif
#include <WiFiUdp.h>

#define FDVR_HAS_INPUTS (FDVR_INPUT_COUNT > 0)
#define FDVR_FX (FDVR_HAS_DMX || FDVR_HAS_AUDIO || FDVR_HAS_INPUTS)

#if FDVR_HAS_ACTUATORS
#include "fourdvr_io.h"
#include "fourdvr_link.h"
#if FDVR_OUTPUT_DAC || FDVR_FEEDBACK
#include <Wire.h>
#endif
#endif
#if FDVR_FX
#include <string.h>

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "fourdvr_fx.h"
#if FDVR_HAS_DMX
#include <driver/uart.h>
#endif
#if FDVR_HAS_AUDIO
#include <driver/i2s.h>
#endif
#endif

namespace {

bool network_up = false;

// ================================================================================ actuator bridge
#if FDVR_HAS_ACTUATORS

static_assert(FDVR_AXES >= 1 && FDVR_AXES <= fdvr::kAxes, "FDVR_AXES: 1 to 6 actuators");
static_assert(FDVR_OUTPUT_PWM + FDVR_OUTPUT_DAC + FDVR_OUTPUT_VALVES == 1,
              "choose exactly one of FDVR_OUTPUT_PWM, FDVR_OUTPUT_DAC, FDVR_OUTPUT_VALVES");
#define FDVR_I2C (FDVR_OUTPUT_DAC || FDVR_FEEDBACK)

// Every pin of the platform side, once: outputs, I2C, enable, e-stop, LED, ready, and not Ethernet's.
constexpr int kOutputPins[fdvr::kAxes] = FDVR_OUTPUT_PINS;
constexpr bool ethernetPin(int pin) {
  return FDVR_NET_ETHERNET && (pin == 0 || pin == FDVR_ETH_PHY_POWER || pin == FDVR_ETH_PHY_MDC || pin == FDVR_ETH_PHY_MDIO ||
                               pin == 19 || pin == 21 || pin == 22 || pin == 25 || pin == 26 || pin == 27);
}
constexpr bool motionPinUsed(int pin) {
  if (pin < 0) return false;
  if (FDVR_OUTPUT_PWM || FDVR_OUTPUT_VALVES) {
    for (int i = 0; i < FDVR_AXES; ++i) {
      if (kOutputPins[i] == pin) return true;
    }
  }
  if (FDVR_I2C && (pin == FDVR_I2C_SDA_PIN || pin == FDVR_I2C_SCL_PIN)) return true;
  return pin == FDVR_ENABLE_PIN || pin == FDVR_ESTOP_PIN || pin == FDVR_STATUS_LED_PIN || pin == FDVR_READY_PIN;
}
constexpr bool motionPinsOk() {
  int pins[fdvr::kAxes + 6] = {};
  int n = 0;
  if (FDVR_OUTPUT_PWM || FDVR_OUTPUT_VALVES) {
    for (int i = 0; i < FDVR_AXES; ++i) pins[n++] = kOutputPins[i];
  }
  if (FDVR_I2C) {
    pins[n++] = FDVR_I2C_SDA_PIN;
    pins[n++] = FDVR_I2C_SCL_PIN;
  }
  const int single[4] = {FDVR_ENABLE_PIN, FDVR_ESTOP_PIN, FDVR_STATUS_LED_PIN, FDVR_READY_PIN};
  for (int k = 0; k < 4; ++k) {
    if (single[k] >= 0) pins[n++] = single[k];
  }
  for (int a = 0; a < n; ++a) {
    if (ethernetPin(pins[a])) return false;
    for (int b = 0; b < a; ++b) {
      if (pins[a] == pins[b]) return false;
    }
  }
  return true;
}
static_assert(motionPinsOk(), "a platform pin (outputs, I2C, enable, e-stop, LED, ready) is used twice or is an Ethernet pin: "
                              "choose free pins in config.h (FDVR_OUTPUT_PINS, FDVR_I2C_SDA_PIN / _SCL_PIN, FDVR_READY_PIN)");

constexpr uint16_t capabilities() {
  return static_cast<uint16_t>((FDVR_FEEDBACK ? fdvr::kCapMeasured : 0) | (FDVR_OUTPUT_DAC ? fdvr::kCapOutputDac : 0) |
                               (FDVR_OUTPUT_VALVES ? fdvr::kCapOutputValves : 0) | (FDVR_READY_PIN >= 0 ? fdvr::kCapSupplyInput : 0));
}

fdvr::GuardConfig makeConfig() {
  fdvr::GuardConfig c;
  const int32_t stroke[fdvr::kAxes] = FDVR_STROKE_UM;
  const int32_t park[fdvr::kAxes] = FDVR_PARK_UM;
  c.platform_id = FDVR_PLATFORM_ID;
  for (int i = 0; i < fdvr::kAxes; ++i) {
    c.stroke_um[i] = stroke[i];
    c.park_um[i] = park[i];
  }
  c.max_speed_um_s = FDVR_MAX_SPEED_UM_S;
  c.park_speed_um_s = FDVR_PARK_SPEED_UM_S;
  c.timeout_ms = FDVR_TIMEOUT_MS;
  c.stale_ms = FDVR_STALE_MS;
  c.invalid_burst = FDVR_INVALID_BURST;
  c.axes = FDVR_AXES;
  c.valves = FDVR_OUTPUT_VALVES;
  c.capabilities = capabilities();
  return c;
}

fdvr::LinkGuard guard(makeConfig());
WiFiUDP udp;
IPAddress peer_ip;
uint16_t peer_port = 0;
bool peer_is_serial = false;
uint32_t last_status_ms = 0;
uint32_t last_update_us = 0;
uint8_t serial_buf[128];
size_t serial_len = 0;
const int32_t kStroke[fdvr::kAxes] = FDVR_STROKE_UM;

#if FDVR_OUTPUT_DAC
uint16_t dac_sent[fdvr::kAxes];       // the code each channel has (0xFFFF: not yet written)
int dac_next = 0;                     // round robin: a few channels per millisecond, the I2C bus stays short
uint32_t dac_refresh_ms = 0;
bool dac_error = false;

bool i2cWrite(uint8_t address, const uint8_t* bytes, size_t n) {
  Wire.beginTransmission(address);
  Wire.write(bytes, n);
  return Wire.endTransmission() == 0;
}
#endif

#if FDVR_FEEDBACK
const int16_t kRawRetracted[fdvr::kAxes] = FDVR_SENSOR_RAW_RETRACTED;
const int16_t kRawExtended[fdvr::kAxes] = FDVR_SENSOR_RAW_EXTENDED;
fdvr::io::FeedbackScanner scanner(FDVR_AXES);
fdvr::io::SensorWatch sensors[fdvr::kAxes];

void pollFeedback(uint32_t now_us) {
  const int axis = scanner.axis();
  const uint8_t address = fdvr::io::adcAddress(axis, FDVR_ADC_ADDR_FIRST);
  switch (scanner.next(now_us)) {
    case fdvr::io::FeedbackScanner::Action::Start: {
      const uint16_t cfg = fdvr::io::ads1115Config(fdvr::io::adcInput(axis), FDVR_ADC_PGA);
      Wire.beginTransmission(address);
      Wire.write(fdvr::io::kAdsConfig);
      Wire.write(static_cast<uint8_t>(cfg >> 8));
      Wire.write(static_cast<uint8_t>(cfg & 0xFF));
      if (Wire.endTransmission() != 0) {
        sensors[axis].bad();
        scanner.result(false, 0);
      }
      break;
    }
    case fdvr::io::FeedbackScanner::Action::Read: {
      Wire.beginTransmission(address);
      Wire.write(fdvr::io::kAdsConversion);
      bool ok = Wire.endTransmission() == 0 && Wire.requestFrom(address, static_cast<uint8_t>(2)) == 2;
      int16_t raw = 0;
      if (ok) {
        const int hi = Wire.read();
        const int lo = Wire.read();
        raw = static_cast<int16_t>((hi << 8) | (lo & 0xFF));  // big endian, two's complement
      }
      const fdvr::io::SensorScale scale = {kRawRetracted[axis], kRawExtended[axis], FDVR_SENSOR_RAW_MIN_VALID,
                                           FDVR_SENSOR_RAW_MAX_VALID};
      float um = 0.0f;
      if (ok && fdvr::io::sensorUm(raw, scale, kStroke[axis], um)) {
        sensors[axis].good(um);
      } else {
        sensors[axis].bad();
      }
      scanner.result(ok, raw);
      break;
    }
    default:
      break;
  }
  for (int i = 0; i < FDVR_AXES; ++i) {
    if (sensors[i].failed()) guard.inputFault(fdvr::kFaultFeedback);
  }
}
#endif

#if FDVR_READY_PIN >= 0
fdvr::io::ReadyInput ready_input;

void pollReady(uint32_t now_ms) {
  const bool high = digitalRead(FDVR_READY_PIN) == HIGH;
  const bool ok = ready_input.update(high == static_cast<bool>(FDVR_READY_ACTIVE_HIGH), now_ms);
  const fdvr::DeviceState s = guard.state();
  if (!ok && (s == fdvr::DeviceState::Running || s == fdvr::DeviceState::Hold)) guard.inputFault(fdvr::kFaultSupply);
}
#endif

void setupOutputs() {
#if FDVR_OUTPUT_PWM
  for (int i = 0; i < FDVR_AXES; ++i) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(kOutputPins[i], FDVR_PWM_FREQ_HZ, FDVR_PWM_BITS);
#else
    ledcSetup(i, FDVR_PWM_FREQ_HZ, FDVR_PWM_BITS);
    ledcAttachPin(kOutputPins[i], i);
#endif
  }
#endif
#if FDVR_OUTPUT_VALVES
  for (int i = 0; i < FDVR_AXES; ++i) {
    pinMode(kOutputPins[i], OUTPUT);
    digitalWrite(kOutputPins[i], FDVR_VALVE_ACTIVE_HIGH ? LOW : HIGH);  // off: the valve's rest position
  }
#endif
#if FDVR_I2C
  Wire.begin(FDVR_I2C_SDA_PIN, FDVR_I2C_SCL_PIN, 400000);
  Wire.setTimeOut(3);  // ms: a missing module must not stall the motion loop
#endif
#if FDVR_OUTPUT_DAC
  for (int i = 0; i < fdvr::kAxes; ++i) dac_sent[i] = 0xFFFF;
  for (int m = 0; m < (FDVR_AXES + 1) / 2; ++m) {
    uint8_t bytes[2];
    const size_t n = fdvr::io::gp8403Range10V(bytes);
    if (!i2cWrite(static_cast<uint8_t>(FDVR_DAC_ADDR_FIRST + m), bytes, n)) dac_error = true;
  }
#endif
#if FDVR_READY_PIN >= 0
  pinMode(FDVR_READY_PIN, INPUT);
#endif
  if (FDVR_ENABLE_PIN >= 0) {
    pinMode(FDVR_ENABLE_PIN, OUTPUT);
    digitalWrite(FDVR_ENABLE_PIN, LOW);
  }
  if (FDVR_STATUS_LED_PIN >= 0) pinMode(FDVR_STATUS_LED_PIN, OUTPUT);
}

void writeOutputs(uint32_t now_ms) {
  const float* out = guard.output();
#if FDVR_OUTPUT_PWM
  for (int i = 0; i < FDVR_AXES; ++i) {
    const uint32_t duty = fdvr::io::pwmDuty(fdvr::io::strokeFraction(out[i], kStroke[i]), FDVR_PWM_DUTY_MIN, FDVR_PWM_DUTY_MAX);
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(kOutputPins[i], duty);
#else
    ledcWrite(i, duty);
#endif
  }
#endif
#if FDVR_OUTPUT_VALVES
  for (int i = 0; i < FDVR_AXES; ++i) {
    const bool on = fdvr::io::valveOn(out[i], kStroke[i]);
    digitalWrite(kOutputPins[i], on == static_cast<bool>(FDVR_VALVE_ACTIVE_HIGH) ? HIGH : LOW);
  }
#endif
#if FDVR_OUTPUT_DAC
  // At most two channels a millisecond: those whose code changed, and every channel again every 100 ms.
  const bool refresh = now_ms - dac_refresh_ms >= 100;
  if (refresh) dac_refresh_ms = now_ms;
  for (int written = 0, tried = 0; written < 2 && tried < FDVR_AXES; ++tried) {
    const int i = dac_next;
    dac_next = (dac_next + 1) % FDVR_AXES;
    const uint16_t code = fdvr::io::dacCode(fdvr::io::strokeFraction(out[i], kStroke[i]), FDVR_DAC_CODE_RETRACTED,
                                            FDVR_DAC_CODE_EXTENDED);
    if (code == dac_sent[i] && !refresh) continue;
    uint8_t bytes[3];
    const size_t n = fdvr::io::gp8403Channel(fdvr::io::dacChannel(i), code, bytes);
    if (i2cWrite(fdvr::io::dacAddress(i, FDVR_DAC_ADDR_FIRST), bytes, n)) {
      dac_sent[i] = code;
    } else {
      dac_error = true;
    }
    ++written;
  }
  if (dac_error) guard.inputFault(fdvr::kFaultOutput);  // a DAC that does not answer: its actuator gets no set-point
#else
  (void)now_ms;
#endif
  if (FDVR_ENABLE_PIN >= 0) {
    // Drives stay enabled while parking (controlled return); disabled on e-stop or output fault.
    bool enable = guard.state() != fdvr::DeviceState::Fault && guard.state() != fdvr::DeviceState::Boot;
    digitalWrite(FDVR_ENABLE_PIN, enable ? HIGH : LOW);
  }
}

void sendStatus(uint32_t now_ms) {
  fdvr::Status st;
  guard.makeStatus(now_ms, st);
#if FDVR_FEEDBACK
  for (int i = 0; i < FDVR_AXES; ++i) {  // the measured positions (bit "measured" in st.capabilities)
    const float v = sensors[i].have() ? sensors[i].um() : guard.output()[i];
    st.positions_um[i] = static_cast<int32_t>(v + 0.5f);
  }
#endif
  uint8_t pkt[fdvr::kStatusSize];
  fdvr::encodeStatus(st, pkt);
  if (peer_is_serial) {
    uint8_t framed[fdvr::kStatusSize + fdvr::kStatusSize / 254 + 2];
    size_t n = fdvr::cobsEncode(pkt, sizeof(pkt), framed);
    framed[n++] = 0;
    Serial.write(framed, n);
  } else if (network_up && peer_port != 0) {
    udp.beginPacket(peer_ip, peer_port);
    udp.write(pkt, sizeof(pkt));
    udp.endPacket();
  }
  last_status_ms = now_ms;
}

void pollUdp(uint32_t now_ms) {
  if (!network_up) return;
  int size;
  while ((size = udp.parsePacket()) > 0) {
    uint8_t buf[96];
    int n = udp.read(buf, sizeof(buf));
    if (guard.handle(buf, n > 0 ? static_cast<size_t>(n) : 0, now_ms)) {
      peer_ip = udp.remoteIP();
      peer_port = udp.remotePort();
      peer_is_serial = false;
      sendStatus(now_ms);
    }
  }
}

void pollSerial(uint32_t now_ms) {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == 0) {  // frame delimiter
      uint8_t decoded[sizeof(serial_buf)];
      size_t len = 0;
      bool ok = fdvr::cobsDecode(serial_buf, serial_len, decoded, len);
      serial_len = 0;
      if (ok && guard.handle(decoded, len, now_ms)) {
        peer_is_serial = true;
        sendStatus(now_ms);
      } else if (!ok) {
        guard.handle(decoded, 0, now_ms);  // counted as invalid
      }
    } else if (serial_len < sizeof(serial_buf)) {
      serial_buf[serial_len++] = static_cast<uint8_t>(c);
    } else {
      serial_len = 0;  // oversize frame: drop
    }
  }
}

void updateLed(uint32_t now_ms) {
  if (FDVR_STATUS_LED_PIN < 0) return;
  bool on;
  switch (guard.state()) {
    case fdvr::DeviceState::Running: on = true; break;                       // solid: tracking
    case fdvr::DeviceState::Fault: on = (now_ms / 100) % 2; break;           // fast blink: fault
    case fdvr::DeviceState::Parking:
    case fdvr::DeviceState::Parked: on = (now_ms / 500) % 2; break;          // slow blink: parked
    default: on = (now_ms / 1000) % 10 == 0; break;                          // short pulse: idle
  }
  digitalWrite(FDVR_STATUS_LED_PIN, on ? HIGH : LOW);
}

#endif  // FDVR_HAS_ACTUATORS

// ================================================================================ effects node
#if FDVR_FX

#if FDVR_HAS_ACTUATORS
// A pin of the effects outputs must not be one the platform side uses.
constexpr bool pinFree(int pin) { return !motionPinUsed(pin); }
#if FDVR_HAS_DMX
static_assert(pinFree(FDVR_DMX_TX_PIN), "FDVR_DMX_TX_PIN is already used by the platform outputs: choose a free pin in config.h");
static_assert(FDVR_DMX_DE_PIN < 0 || pinFree(FDVR_DMX_DE_PIN), "FDVR_DMX_DE_PIN is already used by the platform outputs");
#endif
#if FDVR_HAS_AUDIO
static_assert(pinFree(FDVR_I2S_BCK_PIN) && pinFree(FDVR_I2S_WS_PIN) && pinFree(FDVR_I2S_DOUT_PIN),
              "an I2S pin is already used by the platform outputs: set FDVR_I2S_BCK_PIN / _WS_PIN / _DOUT_PIN to free pins");
#endif
#endif

#if FDVR_HAS_INPUTS
// Seat buttons: each on a pin of its own that nothing else uses.
static_assert(FDVR_INPUT_COUNT <= fdvr::fx::kMaxInputs, "FDVR_INPUT_COUNT: at most 8 seat buttons");
constexpr int kInputPins[] = FDVR_INPUT_PINS;
static_assert(static_cast<int>(sizeof(kInputPins) / sizeof(kInputPins[0])) >= FDVR_INPUT_COUNT,
              "FDVR_INPUT_PINS lists fewer pins than FDVR_INPUT_COUNT");
constexpr bool inputPinsFree() {
  for (int k = 0; k < FDVR_INPUT_COUNT; ++k) {
    const int pin = kInputPins[k];
#if FDVR_HAS_ACTUATORS
    if (!pinFree(pin)) return false;
#endif
#if FDVR_HAS_DMX
    if (pin == FDVR_DMX_TX_PIN || pin == FDVR_DMX_DE_PIN) return false;
#endif
#if FDVR_HAS_AUDIO
    if (pin == FDVR_I2S_BCK_PIN || pin == FDVR_I2S_WS_PIN || pin == FDVR_I2S_DOUT_PIN) return false;
#endif
#if FDVR_NET_ETHERNET  // the LAN8720's lines: its clock, power, management and the fixed RMII pins
    if (pin == 0 || pin == FDVR_ETH_PHY_POWER || pin == FDVR_ETH_PHY_MDC || pin == FDVR_ETH_PHY_MDIO || pin == 19 || pin == 21 ||
        pin == 22 || pin == 25 || pin == 26 || pin == 27) {
      return false;
    }
#endif
    for (int j = 0; j < k; ++j) {
      if (kInputPins[j] == pin) return false;
    }
  }
  return true;
}
static_assert(inputPinsFree(), "FDVR_INPUT_PINS: a seat-button pin is used twice, or already by the platform outputs, DMX, I2S or Ethernet");
#endif

fdvr::fx::Config makeFxConfig() {
  fdvr::fx::Config c;
  c.node_id = FDVR_PLATFORM_ID;
  c.has_actuators = FDVR_HAS_ACTUATORS;
  c.dmx_ports = FDVR_HAS_DMX ? 1 : 0;
  c.audio_channels = FDVR_HAS_AUDIO ? FDVR_AUDIO_CHANNELS : 0;
  c.audio_rate_hz = FDVR_AUDIO_RATE_HZ;
  c.audio_target_ms = FDVR_AUDIO_BUFFER_MS;
  c.audio_output_ms = 25;  // four DMA buffers of 8 ms: about three of them queued on average
  c.dmx_refresh_hz = FDVR_DMX_REFRESH_HZ;
  c.inputs = FDVR_INPUT_COUNT;
  return c;
}

fdvr::fx::Node fx_node(makeFxConfig());
SemaphoreHandle_t fx_mutex = nullptr;  // every call into fx_node holds it
WiFiUDP fx_udp;
volatile bool fx_inhibit = false;      // written by loop(): emergency-stop input active

struct FxLock {
  FxLock() { xSemaphoreTake(fx_mutex, portMAX_DELAY); }
  ~FxLock() { xSemaphoreGive(fx_mutex); }
};

#if FDVR_HAS_INPUTS
// Seat buttons: read here, debounced and reported by the node (fourdvr_fx).
IPAddress fx_peer_ip;         // the controller that last said HELLO: the buttons are reported to it
uint16_t fx_peer_port = 0;
uint32_t fx_peer_ms = 0;
bool fx_peer_known = false;

void setupInputs() {
  for (int k = 0; k < FDVR_INPUT_COUNT; ++k) pinMode(kInputPins[k], FDVR_INPUT_ACTIVE_LOW ? INPUT_PULLUP : INPUT);
}

uint8_t readInputs() {
  uint8_t bits = 0;
  for (int k = 0; k < FDVR_INPUT_COUNT; ++k) {
    const bool high = digitalRead(kInputPins[k]) == HIGH;
    if (high != static_cast<bool>(FDVR_INPUT_ACTIVE_LOW)) bits = static_cast<uint8_t>(bits | (1u << k));
  }
  return bits;
}
#endif

#if FDVR_HAS_DMX
const uart_port_t kDmxUart = UART_NUM_1;

void setupDmx() {
  uart_config_t cfg = {};
  cfg.baud_rate = 250000;
  cfg.data_bits = UART_DATA_8_BITS;
  cfg.parity = UART_PARITY_DISABLE;
  cfg.stop_bits = UART_STOP_BITS_2;
  cfg.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
  cfg.source_clk = UART_SCLK_APB;
  uart_param_config(kDmxUart, &cfg);
  uart_set_pin(kDmxUart, FDVR_DMX_TX_PIN, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
  uart_driver_install(kDmxUart, 256, 1024, 0, nullptr, 0);  // the transmit buffer holds a whole frame: writes do not block
  if (FDVR_DMX_DE_PIN >= 0) {
    pinMode(FDVR_DMX_DE_PIN, OUTPUT);
    digitalWrite(FDVR_DMX_DE_PIN, HIGH);  // transceiver always driving: this node only sends
  }
}

// One DMX512 frame: BREAK (line low, 180 us), MARK AFTER BREAK (20 us), start code 0, the slots.
void sendDmxFrame() {
  static uint8_t frame[1 + fdvr::fx::kSlots];
  int slots;
  {
    FxLock lock;
    slots = fx_node.dmxSlots(0);
    frame[0] = 0;
    memcpy(frame + 1, fx_node.dmxFrame(0), static_cast<size_t>(slots));
  }
  uart_wait_tx_done(kDmxUart, pdMS_TO_TICKS(30));         // the previous frame has left the line
  uart_set_line_inverse(kDmxUart, UART_SIGNAL_TXD_INV);    // idle line inverted = low: BREAK
  delayMicroseconds(180);
  uart_set_line_inverse(kDmxUart, UART_SIGNAL_INV_DISABLE);
  delayMicroseconds(20);                                   // MARK AFTER BREAK
  uart_write_bytes(kDmxUart, reinterpret_cast<const char*>(frame), static_cast<size_t>(1 + slots));
}
#endif  // FDVR_HAS_DMX

#if FDVR_HAS_AUDIO
const i2s_port_t kI2s = I2S_NUM_0;
constexpr size_t kAudioBlock = 32;  // frames of the 4 kHz stream per step: 8 ms

void setupAudio() {
  i2s_config_t cfg = {};
  cfg.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate = FDVR_AUDIO_RATE_HZ * FDVR_AUDIO_UPSAMPLE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = 0;
  cfg.dma_buf_count = 4;
  cfg.dma_buf_len = static_cast<int>(kAudioBlock * FDVR_AUDIO_UPSAMPLE);
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = true;  // silence, not a repeated buffer, if the task were ever late
  i2s_driver_install(kI2s, &cfg, 0, nullptr);
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;  // no master clock output (it would land on GPIO0, the Ethernet clock)
  pins.bck_io_num = FDVR_I2S_BCK_PIN;
  pins.ws_io_num = FDVR_I2S_WS_PIN;
  pins.data_out_num = FDVR_I2S_DOUT_PIN;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  i2s_set_pin(kI2s, &pins);
  i2s_zero_dma_buffer(kI2s);
}

// The DAC sets the pace: i2s_write() blocks until the previous block has been taken.
void audioTask(void*) {
  static int16_t in[kAudioBlock * FDVR_AUDIO_CHANNELS];
  static int16_t out[kAudioBlock * FDVR_AUDIO_UPSAMPLE * 2];
  fdvr::fx::Upsampler up(FDVR_AUDIO_UPSAMPLE, FDVR_AUDIO_CHANNELS);
  for (;;) {
    {
      FxLock lock;
      fx_node.readAudio(in, kAudioBlock);
    }
    up.process(in, kAudioBlock, out);
    size_t written = 0;
    i2s_write(kI2s, out, sizeof(out), &written, portMAX_DELAY);
  }
}
#endif  // FDVR_HAS_AUDIO

// Network side of the effects, time-outs and the DMX refresh.
void fxTask(void*) {
  static uint8_t buf[fdvr::fx::kMaxPacket + 8];
#if FDVR_HAS_DMX
  uint32_t next_dmx_ms = millis();
#endif
  for (;;) {
    const uint32_t now_ms = millis();
    while (network_up && fx_udp.parsePacket() > 0) {
      const int n = fx_udp.read(buf, sizeof(buf));
      uint8_t info[fdvr::fx::kInfoSize];
      size_t info_len = 0;
      {
        FxLock lock;
        if (fx_node.handle(buf, n > 0 ? static_cast<size_t>(n) : 0, now_ms)) info_len = fx_node.makeInfo(now_ms, info);
      }
      if (info_len > 0) {  // a HELLO: tell the controller what this node has
        fx_udp.beginPacket(fx_udp.remoteIP(), fx_udp.remotePort());
        fx_udp.write(info, info_len);
        fx_udp.endPacket();
#if FDVR_HAS_INPUTS
        fx_peer_ip = fx_udp.remoteIP();
        fx_peer_port = fx_udp.remotePort();
        fx_peer_ms = now_ms;
        fx_peer_known = true;
#endif
      }
    }
    {
      FxLock lock;
      fx_node.setInhibit(fx_inhibit);
      fx_node.update(now_ms);
    }
#if FDVR_HAS_INPUTS
    {
      uint8_t report[fdvr::fx::kInputSize];
      size_t report_len = 0;
      const uint8_t levels = readInputs();
      {
        FxLock lock;
        fx_node.setInputs(levels, now_ms);
        report_len = fx_node.makeInput(now_ms, report);
      }
      // only to a controller that has been heard of in the last five seconds
      if (report_len > 0 && network_up && fx_peer_known && static_cast<uint32_t>(now_ms - fx_peer_ms) < 5000) {
        fx_udp.beginPacket(fx_peer_ip, fx_peer_port);
        fx_udp.write(report, report_len);
        fx_udp.endPacket();
      }
    }
#endif
#if FDVR_HAS_DMX
    if (static_cast<int32_t>(now_ms - next_dmx_ms) >= 0) {
      next_dmx_ms += 1000 / FDVR_DMX_REFRESH_HZ;
      if (static_cast<int32_t>(now_ms - next_dmx_ms) > 100) next_dmx_ms = now_ms;
      sendDmxFrame();
    }
#endif
    vTaskDelay(1);
  }
}

void setupEffects() {
  fx_mutex = xSemaphoreCreateMutex();
#if FDVR_HAS_DMX
  setupDmx();
#endif
#if FDVR_HAS_AUDIO
  setupAudio();
#endif
#if FDVR_HAS_INPUTS
  setupInputs();
#endif
  if (network_up) fx_udp.begin(FDVR_FX_UDP_PORT);
  // Core 0, beside the network stack; the motion loop has core 1 to itself.
  xTaskCreatePinnedToCore(fxTask, "fdvr-fx", 6144, nullptr, 2, nullptr, 0);
#if FDVR_HAS_AUDIO
  xTaskCreatePinnedToCore(audioTask, "fdvr-audio", 4096, nullptr, 3, nullptr, 0);
#endif
}

#endif  // FDVR_FX

void setupNetwork() {
  IPAddress ip(FDVR_IP), gateway(FDVR_GATEWAY), subnet(FDVR_SUBNET);
#if FDVR_NET_ETHERNET
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ETH.begin(ETH_PHY_LAN8720, FDVR_ETH_PHY_ADDR, FDVR_ETH_PHY_MDC, FDVR_ETH_PHY_MDIO, FDVR_ETH_PHY_POWER,
            ETH_CLOCK_GPIO0_IN);
#else
  ETH.begin(FDVR_ETH_PHY_ADDR, FDVR_ETH_PHY_POWER, FDVR_ETH_PHY_MDC, FDVR_ETH_PHY_MDIO, ETH_PHY_LAN8720,
            ETH_CLOCK_GPIO0_IN);
#endif
  ETH.config(ip, gateway, subnet);
  network_up = true;
#elif FDVR_NET_WIFI
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // power save adds tens of milliseconds of latency
  WiFi.config(ip, gateway, subnet);
  WiFi.begin(FDVR_WIFI_SSID, FDVR_WIFI_PASSWORD);
  network_up = true;
#endif
#if FDVR_HAS_ACTUATORS
  if (network_up) udp.begin(FDVR_UDP_PORT);
#endif
}

}  // namespace

void setup() {
  Serial.begin(FDVR_SERIAL_BAUD);
  if (FDVR_ESTOP_PIN >= 0) pinMode(FDVR_ESTOP_PIN, INPUT_PULLUP);
#if FDVR_HAS_ACTUATORS
  setupOutputs();
  writeOutputs(0);  // park position, drives disabled until the first valid command
#endif
  setupNetwork();
#if FDVR_FX
  setupEffects();
#endif
  enableLoopWDT();  // hardware watchdog: a hung loop resets the chip (drives must fail safe on signal loss)
#if FDVR_HAS_ACTUATORS
  last_update_us = micros();
#endif
}

void loop() {
  feedLoopWDT();
  const bool estop = FDVR_ESTOP_PIN >= 0 && digitalRead(FDVR_ESTOP_PIN) == LOW;
#if FDVR_FX
  fx_inhibit = estop;  // the effects tasks black out and fall silent while the emergency stop is active
#endif
#if FDVR_HAS_ACTUATORS
  const uint32_t now_ms = millis();
  pollUdp(now_ms);
  pollSerial(now_ms);
  if (FDVR_ESTOP_PIN >= 0) guard.setEstop(estop);
  const uint32_t now_us = micros();
  const uint32_t elapsed = now_us - last_update_us;
#if FDVR_FEEDBACK
  pollFeedback(now_us);
#endif
#if FDVR_READY_PIN >= 0
  pollReady(now_ms);
#endif
  if (elapsed >= 1000) {
    guard.update(now_ms, elapsed / 1e6f);
    last_update_us = now_us;
    writeOutputs(now_ms);
  }
  if (now_ms - last_status_ms >= 100) sendStatus(now_ms);  // keep-alive status when idle
  updateLed(now_ms);
#else
  (void)estop;
  delay(5);  // an effects-only node: everything happens in the effects tasks
#endif
}
