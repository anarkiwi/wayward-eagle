#ifndef CGEN_DATA_H
#define CGEN_DATA_H
#define R0_ORG 0x3000
#define R0_LEN 15
#define R0_INIT                                                                \
  0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF,      \
      0xFF, 0xFF, 0x00
#define R1_ORG 0x3020
#define R1_LEN 15
#define R1_INIT                                                                \
  0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF,      \
      0xFF, 0xFF, 0x00
#define R2_ORG 0x3040
#define R2_LEN 15
#define R2_INIT                                                                \
  0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF,      \
      0xFF, 0xFF, 0x00
#define R3_ORG 0x3060
#define R3_LEN 15
#define R3_INIT                                                                \
  0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF,      \
      0xFF, 0xFF, 0x00
#define CGEN_TRAPS 0x1000, 0x103D
#define CGEN_STORAGE                                                           \
  uint8_t R0[R0_LEN] = {R0_INIT};                                              \
  uint8_t R1[R1_LEN] = {R1_INIT};                                              \
  uint8_t R2[R2_LEN] = {R2_INIT};                                              \
  uint8_t R3[R3_LEN] = {R3_INIT};
#define CGEN_ARRAYS R0, R1, R2, R3
#define CGEN_ARRAYS_ORG R0_ORG, R1_ORG, R2_ORG, R3_ORG
#define CGEN_ARRAYS_LEN R0_LEN, R1_LEN, R2_LEN, R3_LEN
#define CGEN_REGIONS R0, R1, R2, R3
#define CGEN_REGIONS_ORG R0_ORG, R1_ORG, R2_ORG, R3_ORG
#define CGEN_REGIONS_LEN R0_LEN, R1_LEN, R2_LEN, R3_LEN
#define CGEN_SONG0 0x00
#define CGEN_SONGS 1
#endif
