// Extended 4D Editor - effects node: portable core of the "4DFX" link (no Arduino dependencies).
//
// The motion controller sends DMX slot values and a low-rate stream of bass sound to an ESP32 over
// UDP port 47002 (docs/ESP32_EFFECTS_LINK.md). This file is everything about that which does not
// touch hardware: packet checking, the DMX slot buffers with their time-out, the sound buffer with
// start-up fill, gap repair and drift correction, the up-sampler for the DAC, and the INFO reply in
// which the node says what it has. The Arduino glue (src/main.cpp) moves bytes between this core,
// the network, a UART (DMX) and I2S (sound).
//
// The rules are the same as in the controller's simulated node (fourdvr_fx/bridge.py, SimNode); both
// are checked against test/fx_vectors.txt.
//
// Not thread-safe: the glue serialises the calls (one mutex around every call into a Node).
// Nothing here is a safety function. The node fails dark - a second without DMX packets and its
// lines are at zero, no sound data is silence, an inhibit (emergency stop) forces both - but the
// power of devices that must stop in an emergency is removed by the emergency-stop circuit.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace fdvr {
namespace fx {

constexpr uint8_t kVersionMajor = 1;
constexpr uint8_t kVersionMinor = 0;
constexpr uint16_t kFirmwareVersion = 0x0100;  // reported in INFO: major << 8 | minor
constexpr uint16_t kUdpPort = 47002;

constexpr uint8_t kTypeHello = 1;
constexpr uint8_t kTypeDmx = 2;
constexpr uint8_t kTypeAudio = 3;
constexpr uint8_t kTypeStop = 4;
constexpr uint8_t kTypeInfo = 0x81;

constexpr uint8_t kCapActuators = 1;
constexpr uint8_t kCapDmx = 2;
constexpr uint8_t kCapAudio = 4;
constexpr uint8_t kStateDmxActive = 1;
constexpr uint8_t kStateAudioPlaying = 2;
constexpr uint8_t kStateInhibited = 4;
constexpr uint8_t kStopDmx = 1;
constexpr uint8_t kStopAudio = 2;
constexpr uint8_t kAudioStart = 1;

constexpr size_t kHeaderSize = 12;
constexpr size_t kCrcSize = 4;
constexpr size_t kInfoPayload = 38;
constexpr size_t kInfoSize = kHeaderSize + kInfoPayload + kCrcSize;  // 54
constexpr int kSlots = 512;
constexpr int kMaxPorts = 2;
constexpr int kMaxChannels = 2;
constexpr size_t kMaxAudioFrames = 320;                                // per packet: stays inside one Ethernet frame
constexpr size_t kMaxPacket = kHeaderSize + 10 + 2 * kMaxAudioFrames * kMaxChannels + kCrcSize;  // 1306
constexpr size_t kAudioCapacity = 4096;                                // frames the sound buffer holds (1 s at 4 kHz)

enum class Decode : uint8_t { Ok, Short, Magic, Version, Crc };

struct Packet {
  uint8_t type = 0;
  uint8_t node = 0;
  uint32_t sequence = 0;
  const uint8_t* payload = nullptr;
  size_t payload_len = 0;
};

uint32_t crc32(const uint8_t* data, size_t len);
Decode decode(const uint8_t* packet, size_t len, Packet& out);

struct Info {
  uint64_t echo_us = 0;
  uint32_t uptime_ms = 0;
  uint16_t firmware = kFirmwareVersion;
  uint8_t capabilities = 0;
  uint8_t dmx_ports = 0;
  uint8_t audio_channels = 0;
  uint8_t state = 0;
  uint16_t audio_rate_hz = 0;
  uint16_t audio_latency_ms = 0;
  uint16_t audio_level_ms = 0;
  uint8_t dmx_refresh_hz = 0;
  uint32_t packets = 0;
  uint16_t invalid = 0;
  uint16_t dmx_timeouts = 0;
  uint16_t audio_underruns = 0;
  uint16_t audio_gaps = 0;
};

// Writes an INFO packet (kInfoSize bytes) to `out`.
size_t encodeInfo(uint8_t node, uint32_t sequence, const Info& info, uint8_t* out);

struct Config {
  uint8_t node_id = 1;               // 1..250; on an ESP32 that also moves a platform: the platform id
  bool has_actuators = false;        // this ESP32 also runs the motion link (reported, nothing else)
  uint8_t dmx_ports = 1;             // DMX lines of this node: 0, 1 or 2
  uint8_t audio_channels = 2;        // 0: no audio output; 1 or 2
  uint16_t audio_rate_hz = 4000;     // rate of the stream the controller is asked to send
  uint16_t audio_target_ms = 100;    // sound buffered before playing starts (absorbs network jitter)
  uint16_t audio_output_ms = 25;     // delay of the output stage behind the buffer (I2S DMA buffers)
  uint16_t dmx_timeout_ms = 1000;    // without DMX packets for this long a line goes dark
  uint8_t dmx_refresh_hz = 40;       // how often the glue sends the DMX frame (reported)
};

class Node {
 public:
  explicit Node(const Config& config);

  // A datagram from the network. Returns true when it was a HELLO for this node: send makeInfo() back.
  bool handle(const uint8_t* packet, size_t len, uint32_t now_ms);
  // The INFO packet answering the last HELLO (kInfoSize bytes).
  size_t makeInfo(uint32_t now_ms, uint8_t* out);
  // Time-outs. Call every few milliseconds.
  void update(uint32_t now_ms);
  // Emergency stop / fault of the device: outputs are forced dark and silent while set.
  void setInhibit(bool inhibit);
  bool inhibited() const { return inhibit_; }

  // DMX: the 512 slots of a line as they must go out now (all zero when dark or inhibited), and how
  // many slots are worth sending (the highest slot ever written, at least 24).
  const uint8_t* dmxFrame(int port) const;
  int dmxSlots(int port) const;
  bool dmxActive(int port) const;

  // Sound: the next `frames` frames at audio_rate_hz, interleaved, audio_channels per frame. Always
  // fills the buffer (silence when there is nothing to play). Returns the frames taken from the stream.
  size_t readAudio(int16_t* out, size_t frames);
  bool audioPlaying() const { return playing_; }
  uint16_t audioLevelMs() const;
  size_t audioLevel() const { return fill_; }

  const Config& config() const { return cfg_; }
  uint32_t packets() const { return packets_; }
  uint16_t invalid() const { return invalid_; }
  uint16_t dmxTimeouts() const { return dmx_timeouts_; }
  uint16_t audioUnderruns() const { return underruns_; }
  uint16_t audioGaps() const { return gaps_; }

 private:
  void handleDmx(const Packet& p, uint32_t now_ms);
  void handleAudio(const Packet& p);
  void darken(int port);
  void flushAudio();
  void pushFrame(const int16_t* frame);
  void popFrame(int16_t* frame);

  Config cfg_;
  uint8_t dmx_[kMaxPorts][kSlots];
  uint32_t dmx_last_ms_[kMaxPorts];
  bool dmx_active_[kMaxPorts];
  int dmx_slots_[kMaxPorts];
  int16_t ring_[kAudioCapacity * kMaxChannels];
  size_t head_ = 0;  // next frame to read
  size_t fill_ = 0;  // frames buffered
  uint32_t expected_ = 0;
  bool stream_open_ = false;
  bool playing_ = false;
  uint32_t trim_count_ = 0;
  int16_t last_frame_[kMaxChannels];
  bool inhibit_ = false;
  uint64_t echo_us_ = 0;
  uint32_t reply_sequence_ = 0;
  uint32_t packets_ = 0;
  uint16_t invalid_ = 0;
  uint16_t dmx_timeouts_ = 0;
  uint16_t underruns_ = 0;
  uint16_t gaps_ = 0;
};

// Straight-line interpolation from the stream rate to the DAC rate (x factor), always to two output
// channels (a mono stream goes to both). The content is band-limited far below the stream rate
// (bass under 200 Hz at 4 kHz), so the images this leaves are 50 dB down and above what a shaker moves.
class Upsampler {
 public:
  Upsampler(int factor, int channels);
  // in: `frames` interleaved frames; out: frames * factor stereo frames (2 * frames * factor values).
  void process(const int16_t* in, size_t frames, int16_t* out);
  void reset();

 private:
  int factor_;
  int channels_;
  int16_t prev_[kMaxChannels];
};

}  // namespace fx
}  // namespace fdvr
