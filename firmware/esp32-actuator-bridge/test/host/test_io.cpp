// Host tests of the actuator side (lib/fourdvr_io): outputs, the GP8403 and ADS1115 bytes, sensors, supply input.
#include <cmath>
#include <cstdio>

#include "fourdvr_io.h"

using namespace fdvr::io;

static int checks = 0, failed = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    ++checks;                                                           \
    if (!(cond)) {                                                      \
      ++failed;                                                         \
      std::printf("FAILED %s:%d: %s\n", __FILE__, __LINE__, #cond);   \
    }                                                                   \
  } while (0)

static void test_outputs() {
  CHECK(strokeFraction(150000.0f, 300000) == 0.5f);
  CHECK(strokeFraction(-5.0f, 300000) == 0.0f && strokeFraction(400000.0f, 300000) == 1.0f);
  CHECK(strokeFraction(10.0f, 0) == 0.0f);
  CHECK(pwmDuty(0.0f, 1638, 14745) == 1638 && pwmDuty(1.0f, 1638, 14745) == 14745);
  CHECK(pwmDuty(0.5f, 1000, 3000) == 2000 && pwmDuty(2.0f, 1000, 3000) == 3000);
  CHECK(dacCode(0.0f, 0, 4095) == 0 && dacCode(1.0f, 0, 4095) == 4095 && dacCode(0.5f, 0, 4095) == 2048);
  CHECK(dacCode(0.0f, 4095, 819) == 4095 && dacCode(1.0f, 4095, 819) == 819);   // reversed, 10 V .. 2 V
  CHECK(dacCode(-1.0f, 0, 4095) == 0 && dacCode(3.0f, 0, 4095) == 4095);
  CHECK(valveOn(150000.0f, 300000) && !valveOn(149999.0f, 300000) && valveOn(300000.0f, 300000) && !valveOn(0.0f, 300000));
}

static void test_gp8403() {
  uint8_t b[3];
  CHECK(gp8403Range10V(b) == 2 && b[0] == 0x01 && b[1] == 0x11);
  CHECK(gp8403Channel(0, 4095, b) == 3 && b[0] == 0x02 && b[1] == 0xF0 && b[2] == 0xFF);   // 4095 << 4 = 0xFFF0
  CHECK(gp8403Channel(1, 0x123, b) == 3 && b[0] == 0x04 && b[1] == 0x30 && b[2] == 0x12);  // 0x1230, low byte first
  CHECK(gp8403Channel(1, 5000, b) == 3 && b[1] == 0xF0 && b[2] == 0xFF);                    // clipped to 4095
  CHECK(dacAddress(0, 0x58) == 0x58 && dacAddress(1, 0x58) == 0x58 && dacAddress(2, 0x58) == 0x59 && dacAddress(5, 0x58) == 0x5A);
  CHECK(dacChannel(0) == 0 && dacChannel(1) == 1 && dacChannel(4) == 0);
}

static void test_ads1115() {
  // OS 1, MUX 100 (AIN0-GND), PGA 001, MODE 1, DR 111, comparator off 00011
  CHECK(ads1115Config(0, 1) == 0xC3E3);
  CHECK(ads1115Config(3, 1) == 0xF3E3);
  CHECK(ads1115Config(2, 0) == 0xE1E3);
  CHECK(adcAddress(0, 0x48) == 0x48 && adcAddress(3, 0x48) == 0x48 && adcAddress(4, 0x48) == 0x49);
  CHECK(adcInput(5) == 1);
  const SensorScale s = {4800, 24000, 4320, 24600};
  float um = -1.0f;
  CHECK(sensorUm(4800, s, 300000, um) && std::fabs(um) < 0.5f);
  CHECK(sensorUm(24000, s, 300000, um) && std::fabs(um - 300000.0f) < 0.5f);
  CHECK(sensorUm(14400, s, 300000, um) && std::fabs(um - 150000.0f) < 1.0f);
  CHECK(sensorUm(4500, s, 300000, um) && um == 0.0f);     // just below the end: clipped, still valid
  CHECK(!sensorUm(4000, s, 300000, um));                   // 3.3 mA: a broken wire
  CHECK(!sensorUm(30000, s, 300000, um));                  // over range
  const SensorScale reversed = {24000, 4800, 4320, 24600};
  CHECK(sensorUm(24000, reversed, 300000, um) && std::fabs(um) < 0.5f);
  CHECK(!sensorUm(10000, SensorScale{5, 5, 0, 100}, 300000, um));   // a scale without a span is refused
}

static void test_scanner() {
  FeedbackScanner sc(3, 1300);
  CHECK(sc.axis() == 0 && sc.next(0) == FeedbackScanner::Action::Start);
  CHECK(sc.next(500) == FeedbackScanner::Action::None);
  CHECK(sc.next(1300) == FeedbackScanner::Action::Read && sc.axis() == 0);
  sc.result(true, 100);
  CHECK(sc.axis() == 1 && sc.next(1400) == FeedbackScanner::Action::Start);
  sc.result(false, 0);                                       // the start failed: on to the next sensor
  CHECK(sc.axis() == 2 && sc.errors() == 1 && sc.next(1500) == FeedbackScanner::Action::Start);
  CHECK(sc.next(2800) == FeedbackScanner::Action::Read);
  sc.result(true, 0);
  CHECK(sc.axis() == 0);
  FeedbackScanner wraps(1, 1300);                            // the microsecond counter wraps around
  CHECK(wraps.next(0xFFFFFF00u) == FeedbackScanner::Action::Start);
  CHECK(wraps.next(0x00000500u) == FeedbackScanner::Action::Read);
  SensorWatch w;
  CHECK(!w.have() && !w.failed());
  w.bad();
  w.bad();
  CHECK(!w.failed());
  w.good(1234.0f);
  w.bad();
  w.bad();
  CHECK(!w.failed() && w.have() && w.um() == 1234.0f);
  w.bad();
  CHECK(w.failed());
}

static void test_ready() {
  ReadyInput r(50);
  CHECK(!r.update(true, 0));         // "not ok" until it has been ok for 50 ms
  CHECK(!r.update(true, 40));
  CHECK(r.update(true, 50) && r.ok());
  CHECK(r.update(false, 60));        // a 30 ms dip is ignored
  CHECK(r.update(true, 90));
  CHECK(r.update(false, 100) && r.update(false, 149));
  CHECK(!r.update(false, 150));      // 50 ms without supply: lost
}

int main() {
  test_outputs();
  test_gp8403();
  test_ads1115();
  test_scanner();
  test_ready();
  std::printf("%d checks, %d failed\n", checks, failed);
  return failed ? 1 : 0;
}
