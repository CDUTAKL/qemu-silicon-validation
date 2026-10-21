/*
 * string.h - freestanding shim.
 *
 * The arm-none-eabi toolchain ships without newlib headers, so string
 * routines come from minilib.c. This header exists so third-party code
 * (the FreeRTOS kernel) can include <string.h> in the usual way.
 */
#ifndef SHIM_STRING_H
#define SHIM_STRING_H

#include <stddef.h>

void *memcpy(void *dst, const void *src, size_t n);
void *memmove(void *dst, const void *src, size_t n);
void *memset(void *dst, int c, size_t n);
int memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);
int strcmp(const char *a, const char *b);
int strncmp(const char *a, const char *b, size_t n);
char *strcpy(char *dst, const char *src);

#endif /* SHIM_STRING_H */
