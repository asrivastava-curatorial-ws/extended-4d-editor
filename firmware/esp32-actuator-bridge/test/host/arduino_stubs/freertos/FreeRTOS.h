// Host stand-in for <freertos/FreeRTOS.h> (compile check only).
#pragma once
#include <stdint.h>
typedef uint32_t TickType_t;
typedef int BaseType_t;
typedef unsigned UBaseType_t;
typedef void* QueueHandle_t;
#define portMAX_DELAY (static_cast<TickType_t>(0xffffffffUL))
#define pdMS_TO_TICKS(ms) (static_cast<TickType_t>(ms))
