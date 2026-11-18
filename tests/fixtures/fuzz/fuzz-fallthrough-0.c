#include "fuzz-fallthrough-0.h"
#include <stdint.h>

uint8_t io_read(uint16_t a);
void io_write(uint16_t a, uint8_t v);
__attribute__((noreturn, leaf)) void trap(uint16_t k);

CGEN_STORAGE
uint8_t V0_f00[3] = {V0_f00_INIT};
uint8_t V1_f00[3] = {V1_f00_INIT};
uint8_t V2_f00[3] = {V2_f00_INIT};
static void f1(uint8_t v1) {
  uint8_t v2 = V1_f00[v1 / 7];
  if ((int8_t)v2 <= -1) {
    V1_f00[v1 / 7] = v2 + 6;
  }
  uint8_t v3 = V1_f00[v1 / 7];
  if ((v3 & 0x10) != 0) {
    V1_f00[v1 / 7] = v3 + 8;
  }
  if ((V0_f00[v1 / 7] & 0x20) != 0) {
    V2_f00[v1 / 7] = V2_f00[v1 / 7] + 1;
  }
  io_write(0xD406 + v1, V0_f00[v1 / 7]);
  uint8_t v4 = 1;
  for (;;) {
    V1_f00[v1 / 7] = V1_f00[v1 / 7] + 1;
    if ((int8_t)v4 <= 0) {
      uint8_t v5 = V2_f00[v1 / 7];
      io_write(0xD405 + v1, v5);
      V2_f00[v1 / 7] = v5 + 0xC;
      return;
    }
    v4 = v4 - 1;
  }
}
void cgen_play(uint8_t a) {
  (void)a;
  f1(0);
  f1(7);
  f1(0xE);
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
