// Stand-in for the Arduino-ESP32 Wire (I2C) library: only for the compile check of main.cpp on a PC.
#pragma once
#include <stddef.h>
#include <stdint.h>

class TwoWire {
 public:
  bool begin(int, int, uint32_t) { return true; }
  void setTimeOut(uint16_t) {}
  void beginTransmission(uint8_t) {}
  size_t write(uint8_t) { return 1; }
  size_t write(const uint8_t*, size_t n) { return n; }
  uint8_t endTransmission(bool = true) { return 0; }
  uint8_t requestFrom(uint8_t, uint8_t n) { return n; }
  int read() { return 0; }
};
inline TwoWire Wire;
