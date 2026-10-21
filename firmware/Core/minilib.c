/*
 * minilib.c - freestanding C library routines we rely on.
 *
 * GCC emits calls to memcpy/memset/strlen/strcmp and the ARM EABI
 * __aeabi_* memory helpers even with -ffreestring, so we provide them all.
 */
#include "minilib.h"

void *memcpy(void *dst, const void *src, size_t n)
{
    uint8_t *d = (uint8_t *)dst;
    const uint8_t *s = (const uint8_t *)src;

    while (n--) {
        *d++ = *s++;
    }
    return dst;
}

void *memmove(void *dst, const void *src, size_t n)
{
    uint8_t *d = (uint8_t *)dst;
    const uint8_t *s = (const uint8_t *)src;

    if (d == s || n == 0u) {
        return dst;
    }
    if (d < s) {
        while (n--) {
            *d++ = *s++;
        }
    } else {
        d += n;
        s += n;
        while (n--) {
            *--d = *--s;
        }
    }
    return dst;
}

void *memset(void *dst, int c, size_t n)
{
    uint8_t *d = (uint8_t *)dst;

    while (n--) {
        *d++ = (uint8_t)c;
    }
    return dst;
}

int memcmp(const void *a, const void *b, size_t n)
{
    const uint8_t *pa = (const uint8_t *)a;
    const uint8_t *pb = (const uint8_t *)b;

    while (n--) {
        if (*pa != *pb) {
            return (int)*pa - (int)*pb;
        }
        pa++;
        pb++;
    }
    return 0;
}

size_t strlen(const char *s)
{
    const char *p = s;

    while (*p) {
        p++;
    }
    return (size_t)(p - s);
}

int strcmp(const char *a, const char *b)
{
    while (*a && (*a == *b)) {
        a++;
        b++;
    }
    return (int)(unsigned char)*a - (int)(unsigned char)*b;
}

int strncmp(const char *a, const char *b, size_t n)
{
    while (n && *a && (*a == *b)) {
        a++;
        b++;
        n--;
    }
    if (n == 0u) {
        return 0;
    }
    return (int)(unsigned char)*a - (int)(unsigned char)*b;
}

char *strcpy(char *dst, const char *src)
{
    char *d = dst;

    while ((*d++ = *src++) != '\0') {
    }
    return dst;
}

/* ARM EABI entry points that GCC may emit instead of the above. */
void *__aeabi_memcpy(void *dst, const void *src, size_t n)
{
    return memcpy(dst, src, n);
}

void *__aeabi_memmove(void *dst, const void *src, size_t n)
{
    return memmove(dst, src, n);
}

void *__aeabi_memset(void *dst, int c, size_t n)
{
    return memset(dst, c, n);
}

void __aeabi_memclr(void *dst, size_t n)
{
    (void)memset(dst, 0, n);
}

void __aeabi_memclr4(void *dst, size_t n)
{
    (void)memset(dst, 0, n);
}

void __aeabi_memclr8(void *dst, size_t n)
{
    (void)memset(dst, 0, n);
}

void __aeabi_memcpy4(void *dst, const void *src, size_t n)
{
    (void)memcpy(dst, src, n);
}

void __aeabi_memcpy8(void *dst, const void *src, size_t n)
{
    (void)memcpy(dst, src, n);
}

/* Unsigned 32-bit to decimal text. Returns length written (no NUL). */
size_t u32_to_dec(uint32_t v, char *out)
{
    char tmp[10];
    size_t n = 0u;

    do {
        tmp[n++] = (char)('0' + (v % 10u));
        v /= 10u;
    } while (v != 0u);

    for (size_t i = 0u; i < n; i++) {
        out[i] = tmp[n - 1u - i];
    }
    return n;
}
