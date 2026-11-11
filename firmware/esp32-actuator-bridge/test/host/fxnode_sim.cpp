// A simulated effects node for a PC: the firmware's own core (lib/fourdvr_fx) behind a UDP socket.
//
//   make -C firmware/esp32-actuator-bridge/test/host fxnode_sim
//   ./fxnode_sim --host 127.0.0.1 --port 47002 --node 1 --dmx-ports 1 --audio-channels 2 --log /tmp/node1
//
// It does what the ESP32 does with the network side of the 4DFX link, in real time: answers HELLO, keeps
// the DMX lines (time-out included) and plays the sound stream out of the buffer at the stream rate.
// Instead of a UART and a DAC it writes what it would send:
//   <log>/dmx.log     one line per change of a line: <milliseconds> <port> <hex of the slots that are sent>
//   <log>/audio.raw   the sound as played (int16, interleaved), silence included
//   <log>/summary.json  counters, written when the program ends (SIGINT/SIGTERM, or after --seconds)
// SIGUSR1 switches the inhibit (emergency stop) on and off. POSIX only (Linux, macOS).
#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include <string>

#include "fourdvr_fx.h"

using namespace fdvr::fx;

static volatile sig_atomic_t g_stop = 0;
static volatile sig_atomic_t g_toggle = 0;
static void onStop(int) { g_stop = 1; }
static void onToggle(int) { g_toggle = 1; }

static double nowSeconds() {
  timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1e9;
}

int main(int argc, char** argv) {
  std::string host = "127.0.0.1", log_dir = ".";
  int port = kUdpPort;
  double seconds = 0.0;
  Config cfg;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    const char* v = i + 1 < argc ? argv[i + 1] : "";
    if (a == "--host") host = v, ++i;
    else if (a == "--port") port = atoi(v), ++i;
    else if (a == "--node") cfg.node_id = static_cast<uint8_t>(atoi(v)), ++i;
    else if (a == "--dmx-ports") cfg.dmx_ports = static_cast<uint8_t>(atoi(v)), ++i;
    else if (a == "--audio-channels") cfg.audio_channels = static_cast<uint8_t>(atoi(v)), ++i;
    else if (a == "--audio-rate") cfg.audio_rate_hz = static_cast<uint16_t>(atoi(v)), ++i;
    else if (a == "--audio-target-ms") cfg.audio_target_ms = static_cast<uint16_t>(atoi(v)), ++i;
    else if (a == "--actuators") cfg.has_actuators = true;
    else if (a == "--log") log_dir = v, ++i;
    else if (a == "--seconds") seconds = atof(v), ++i;
    else {
      fprintf(stderr, "usage: fxnode_sim [--host IP] [--port N] [--node ID] [--dmx-ports 0..2] [--audio-channels 0..2]\n"
                      "                  [--audio-rate HZ] [--audio-target-ms MS] [--actuators] [--log DIR] [--seconds S]\n");
      return 2;
    }
  }
  static Node node(cfg);
  const int sock = socket(AF_INET, SOCK_DGRAM, 0);
  sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons(static_cast<uint16_t>(port));
  if (sock < 0 || inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1 || bind(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    perror("fxnode_sim: cannot listen");
    return 2;
  }
  FILE* dmx_log = fopen((log_dir + "/dmx.log").c_str(), "w");
  FILE* audio_raw = fopen((log_dir + "/audio.raw").c_str(), "wb");
  if (dmx_log == nullptr || audio_raw == nullptr) {
    perror("fxnode_sim: cannot write the log files");
    return 2;
  }
  signal(SIGINT, onStop);
  signal(SIGTERM, onStop);
  signal(SIGUSR1, onToggle);
  const int ports = node.config().dmx_ports;
  const int channels = node.config().audio_channels > 0 ? node.config().audio_channels : 1;
  static uint8_t shown[kMaxPorts][kSlots];
  int shown_slots[kMaxPorts] = {-1, -1};
  memset(shown, 0, sizeof(shown));
  const double start = nowSeconds();
  double next_dmx = start;
  unsigned long long frames_read = 0, frames_from_stream = 0;
  printf("fxnode_sim: node %d on %s:%d, %d DMX line(s), %d audio channel(s)\n", cfg.node_id, host.c_str(), port, ports, cfg.audio_channels);
  fflush(stdout);
  while (!g_stop) {
    pollfd pfd = {sock, POLLIN, 0};
    poll(&pfd, 1, 1);
    const double now = nowSeconds();
    const uint32_t now_ms = static_cast<uint32_t>((now - start) * 1000.0);
    if (seconds > 0.0 && now - start >= seconds) break;
    if (g_toggle) {
      g_toggle = 0;
      node.setInhibit(!node.inhibited());
    }
    while (true) {
      uint8_t buf[2048];
      sockaddr_in from;
      socklen_t from_len = sizeof(from);
      const ssize_t n = recvfrom(sock, buf, sizeof(buf), MSG_DONTWAIT, reinterpret_cast<sockaddr*>(&from), &from_len);
      if (n < 0) break;
      if (node.handle(buf, static_cast<size_t>(n), now_ms)) {
        uint8_t info[kInfoSize];
        const size_t len = node.makeInfo(now_ms, info);
        sendto(sock, info, len, 0, reinterpret_cast<sockaddr*>(&from), from_len);
      }
    }
    node.update(now_ms);
    if (now >= next_dmx) {  // the DMX refresh: what would go out on each line now
      next_dmx += 1.0 / node.config().dmx_refresh_hz;
      if (next_dmx < now) next_dmx = now;
      for (int p = 0; p < ports; ++p) {
        const int slots = node.dmxSlots(p);
        if (slots != shown_slots[p] || memcmp(shown[p], node.dmxFrame(p), kSlots) != 0) {
          memcpy(shown[p], node.dmxFrame(p), kSlots);
          shown_slots[p] = slots;
          fprintf(dmx_log, "%u %d ", now_ms, p);
          for (int s = 0; s < slots; ++s) fprintf(dmx_log, "%02x", shown[p][s]);
          fputc('\n', dmx_log);
          fflush(dmx_log);
        }
      }
    }
    if (node.config().audio_channels > 0) {  // the DAC: takes frames at the stream rate, by the clock
      const unsigned long long due = static_cast<unsigned long long>((now - start) * node.config().audio_rate_hz);
      while (frames_read < due) {
        int16_t frames[64 * kMaxChannels];
        const size_t chunk = due - frames_read > 64 ? 64 : static_cast<size_t>(due - frames_read);
        frames_from_stream += node.readAudio(frames, chunk);
        fwrite(frames, sizeof(int16_t) * static_cast<size_t>(channels), chunk, audio_raw);
        frames_read += chunk;
      }
    }
  }
  fclose(dmx_log);
  fclose(audio_raw);
  FILE* summary = fopen((log_dir + "/summary.json").c_str(), "w");
  if (summary != nullptr) {
    fprintf(summary,
            "{\"node\": %d, \"packets\": %u, \"invalid\": %u, \"dmx_timeouts\": %u, \"audio_underruns\": %u, \"audio_gaps\": %u,\n"
            " \"frames_read\": %llu, \"frames_from_stream\": %llu, \"audio_rate_hz\": %u, \"audio_channels\": %d, \"seconds\": %.3f}\n",
            cfg.node_id, node.packets(), node.invalid(), node.dmxTimeouts(), node.audioUnderruns(), node.audioGaps(), frames_read,
            frames_from_stream, node.config().audio_rate_hz, cfg.audio_channels, nowSeconds() - start);
    fclose(summary);
  }
  close(sock);
  return 0;
}
