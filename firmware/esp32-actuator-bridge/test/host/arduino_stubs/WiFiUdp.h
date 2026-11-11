// Host stand-in for <WiFiUdp.h> (compile check only).
#pragma once
#include "Arduino.h"
class WiFiUDP {
 public:
  uint8_t begin(uint16_t) { return 1; }
  int parsePacket() { return 0; }
  int read(uint8_t*, size_t) { return 0; }
  IPAddress remoteIP() { return IPAddress(); }
  uint16_t remotePort() { return 0; }
  int beginPacket(IPAddress, uint16_t) { return 1; }
  size_t write(const uint8_t*, size_t n) { return n; }
  int endPacket() { return 1; }
};
