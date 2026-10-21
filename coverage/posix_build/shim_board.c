/*
 * shim_board.c - POSIX implementation of the board.h system_reset() hook.
 */
#include <stdint.h>

static volatile uint32_t s_reset_count;

void system_reset(void)
{
    /* On target this writes SCB->AIRCR and reboots. Under POSIX we only
     * record the request; the caller (diag RESET command / heartbeat-loss
     * checker) continues, which is fine for logic coverage. */
    s_reset_count++;
}

uint32_t shim_board_reset_count(void)
{
    return s_reset_count;
}
