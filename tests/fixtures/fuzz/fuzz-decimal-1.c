#include "fuzz-decimal-1.h"
#include <stdint.h>

uint8_t io_read(uint16_t a);
void io_write(uint16_t a, uint8_t v);
__attribute__((noreturn, leaf)) void trap(uint16_t k);

CGEN_STORAGE
uint8_t V0_f00[3] = {V0_f00_INIT};
uint8_t V1_f00[3] = {V1_f00_INIT};
uint8_t V2_f00[3] = {V2_f00_INIT};
uint8_t V3_f00[3] = {V3_f00_INIT};
void cgen_play(uint8_t a) {
  (void)a;
  uint8_t v1 = 0xE;
  uint8_t v2 = 2;
  for (;;) {
    if ((V3_f00[v2] & 4) != 0) {
      V0_f00[v2] = V0_f00[v2] + 7;
    }
    uint8_t v3 = V1_f00[v2];
    V1_f00[v2] = v3 + 0x12;
    if ((V3_f00[v2] & 8) != 0) {
      V1_f00[v2] = v3 + 0x18;
    }
    io_write(0xD400 + v1, V2_f00[v2]);
    uint8_t v4 = 4;
    for (;;) {
      V0_f00[v2] = V0_f00[v2] + 1;
      uint8_t v5 = V1_f00[v2];
      uint8_t v6 = v5 & 0xF0;
      uint8_t v7 = v6 + 0x10 + ((v5 & 0xF) + 0x10);
      V1_f00[v2] =
          v7 + ((v5 > 0xEF) || (v7 < (uint8_t)(v6 + 0x10)) || (v7 > 0x9F) ? 0x60
                                                                          : 0);
      if ((int8_t)v4 > 0) {
        v4 = v4 - 1;
        continue;
      }
      if ((int8_t)v1 > 6) {
        v1 = v1 - 7;
        v2 = v2 - 1;
        break;
      }
      return;
    }
  }
}
void cgen_init(uint8_t a) {
  if ((uint8_t)(a - CGEN_SONG0) >= CGEN_SONGS) {
    trap(0);
  }
  (void)a;
  V0_f00[0] = 0xC0;
  V0_f00[1] = 0xD6;
  V0_f00[2] = 0x89;
  V1_f00[0] = 0xD1;
  V1_f00[1] = 0x54;
  V1_f00[2] = 0x73;
  V2_f00[0] = 0xC9;
  V2_f00[1] = 0x1F;
  V2_f00[2] = 0x4D;
  V3_f00[0] = 0x1F;
  V3_f00[1] = 0x74;
  V3_f00[2] = 0xFA;
}

#include "driver.h"

#ifndef CGEN_HOST
uint8_t io_read(uint16_t a) { return *(volatile uint8_t *)a; }

void io_write(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }
#endif

void tune_init(uint8_t song) { cgen_init(song); }

void tune_play(void) { cgen_play(0); }
