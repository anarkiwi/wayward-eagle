#include "fuzz-smc-0.h"
#include <stdint.h>

uint8_t io_read(uint16_t a);
void io_write(uint16_t a, uint8_t v);
__attribute__((noreturn, leaf)) void trap(uint16_t k);

CGEN_STORAGE
uint8_t V0_f00[3] = {V0_f00_INIT};
uint8_t V0_f25[3] = {V0_f25_INIT};
static const uint8_t K0[] = {0, 7, 0xE};
static const uint8_t K1[] = {0x20, 0, 0x20};
static const uint8_t K2[] = {0, 0x53, 0xA6};
static void f1(uint8_t v1) {
  uint8_t v2 = K0[v1];
  uint8_t v3 = V0_f00[v1];
  if ((int8_t)v3 <= -1) {
    V0_f00[v1] = v3 + 6;
  }
  uint8_t v4 = V0_f00[v1];
  if ((v4 & 0x10) != 0) {
    V0_f00[v1] = v4 + 8;
  }
  if (K1[v1] != 0) {
    V0_f25[v1] = V0_f25[v1] + 1;
  }
  io_write(0xD406 + v2, C0[K2[v1]]);
  uint8_t v5 = 1;
  for (;;) {
    V0_f00[v1] = V0_f00[v1] + 1;
    if ((int8_t)v5 <= 0) {
      io_write(0xD405 + v2, V0_f25[v1]);
      V0_f25[v1] = V0_f25[v1] + 0xC;
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
}

#include "driver.h"

#ifndef CGEN_HOST
uint8_t io_read(uint16_t a) { return *(volatile uint8_t *)a; }

void io_write(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }
#endif

void tune_init(uint8_t song) { cgen_init(song); }

void tune_play(void) { cgen_play(0); }
