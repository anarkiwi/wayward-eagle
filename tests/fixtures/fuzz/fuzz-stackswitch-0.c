#include "fuzz-stackswitch-0.h"
#include <stdint.h>

uint8_t io_read(uint16_t a);
void io_write(uint16_t a, uint8_t v);
__attribute__((noreturn, leaf)) void trap(uint16_t k);

CGEN_STORAGE
uint8_t V0_f00[3] = {V0_f00_INIT};
uint8_t V1_f00[3] = {V1_f00_INIT};
uint8_t V2_f00[3] = {V2_f00_INIT};
void cgen_play(uint8_t a) {
  (void)a;
  uint8_t v1 = 0xE;
  uint8_t v2 = 2;
  for (;;) {
    uint8_t v3 = V1_f00[v2];
    if ((int8_t)v3 <= -1) {
      V1_f00[v2] = v3 + 6;
    }
    uint8_t v4 = V1_f00[v2];
    if ((v4 & 0x10) != 0) {
      V1_f00[v2] = v4 + 8;
    }
    if ((V0_f00[v2] & 0x20) != 0) {
      V2_f00[v2] = V2_f00[v2] + 1;
    }
    io_write(0xD406 + v1, V0_f00[v2]);
    V1_f00[v2] = V1_f00[v2] + 0x22;
    uint8_t v5 = 1;
    for (;;) {
      V1_f00[v2] = V1_f00[v2] + 1;
      if ((int8_t)v5 > 0) {
        v5 = v5 - 1;
        continue;
      }
      uint8_t v6 = V2_f00[v2];
      io_write(0xD405 + v1, v6);
      V2_f00[v2] = v6 + 0xC;
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
  V0_f00[0] = 0x8A;
  V0_f00[1] = 0x14;
  V0_f00[2] = 0x4C;
  V1_f00[0] = 0x7B;
  V1_f00[1] = 0x6C;
  V1_f00[2] = 0x67;
  V2_f00[0] = 7;
  V2_f00[1] = 1;
  V2_f00[2] = 0x1F;
}

#include "driver.h"

#ifndef CGEN_HOST
uint8_t io_read(uint16_t a) { return *(volatile uint8_t *)a; }

void io_write(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }
#endif

void tune_init(uint8_t song) { cgen_init(song); }

void tune_play(void) { cgen_play(0); }
