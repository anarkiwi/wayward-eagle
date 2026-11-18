#include "fuzz-smc-1.h"
#include <stdint.h>

uint8_t io_read(uint16_t a);
void io_write(uint16_t a, uint8_t v);
__attribute__((noreturn, leaf)) void trap(uint16_t k);

CGEN_STORAGE
uint8_t V0_f00[3] = {V0_f00_INIT};
uint8_t V0_f08[3] = {V0_f08_INIT};
static const uint8_t K0[] = {0, 0x3C, 0x78};
static const uint8_t K1[] = {0, 7, 0xE};
static const uint8_t K2[] = {0xE7, 5, 0x7F};
static void f1(uint8_t v1, uint8_t v2) {
  if ((v1 & 4) != 0) {
    V0_f00[v2] = V0_f00[v2] + 7;
  }
  uint8_t v3 = V0_f08[v2];
  V0_f08[v2] = v3 + 0x12;
  if ((C0[K0[v2]] & 8) != 0) {
    V0_f08[v2] = v3 + 0x18;
  }
  io_write(0xD400 + K1[v2], K2[v2]);
  uint8_t v4 = 4;
  for (;;) {
    V0_f00[v2] = V0_f00[v2] + 1;
    if ((int8_t)v4 <= 0) {
      return;
    }
    v4 = v4 - 1;
  }
}
void cgen_play(uint8_t a) {
  (void)a;
  f1(0x22, 0);
  f1(0x34, 1);
  f1(0xC0, 2);
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
