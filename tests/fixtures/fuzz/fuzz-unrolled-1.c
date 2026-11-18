#include "fuzz-unrolled-1.h"
#include <stdint.h>

uint8_t io_read(uint16_t a);
void io_write(uint16_t a, uint8_t v);
__attribute__((noreturn, leaf)) void trap(uint16_t k);

CGEN_STORAGE
static const uint8_t K0[] = {0, 7, 0xE};
static void f1(uint8_t v1) {
  uint8_t v2 = K0[v1];
  if ((R3[v2] & 4) != 0) {
    R0[v2] = R0[v2] + 7;
  }
  uint8_t v3 = R1[v2];
  R1[v2] = v3 + 0x12;
  if ((R3[v2] & 8) != 0) {
    R1[v2] = v3 + 0x18;
  }
  io_write(0xD400 + v2, R2[v2]);
  uint8_t v4 = 4;
  for (;;) {
    R0[v2] = R0[v2] + 1;
    if ((int8_t)v4 <= 0) {
      return;
    }
    v4 = v4 - 1;
  }
}
void cgen_play(uint8_t a) {
  (void)a;
  f1(0);
  f1(1);
  f1(2);
}
void cgen_init(uint8_t a) {
  if ((uint8_t)(a - CGEN_SONG0) >= CGEN_SONGS) {
    trap(0);
  }
  (void)a;
  R0[0] = 0xC0;
  R0[7] = 0xD6;
  R0[0xE] = 0x89;
  R1[0] = 0xD1;
  R1[7] = 0x54;
  R1[0xE] = 0x73;
  R2[0] = 0xC9;
  R2[7] = 0x1F;
  R2[0xE] = 0x4D;
  R3[0] = 0x1F;
  R3[7] = 0x74;
  R3[0xE] = 0xFA;
}

#include "driver.h"

#ifndef CGEN_HOST
uint8_t io_read(uint16_t a) { return *(volatile uint8_t *)a; }

void io_write(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }
#endif

void tune_init(uint8_t song) { cgen_init(song); }

void tune_play(void) { cgen_play(0); }
