// Host stand-in for ESP-IDF <driver/i2s.h>, the "legacy" I2S driver (compile check only). Names, fields
// and prototypes are those of ESP-IDF 4.4.7 (Arduino-ESP32 2.0.17); 5.1 keeps them as deprecated aliases.
#pragma once
#include <stddef.h>
#include <stdint.h>

#include "../freertos/FreeRTOS.h"
typedef int esp_err_t;
#define I2S_PIN_NO_CHANGE (-1)
typedef enum { I2S_NUM_0 = 0, I2S_NUM_1 = 1 } i2s_port_t;
typedef enum { I2S_MODE_MASTER = (0x1 << 0), I2S_MODE_SLAVE = (0x1 << 1), I2S_MODE_TX = (0x1 << 2), I2S_MODE_RX = (0x1 << 3) } i2s_mode_t;
typedef enum { I2S_BITS_PER_SAMPLE_8BIT = 8, I2S_BITS_PER_SAMPLE_16BIT = 16, I2S_BITS_PER_SAMPLE_24BIT = 24, I2S_BITS_PER_SAMPLE_32BIT = 32 } i2s_bits_per_sample_t;
typedef enum { I2S_CHANNEL_FMT_RIGHT_LEFT, I2S_CHANNEL_FMT_ALL_RIGHT, I2S_CHANNEL_FMT_ALL_LEFT, I2S_CHANNEL_FMT_ONLY_RIGHT, I2S_CHANNEL_FMT_ONLY_LEFT } i2s_channel_fmt_t;
typedef enum { I2S_COMM_FORMAT_STAND_I2S = 0x01, I2S_COMM_FORMAT_STAND_MSB = 0x02 } i2s_comm_format_t;
typedef enum { I2S_MCLK_MULTIPLE_DEFAULT = 0 } i2s_mclk_multiple_t;
typedef enum { I2S_BITS_PER_CHAN_DEFAULT = 0 } i2s_bits_per_chan_t;
typedef struct {
  i2s_mode_t mode;
  uint32_t sample_rate;
  i2s_bits_per_sample_t bits_per_sample;
  i2s_channel_fmt_t channel_format;
  i2s_comm_format_t communication_format;
  int intr_alloc_flags;
  int dma_buf_count;
  int dma_buf_len;
  bool use_apll;
  bool tx_desc_auto_clear;
  int fixed_mclk;
  i2s_mclk_multiple_t mclk_multiple;
  i2s_bits_per_chan_t bits_per_chan;
} i2s_config_t;
typedef struct {
  int mck_io_num;
  int bck_io_num;
  int ws_io_num;
  int data_out_num;
  int data_in_num;
} i2s_pin_config_t;
inline esp_err_t i2s_set_pin(i2s_port_t, const i2s_pin_config_t*) { return 0; }
inline esp_err_t i2s_driver_install(i2s_port_t, const i2s_config_t*, int, void*) { return 0; }
inline esp_err_t i2s_write(i2s_port_t, const void*, size_t size, size_t* bytes_written, TickType_t) {
  *bytes_written = size;
  return 0;
}
inline esp_err_t i2s_zero_dma_buffer(i2s_port_t) { return 0; }
