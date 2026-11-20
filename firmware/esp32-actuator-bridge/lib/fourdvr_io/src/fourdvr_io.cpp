// Extended 4D Editor - ESP32 actuator bridge: the actuator side, portable (see fourdvr_io.h).
#include "fourdvr_io.h"

namespace fdvr {
namespace io {

uint32_t pwmDuty(float fraction, uint32_t duty_min, uint32_t duty_max) {
  if (fraction < 0.0f) fraction = 0.0f;
  if (fraction > 1.0f) fraction = 1.0f;
  return duty_min + static_cast<uint32_t>(fraction * static_cast<float>(duty_max - duty_min) + 0.5f);
}

uint16_t dacCode(float fraction, uint16_t code_retracted, uint16_t code_extended) {
  if (fraction < 0.0f) fraction = 0.0f;
  if (fraction > 1.0f) fraction = 1.0f;
  const float code = static_cast<float>(code_retracted) + fraction * (static_cast<float>(code_extended) - static_cast<float>(code_retracted));
  const int rounded = static_cast<int>(code + 0.5f);
  return static_cast<uint16_t>(rounded < 0 ? 0 : (rounded > 4095 ? 4095 : rounded));
}

size_t gp8403Range10V(uint8_t* out) {
  out[0] = 0x01;  // output range register
  out[1] = 0x11;  // 0-10 V on both channels
  return 2;
}

size_t gp8403Channel(uint8_t channel, uint16_t code, uint8_t* out) {
  const uint16_t data = static_cast<uint16_t>((code > 4095 ? 4095 : code) << 4);
  out[0] = channel == 0 ? 0x02 : 0x04;
  out[1] = static_cast<uint8_t>(data & 0xFF);
  out[2] = static_cast<uint8_t>(data >> 8);
  return 3;
}

uint16_t ads1115Config(uint8_t input, uint8_t pga) {
  return static_cast<uint16_t>(0x8000u                               // OS: start a single conversion
                               | ((4u + (input & 3u)) << 12)          // MUX: AINx against GND
                               | ((pga & 7u) << 9)                   // PGA
                               | 0x0100u                             // MODE: single shot
                               | (7u << 5)                           // DR: 860 samples/s
                               | 0x0003u);                           // comparator off
}

bool sensorUm(int16_t raw, const SensorScale& s, int32_t stroke_um, float& um) {
  if (raw < s.raw_min_valid || raw > s.raw_max_valid || s.raw_extended == s.raw_retracted) return false;
  const float f = (static_cast<float>(raw) - s.raw_retracted) / (static_cast<float>(s.raw_extended) - s.raw_retracted);
  um = (f < 0.0f ? 0.0f : (f > 1.0f ? 1.0f : f)) * static_cast<float>(stroke_um);
  return true;
}

FeedbackScanner::Action FeedbackScanner::next(uint32_t now_us) {
  if (axes_ == 0) return Action::None;
  if (!waiting_) {
    waiting_ = true;
    started_us_ = now_us;
    return Action::Start;
  }
  if (now_us - started_us_ >= conversion_us_) return Action::Read;
  return Action::None;
}

void FeedbackScanner::result(bool ok, int16_t /*raw*/) {
  if (!ok) ++errors_;
  waiting_ = false;
  axis_ = (axis_ + 1) % axes_;
}

bool ReadyInput::update(bool raw_ok, uint32_t now_ms) {
  if (!have_ || raw_ok != last_raw_) {
    have_ = true;
    last_raw_ = raw_ok;
    since_ms_ = now_ms;
  }
  if (raw_ok != ok_ && now_ms - since_ms_ >= debounce_ms_) ok_ = raw_ok;
  return ok_;
}

}  // namespace io
}  // namespace fdvr
