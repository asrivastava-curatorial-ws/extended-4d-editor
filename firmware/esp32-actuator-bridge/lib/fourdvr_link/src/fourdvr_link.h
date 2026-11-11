// 4D VR Theatre - ESP32 actuator bridge: portable link core (no Arduino dependencies).
//
// Implements the binary link protocol 1.0 (ESP32_PROTOCOL.md): packet encoding/decoding with
// CRC-32, COBS framing for serial links, duplicate/loss/stale detection, the communication
// watchdog, latched faults and the speed-limited ramp to the park position.
// The Motion Controller's simulator (fourdvr_motion/sim_esp32.py) is a line-by-line port of
// this file; both are checked against test/vectors.txt.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace fdvr {

constexpr uint8_t kVersionMajor = 1;
constexpr uint8_t kVersionMinor = 0;
constexpr uint8_t kTypeCommand = 1;
constexpr uint8_t kTypeStatus = 2;
constexpr size_t kCommandSize = 52;
constexpr size_t kStatusSize = 72;
constexpr int kAxes = 6;

enum class CommandState : uint8_t { Idle = 0, Run = 1, Hold = 2, Park = 3, FaultReset = 4 };
enum class DeviceState : uint8_t { Boot = 0, Idle = 1, Running = 2, Hold = 3, Parking = 4, Parked = 5, Fault = 6 };

enum FaultBits : uint8_t {
  kFaultNone = 0,
  kFaultTimeout = 1,
  kFaultInvalidBurst = 2,
  kFaultStale = 4,
  kFaultOutput = 8,
  kFaultEstop = 16,
  kFaultConfig = 32,
};

enum class DecodeResult : uint8_t { Ok, BadLength, BadMagic, BadVersion, BadType, BadCrc, BadState };

struct Command {
  uint8_t platform_id = 0;
  uint32_t sequence = 0;
  uint64_t timestamp_us = 0;
  CommandState state = CommandState::Idle;
  uint8_t flags = 0;
  int32_t targets_um[kAxes] = {0, 0, 0, 0, 0, 0};
};

struct Status {
  uint8_t platform_id = 0;
  uint32_t last_sequence = 0;
  uint64_t echo_timestamp_us = 0;
  uint32_t uptime_ms = 0;
  DeviceState device_state = DeviceState::Boot;
  uint8_t faults = 0;
  int32_t positions_um[kAxes] = {0, 0, 0, 0, 0, 0};
  uint32_t received = 0;
  uint16_t lost = 0;
  uint16_t duplicate = 0;
  uint16_t invalid = 0;
  uint16_t stale = 0;
  uint16_t timeouts = 0;
};

uint32_t crc32(const uint8_t* data, size_t len);

size_t encodeCommand(const Command& cmd, uint8_t* out);  // out must hold kCommandSize bytes
DecodeResult decodeCommand(const uint8_t* buf, size_t len, Command& out);
size_t encodeStatus(const Status& st, uint8_t* out);  // out must hold kStatusSize bytes
DecodeResult decodeStatus(const uint8_t* buf, size_t len, Status& out);

// COBS: encoded output needs len + len / 254 + 1 bytes. Returns encoded length.
size_t cobsEncode(const uint8_t* in, size_t len, uint8_t* out);
// Returns false on malformed input. out must hold at least len bytes.
bool cobsDecode(const uint8_t* in, size_t len, uint8_t* out, size_t& out_len);

class SequenceTracker {
 public:
  bool accept(uint32_t seq);
  bool have() const { return have_; }
  uint32_t last() const { return last_; }
  uint32_t lost() const { return lost_; }
  uint32_t duplicate() const { return duplicate_; }

 private:
  bool have_ = false;
  uint32_t last_ = 0;
  uint32_t lost_ = 0;
  uint32_t duplicate_ = 0;
};

struct GuardConfig {
  uint8_t platform_id = 1;
  int32_t stroke_um[kAxes] = {300000, 300000, 300000, 300000, 300000, 300000};
  int32_t park_um[kAxes] = {150000, 150000, 150000, 150000, 150000, 150000};
  int32_t max_speed_um_s = 500000;   // slew limit while tracking (defence in depth)
  int32_t park_speed_um_s = 60000;   // ramp speed toward the park position
  uint32_t timeout_ms = 200;         // no valid command for this long => fault + park
  uint32_t stale_ms = 100;           // command older than this (vs learned time base) is dropped
  uint16_t invalid_burst = 20;       // invalid packets within 1 s that latch a fault
};

class LinkGuard {
 public:
  explicit LinkGuard(const GuardConfig& cfg);

  // Process one received packet. Returns true when the command was accepted.
  bool handle(const uint8_t* data, size_t len, uint32_t now_ms);
  // Advance outputs (call every ~1 ms).
  void update(uint32_t now_ms, float dt_s);
  // Hardware emergency-stop/safety-chain input (informational; never replaces the hardware chain).
  void setEstop(bool active) { estop_ = active; }

  void makeStatus(uint32_t now_ms, Status& st) const;
  const float* output() const { return output_; }
  DeviceState state() const { return state_; }
  uint8_t faults() const { return faults_; }
  uint32_t received() const { return received_; }
  uint32_t invalid() const { return invalid_; }
  uint32_t stale() const { return stale_; }
  uint32_t timeouts() const { return timeouts_; }
  const SequenceTracker& sequence() const { return seq_; }

 private:
  void invalidPacket(uint32_t now_ms);
  void latch(uint8_t fault);
  void apply(const Command& cmd);

  GuardConfig cfg_;
  DeviceState state_ = DeviceState::Idle;
  uint8_t faults_ = kFaultNone;
  uint32_t received_ = 0;
  uint32_t invalid_ = 0;
  uint32_t stale_ = 0;
  uint32_t timeouts_ = 0;
  bool have_rx_ = false;
  uint32_t last_rx_ms_ = 0;
  uint64_t echo_ts_us_ = 0;
  SequenceTracker seq_;
  bool have_offset_ = false;
  int64_t offset_us_ = 0;
  uint8_t stale_run_ = 0;
  uint32_t invalid_window_start_ms_ = 0;
  uint16_t invalid_in_window_ = 0;
  bool estop_ = false;
  float output_[kAxes];
  float target_[kAxes];
};

}  // namespace fdvr
