# Host build of the portable cores (motion link, effects node) + unit tests, a compile check of the
# Arduino glue, and a simulated effects node made of the firmware's own core.
#   make -C firmware/esp32-actuator-bridge/test/host              build and run the unit tests
#   make -C firmware/esp32-actuator-bridge/test/host check-main   compile-check src/main.cpp (all build variants)
#   make -C firmware/esp32-actuator-bridge/test/host fxnode_sim   the effects node as a program for this PC (UDP)
CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Werror -pedantic
LIB := ../../lib/fourdvr_link/src
FX := ../../lib/fourdvr_fx/src
FW := ../..

.PHONY: all test check-main clean
all: test check-main fxnode_sim

test_link: test_link.cpp $(LIB)/fourdvr_link.cpp $(LIB)/fourdvr_link.h
	$(CXX) $(CXXFLAGS) -I$(LIB) -o $@ test_link.cpp $(LIB)/fourdvr_link.cpp

test_fx: test_fx.cpp $(FX)/fourdvr_fx.cpp $(FX)/fourdvr_fx.h
	$(CXX) $(CXXFLAGS) -I$(FX) -o $@ test_fx.cpp $(FX)/fourdvr_fx.cpp

fxnode_sim: fxnode_sim.cpp $(FX)/fourdvr_fx.cpp $(FX)/fourdvr_fx.h
	$(CXX) $(CXXFLAGS) -I$(FX) -o $@ fxnode_sim.cpp $(FX)/fourdvr_fx.cpp

test: test_link test_fx
	./test_link ../vectors.txt
	./test_fx ../fx_vectors.txt

CHECK := $(CXX) -std=c++17 -Wall -Wextra -Werror -fsyntax-only -Iarduino_stubs -I$(LIB) -I$(FX) -I$(FW)/include -x c++ $(FW)/src/main.cpp
FX_ONLY := -DFDVR_HAS_ACTUATORS=0 -DFDVR_HAS_DMX=1 -DFDVR_HAS_AUDIO=1
ALL_IN_ONE := -DFDVR_HAS_DMX=1 -DFDVR_HAS_AUDIO=1 -DFDVR_I2S_BCK_PIN=26 -DFDVR_I2S_WS_PIN=25 -DFDVR_I2S_DOUT_PIN=27

# Every build variant of platformio.ini, against stand-ins for the Arduino-ESP32 / ESP-IDF headers.
check-main:
	$(CHECK)
	$(CHECK) -DESP_ARDUINO_VERSION_MAJOR=3
	$(CHECK) -DFDVR_NET_ETHERNET=0 -DFDVR_NET_WIFI=1
	$(CHECK) -DFDVR_NET_ETHERNET=0 -DFDVR_NET_WIFI=0
	$(CHECK) -DFDVR_HAS_DMX=1
	$(CHECK) $(FX_ONLY)
	$(CHECK) $(FX_ONLY) -DESP_ARDUINO_VERSION_MAJOR=3
	$(CHECK) $(FX_ONLY) -DFDVR_NET_ETHERNET=0 -DFDVR_NET_WIFI=1 -DFDVR_AUDIO_BUFFER_MS=200
	$(CHECK) -DFDVR_HAS_ACTUATORS=0 -DFDVR_HAS_DMX=1
	$(CHECK) -DFDVR_HAS_ACTUATORS=0 -DFDVR_HAS_AUDIO=1
	$(CHECK) $(ALL_IN_ONE) -DFDVR_NET_ETHERNET=0 -DFDVR_NET_WIFI=1
	@# a pin used twice must stop the build: the default I2S pins belong to the platform outputs
	@if $(CHECK) -DFDVR_HAS_AUDIO=1 2>/dev/null; then echo "pin conflict was NOT detected"; exit 1; fi
	@if $(CHECK) -DFDVR_HAS_DMX=1 -DFDVR_DMX_TX_PIN=14 2>/dev/null; then echo "pin conflict was NOT detected"; exit 1; fi
	@echo "main.cpp compile check OK (Arduino-ESP32 2.x and 3.x APIs; Ethernet, Wi-Fi and serial; platform, effects-only and all-in-one builds; pin conflicts refused)"

clean:
	rm -f test_link test_fx fxnode_sim
