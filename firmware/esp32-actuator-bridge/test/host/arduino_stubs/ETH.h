// Host stand-in for <ETH.h> (compile check only).
#pragma once
#include "Arduino.h"
enum eth_phy_type_t { ETH_PHY_LAN8720 };
enum eth_clock_mode_t { ETH_CLOCK_GPIO0_IN };
class ETHClass {
 public:
  // Arduino-ESP32 2.x: begin(addr, power, mdc, mdio, type, clock)
  bool begin(int, int, int, int, eth_phy_type_t, eth_clock_mode_t) { return true; }
  // Arduino-ESP32 3.x: begin(type, addr, mdc, mdio, power, clock)
  bool begin(eth_phy_type_t, int, int, int, int, eth_clock_mode_t) { return true; }
  bool config(IPAddress, IPAddress, IPAddress) { return true; }
};
inline ETHClass ETH;
