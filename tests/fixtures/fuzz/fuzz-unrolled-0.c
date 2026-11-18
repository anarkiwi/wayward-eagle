#include "fuzz-unrolled-0.h"
#include <stdint.h>

uint8_t io_read(uint16_t a);
void io_write(uint16_t a, uint8_t v);
__attribute__((noreturn, leaf)) void trap(uint16_t k);

CGEN_STORAGE
static const uint8_t K0[] = {0, 7, 0xE};
static void f1(uint8_t v1) {
  uint8_t v2 = K0[v1];
  uint8_t v3 = R1[v2];
  if ((int8_t)v3 <= -1) {
    R1[v2] = v3 + 6;
  }
  uint8_t v4 = R1[v2];
  if ((v4 & 0x10) != 0) {
    R1[v2] = v4 + 8;
  }
  if ((R0[v2] & 0x20) != 0) {
    R2[v2] = R2[v2] + 1;
  }
  io_write(0xD406 + v2, R0[v2]);
  uint8_t v5 = 1;
  for (;;) {
    R1[v2] = R1[v2] + 1;
    if ((int8_t)v5 <= 0) {
      io_write(0xD405 + v2, R2[v2]);
      R2[v2] = R2[v2] + 0xC;
      return;
    }
    v5 = v5 - 1;
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
  R0[0] = 0x8A;
  R0[7] = 0x14;
  R0[0xE] = 0x4C;
  R1[0] = 0x7B;
  R1[7] = 0x6C;
  R1[0xE] = 0x67;
  R2[0] = 7;
  R2[7] = 1;
  R2[0xE] = 0x1F;
}

#include "driver.h"

#ifndef CGEN_HOST
uint8_t io_read(uint16_t a) { return *(volatile uint8_t *)a; }

void io_write(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }
#endif

void tune_init(uint8_t song) { cgen_init(song); }

void tune_play(void) { cgen_play(0); }
