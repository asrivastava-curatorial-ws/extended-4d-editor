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

#define FDVR_FX (FDVR_HAS_DMX || FDVR_HAS_AUDIO)

#if FDVR_HAS_ACTUATORS
#include "fourdvr_link.h"
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
const int kPwmPins[fdvr::kAxes] = FDVR_PWM_PINS;
const int32_t kStroke[fdvr::kAxes] = FDVR_STROKE_UM;

void setupOutputs() {
#if FDVR_OUTPUT_PWM
  for (int i = 0; i < fdvr::kAxes; ++i) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(kPwmPins[i], FDVR_PWM_FREQ_HZ, FDVR_PWM_BITS);
#else
    ledcSetup(i, FDVR_PWM_FREQ_HZ, FDVR_PWM_BITS);
    ledcAttachPin(kPwmPins[i], i);
#endif
  }
#endif
  if (FDVR_ENABLE_PIN >= 0) {
    pinMode(FDVR_ENABLE_PIN, OUTPUT);
    digitalWrite(FDVR_ENABLE_PIN, LOW);
  }
  if (FDVR_STATUS_LED_PIN >= 0) pinMode(FDVR_STATUS_LED_PIN, OUTPUT);
}

void writeOutputs() {
  const float* out = guard.output();
#if FDVR_OUTPUT_PWM
  for (int i = 0; i < fdvr::kAxes; ++i) {
    float frac = kStroke[i] > 0 ? out[i] / static_cast<float>(kStroke[i]) : 0.0f;
    if (frac < 0.0f) frac = 0.0f;
    if (frac > 1.0f) frac = 1.0f;
    uint32_t duty = FDVR_PWM_DUTY_MIN + static_cast<uint32_t>(frac * (FDVR_PWM_DUTY_MAX - FDVR_PWM_DUTY_MIN));
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(kPwmPins[i], duty);
#else
    ledcWrite(i, duty);
#endif
  }
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
// A pin of the effects outputs must not be one the platform outputs use.
constexpr int kMotionPins[fdvr::kAxes] = FDVR_PWM_PINS;
constexpr bool pinFree(int pin) {
  for (int i = 0; i < fdvr::kAxes; ++i) {
    if (FDVR_OUTPUT_PWM && kMotionPins[i] == pin) return false;
  }
  return pin != FDVR_ENABLE_PIN && pin != FDVR_ESTOP_PIN && pin != FDVR_STATUS_LED_PIN;
}
#if FDVR_HAS_DMX
static_assert(pinFree(FDVR_DMX_TX_PIN), "FDVR_DMX_TX_PIN is already used by the platform outputs: choose a free pin in config.h");
static_assert(FDVR_DMX_DE_PIN < 0 || pinFree(FDVR_DMX_DE_PIN), "FDVR_DMX_DE_PIN is already used by the platform outputs");
#endif
#if FDVR_HAS_AUDIO
static_assert(pinFree(FDVR_I2S_BCK_PIN) && pinFree(FDVR_I2S_WS_PIN) && pinFree(FDVR_I2S_DOUT_PIN),
              "an I2S pin is already used by the platform outputs: set FDVR_I2S_BCK_PIN / _WS_PIN / _DOUT_PIN to free pins");
#endif
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
      }
    }
    {
      FxLock lock;
      fx_node.setInhibit(fx_inhibit);
      fx_node.update(now_ms);
    }
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
  writeOutputs();  // park position, drives disabled until the first valid command
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
  if (elapsed >= 1000) {
    guard.update(now_ms, elapsed / 1e6f);
    last_update_us = now_us;
    writeOutputs();
  }
  if (now_ms - last_status_ms >= 100) sendStatus(now_ms);  // keep-alive status when idle
  updateLed(now_ms);
#else
  (void)estop;
  delay(5);  // an effects-only node: everything happens in the effects tasks
#endif
}
