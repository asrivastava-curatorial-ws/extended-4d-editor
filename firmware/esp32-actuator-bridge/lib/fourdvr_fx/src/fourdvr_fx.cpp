// Extended 4D Editor - effects node: portable core of the "4DFX" link. See fourdvr_fx.h.
#include "fourdvr_fx.h"

#include <string.h>

namespace fdvr {
namespace fx {

namespace {

uint16_t get16(const uint8_t* p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }
uint32_t get32(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) | (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}
uint64_t get64(const uint8_t* p) { return static_cast<uint64_t>(get32(p)) | (static_cast<uint64_t>(get32(p + 4)) << 32); }
void put16(uint8_t* p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v);
  p[1] = static_cast<uint8_t>(v >> 8);
}
void put32(uint8_t* p, uint32_t v) {
  put16(p, static_cast<uint16_t>(v));
  put16(p + 2, static_cast<uint16_t>(v >> 16));
}
void put64(uint8_t* p, uint64_t v) {
  put32(p, static_cast<uint32_t>(v));
  put32(p + 4, static_cast<uint32_t>(v >> 32));
}
void bump(uint16_t& counter) {
  if (counter != 0xFFFFu) ++counter;
}

const uint8_t kMagic[4] = {'4', 'D', 'F', 'X'};
const uint8_t kDark[kSlots] = {0};

}  // namespace

uint32_t crc32(const uint8_t* data, size_t len) {
  // CRC-32 (IEEE 802.3, as zlib), four bits at a time
  static const uint32_t table[16] = {0x00000000u, 0x1DB71064u, 0x3B6E20C8u, 0x26D930ACu, 0x76DC4190u, 0x6B6B51F4u,
                                     0x4DB26158u, 0x5005713Cu, 0xEDB88320u, 0xF00F9344u, 0xD6D6A3E8u, 0xCB61B38Cu,
                                     0x9B64C2B0u, 0x86D3D2D4u, 0xA00AE278u, 0xBDBDF21Cu};
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    crc = table[crc & 0x0Fu] ^ (crc >> 4);
    crc = table[crc & 0x0Fu] ^ (crc >> 4);
  }
  return ~crc;
}

Decode decode(const uint8_t* packet, size_t len, Packet& out) {
  if (packet == nullptr || len < kHeaderSize + kCrcSize) return Decode::Short;
  if (memcmp(packet, kMagic, 4) != 0) return Decode::Magic;
  if (packet[4] != kVersionMajor) return Decode::Version;
  if (get32(packet + len - kCrcSize) != crc32(packet, len - kCrcSize)) return Decode::Crc;
  out.type = packet[6];
  out.node = packet[7];
  out.sequence = get32(packet + 8);
  out.payload = packet + kHeaderSize;
  out.payload_len = len - kHeaderSize - kCrcSize;
  return Decode::Ok;
}

size_t encodeInfo(uint8_t node, uint32_t sequence, const Info& info, uint8_t* out) {
  memcpy(out, kMagic, 4);
  out[4] = kVersionMajor;
  out[5] = kVersionMinor;
  out[6] = kTypeInfo;
  out[7] = node;
  put32(out + 8, sequence);
  uint8_t* p = out + kHeaderSize;
  put64(p, info.echo_us);
  put32(p + 8, info.uptime_ms);
  put16(p + 12, info.firmware);
  p[14] = info.capabilities;
  p[15] = info.dmx_ports;
  p[16] = info.audio_channels;
  p[17] = info.state;
  put16(p + 18, info.audio_rate_hz);
  put16(p + 20, info.audio_latency_ms);
  put16(p + 22, info.audio_level_ms);
  p[24] = info.dmx_refresh_hz;
  p[25] = 0;
  put32(p + 26, info.packets);
  put16(p + 30, info.invalid);
  put16(p + 32, info.dmx_timeouts);
  put16(p + 34, info.audio_underruns);
  put16(p + 36, info.audio_gaps);
  put32(out + kHeaderSize + kInfoPayload, crc32(out, kHeaderSize + kInfoPayload));
  return kInfoSize;
}

Node::Node(const Config& config) : cfg_(config) {
  if (cfg_.dmx_ports > kMaxPorts) cfg_.dmx_ports = kMaxPorts;
  if (cfg_.audio_channels > kMaxChannels) cfg_.audio_channels = kMaxChannels;
  if (cfg_.audio_rate_hz < 1000) cfg_.audio_rate_hz = 1000;
  memset(dmx_, 0, sizeof(dmx_));
  memset(ring_, 0, sizeof(ring_));
  for (int i = 0; i < kMaxPorts; ++i) {
    dmx_last_ms_[i] = 0;
    dmx_active_[i] = false;
    dmx_slots_[i] = 0;
  }
  last_frame_[0] = last_frame_[1] = 0;
}

void Node::darken(int port) {
  memset(dmx_[port], 0, kSlots);
  dmx_active_[port] = false;
}

void Node::flushAudio() {
  head_ = 0;
  fill_ = 0;
  stream_open_ = false;
  playing_ = false;
  trim_count_ = 0;
}

void Node::setInhibit(bool inhibit) {
  if (inhibit && !inhibit_) {
    for (int port = 0; port < kMaxPorts; ++port) darken(port);
    flushAudio();
  }
  inhibit_ = inhibit;
}

bool Node::handle(const uint8_t* packet, size_t len, uint32_t now_ms) {
  Packet p;
  if (decode(packet, len, p) != Decode::Ok || (p.node != 0 && p.node != cfg_.node_id)) {
    bump(invalid_);
    return false;
  }
  switch (p.type) {
    case kTypeHello:
      if (p.payload_len < 8) break;
      echo_us_ = get64(p.payload);
      ++packets_;
      return true;
    case kTypeDmx:
      handleDmx(p, now_ms);
      return false;
    case kTypeAudio:
      handleAudio(p);
      return false;
    case kTypeStop:
      if (p.payload_len < 2) break;
      ++packets_;
      if (p.payload[0] & kStopDmx) {
        for (int port = 0; port < kMaxPorts; ++port) darken(port);
      }
      if (p.payload[0] & kStopAudio) flushAudio();
      return false;
    default:
      break;
  }
  bump(invalid_);
  return false;
}

void Node::handleDmx(const Packet& p, uint32_t now_ms) {
  if (p.payload_len < 6) {
    bump(invalid_);
    return;
  }
  const uint8_t port = p.payload[0];
  const uint16_t first = get16(p.payload + 2);
  const uint16_t count = get16(p.payload + 4);
  if (count < 1 || first < 1 || first + count - 1 > kSlots || p.payload_len != 6u + count) {
    bump(invalid_);
    return;
  }
  ++packets_;
  if (port >= cfg_.dmx_ports) return;  // a line this node does not have: nothing to do
  if (inhibit_) {
    memset(dmx_[port] + first - 1, 0, count);
  } else {
    memcpy(dmx_[port] + first - 1, p.payload + 6, count);
  }
  dmx_last_ms_[port] = now_ms;
  dmx_active_[port] = true;
  if (first + count - 1 > dmx_slots_[port]) dmx_slots_[port] = first + count - 1;
}

void Node::update(uint32_t now_ms) {
  for (int port = 0; port < cfg_.dmx_ports; ++port) {
    if (dmx_active_[port] && static_cast<uint32_t>(now_ms - dmx_last_ms_[port]) >= cfg_.dmx_timeout_ms) {
      darken(port);
      bump(dmx_timeouts_);
    }
  }
}

const uint8_t* Node::dmxFrame(int port) const {
  return (port >= 0 && port < cfg_.dmx_ports) ? dmx_[port] : kDark;
}

int Node::dmxSlots(int port) const {
  const int seen = (port >= 0 && port < cfg_.dmx_ports) ? dmx_slots_[port] : 0;
  return seen > 24 ? seen : 24;
}

bool Node::dmxActive(int port) const { return port >= 0 && port < cfg_.dmx_ports && dmx_active_[port]; }

void Node::pushFrame(const int16_t* frame) {
  if (fill_ == kAudioCapacity) {  // full: the oldest frame makes room
    head_ = (head_ + 1) % kAudioCapacity;
    --fill_;
  }
  const size_t at = ((head_ + fill_) % kAudioCapacity) * kMaxChannels;
  ring_[at] = frame[0];
  ring_[at + 1] = frame[1];
  ++fill_;
}

void Node::popFrame(int16_t* frame) {
  frame[0] = ring_[head_ * kMaxChannels];
  frame[1] = ring_[head_ * kMaxChannels + 1];
  head_ = (head_ + 1) % kAudioCapacity;
  --fill_;
}

void Node::handleAudio(const Packet& p) {
  if (p.payload_len < 10) {
    bump(invalid_);
    return;
  }
  const uint8_t channels = p.payload[0];
  const uint8_t flags = p.payload[1];
  const uint16_t rate = get16(p.payload + 2);
  const uint32_t position = get32(p.payload + 4);
  const uint16_t frames = get16(p.payload + 8);
  if (channels < 1 || channels > kMaxChannels || frames < 1 || frames > kMaxAudioFrames || rate < 1000 || rate > 48000 ||
      p.payload_len != 10u + 2u * frames * channels) {
    bump(invalid_);
    return;
  }
  ++packets_;
  if (cfg_.audio_channels == 0) return;  // no audio output on this node
  if (rate != cfg_.audio_rate_hz) {      // not the stream this node asked for in INFO
    bump(invalid_);
    return;
  }
  if (inhibit_) return;
  const int16_t silence[kMaxChannels] = {0, 0};
  if ((flags & kAudioStart) || !stream_open_) {
    flushAudio();
    stream_open_ = true;
  } else if (position != expected_) {
    const int32_t delta = static_cast<int32_t>(position - expected_);
    bump(gaps_);
    if (delta > 0 && static_cast<size_t>(delta) <= kAudioCapacity / 4) {
      for (int32_t i = 0; i < delta; ++i) pushFrame(silence);  // lost packets: keep the timing, fill with silence
    } else if (delta < 0 && static_cast<size_t>(-delta) < kAudioCapacity) {
      return;  // an old or repeated packet
    } else {
      flushAudio();  // the stream jumped: start again from here
      stream_open_ = true;
    }
  }
  const uint8_t* s = p.payload + 10;
  for (uint16_t i = 0; i < frames; ++i) {
    int16_t frame[kMaxChannels];
    frame[0] = static_cast<int16_t>(get16(s));
    frame[1] = channels > 1 ? static_cast<int16_t>(get16(s + 2)) : frame[0];
    pushFrame(frame);
    s += 2 * channels;
  }
  expected_ = position + frames;
}

size_t Node::readAudio(int16_t* out, size_t frames) {
  const size_t target = static_cast<size_t>(cfg_.audio_target_ms) * cfg_.audio_rate_hz / 1000u;
  const int channels = cfg_.audio_channels > 0 ? cfg_.audio_channels : 1;
  size_t taken = 0;
  for (size_t i = 0; i < frames; ++i) {
    int16_t frame[kMaxChannels] = {0, 0};
    if (!inhibit_ && !playing_ && fill_ > 0 && fill_ >= target) playing_ = true;  // enough buffered: start
    if (!inhibit_ && playing_ && fill_ > 0) {
      popFrame(frame);
      ++taken;
      if (++trim_count_ >= 64) {  // drift between the sender's clock and this DAC's: one frame in 64 at most
        trim_count_ = 0;
        if (fill_ > target + target / 2 && fill_ > 1) {
          int16_t skipped[kMaxChannels];
          popFrame(skipped);  // too much buffered: the sender is faster
        } else if (fill_ > 0 && fill_ < target / 2) {
          head_ = (head_ + kAudioCapacity - 1) % kAudioCapacity;  // too little: play this frame twice
          ++fill_;
        }
      }
      last_frame_[0] = frame[0];
      last_frame_[1] = frame[1];
    } else {
      if (playing_) {  // ran dry while playing
        playing_ = false;
        stream_open_ = stream_open_ && fill_ > 0;
        if (!inhibit_) bump(underruns_);
      }
      for (int c = 0; c < kMaxChannels; ++c) {  // fade the last value out instead of a step (no click)
        last_frame_[c] = static_cast<int16_t>((last_frame_[c] > -8 && last_frame_[c] < 8) ? 0 : last_frame_[c] - last_frame_[c] / 4);
        frame[c] = last_frame_[c];
      }
    }
    out[i * channels] = frame[0];
    if (channels > 1) out[i * channels + 1] = frame[1];
  }
  return taken;
}

uint16_t Node::audioLevelMs() const {
  const uint32_t ms = static_cast<uint32_t>(fill_) * 1000u / cfg_.audio_rate_hz;
  return static_cast<uint16_t>(ms > 0xFFFFu ? 0xFFFFu : ms);
}

size_t Node::makeInfo(uint32_t now_ms, uint8_t* out) {
  Info info;
  info.echo_us = echo_us_;
  info.uptime_ms = now_ms;
  info.capabilities = static_cast<uint8_t>((cfg_.has_actuators ? kCapActuators : 0) | (cfg_.dmx_ports ? kCapDmx : 0) |
                                           (cfg_.audio_channels ? kCapAudio : 0));
  info.dmx_ports = cfg_.dmx_ports;
  info.audio_channels = cfg_.audio_channels;
  bool dmx_live = false;
  for (int port = 0; port < cfg_.dmx_ports; ++port) dmx_live = dmx_live || dmx_active_[port];
  info.state = static_cast<uint8_t>((dmx_live ? kStateDmxActive : 0) | (playing_ ? kStateAudioPlaying : 0) |
                                    (inhibit_ ? kStateInhibited : 0));
  info.audio_rate_hz = cfg_.audio_rate_hz;
  info.audio_latency_ms = static_cast<uint16_t>(cfg_.audio_target_ms + cfg_.audio_output_ms);
  info.audio_level_ms = audioLevelMs();
  info.dmx_refresh_hz = cfg_.dmx_refresh_hz;
  info.packets = packets_;
  info.invalid = invalid_;
  info.dmx_timeouts = dmx_timeouts_;
  info.audio_underruns = underruns_;
  info.audio_gaps = gaps_;
  return encodeInfo(cfg_.node_id, ++reply_sequence_, info, out);
}

Upsampler::Upsampler(int factor, int channels) : factor_(factor < 1 ? 1 : factor), channels_(channels < 2 ? 1 : 2) { reset(); }

void Upsampler::reset() { prev_[0] = prev_[1] = 0; }

void Upsampler::process(const int16_t* in, size_t frames, int16_t* out) {
  for (size_t n = 0; n < frames; ++n) {
    int16_t cur[kMaxChannels];
    cur[0] = in[n * channels_];
    cur[1] = channels_ > 1 ? in[n * channels_ + 1] : cur[0];
    for (int k = 1; k <= factor_; ++k) {
      for (int c = 0; c < kMaxChannels; ++c) {
        const int32_t step = (static_cast<int32_t>(cur[c]) - prev_[c]) * k / factor_;
        *out++ = static_cast<int16_t>(prev_[c] + step);
      }
    }
    prev_[0] = cur[0];
    prev_[1] = cur[1];
  }
}

}  // namespace fx
}  // namespace fdvr
