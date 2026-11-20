// Extended 4D Editor - ESP32 actuator bridge: the actuator side, portable (no Arduino dependencies).
//
// What a board does with each actuator's commanded position, and what it reads back:
//   * a position signal: PWM (for drives with a PWM input, or a PWM-to-0-10 V converter), or
//   * an analog 0-10 V set-point from GP8403 I2C DACs (two channels each, e.g. DFRobot DFR0971), for
//     servo drives, hydraulic valve amplifiers and servo-pneumatic positioners (their own position
//     loop holds the actuator; ranges such as +-10 V or 4-20 mA come from a signal converter), or
//   * plain valves: an output on when the commanded position is at least half the stroke;
//   * position sensors (4-20 mA or 0-10 V transducers) read by ADS1115 I2C ADCs (four inputs each);
//   * a supply / drive-ready input (pressure switch, pump running, drive ready).
// main.cpp moves the bytes over I2C (Wire) and the pins; everything decided here is tested on a PC
// (test/host/test_io.cpp).
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace fdvr {
namespace io {

// ------------------------------------------------------------------------------------------- outputs
inline float strokeFraction(float um, int32_t stroke_um) {
  if (stroke_um <= 0) return 0.0f;
  float f = um / static_cast<float>(stroke_um);
  return f < 0.0f ? 0.0f : (f > 1.0f ? 1.0f : f);
}

// PWM duty for a stroke fraction, mapped onto duty_min..duty_max.
uint32_t pwmDuty(float fraction, uint32_t duty_min, uint32_t duty_max);

// 12-bit DAC code for a stroke fraction: code_retracted at 0, code_extended at 1 (either may be the larger).
uint16_t dacCode(float fraction, uint16_t code_retracted, uint16_t code_extended);

// GP8403: I2C address of the DAC for an actuator (two channels per DAC) and its channel.
inline uint8_t dacAddress(int axis, uint8_t first) { return static_cast<uint8_t>(first + axis / 2); }
inline uint8_t dacChannel(int axis) { return static_cast<uint8_t>(axis % 2); }
// Bytes after the I2C address: select the 0-10 V output range (once, at start-up) ...
size_t gp8403Range10V(uint8_t* out);  // writes 2 bytes
// ... and a channel's code (register 0x02 for channel 0, 0x04 for channel 1; code << 4, low byte first).
size_t gp8403Channel(uint8_t channel, uint16_t code, uint8_t* out);  // writes 3 bytes

// A valve is on (extend) when the commanded position is at least half the stroke.
inline bool valveOn(float um, int32_t stroke_um) { return um * 2.0f >= static_cast<float>(stroke_um); }

// ------------------------------------------------------------------------------------------- feedback
// ADS1115: I2C address of the ADC for an actuator (four inputs per ADC) and its input.
inline uint8_t adcAddress(int axis, uint8_t first) { return static_cast<uint8_t>(first + axis / 4); }
inline uint8_t adcInput(int axis) { return static_cast<uint8_t>(axis % 4); }
// Configuration register (0x01) for one single-shot conversion of input AINx against GND at 860
// samples/s, comparator off. pga: 0 = +-6.144 V, 1 = +-4.096 V, 2 = +-2.048 V, 3 = +-1.024 V.
uint16_t ads1115Config(uint8_t input, uint8_t pga);
constexpr uint8_t kAdsConversion = 0x00;
constexpr uint8_t kAdsConfig = 0x01;
constexpr uint32_t kAdsConversionUs = 1300;  // 860 samples/s = 1.16 ms, with a margin

struct SensorScale {
  int16_t raw_retracted;  // ADC reading with the actuator fully retracted ...
  int16_t raw_extended;   // ... and fully extended (either may be the larger)
  int16_t raw_min_valid;  // readings outside these are a broken wire or a missing sensor
  int16_t raw_max_valid;
};

// A reading as micrometres from the retracted end; false if it lies outside the valid range.
bool sensorUm(int16_t raw, const SensorScale& s, int32_t stroke_um, float& um);

// Reads the sensors one after the other without ever waiting: each call says what to do now.
class FeedbackScanner {
 public:
  enum class Action : uint8_t { None, Start, Read };
  FeedbackScanner(uint8_t axes, uint32_t conversion_us = kAdsConversionUs) : axes_(axes), conversion_us_(conversion_us) {}
  Action next(uint32_t now_us);
  int axis() const { return axis_; }  // the actuator the action is for
  // After a Read (or a failed transaction): the reading of axis(); moves on to the next actuator.
  void result(bool ok, int16_t raw);
  uint32_t errors() const { return errors_; }

 private:
  uint8_t axes_;
  uint32_t conversion_us_;
  int axis_ = 0;
  bool waiting_ = false;
  uint32_t started_us_ = 0;
  uint32_t errors_ = 0;
};

// Per actuator: the last good position and how many readings in a row were bad.
class SensorWatch {
 public:
  static constexpr uint8_t kBadInARow = 3;  // this many bad readings in a row: a FEEDBACK fault
  void good(float um) {
    um_ = um;
    bad_ = 0;
    have_ = true;
  }
  void bad() {
    if (bad_ < 255) ++bad_;
  }
  bool failed() const { return bad_ >= kBadInARow; }
  bool have() const { return have_; }
  float um() const { return um_; }

 private:
  float um_ = 0.0f;
  uint8_t bad_ = 0;
  bool have_ = false;
};

// --------------------------------------------------------------------------------- the supply input
// A supply / drive-ready input, debounced: it must be "ok" or "not ok" for debounce_ms before it counts.
class ReadyInput {
 public:
  explicit ReadyInput(uint32_t debounce_ms = 50) : debounce_ms_(debounce_ms) {}
  bool update(bool raw_ok, uint32_t now_ms);  // returns the debounced state
  bool ok() const { return ok_; }

 private:
  uint32_t debounce_ms_;
  bool ok_ = false;
  bool last_raw_ = false;
  bool have_ = false;
  uint32_t since_ms_ = 0;
};

}  // namespace io
}  // namespace fdvr
