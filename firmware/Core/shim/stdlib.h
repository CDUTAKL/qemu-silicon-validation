/*
 * stdlib.h - freestanding shim (see shim/string.h for why this exists).
 *
 * The FreeRTOS kernel includes <stdlib.h> for malloc/free prototypes it
 * never calls in this configuration (heap_4 owns allocation). Keep this
 * header deliberately minimal.
 */
#ifndef SHIM_STDLIB_H
#define SHIM_STDLIB_H

#include <stddef.h>

#endif /* SHIM_STDLIB_H */
