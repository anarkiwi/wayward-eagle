#include "fuzz-jsr3-1.h"
#include <stdint.h>

uint8_t io_read(uint16_t a);
void io_write(uint16_t a, uint8_t v);
__attribute__((noreturn, leaf)) void trap(uint16_t k);

CGEN_STORAGE
uint8_t V0_f00[3] = {V0_f00_INIT};
uint8_t V1_f00[3] = {V1_f00_INIT};
uint8_t V2_f00[3] = {V2_f00_INIT};
uint8_t V3_f00[3] = {V3_f00_INIT};
static void f1(uint8_t v1) {
  if ((V3_f00[v1 / 7] & 4) != 0) {
    V0_f00[v1 / 7] = V0_f00[v1 / 7] + 7;
  }
  uint8_t v2 = V1_f00[v1 / 7];
  V1_f00[v1 / 7] = v2 + 0x12;
  if ((V3_f00[v1 / 7] & 8) != 0) {
    V1_f00[v1 / 7] = v2 + 0x18;
  }
  io_write(0xD400 + v1, V2_f00[v1 / 7]);
  uint8_t v3 = 4;
  for (;;) {
    V0_f00[v1 / 7] = V0_f00[v1 / 7] + 1;
    if ((int8_t)v3 <= 0) {
      return;
    }
    v3 = v3 - 1;
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
