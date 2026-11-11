// Host unit tests for the portable link core (lib/fourdvr_link). Build and run with:
//   make -C firmware/esp32-actuator-bridge/test/host
#include <stdio.h>
#include <string.h>

#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "fourdvr_link.h"

using namespace fdvr;

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

static std::vector<uint8_t> cmdPacket(uint32_t seq, uint64_t ts_us, CommandState st = CommandState::Run,
                                      int32_t target = 1000, uint8_t pid = 1) {
  Command c;
  c.platform_id = pid;
  c.sequence = seq;
  c.timestamp_us = ts_us;
  c.state = st;
  c.flags = 1;
  for (int i = 0; i < kAxes; ++i) c.targets_um[i] = target;
  std::vector<uint8_t> buf(kCommandSize);
  encodeCommand(c, buf.data());
  return buf;
}

static void test_crc() {
  const char* s = "123456789";
  CHECK(crc32(reinterpret_cast<const uint8_t*>(s), 9) == 0xCBF43926u);  // CRC-32/ISO-HDLC check value
}

static void test_vectors(const char* path) {
  std::ifstream f(path);
  CHECK(f.good());
  std::string line;
  int commands = 0, statuses = 0, cobs = 0;
  while (std::getline(f, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream ss(line);
    std::string kind, a, b;
    ss >> kind >> a >> b;
    if (kind == "command") {
      std::vector<uint8_t> raw = unhex(a);
      Command c;
      CHECK(decodeCommand(raw.data(), raw.size(), c) == DecodeResult::Ok);
      std::vector<uint8_t> again(kCommandSize);
      encodeCommand(c, again.data());
      CHECK(again == raw);  // byte-identical with the Python encoder
      if (commands == 0) {
        CHECK(c.platform_id == 7 && c.sequence == 123456 && c.timestamp_us == 1798000000123456ULL);
        CHECK(c.state == CommandState::Run && c.targets_um[3] == 299999 && c.targets_um[5] == 123456);
      } else {
        CHECK(c.platform_id == 250 && c.sequence == 0xFFFFFFFFu && c.state == CommandState::Park && c.flags == 0);
      }
      ++commands;
    } else if (kind == "status") {
      std::vector<uint8_t> raw = unhex(a);
      Status st;
      CHECK(decodeStatus(raw.data(), raw.size(), st) == DecodeResult::Ok);
      CHECK(st.device_state == DeviceState::Running && st.faults == (kFaultTimeout | kFaultStale));
      CHECK(st.positions_um[5] == 600 && st.received == 1000 && st.timeouts == 5);
      std::vector<uint8_t> again(kStatusSize);
      encodeStatus(st, again.data());
      CHECK(again == raw);
      ++statuses;
    } else if (kind == "cobs") {
      std::vector<uint8_t> in = unhex(a), expected = unhex(b);
      std::vector<uint8_t> out(in.size() + 8);
      size_t n = cobsEncode(in.data(), in.size(), out.data());
      out.resize(n);
      CHECK(out == expected);
      std::vector<uint8_t> back(in.size() + 8);
      size_t blen = 0;
      CHECK(cobsDecode(out.data(), out.size(), back.data(), blen));
      back.resize(blen);
      CHECK(back == in);
      ++cobs;
    }
  }
  CHECK(commands == 2 && statuses == 1 && cobs == 1);
}

static void test_decode_errors() {
  std::vector<uint8_t> p = cmdPacket(1, 1000);
  Command c;
  p[30] ^= 1;
  CHECK(decodeCommand(p.data(), p.size(), c) == DecodeResult::BadCrc);
  p = cmdPacket(1, 1000);
  CHECK(decodeCommand(p.data(), p.size() - 1, c) == DecodeResult::BadLength);
  p[0] = 'X';
  CHECK(decodeCommand(p.data(), p.size(), c) == DecodeResult::BadMagic);
}

static void test_cobs_long() {
  std::vector<uint8_t> in(600);
  for (size_t i = 0; i < in.size(); ++i) in[i] = static_cast<uint8_t>(i % 7 == 0 ? 0 : i);
  std::vector<uint8_t> enc(in.size() + in.size() / 254 + 2);
  size_t n = cobsEncode(in.data(), in.size(), enc.data());
  for (size_t i = 0; i < n; ++i) CHECK(enc[i] != 0);
  std::vector<uint8_t> dec(in.size() + 4);
  size_t dl = 0;
  CHECK(cobsDecode(enc.data(), n, dec.data(), dl));
  dec.resize(dl);
  CHECK(dec == in);
}

static void test_sequence() {
  SequenceTracker t;
  CHECK(t.accept(10) && t.accept(11) && !t.accept(11) && !t.accept(9));
  CHECK(t.accept(15) && t.lost() == 3 && t.duplicate() == 2);
  SequenceTracker w;
  CHECK(w.accept(0xFFFFFFFEu) && w.accept(0xFFFFFFFFu) && w.accept(0) && w.lost() == 0);
}

static void test_guard_run_clamp_dup_stale() {
  GuardConfig cfg;
  cfg.max_speed_um_s = 2000000;
  LinkGuard g(cfg);
  std::vector<uint8_t> p = cmdPacket(1, 1000000, CommandState::Run, 400000);
  CHECK(g.handle(p.data(), p.size(), 0));
  CHECK(g.state() == DeviceState::Running);
  CHECK(!g.handle(p.data(), p.size(), 1));  // duplicate delivery
  CHECK(g.sequence().duplicate() == 1 && g.stale() == 0);
  for (int k = 1; k <= 150; ++k) g.update(k, 0.001f);  // within the 200 ms watchdog
  CHECK(g.output()[0] > 299999.0f && g.output()[0] <= 300000.0f);  // clamped to the stroke
  std::vector<uint8_t> wrong = cmdPacket(2, 1151000, CommandState::Run, 1000, 9);
  CHECK(!g.handle(wrong.data(), wrong.size(), 151) && g.invalid() == 1);
  std::vector<uint8_t> next = cmdPacket(5, 1160000);
  CHECK(g.handle(next.data(), next.size(), 160) && g.sequence().lost() == 3);
  std::vector<uint8_t> stale = cmdPacket(6, 1170000);
  CHECK(!g.handle(stale.data(), stale.size(), 480) && g.stale() == 1);  // 310 ms behind the time base
}

static void test_guard_timeout_park_reset() {
  GuardConfig cfg;
  cfg.timeout_ms = 100;
  cfg.park_speed_um_s = 1000000;
  LinkGuard g(cfg);
  std::vector<uint8_t> p = cmdPacket(1, 0, CommandState::Run, 250000);
  CHECK(g.handle(p.data(), p.size(), 0));
  for (int k = 1; k <= 400; ++k) g.update(k, 0.001f);
  CHECK((g.faults() & kFaultTimeout) != 0 && g.timeouts() == 1);
  CHECK(g.state() == DeviceState::Parked);
  CHECK(g.output()[0] > 149999.0f && g.output()[0] < 150001.0f);
  std::vector<uint8_t> run = cmdPacket(2, 400000, CommandState::Run, 10000);
  CHECK(g.handle(run.data(), run.size(), 400));
  CHECK(g.state() == DeviceState::Parked);  // latched until FAULT_RESET
  std::vector<uint8_t> reset = cmdPacket(3, 410000, CommandState::FaultReset);
  CHECK(g.handle(reset.data(), reset.size(), 410));
  CHECK(g.faults() == kFaultNone && g.state() == DeviceState::Idle);
}

static void test_guard_estop_and_invalid_burst() {
  GuardConfig cfg;
  cfg.invalid_burst = 5;
  LinkGuard g(cfg);
  uint8_t junk[3] = {1, 2, 3};
  for (int i = 0; i < 5; ++i) g.handle(junk, sizeof(junk), i);
  CHECK((g.faults() & kFaultInvalidBurst) != 0);
  LinkGuard e(cfg);
  e.setEstop(true);
  e.update(1, 0.001f);
  CHECK(e.state() == DeviceState::Fault && (e.faults() & kFaultEstop) != 0);
  std::vector<uint8_t> reset = cmdPacket(1, 1000, CommandState::FaultReset);
  e.handle(reset.data(), reset.size(), 2);
  CHECK(e.faults() != kFaultNone);  // cannot reset while the e-stop input is active
}

int main(int argc, char** argv) {
  const char* vectors = argc > 1 ? argv[1] : "../vectors.txt";
  test_crc();
  test_vectors(vectors);
  test_decode_errors();
  test_cobs_long();
  test_sequence();
  test_guard_run_clamp_dup_stale();
  test_guard_timeout_park_reset();
  test_guard_estop_and_invalid_burst();
  printf("%d checks, %d failed\n", g_checks, g_failed);
  return g_failed == 0 ? 0 : 1;
}
