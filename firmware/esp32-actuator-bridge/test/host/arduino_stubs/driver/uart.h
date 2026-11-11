// Host stand-in for ESP-IDF <driver/uart.h> (compile check only). Names, fields and prototypes are those
// of ESP-IDF 4.4.7 (Arduino-ESP32 2.0.17) - unchanged in 5.1 - so that the glue is checked against the
// real argument lists; nothing here does anything.
#pragma once
#include <stddef.h>
#include <stdint.h>

#include "../freertos/FreeRTOS.h"
typedef int esp_err_t;
typedef int uart_port_t;
#define UART_NUM_0 (0)
#define UART_NUM_1 (1)
#define UART_NUM_2 (2)
#define UART_PIN_NO_CHANGE (-1)
typedef enum { UART_DATA_5_BITS = 0x0, UART_DATA_6_BITS = 0x1, UART_DATA_7_BITS = 0x2, UART_DATA_8_BITS = 0x3 } uart_word_length_t;
typedef enum { UART_STOP_BITS_1 = 0x1, UART_STOP_BITS_1_5 = 0x2, UART_STOP_BITS_2 = 0x3 } uart_stop_bits_t;
typedef enum { UART_PARITY_DISABLE = 0x0, UART_PARITY_EVEN = 0x2, UART_PARITY_ODD = 0x3 } uart_parity_t;
typedef enum { UART_HW_FLOWCTRL_DISABLE = 0x0, UART_HW_FLOWCTRL_RTS = 0x1 } uart_hw_flowcontrol_t;
typedef enum { UART_SIGNAL_INV_DISABLE = 0, UART_SIGNAL_RXD_INV = (0x1 << 2), UART_SIGNAL_TXD_INV = (0x1 << 5) } uart_signal_inv_t;
typedef enum { UART_SCLK_APB = 0x0, UART_SCLK_REF_TICK = 0x3 } uart_sclk_t;
typedef struct {
  int baud_rate;
  uart_word_length_t data_bits;
  uart_parity_t parity;
  uart_stop_bits_t stop_bits;
  uart_hw_flowcontrol_t flow_ctrl;
  uint8_t rx_flow_ctrl_thresh;
  uart_sclk_t source_clk;
} uart_config_t;
inline esp_err_t uart_driver_install(uart_port_t, int, int, int, QueueHandle_t*, int) { return 0; }
inline esp_err_t uart_set_line_inverse(uart_port_t, uint32_t) { return 0; }
inline esp_err_t uart_set_pin(uart_port_t, int, int, int, int) { return 0; }
inline esp_err_t uart_param_config(uart_port_t, const uart_config_t*) { return 0; }
inline esp_err_t uart_wait_tx_done(uart_port_t, TickType_t) { return 0; }
inline int uart_write_bytes(uart_port_t, const void*, size_t size) { return static_cast<int>(size); }
