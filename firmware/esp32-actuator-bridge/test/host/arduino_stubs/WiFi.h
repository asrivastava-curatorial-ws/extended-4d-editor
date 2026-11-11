// Host stand-in for <WiFi.h> (compile check only).
#pragma once
#include "Arduino.h"
#define WIFI_STA 1
class WiFiClass {
 public:
  void mode(int) {}
  void setSleep(bool) {}
  bool config(IPAddress, IPAddress, IPAddress) { return true; }
  void begin(const char*, const char*) {}
};
inline WiFiClass WiFi;
