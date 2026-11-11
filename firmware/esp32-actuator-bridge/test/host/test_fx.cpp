// Host unit tests for the effects-node core (lib/fourdvr_fx). Build and run with:
//   make -C firmware/esp32-actuator-bridge/test/host
#include <math.h>
#include <stdio.h>
#include <string.h>

#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "fourdvr_fx.h"

using namespace fdvr::fx;

static int g_failed = 0, g_checks = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    ++g_checks;                                                         \
    if (!(cond)) {                                                      \
      ++g_failed;                                                       \
      fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
    }                                                                   \
  } while (0)

static std::vector<uint8_t> unhex(const std::string& s) {
  std::vector<uint8_t> out;
  for (size_t i = 0; i + 1 < s.size(); i += 2) out.push_back(static_cast<uint8_t>(std::stoi(s.substr(i, 2), nullptr, 16)));
  return out;
}

static void put16(std::vector<uint8_t>& v, uint16_t x) {
  v.push_back(static_cast<uint8_t>(x));
  v.push_back(static_cast<uint8_t>(x >> 8));
}
static void put32(std::vector<uint8_t>& v, uint32_t x) {
  put16(v, static_cast<uint16_t>(x));
  put16(v, static_cast<uint16_t>(x >> 16));
}

static std::vector<uint8_t> packet(uint8_t type, uint8_t node, uint32_t seq, const std::vector<uint8_t>& payload) {
  std::vector<uint8_t> p = {'4', 'D', 'F', 'X', kVersionMajor, kVersionMinor, type, node};
  put32(p, seq);
  p.insert(p.end(), payload.begin(), payload.end());
  put32(p, crc32(p.data(), p.size()));
  return p;
}

static std::vector<uint8_t> dmxPacket(uint8_t node, uint8_t port, uint16_t first, const std::vector<uint8_t>& data) {
  std::vector<uint8_t> pl = {port, 0};
  put16(pl, first);
  put16(pl, static_cast<uint16_t>(data.size()));
  pl.insert(pl.end(), data.begin(), data.end());
  return packet(kTypeDmx, node, 1, pl);
}

static std::vector<uint8_t> audioPacket(uint8_t node, uint32_t position, const std::vector<int16_t>& samples, uint8_t channels = 1,
                                        uint8_t flags = 0, uint16_t rate = 4000) {
  std::vector<uint8_t> pl = {channels, flags};
  put16(pl, rate);
  put32(pl, position);
  put16(pl, static_cast<uint16_t>(samples.size() / channels));
  for (int16_t s : samples) put16(pl, static_cast<uint16_t>(s));
  return packet(kTypeAudio, node, 1, pl);
}

static std::vector<int16_t> ramp(int first, int count) {
  std::vector<int16_t> v;
  for (int i = 0; i < count; ++i) v.push_back(static_cast<int16_t>(first + i));
  return v;
}

static void test_crc() {
  const char* s = "123456789";
  CHECK(crc32(reinterpret_cast<const uint8_t*>(s), 9) == 0xCBF43926u);  // CRC-32/ISO-HDLC check value
  CHECK(crc32(nullptr, 0) == 0u);
}

// The controller's packets (fourdvr fx test-vectors): every "ok" line is accepted with these fields, every
// "bad" line is refused, and the INFO packet is built byte for byte.
static void test_vectors(const char* path) {
  std::ifstream f(path);
  CHECK(f.good());
  std::string line;
  int ok = 0, bad = 0, info = 0;
  while (std::getline(f, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream in(line);
    std::string kind;
    in >> kind;
    if (kind == "ok") {
      unsigned type, node, seq;
      std::string hex;
      in >> type >> node >> seq >> hex;
      std::vector<uint8_t> p = unhex(hex);
      Packet out;
      CHECK(decode(p.data(), p.size(), out) == Decode::Ok);
      CHECK(out.type == type && out.node == node && out.sequence == seq);
      CHECK(p.size() <= kMaxPacket);
      Config c;
      c.node_id = static_cast<uint8_t>(node ? node : 1);
      c.dmx_ports = 2;
      Node n(c);
      const bool hello = n.handle(p.data(), p.size(), 10);
      CHECK(hello == (type == kTypeHello));
      CHECK(n.invalid() == 0 && n.packets() == 1);
      ++ok;
    } else if (kind == "bad") {
      std::string reason, hex;
      in >> reason >> hex;
      std::vector<uint8_t> p = unhex(hex);
      Config c;
      c.dmx_ports = 2;
      Node n(c);
      CHECK(!n.handle(p.data(), p.size(), 10));
      CHECK(n.invalid() == 1 && n.packets() == 0);
      CHECK(!n.dmxActive(0) && n.audioLevel() == 0);
      ++bad;
    } else if (kind == "info") {
      unsigned node, seq;
      unsigned long long echo;
      unsigned v[15];
      std::string hex;
      in >> node >> seq >> echo;
      for (unsigned& x : v) in >> x;
      in >> hex;
      Info i;
      i.echo_us = echo;
      i.uptime_ms = v[0];
      i.firmware = static_cast<uint16_t>(v[1]);
      i.capabilities = static_cast<uint8_t>(v[2]);
      i.dmx_ports = static_cast<uint8_t>(v[3]);
      i.audio_channels = static_cast<uint8_t>(v[4]);
      i.state = static_cast<uint8_t>(v[5]);
      i.audio_rate_hz = static_cast<uint16_t>(v[6]);
      i.audio_latency_ms = static_cast<uint16_t>(v[7]);
      i.audio_level_ms = static_cast<uint16_t>(v[8]);
      i.dmx_refresh_hz = static_cast<uint8_t>(v[9]);
      i.packets = v[10];
      i.invalid = static_cast<uint16_t>(v[11]);
      i.dmx_timeouts = static_cast<uint16_t>(v[12]);
      i.audio_underruns = static_cast<uint16_t>(v[13]);
      i.audio_gaps = static_cast<uint16_t>(v[14]);
      uint8_t out[kInfoSize];
      CHECK(encodeInfo(static_cast<uint8_t>(node), seq, i, out) == kInfoSize);
      std::vector<uint8_t> want = unhex(hex);
      CHECK(want.size() == kInfoSize && memcmp(out, want.data(), kInfoSize) == 0);
      ++info;
    }
  }
  CHECK(ok >= 9 && bad >= 9 && info == 1);
}

static void test_hello_and_info() {
  Config c;
  c.node_id = 7;
  c.has_actuators = true;
  c.dmx_ports = 1;
  c.audio_channels = 2;
  Node n(c);
  std::vector<uint8_t> pl(8);
  for (int i = 0; i < 8; ++i) pl[i] = static_cast<uint8_t>(0x10 + i);
  std::vector<uint8_t> hello = packet(kTypeHello, 0, 5, pl);  // node 0: any node answers
  CHECK(n.handle(hello.data(), hello.size(), 1234));
  uint8_t out[kInfoSize];
  CHECK(n.makeInfo(1234, out) == kInfoSize);
  Packet p;
  CHECK(decode(out, kInfoSize, p) == Decode::Ok && p.type == kTypeInfo && p.node == 7 && p.payload_len == kInfoPayload);
  CHECK(memcmp(p.payload, pl.data(), 8) == 0);                       // the controller's time, echoed
  CHECK(p.payload[14] == (kCapActuators | kCapDmx | kCapAudio));     // what this node has
  CHECK(p.payload[15] == 1 && p.payload[16] == 2 && p.payload[17] == 0);
  CHECK(p.payload[18] == (4000 & 0xFF) && p.payload[19] == (4000 >> 8));
  CHECK(p.payload[20] == 125 && p.payload[21] == 0);                 // lead the sound by buffer + output stage
  std::vector<uint8_t> other = packet(kTypeHello, 8, 6, pl);          // a HELLO for another node
  CHECK(!n.handle(other.data(), other.size(), 1300) && n.invalid() == 1);
  std::vector<uint8_t> mine = packet(kTypeHello, 7, 7, pl);
  CHECK(n.handle(mine.data(), mine.size(), 1300) && n.packets() == 2);
  Config bare;
  bare.dmx_ports = 0;
  bare.audio_channels = 0;
  Node none(bare);
  CHECK(none.handle(hello.data(), hello.size(), 1));
  none.makeInfo(1, out);
  decode(out, kInfoSize, p);
  CHECK(p.payload[14] == 0 && p.payload[15] == 0 && p.payload[16] == 0);   // a node with nothing says so
}

static void test_dmx() {
  Config c;
  c.dmx_ports = 2;
  Node n(c);
  CHECK(!n.dmxActive(0) && n.dmxSlots(0) == 24 && n.dmxFrame(0)[0] == 0);
  std::vector<uint8_t> a = dmxPacket(1, 1, 5, {10, 20, 30});
  n.handle(a.data(), a.size(), 100);
  CHECK(n.dmxActive(1) && !n.dmxActive(0));
  CHECK(n.dmxFrame(1)[4] == 10 && n.dmxFrame(1)[5] == 20 && n.dmxFrame(1)[6] == 30 && n.dmxFrame(1)[3] == 0);
  std::vector<uint8_t> b = dmxPacket(1, 1, 6, {99});                  // other slots keep their values
  n.handle(b.data(), b.size(), 200);
  CHECK(n.dmxFrame(1)[4] == 10 && n.dmxFrame(1)[5] == 99 && n.dmxFrame(1)[6] == 30);
  std::vector<uint8_t> far = dmxPacket(1, 1, 500, std::vector<uint8_t>(13, 255));
  n.handle(far.data(), far.size(), 300);
  CHECK(n.dmxSlots(1) == 512 && n.dmxFrame(1)[511] == 255 && n.dmxFrame(1)[498] == 0);
  std::vector<uint8_t> absent = dmxPacket(1, 5, 1, {1});               // a line this node does not have
  n.handle(absent.data(), absent.size(), 300);
  CHECK(n.invalid() == 0 && n.packets() == 4);
  CHECK(n.dmxFrame(5)[0] == 0 && n.dmxFrame(-1)[0] == 0 && !n.dmxActive(5));
  n.update(1299);
  CHECK(n.dmxActive(1) && n.dmxTimeouts() == 0);
  n.update(1300);                                                      // one second after the last packet: dark
  CHECK(!n.dmxActive(1) && n.dmxFrame(1)[5] == 0 && n.dmxFrame(1)[511] == 0 && n.dmxTimeouts() == 1);
  n.update(5000);
  CHECK(n.dmxTimeouts() == 1);
  n.handle(a.data(), a.size(), 0xFFFFFF00u);                           // the millisecond clock wraps
  n.update(0x00000100u);
  CHECK(n.dmxActive(1));
  n.update(0x00000300u);
  CHECK(!n.dmxActive(1) && n.dmxTimeouts() == 2);
  n.handle(a.data(), a.size(), 6000);
  std::vector<uint8_t> stop = packet(kTypeStop, 1, 9, {kStopDmx, 0});
  n.handle(stop.data(), stop.size(), 6001);
  CHECK(!n.dmxActive(1) && n.dmxFrame(1)[5] == 0 && n.dmxTimeouts() == 2);     // told to go dark: not a time-out
  n.handle(a.data(), a.size(), 6100);
  n.setInhibit(true);                                                  // emergency stop: dark at once ...
  CHECK(n.inhibited() && n.dmxFrame(1)[5] == 0 && !n.dmxActive(1));
  n.handle(a.data(), a.size(), 6200);                                  // ... and whatever arrives stays dark
  CHECK(n.dmxFrame(1)[5] == 0);
  n.setInhibit(false);
  CHECK(n.dmxFrame(1)[5] == 0);                                        // nothing old comes back
  n.handle(a.data(), a.size(), 6300);
  CHECK(n.dmxFrame(1)[5] == 20);
  Config none;
  none.dmx_ports = 0;
  Node m(none);
  m.handle(a.data(), a.size(), 1);
  CHECK(m.invalid() == 0 && m.packets() == 1 && m.dmxFrame(0)[5] == 0 && !m.dmxActive(0));   // no DMX line: ignored quietly
}

static void test_audio_stream() {
  Config c;
  c.audio_channels = 2;
  c.audio_target_ms = 100;  // 400 frames at 4 kHz
  Node n(c);
  int16_t out[2 * 1000];
  CHECK(n.readAudio(out, 100) == 0 && out[0] == 0 && out[199] == 0 && !n.audioPlaying());
  std::vector<uint8_t> first = audioPacket(1, 0, ramp(1, 300), 1, kAudioStart);
  n.handle(first.data(), first.size(), 0);
  CHECK(n.audioLevel() == 300 && n.audioLevelMs() == 75);
  CHECK(n.readAudio(out, 50) == 0 && out[0] == 0 && !n.audioPlaying());           // not enough buffered yet
  std::vector<uint8_t> second = audioPacket(1, 300, ramp(301, 200));
  n.handle(second.data(), second.size(), 0);
  CHECK(n.readAudio(out, 100) == 100 && n.audioPlaying());
  CHECK(out[0] == 1 && out[1] == 1 && out[198] == 100 && out[199] == 100);        // a mono stream goes to both channels
  // a lost packet: the gap is filled with silence, the timing is kept
  std::vector<uint8_t> after_gap = audioPacket(1, 600, ramp(601, 100));           // 500..599 never arrived
  n.handle(after_gap.data(), after_gap.size(), 0);
  CHECK(n.audioGaps() == 1 && n.audioLevel() == 600);
  CHECK(n.readAudio(out, 600) >= 591);                                            // (drift correction may hold a few frames back)
  // an old packet again: ignored
  n.handle(second.data(), second.size(), 0);
  CHECK(n.audioGaps() == 2);
  // the buffer runs dry: one underrun, silence, and playing starts again only when enough is buffered
  while (n.audioLevel() > 0) n.readAudio(out, 1);
  CHECK(n.readAudio(out, 10) == 0 && !n.audioPlaying() && n.audioUnderruns() == 1);
  CHECK(n.readAudio(out, 10) == 0 && n.audioUnderruns() == 1);
  // stereo, START flushes what was buffered
  std::vector<int16_t> stereo;
  for (int i = 0; i < 300; ++i) {
    stereo.push_back(static_cast<int16_t>(1000 + i));
    stereo.push_back(static_cast<int16_t>(-1000 - i));
  }
  std::vector<uint8_t> s1 = audioPacket(1, 5000, stereo, 2, kAudioStart);
  n.handle(first.data(), first.size(), 0);
  n.handle(s1.data(), s1.size(), 0);
  CHECK(n.audioLevel() == 300);
  std::vector<uint8_t> s2 = audioPacket(1, 5300, stereo, 2);
  n.handle(s2.data(), s2.size(), 0);
  CHECK(n.readAudio(out, 10) == 10 && out[0] == 1000 && out[1] == -1000 && out[18] == 1009 && out[19] == -1009);
  // STOP: silent at once (after a short fade), and not an underrun
  std::vector<uint8_t> stop = packet(kTypeStop, 1, 1, {kStopAudio, 0});
  const uint16_t before = n.audioUnderruns();
  n.handle(stop.data(), stop.size(), 0);
  CHECK(n.audioLevel() == 0 && n.readAudio(out, 40) == 0 && n.audioUnderruns() == before);
  CHECK(out[0] != 0 && abs(out[0]) < 1009 && out[78] == 0 && out[79] == 0);       // faded, not cut
  // inhibit: silence, and what arrives meanwhile is dropped
  n.handle(s1.data(), s1.size(), 0);
  n.handle(s2.data(), s2.size(), 0);
  n.readAudio(out, 5);
  n.setInhibit(true);
  CHECK(n.audioLevel() == 0);
  n.handle(s1.data(), s1.size(), 0);
  CHECK(n.audioLevel() == 0);
  n.readAudio(out, 200);
  CHECK(out[398] == 0 && out[399] == 0);
  n.setInhibit(false);
  // the wrong rate is not this node's stream; a node without audio output ignores sound quietly
  std::vector<uint8_t> wrong = audioPacket(1, 0, ramp(1, 100), 1, kAudioStart, 8000);
  const uint16_t invalid = n.invalid();
  n.handle(wrong.data(), wrong.size(), 0);
  CHECK(n.invalid() == invalid + 1 && n.audioLevel() == 0);
  Config none;
  none.audio_channels = 0;
  Node m(none);
  m.handle(first.data(), first.size(), 0);
  CHECK(m.invalid() == 0 && m.audioLevel() == 0 && m.readAudio(out, 10) == 0);
  // a mono node plays the first channel of a stereo stream
  Config mono;
  mono.audio_channels = 1;
  mono.audio_target_ms = 10;
  Node k(mono);
  k.handle(s1.data(), s1.size(), 0);
  CHECK(k.readAudio(out, 3) == 3 && out[0] == 1000 && out[1] == 1001 && out[2] == 1002);
}

// The sender's clock and the DAC's clock differ: the buffer level must stay near its target either way,
// without a gap and without running dry.
static void test_audio_drift() {
  for (int direction = -1; direction <= 1; direction += 2) {
    Config c;
    c.audio_channels = 1;
    c.audio_target_ms = 100;
    Node n(c);
    int16_t out[64];
    uint32_t position = 0;
    double owed = 0.0;
    size_t lo = 100000, hi = 0;
    std::vector<uint8_t> p = audioPacket(1, position, std::vector<int16_t>(400, 1000), 1, kAudioStart);
    n.handle(p.data(), p.size(), 0);
    position += 400;
    for (int ms = 0; ms < 120000; ms += 25) {                         // two minutes
      p = audioPacket(1, position, std::vector<int16_t>(100, 1000));  // the sender: 100 frames every 25 ms
      n.handle(p.data(), p.size(), 0);
      position += 100;
      owed += 100.0 * (1.0 + direction * 0.004);                      // the DAC: 0.4 % fast or slow (far beyond a crystal)
      while (owed >= 1.0) {
        const size_t chunk = owed >= 32.0 ? 32 : static_cast<size_t>(owed);
        n.readAudio(out, chunk);
        owed -= static_cast<double>(chunk);
      }
      if (ms > 5000) {
        if (n.audioLevel() < lo) lo = n.audioLevel();
        if (n.audioLevel() > hi) hi = n.audioLevel();
      }
    }
    CHECK(n.audioUnderruns() == 0 && n.audioGaps() == 0);
    CHECK(lo >= 100 && hi <= 800);                                    // 25 .. 200 ms around the 100 ms target
  }
  // a sender that floods the buffer: the oldest sound goes, the newest stays
  Config c;
  c.audio_channels = 1;
  Node n(c);
  uint32_t position = 0;
  for (int i = 0; i < 30; ++i) {
    std::vector<uint8_t> p = audioPacket(1, position, ramp(static_cast<int>(position % 30000), 320), 1, i == 0 ? kAudioStart : 0);
    n.handle(p.data(), p.size(), 0);
    position += 320;
  }
  CHECK(n.audioLevel() == kAudioCapacity);
  int16_t one;
  n.readAudio(&one, 1);
  CHECK(one == static_cast<int16_t>(position - kAudioCapacity));
  // a jump far ahead starts the stream again from there
  std::vector<uint8_t> jump = audioPacket(1, position + 100000, ramp(7, 50));
  n.handle(jump.data(), jump.size(), 0);
  CHECK(n.audioLevel() == 50);
}

static void test_upsampler() {
  Upsampler mono(4, 1);
  int16_t in[3] = {400, 800, 0};
  int16_t out[3 * 4 * 2];
  mono.process(in, 3, out);
  const int16_t want[12] = {100, 200, 300, 400, 500, 600, 700, 800, 600, 400, 200, 0};
  for (int i = 0; i < 12; ++i) CHECK(out[2 * i] == want[i] && out[2 * i + 1] == want[i]);   // straight lines, both channels
  Upsampler stereo(2, 2);
  int16_t pairs[4] = {100, -100, 300, -300};
  stereo.process(pairs, 2, out);
  CHECK(out[0] == 50 && out[1] == -50 && out[2] == 100 && out[3] == -100 && out[4] == 200 && out[5] == -200 && out[6] == 300 && out[7] == -300);
  // a 40 Hz tone at 4 kHz, eight-fold: stays a clean 40 Hz tone at 32 kHz
  Upsampler up(8, 1);
  std::vector<int16_t> tone(400), wide(400 * 8 * 2);
  for (int i = 0; i < 400; ++i) tone[i] = static_cast<int16_t>(20000.0 * sin(2.0 * M_PI * 40.0 * i / 4000.0));
  up.process(tone.data(), 400, wide.data());
  double worst = 0.0;
  for (int i = 200; i < 3000; ++i) {
    const double ideal = 20000.0 * sin(2.0 * M_PI * 40.0 * (i - 7) / 32000.0);   // (the output lags the input by 7 of the 8 steps)
    worst = fmax(worst, fabs(wide[2 * i] - ideal));
  }
  CHECK(worst < 60.0);                                                // within 0.3 % of full scale
  up.reset();
  int16_t zero[1] = {0};
  up.process(zero, 1, out);
  CHECK(out[0] == 0 && out[14] == 0);
}

int main(int argc, char** argv) {
  const char* vectors = argc > 1 ? argv[1] : "../fx_vectors.txt";
  test_crc();
  test_vectors(vectors);
  test_hello_and_info();
  test_dmx();
  test_audio_stream();
  test_audio_drift();
  test_upsampler();
  printf("%d checks, %d failed\n", g_checks, g_failed);
  return g_failed == 0 ? 0 : 1;
}
