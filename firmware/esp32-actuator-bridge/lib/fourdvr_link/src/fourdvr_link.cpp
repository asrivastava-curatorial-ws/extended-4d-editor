// 4D VR Theatre - ESP32 actuator bridge: portable link core. See fourdvr_link.h.
#include "fourdvr_link.h"

#include <string.h>

namespace fdvr {
namespace {

const uint8_t kCmdMagic[4] = {'4', 'D', 'M', 'C'};
const uint8_t kStatusMagic[4] = {'4', 'D', 'M', 'S'};

void put16(uint8_t* p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v);
  p[1] = static_cast<uint8_t>(v >> 8);
}
void put32(uint8_t* p, uint32_t v) {
  for (int i = 0; i < 4; ++i) p[i] = static_cast<uint8_t>(v >> (8 * i));
}
void put64(uint8_t* p, uint64_t v) {
  for (int i = 0; i < 8; ++i) p[i] = static_cast<uint8_t>(v >> (8 * i));
}
uint16_t get16(const uint8_t* p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }
uint32_t get32(const uint8_t* p) {
  uint32_t v = 0;
  for (int i = 3; i >= 0; --i) v = (v << 8) | p[i];
  return v;
}
uint64_t get64(const uint8_t* p) {
  uint64_t v = 0;
  for (int i = 7; i >= 0; --i) v = (v << 8) | p[i];
  return v;
}

DecodeResult checkHeader(const uint8_t* buf, size_t len, size_t size, const uint8_t* magic, uint8_t type) {
  if (len != size) return DecodeResult::BadLength;
  if (memcmp(buf, magic, 4) != 0) return DecodeResult::BadMagic;
  if (buf[4] != kVersionMajor) return DecodeResult::BadVersion;
  if (buf[6] != type) return DecodeResult::BadType;
  if (get32(buf + size - 4) != crc32(buf, size - 4)) return DecodeResult::BadCrc;
  return DecodeResult::Ok;
}

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

}  // namespace

uint32_t crc32(const uint8_t* data, size_t len) {
  // CRC-32/ISO-HDLC (zlib): reflected, polynomial 0xEDB88320, init/xorout 0xFFFFFFFF.
  static uint32_t table[256];
  static bool ready = false;
  if (!ready) {
    for (uint32_t i = 0; i < 256; ++i) {
      uint32_t c = i;
      for (int k = 0; k < 8; ++k) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
      table[i] = c;
    }
    ready = true;
  }
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < len; ++i) crc = table[(crc ^ data[i]) & 0xFF] ^ (crc >> 8);
  return crc ^ 0xFFFFFFFFu;
}

size_t encodeCommand(const Command& cmd, uint8_t* out) {
  memcpy(out, kCmdMagic, 4);
  out[4] = kVersionMajor;
  out[5] = kVersionMinor;
  out[6] = kTypeCommand;
  out[7] = cmd.platform_id;
  put32(out + 8, cmd.sequence);
  put64(out + 12, cmd.timestamp_us);
  out[20] = static_cast<uint8_t>(cmd.state);
  out[21] = cmd.flags;
  put16(out + 22, 0);
  for (int i = 0; i < kAxes; ++i) put32(out + 24 + 4 * i, static_cast<uint32_t>(cmd.targets_um[i]));
  put32(out + 48, crc32(out, 48));
  return kCommandSize;
}

DecodeResult decodeCommand(const uint8_t* buf, size_t len, Command& out) {
  DecodeResult r = checkHeader(buf, len, kCommandSize, kCmdMagic, kTypeCommand);
  if (r != DecodeResult::Ok) return r;
  if (buf[20] > static_cast<uint8_t>(CommandState::FaultReset)) return DecodeResult::BadState;
  out.platform_id = buf[7];
  out.sequence = get32(buf + 8);
  out.timestamp_us = get64(buf + 12);
  out.state = static_cast<CommandState>(buf[20]);
  out.flags = buf[21];
  for (int i = 0; i < kAxes; ++i) out.targets_um[i] = static_cast<int32_t>(get32(buf + 24 + 4 * i));
  return DecodeResult::Ok;
}

size_t encodeStatus(const Status& st, uint8_t* out) {
  memcpy(out, kStatusMagic, 4);
  out[4] = kVersionMajor;
  out[5] = kVersionMinor;
  out[6] = kTypeStatus;
  out[7] = st.platform_id;
  put32(out + 8, st.last_sequence);
  put64(out + 12, st.echo_timestamp_us);
  put32(out + 20, st.uptime_ms);
  out[24] = static_cast<uint8_t>(st.device_state);
  out[25] = st.faults;
  put16(out + 26, 0);
  for (int i = 0; i < kAxes; ++i) put32(out + 28 + 4 * i, static_cast<uint32_t>(st.positions_um[i]));
  put32(out + 52, st.received);
  put16(out + 56, st.lost);
  put16(out + 58, st.duplicate);
  put16(out + 60, st.invalid);
  put16(out + 62, st.stale);
  put16(out + 64, st.timeouts);
  put16(out + 66, 0);
  put32(out + 68, crc32(out, 68));
  return kStatusSize;
}

DecodeResult decodeStatus(const uint8_t* buf, size_t len, Status& out) {
  DecodeResult r = checkHeader(buf, len, kStatusSize, kStatusMagic, kTypeStatus);
  if (r != DecodeResult::Ok) return r;
  out.platform_id = buf[7];
  out.last_sequence = get32(buf + 8);
  out.echo_timestamp_us = get64(buf + 12);
  out.uptime_ms = get32(buf + 20);
  out.device_state = static_cast<DeviceState>(buf[24]);
  out.faults = buf[25];
  for (int i = 0; i < kAxes; ++i) out.positions_um[i] = static_cast<int32_t>(get32(buf + 28 + 4 * i));
  out.received = get32(buf + 52);
  out.lost = get16(buf + 56);
  out.duplicate = get16(buf + 58);
  out.invalid = get16(buf + 60);
  out.stale = get16(buf + 62);
  out.timeouts = get16(buf + 64);
  return DecodeResult::Ok;
}

size_t cobsEncode(const uint8_t* in, size_t len, uint8_t* out) {
  size_t write = 1, code_pos = 0;
  uint8_t code = 1;
  for (size_t i = 0; i < len; ++i) {
    if (in[i] == 0) {
      out[code_pos] = code;
      code_pos = write++;
      code = 1;
    } else {
      out[write++] = in[i];
      if (++code == 0xFF) {
        out[code_pos] = code;
        code_pos = write++;
        code = 1;
      }
    }
  }
  out[code_pos] = code;
  return write;
}

bool cobsDecode(const uint8_t* in, size_t len, uint8_t* out, size_t& out_len) {
  size_t i = 0, w = 0;
  while (i < len) {
    uint8_t code = in[i];
    if (code == 0) return false;
    ++i;
    size_t end = i + code - 1;
    if (end > len) return false;
    while (i < end) out[w++] = in[i++];
    if (code < 0xFF && i < len) out[w++] = 0;
  }
  out_len = w;
  return true;
}

bool SequenceTracker::accept(uint32_t seq) {
  if (!have_) {
    have_ = true;
    last_ = seq;
    return true;
  }
  uint32_t diff = seq - last_;  // unsigned wrap-around arithmetic
  if (diff == 0 || diff >= 0x80000000u) {
    ++duplicate_;
    return false;
  }
  lost_ += diff - 1;
  last_ = seq;
  return true;
}

LinkGuard::LinkGuard(const GuardConfig& cfg) : cfg_(cfg) {
  for (int i = 0; i < kAxes; ++i) {
    output_[i] = static_cast<float>(cfg_.park_um[i]);
    target_[i] = output_[i];
  }
}

void LinkGuard::invalidPacket(uint32_t now_ms) {
  ++invalid_;
  if (now_ms - invalid_window_start_ms_ >= 1000) {
    invalid_window_start_ms_ = now_ms;
    invalid_in_window_ = 0;
  }
  if (++invalid_in_window_ >= cfg_.invalid_burst) latch(kFaultInvalidBurst);
}

void LinkGuard::latch(uint8_t fault) {
  faults_ |= fault;
  if (state_ != DeviceState::Parked && state_ != DeviceState::Fault) state_ = DeviceState::Parking;
}

bool LinkGuard::handle(const uint8_t* data, size_t len, uint32_t now_ms) {
  Command cmd;
  if (decodeCommand(data, len, cmd) != DecodeResult::Ok || cmd.platform_id != cfg_.platform_id) {
    invalidPacket(now_ms);
    return false;
  }
  // Learn the controller time base: the largest (timestamp - local time) belongs to the packet
  // with the shortest transit; a packet lagging that estimate by more than stale_ms is stale.
  const int64_t now_us = static_cast<int64_t>(now_ms) * 1000;
  const int64_t offset = static_cast<int64_t>(cmd.timestamp_us) - now_us;
  if (!have_offset_ || offset > offset_us_) {
    offset_us_ = offset;
    have_offset_ = true;
  }
  const int64_t age_us = now_us + offset_us_ - static_cast<int64_t>(cmd.timestamp_us);
  if (age_us > static_cast<int64_t>(cfg_.stale_ms) * 1000) {
    ++stale_;
    if (++stale_run_ >= 5) {  // controller time base changed (restart/resync): relearn
      have_offset_ = false;
      stale_run_ = 0;
    }
    return false;
  }
  stale_run_ = 0;
  if (!seq_.accept(cmd.sequence)) return false;
  ++received_;
  have_rx_ = true;
  last_rx_ms_ = now_ms;
  echo_ts_us_ = cmd.timestamp_us;
  apply(cmd);
  return true;
}

void LinkGuard::apply(const Command& cmd) {
  if (cmd.state == CommandState::FaultReset) {
    if (faults_ != kFaultNone && !estop_) {
      faults_ = kFaultNone;
      state_ = DeviceState::Idle;
      for (int i = 0; i < kAxes; ++i) target_[i] = output_[i];
    }
    return;
  }
  if (faults_ != kFaultNone) return;  // latched: only FAULT_RESET is accepted
  switch (cmd.state) {
    case CommandState::Run:
      if (cmd.flags & 1) {
        for (int i = 0; i < kAxes; ++i)
          target_[i] = clampf(static_cast<float>(cmd.targets_um[i]), 0.0f, static_cast<float>(cfg_.stroke_um[i]));
        state_ = DeviceState::Running;
      }
      break;
    case CommandState::Hold:
      for (int i = 0; i < kAxes; ++i) target_[i] = output_[i];
      state_ = DeviceState::Hold;
      break;
    case CommandState::Park:
      if (state_ != DeviceState::Parked) state_ = DeviceState::Parking;
      break;
    case CommandState::Idle:
      for (int i = 0; i < kAxes; ++i) target_[i] = output_[i];
      state_ = DeviceState::Idle;
      break;
    default:
      break;
  }
}

void LinkGuard::update(uint32_t now_ms, float dt_s) {
  if (estop_) {
    faults_ |= kFaultEstop;
    state_ = DeviceState::Fault;
    for (int i = 0; i < kAxes; ++i) target_[i] = output_[i];
  } else if ((state_ == DeviceState::Running || state_ == DeviceState::Hold || state_ == DeviceState::Idle) &&
             have_rx_ && (now_ms - last_rx_ms_) > cfg_.timeout_ms) {
    ++timeouts_;
    latch(kFaultTimeout);
  }
  float speed = static_cast<float>(cfg_.max_speed_um_s);
  if (state_ == DeviceState::Parking || state_ == DeviceState::Parked) {
    for (int i = 0; i < kAxes; ++i) target_[i] = static_cast<float>(cfg_.park_um[i]);
    speed = static_cast<float>(cfg_.park_speed_um_s);
  }
  if (state_ == DeviceState::Fault) return;
  const float step = speed * (dt_s > 0 ? dt_s : 0.0f);
  bool at_park = true;
  for (int i = 0; i < kAxes; ++i) {
    output_[i] += clampf(target_[i] - output_[i], -step, step);
    float d = target_[i] - output_[i];
    if (d > 1.0f || d < -1.0f) at_park = false;
  }
  if (state_ == DeviceState::Parking && at_park) state_ = DeviceState::Parked;
}

void LinkGuard::makeStatus(uint32_t now_ms, Status& st) const {
  st.platform_id = cfg_.platform_id;
  st.last_sequence = seq_.last();
  st.echo_timestamp_us = echo_ts_us_;
  st.uptime_ms = now_ms;
  st.device_state = state_;
  st.faults = faults_;
  for (int i = 0; i < kAxes; ++i) {
    float v = output_[i];
    st.positions_um[i] = static_cast<int32_t>(v >= 0 ? v + 0.5f : v - 0.5f);
  }
  st.received = received_;
  st.lost = static_cast<uint16_t>(seq_.lost() > 0xFFFF ? 0xFFFF : seq_.lost());
  st.duplicate = static_cast<uint16_t>(seq_.duplicate() > 0xFFFF ? 0xFFFF : seq_.duplicate());
  st.invalid = static_cast<uint16_t>(invalid_ > 0xFFFF ? 0xFFFF : invalid_);
  st.stale = static_cast<uint16_t>(stale_ > 0xFFFF ? 0xFFFF : stale_);
  st.timeouts = static_cast<uint16_t>(timeouts_ > 0xFFFF ? 0xFFFF : timeouts_);
}

}  // namespace fdvr
