#ifndef MINILIB_H
#define MINILIB_H

#include <stddef.h>
#include <stdint.h>

void *memcpy(void *dst, const void *src, size_t n);
void *memmove(void *dst, const void *src, size_t n);
void *memset(void *dst, int c, size_t n);
int memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);
int strcmp(const char *a, const char *b);
int strncmp(const char *a, const char *b, size_t n);
char *strcpy(char *dst, const char *src);

/* ARM EABI memory helpers GCC may emit instead of the above. */
void *__aeabi_memcpy(void *dst, const void *src, size_t n);
void *__aeabi_memmove(void *dst, const void *src, size_t n);
void *__aeabi_memset(void *dst, int c, size_t n);
void __aeabi_memclr(void *dst, size_t n);
void __aeabi_memclr4(void *dst, size_t n);
void __aeabi_memclr8(void *dst, size_t n);
void __aeabi_memcpy4(void *dst, const void *src, size_t n);
void __aeabi_memcpy8(void *dst, const void *src, size_t n);

size_t u32_to_dec(uint32_t v, char *out);

/* Parse a non-negative decimal float like "15" or "15.25".
 * Returns 0 on success, -1 on malformed input. Deterministic (no libm). */
static inline int parse_f32(const char *s, float *out)
{
    float v = 0.0f;
    float frac = 0.1f;
    int seen_digit = 0;
    int seen_dot = 0;

    if (*s == '\0') {
        return -1;
    }
    for (; *s != '\0'; s++) {
        char c = *s;
        if (c >= '0' && c <= '9') {
            if (seen_dot) {
                v += (float)(c - '0') * frac;
                frac *= 0.1f;
            } else {
                v = v * 10.0f + (float)(c - '0');
            }
            seen_digit = 1;
        } else if (c == '.' && !seen_dot) {
            seen_dot = 1;
        } else {
            return -1;
        }
    }
    if (!seen_digit) {
        return -1;
    }
    *out = v;
    return 0;
}

#endif /* MINILIB_H */
