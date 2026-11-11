// Host stand-in for <freertos/task.h> (compile check only).
#pragma once
#include "FreeRTOS.h"
typedef void (*TaskFunction_t)(void*);
typedef void* TaskHandle_t;
inline BaseType_t xTaskCreatePinnedToCore(TaskFunction_t, const char*, uint32_t, void*, UBaseType_t, TaskHandle_t*, BaseType_t) { return 1; }
inline void vTaskDelay(TickType_t) {}
