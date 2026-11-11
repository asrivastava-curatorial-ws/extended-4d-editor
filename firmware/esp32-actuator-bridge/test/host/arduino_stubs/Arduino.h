// Minimal host stand-ins for the Arduino-ESP32 API used by src/main.cpp.
// They exist only so the firmware glue can be compile-checked on a PC (make -C test/host check-main);
// they do not emulate hardware. The real build uses the Arduino-ESP32 core via PlatformIO.
#pragma once
#include <stddef.h>
#include <stdint.h>

#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define INPUT_PULLUP 2

inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}
inline int digitalRead(int) { return HIGH; }
inline uint32_t millis() { return 0; }
inline uint32_t micros() { return 0; }
inline void delay(uint32_t) {}
inline void delayMicroseconds(uint32_t) {}
inline void enableLoopWDT() {}
inline void feedLoopWDT() {}
// Arduino-ESP32 2.x LEDC API
inline double ledcSetup(uint8_t, double, uint8_t) { return 0; }
inline void ledcAttachPin(uint8_t, uint8_t) {}
// Arduino-ESP32 3.x LEDC API (ledcWrite takes a pin in 3.x and a channel in 2.x; same signature)
inline bool ledcAttach(uint8_t, uint32_t, uint8_t) { return true; }
inline bool ledcWrite(uint8_t, uint32_t) { return true; }

class HardwareSerial {
 public:
  void begin(unsigned long) {}
  int available() { return 0; }
  int read() { return -1; }
  size_t write(const uint8_t*, size_t n) { return n; }
};
inline HardwareSerial Serial;

class IPAddress {
 public:
  IPAddress() = default;
  IPAddress(uint8_t, uint8_t, uint8_t, uint8_t) {}
};
