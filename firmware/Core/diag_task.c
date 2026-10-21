/*
 * diag_task.c - DiagTask (priority 3).
 *
 * Owns the UART RX path. Two input grammars share the byte stream:
 *
 *   1. Text command lines ending in '\n' (see command table below).
 *      Replies are plain ASCII "OK ..." / "ERR ..." lines.
 *   2. Binary frames from the host: 0xAA 0x55 seq(BE) type payload crc(BE).
 *      A type 0x04 ACK-request frame is answered with a type 0x05 ACK
 *      frame that goes to the FRONT of the TX queue.
 *
 * Text commands (case-sensitive):
 *   STAT?               -> OK STAT tasks=N qdepth=D run=R hb=H seq=S
 *   FAULT OV <v>        -> force next transmitted data frame voltage to v;
 *                          SensorTask emits alarm frame fault_id=1 level=2
 *                          in the same critical section as that data frame
 *   FAULT OC <i>        -> force next transmitted data frame current to i,
 *                          alarm frame fault_id=2 level=2 (same mechanism)
 *   FAULT DISCONN       -> pause data frame reporting (heartbeat goes on)
 *   FAULT CONN          -> resume data frame reporting
 *   FAULT HANG <task>   -> vTaskSuspend task (sensor|comm|diag|watchdog)
 *   RESUME <task>       -> vTaskResume task
 *   RESET               -> NVIC_SystemReset()
 */
#include "app.h"

#include "board.h"
#include "crc16.h"
#include "frame.h"
#include "minilib.h"
#include "semphr.h"
#include "uart.h"

#define LINE_MAX 48u

static void reply(const char *s)
{
    uart_write_str(s);
}

/* ---------------- command dispatch ---------------- */

static TaskHandle_t *find_task(const char *name)
{
    if (strcmp(name, "sensor") == 0) {
        return &th_sensor;
    }
    if (strcmp(name, "comm") == 0) {
        return &th_comm;
    }
    if (strcmp(name, "diag") == 0) {
        return &th_diag;
    }
    if (strcmp(name, "watchdog") == 0) {
        return &th_watchdog;
    }
    return NULL;
}

static void cmd_stat(void)
{
    char buf[96];
    size_t n;

    (void)strcpy(buf, "OK STAT tasks=");
    n = strlen(buf);
    n += u32_to_dec(app_running_task_count(), &buf[n]);
    (void)strcpy(&buf[n], " qdepth=");
    n = strlen(buf);
    n += u32_to_dec(uxQueueMessagesWaiting(q_tx), &buf[n]);
    (void)strcpy(&buf[n], " run=");
    n = strlen(buf);
    n += u32_to_dec(g_run_count, &buf[n]);
    (void)strcpy(&buf[n], " hb=");
    n = strlen(buf);
    n += u32_to_dec(hb_count, &buf[n]);
    (void)strcpy(&buf[n], " seq=");
    n = strlen(buf);
    n += u32_to_dec(g_seq, &buf[n]);
    buf[n++] = '\n';
    buf[n] = '\0';
    reply(buf);
}

static void cmd_fault_ov(const char *arg)
{
    float v;

    if (parse_f32(arg, &v) != 0) {
        reply("ERR BAD VALUE\n");
        return;
    }
    g_ov_value = v;
    g_ov_pending = 1u;
    reply("OK FAULT OV\n");
}

static void cmd_fault_oc(const char *arg)
{
    float v;

    if (parse_f32(arg, &v) != 0) {
        reply("ERR BAD VALUE\n");
        return;
    }
    g_oc_value = v;
    g_oc_pending = 1u;
    reply("OK FAULT OC\n");
}

static void cmd_fault_hang(const char *name)
{
    TaskHandle_t *th = find_task(name);

    if (th == NULL) {
        reply("ERR BAD TASK\n");
        return;
    }
    vTaskSuspend(*th);
    reply("OK FAULT HANG\n");
}

static void cmd_resume(const char *name)
{
    TaskHandle_t *th = find_task(name);

    if (th == NULL) {
        reply("ERR BAD TASK\n");
        return;
    }
    vTaskResume(*th);
    reply("OK RESUME\n");
}

/* Split line into whitespace-separated tokens (in place). */
static uint8_t tokenize(char *line, char **tok, uint8_t max_tok)
{
    uint8_t n = 0u;
    char *p = line;

    while (*p != '\0' && n < max_tok) {
        while (*p == ' ' || *p == '\t') {
            p++;
        }
        if (*p == '\0') {
            break;
        }
        tok[n++] = p;
        while (*p != '\0' && *p != ' ' && *p != '\t') {
            p++;
        }
        if (*p != '\0') {
            *p++ = '\0';
        }
    }
    return n;
}

static void dispatch_line(char *line)
{
    char *tok[4];
    uint8_t nt = tokenize(line, tok, 4u);

    if (nt == 0u) {
        return;
    }
    if (strcmp(tok[0], "STAT?") == 0) {
        cmd_stat();
    } else if (strcmp(tok[0], "RESET") == 0) {
        reply("OK RESET\n");
        system_reset();
    } else if (strcmp(tok[0], "FAULT") == 0 && nt >= 2u) {
        if (strcmp(tok[1], "OV") == 0 && nt == 3u) {
            cmd_fault_ov(tok[2]);
        } else if (strcmp(tok[1], "OC") == 0 && nt == 3u) {
            cmd_fault_oc(tok[2]);
        } else if (strcmp(tok[1], "DISCONN") == 0 && nt == 2u) {
            g_stream_paused = 1u;
            reply("OK FAULT DISCONN\n");
        } else if (strcmp(tok[1], "CONN") == 0 && nt == 2u) {
            g_stream_paused = 0u;
            reply("OK FAULT CONN\n");
        } else if (strcmp(tok[1], "HANG") == 0 && nt == 3u) {
            cmd_fault_hang(tok[2]);
        } else {
            reply("ERR USAGE\n");
        }
    } else if (strcmp(tok[0], "RESUME") == 0 && nt == 2u) {
        cmd_resume(tok[1]);
    } else {
        reply("ERR UNKNOWN CMD\n");
    }
}

/* ---------------- host binary frame handling ---------------- */

/* Feed a fully received, CRC-validated host frame. */
static void handle_host_frame(const frame_t *f)
{
    if (f->type == FRAME_TYPE_ACK_REQ && f->len == 2u &&
        f->payload[0] == 0x00u && f->payload[1] == 0x01u) {
        frame_t ack;

        memset(&ack, 0, sizeof(ack));
        ack.type = FRAME_TYPE_ACK;
        ack.len = 2u;
        ack.payload[0] = 0x00u; /* status = ok */
        ack.payload[1] = 0x00u;
        (void)xQueueSendToFront(q_tx, &ack, 0);
    }
    /* Other host frame types: silently ignored. */
}

/* ---------------- RX stream state machine ---------------- */

typedef enum {
    RX_TEXT,
    RX_HDR1,
    RX_SEQ0,
    RX_SEQ1,
    RX_TYPE,
    RX_PAYLOAD,
    RX_CRC0,
    RX_CRC1,
} rx_state_t;

static rx_state_t s_state = RX_TEXT;
static uint8_t s_line[LINE_MAX];
static uint16_t s_line_len;
static uint16_t s_seq;
static uint8_t s_type;
static uint8_t s_payload[FRAME_MAX_PAYLOAD];
static uint8_t s_payload_len;
static uint8_t s_payload_idx;
static uint8_t s_crc_idx;
static uint8_t s_crc_buf[2];

static void feed_byte(uint8_t b)
{
    if (s_state == RX_TEXT) {
        if (b == FRAME_HDR0) {
            s_state = RX_HDR1;
            s_line_len = 0u;
            return;
        }
        if (b == '\n') {
            s_line[s_line_len] = '\0';
            dispatch_line((char *)s_line);
            s_line_len = 0u;
            return;
        }
        if (b == '\r') {
            return;
        }
        if (s_line_len < LINE_MAX - 1u) {
            s_line[s_line_len++] = b;
        } else {
            s_line_len = 0u; /* overflow: drop the line */
        }
        return;
    }

    switch (s_state) {
    case RX_HDR1:
        if (b == FRAME_HDR1) {
            s_state = RX_SEQ0;
        } else {
            reply("ERR FRAME\n");
            s_state = RX_TEXT;
        }
        break;
    case RX_SEQ0:
        s_seq = (uint16_t)((uint16_t)b << 8);
        s_state = RX_SEQ1;
        break;
    case RX_SEQ1:
        s_seq |= b;
        s_state = RX_TYPE;
        break;
    case RX_TYPE:
        s_type = b;
        s_payload_len = frame_payload_len(b);
        if (s_payload_len == 0u) {
            reply("ERR FRAME\n");
            s_state = RX_TEXT;
        } else {
            s_payload_idx = 0u;
            s_state = RX_PAYLOAD;
        }
        break;
    case RX_PAYLOAD:
        s_payload[s_payload_idx++] = b;
        if (s_payload_idx >= s_payload_len) {
            s_crc_idx = 0u;
            s_state = RX_CRC0;
        }
        break;
    case RX_CRC0:
        s_crc_buf[0] = b;
        s_state = RX_CRC1;
        break;
    case RX_CRC1:
        s_crc_buf[1] = b;
        {
            uint8_t wire[5u + FRAME_MAX_PAYLOAD + 2u];
            uint16_t n = 0u;

            wire[n++] = FRAME_HDR0;
            wire[n++] = FRAME_HDR1;
            wire[n++] = (uint8_t)(s_seq >> 8);
            wire[n++] = (uint8_t)(s_seq & 0xFFu);
            wire[n++] = s_type;
            for (uint8_t i = 0u; i < s_payload_len; i++) {
                wire[n++] = s_payload[i];
            }
            uint16_t crc = crc16_ccitt(wire, n);
            if (crc == (uint16_t)(((uint16_t)s_crc_buf[0] << 8) | s_crc_buf[1])) {
                frame_t f;

                f.type = s_type;
                f.len = s_payload_len;
                (void)memcpy(f.payload, s_payload, s_payload_len);
                handle_host_frame(&f);
            } else {
                reply("ERR CRC\n");
            }
        }
        s_state = RX_TEXT;
        break;
    default:
        s_state = RX_TEXT;
        break;
    }
}

void diag_task_fn(void *arg)
{
    (void)arg;
    uint8_t b;

    for (;;) {
        while (uart_rx_get(&b) != 0) {
            feed_byte(b);
        }
        (void)xSemaphoreTake(uart_rx_sem(), portMAX_DELAY);
    }
}
