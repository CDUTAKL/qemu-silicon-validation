/*
 * posix_main.c - FreeRTOS POSIX-port integration test driver.
 *
 * Runs the real firmware application (app_tasks_create: sensor/comm/diag/
 * watchdog tasks + heartbeat timers) on the host kernel and exercises it
 * over the UART shim exactly like the QEMU harness does over -serial:
 *
 *   - streaming DATA/HEARTBEAT frames: CRC, wire layout, seq continuity
 *   - STAT? / FAULT OV|OC|DISCONN|CONN|HANG / RESUME / RESET text commands
 *   - binary ACKREQ host frame -> ACK frame answer
 *   - bad CRC / bad header / unknown type rejection paths in DiagTask
 *
 * Prints PASS/FAIL statistics; main() returns non-zero on any failure.
 */
#include <stdio.h>
#include <string.h>

#include "FreeRTOS.h"
#include "task.h"

#include "app.h"
#include "frame.h"
#include "shim_uart.h"
#include "stream_parser.h"
#include "uart.h"

extern uint32_t shim_board_reset_count(void);

static unsigned g_checks;
static unsigned g_failures;

#define CHECK(cond)                                                           \
    do {                                                                      \
        g_checks++;                                                           \
        if (!(cond)) {                                                        \
            g_failures++;                                                     \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);           \
        }                                                                     \
    } while (0)

/* ---------------- TX capture: binary frames + ASCII replies ---------------- */

typedef struct {
    unsigned n_data;
    unsigned n_alarm;
    unsigned n_hb;
    unsigned n_ack;
    uint8_t last_hb_bitmap;
    uint8_t last_alarm_id;
    uint8_t last_alarm_level;
    uint16_t last_alarm_value;
    unsigned ack_ok; /* ACK frames with status == 0 */
    float data_volt_ring[4]; /* ring of the most recent DATA voltages */
    unsigned data_volt_ring_n;
} obs_t;

static obs_t g_obs;
static parser_t g_parser;

/* Recent ASCII text from TX (replies), searched per test step. */
static char g_text[8192];
static size_t g_text_len;

static void obs_cb(const frame_t *f, uint16_t seq, void *ctx)
{
    (void)seq;
    (void)ctx;

    switch (f->type) {
    case FRAME_TYPE_DATA: {
        uint32_t uv = ((uint32_t)f->payload[0] << 24) |
                      ((uint32_t)f->payload[1] << 16) |
                      ((uint32_t)f->payload[2] << 8) | f->payload[3];
        float v;

        memcpy(&v, &uv, sizeof(v));
        g_obs.data_volt_ring[g_obs.data_volt_ring_n % 4u] = v;
        g_obs.data_volt_ring_n++;
        g_obs.n_data++;
        break;
    }
    case FRAME_TYPE_ALARM:
        g_obs.n_alarm++;
        g_obs.last_alarm_id = f->payload[0];
        g_obs.last_alarm_level = f->payload[1];
        g_obs.last_alarm_value =
            (uint16_t)(((uint16_t)f->payload[2] << 8) | f->payload[3]);
        break;
    case FRAME_TYPE_HEARTBEAT:
        g_obs.n_hb++;
        g_obs.last_hb_bitmap = f->payload[0];
        break;
    case FRAME_TYPE_ACK:
        g_obs.n_ack++;
        if (f->len == 2u && f->payload[0] == 0u && f->payload[1] == 0u) {
            g_obs.ack_ok++;
        }
        break;
    default:
        break;
    }
}

/* Drain the UART shim TX buffer into the frame parser and the text log. */
static void drain_tx(void)
{
    uint8_t buf[256];
    size_t n;

    while ((n = shim_uart_tx_read(buf, sizeof(buf))) > 0u) {
        parser_feed(&g_parser, buf, n);
        for (size_t i = 0u; i < n; i++) {
            uint8_t b = buf[i];
            if ((b >= 0x20u && b < 0x7Fu) || b == '\n') {
                if (g_text_len < sizeof(g_text) - 1u) {
                    g_text[g_text_len++] = (char)b;
                    g_text[g_text_len] = '\0';
                }
            }
        }
    }
}

static size_t text_mark(void)
{
    drain_tx();
    return g_text_len;
}

static int text_has_since(size_t mark, const char *needle)
{
    drain_tx();
    if ((size_t)(g_text_len) < mark) {
        return 0;
    }
    g_text[g_text_len] = '\0';
    return strstr(g_text + mark, needle) != NULL;
}

/* Wait up to timeout_ms for cond() to become true, draining TX while
 * waiting. Polls every 10 ms. */
static int wait_for(int timeout_ms, int (*cond)(void), void (*reset)(void))
{
    if (reset != NULL) {
        reset();
    }
    for (int t = 0; t < timeout_ms; t += 10) {
        drain_tx();
        if (cond()) {
            return 1;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return 0;
}

/* ---- condition closures (file-scope state, single-threaded test) ---- */

static size_t s_mark;
static const char *s_needle;

static int cond_text(void)
{
    return text_has_since(s_mark, s_needle);
}

static int wait_text(int timeout_ms, size_t mark, const char *needle)
{
    s_mark = mark;
    s_needle = needle;
    return wait_for(timeout_ms, cond_text, NULL);
}

static unsigned *g_counter;
static unsigned g_target;

static int cond_count(void)
{
    return *g_counter >= g_target;
}

static int wait_count(int timeout_ms, unsigned *counter, unsigned target)
{
    g_counter = counter;
    g_target = target;
    return wait_for(timeout_ms, cond_count, NULL);
}

/* ---- host frame helpers ---- */

static void host_send_frame(uint8_t type, uint16_t seq, const uint8_t *payload,
                            uint8_t len, int corrupt_crc)
{
    frame_t f;
    uint8_t wire[FRAME_WIRE_SIZE_DATA];

    memset(&f, 0, sizeof(f));
    f.type = type;
    f.len = len;
    if (payload != NULL) {
        memcpy(f.payload, payload, len);
    }
    uint16_t n = frame_serialize(wire, &f, seq);
    if (corrupt_crc != 0) {
        wire[n - 1u] ^= 0xFFu;
    }
    (void)shim_uart_host_send(wire, n);
}

/* ---------------- test scenario ---------------- */

static void scenario_streaming(void)
{
    /* Free-run the app: sensor data every 100 ms, heartbeat every 1 s. */
    drain_tx();
    unsigned d0 = g_obs.n_data;
    unsigned h0 = g_obs.n_hb;

    CHECK(wait_count(2500, &g_obs.n_data, d0 + 3u));
    CHECK(wait_count(2500, &g_obs.n_hb, h0 + 1u));
    CHECK(g_parser.crc_errors == 0u);
    CHECK(g_parser.seq_gaps == 0u);
    CHECK(g_obs.last_hb_bitmap == 0x0Fu); /* all four tasks running */
}

static void scenario_stat(void)
{
    size_t m = text_mark();

    (void)shim_uart_host_send_str("STAT?\n");
    CHECK(wait_text(1000, m, "OK STAT tasks=4"));
    CHECK(wait_text(200, m, " qdepth="));
}

static void scenario_ackreq(void)
{
    static const uint8_t ping[] = {0x00u, 0x01u}; /* FRAME_ACKREQ_PING */
    unsigned a0 = g_obs.ack_ok;
    size_t m;
    unsigned before;

    /* Valid ACKREQ ping -> ACK frame with status 0. */
    host_send_frame(FRAME_TYPE_ACK_REQ, 42u, ping, sizeof(ping), 0);
    CHECK(wait_count(1000, &g_obs.ack_ok, a0 + 1u));

    /* Bad CRC -> rejected with an error line, no ACK. */
    m = text_mark();
    before = g_obs.ack_ok;
    host_send_frame(FRAME_TYPE_ACK_REQ, 43u, ping, sizeof(ping), 1);
    CHECK(wait_text(1000, m, "ERR CRC\n"));
    drain_tx();
    CHECK(g_obs.ack_ok == before);

    /* 0xAA followed by a wrong second byte -> ERR FRAME, then a valid
     * frame on the same stream must still be accepted (resync). */
    {
        static const uint8_t bad_hdr[] = {0xAAu, 0x00u};

        m = text_mark();
        (void)shim_uart_host_send(bad_hdr, sizeof(bad_hdr));
        host_send_frame(FRAME_TYPE_ACK_REQ, 44u, ping, sizeof(ping), 0);
        CHECK(wait_text(1000, m, "ERR FRAME\n"));
        CHECK(wait_count(1000, &g_obs.ack_ok, before + 1u));
    }

    /* Unknown frame type -> ERR FRAME. */
    {
        static const uint8_t bogus_pl[] = {0x01u, 0x02u};
        uint8_t wire[16];
        uint16_t n;
        frame_t f;

        /* Serialize a well-formed frame with an unknown type by hand:
         * frame_serialize takes any type byte verbatim. */
        memset(&f, 0, sizeof(f));
        f.type = 0x77u;
        f.len = sizeof(bogus_pl);
        memcpy(f.payload, bogus_pl, sizeof(bogus_pl));
        m = text_mark();
        n = frame_serialize(wire, &f, 45u);
        (void)shim_uart_host_send(wire, n);
        CHECK(wait_text(1000, m, "ERR FRAME\n"));
    }
}

static void scenario_fault_inject(void)
{
    size_t m;
    unsigned a0;
    unsigned d0;

    /* The previous scenario leaves rejected binary-frame bytes sitting in
     * DiagTask's text line buffer; flush the line so the next text command
     * parses cleanly (an empty line is silently ignored by design). */
    (void)shim_uart_host_send_str("\n");
    vTaskDelay(pdMS_TO_TICKS(100));

    /* FAULT OV 15.25 -> alarm (id=1, level=2, value=15) + next DATA frame
     * carries voltage 15.25. */
    m = text_mark();
    a0 = g_obs.n_alarm;
    d0 = g_obs.n_data;
    (void)shim_uart_host_send_str("FAULT OV 15.25\n");
    CHECK(wait_text(1000, m, "OK FAULT OV\n"));
    CHECK(wait_count(1000, &g_obs.n_alarm, a0 + 1u));
    drain_tx();
    CHECK(g_obs.last_alarm_id == 1u);
    CHECK(g_obs.last_alarm_level == 2u);
    CHECK(g_obs.last_alarm_value == 15u); /* (uint16_t)(15.25 + 0.5) */
    {
        int found = 0;

        /* The override applies to one sample; it may race a sample that
         * was already in flight, so accept it within the next 4 DATA
         * frames (kept in the voltage ring). */
        CHECK(wait_count(1500, &g_obs.n_data, d0 + 4u));
        drain_tx();
        for (unsigned j = 0u; j < 4u && j < g_obs.data_volt_ring_n; j++) {
            unsigned idx = (g_obs.data_volt_ring_n - 1u - j) % 4u;
            float v = g_obs.data_volt_ring[idx];
            if (v > 15.24f && v < 15.26f) {
                found = 1;
            }
        }
        CHECK(found);
    }

    /* FAULT OC 3.5 -> alarm id=2, value 4 (3.5 + 0.5 truncates to 4). */
    m = text_mark();
    a0 = g_obs.n_alarm;
    (void)shim_uart_host_send_str("FAULT OC 3.5\n");
    CHECK(wait_text(1000, m, "OK FAULT OC\n"));
    CHECK(wait_count(1000, &g_obs.n_alarm, a0 + 1u));
    drain_tx();
    CHECK(g_obs.last_alarm_id == 2u);
    CHECK(g_obs.last_alarm_value == 4u);

    /* Malformed value / bad usage / unknown subcommand. */
    m = text_mark();
    (void)shim_uart_host_send_str("FAULT OV abc\n");
    CHECK(wait_text(1000, m, "ERR BAD VALUE\n"));

    m = text_mark();
    (void)shim_uart_host_send_str("FAULT OC xyz\n");
    CHECK(wait_text(1000, m, "ERR BAD VALUE\n"));

    m = text_mark();
    (void)shim_uart_host_send_str("FAULT BOGUS\n");
    CHECK(wait_text(1000, m, "ERR USAGE\n"));

    m = text_mark();
    (void)shim_uart_host_send_str("FAULT OV\n");
    CHECK(wait_text(1000, m, "ERR USAGE\n"));
}

static void scenario_stream_pause(void)
{
    size_t m;
    unsigned d0;

    m = text_mark();
    (void)shim_uart_host_send_str("FAULT DISCONN\n");
    CHECK(wait_text(1000, m, "OK FAULT DISCONN\n"));

    /* No DATA frames while paused; heartbeat continues. */
    drain_tx();
    d0 = g_obs.n_data;
    {
        unsigned h0 = g_obs.n_hb;

        vTaskDelay(pdMS_TO_TICKS(1200));
        drain_tx();
        CHECK(g_obs.n_data == d0);
        CHECK(g_obs.n_hb >= h0 + 1u);
    }

    m = text_mark();
    (void)shim_uart_host_send_str("FAULT CONN\n");
    CHECK(wait_text(1000, m, "OK FAULT CONN\n"));
    CHECK(wait_count(1500, &g_obs.n_data, d0 + 1u));
}

static void scenario_hang_resume(void)
{
    size_t m;

    m = text_mark();
    (void)shim_uart_host_send_str("FAULT HANG sensor\n");
    CHECK(wait_text(1000, m, "OK FAULT HANG\n"));

    m = text_mark();
    (void)shim_uart_host_send_str("STAT?\n");
    CHECK(wait_text(1000, m, "OK STAT tasks=3"));

    /* Heartbeat keeps flowing and reports sensor bit cleared. */
    CHECK(wait_count(2500, &g_obs.n_hb, g_obs.n_hb + 1u));
    drain_tx();
    CHECK((g_obs.last_hb_bitmap & 0x01u) == 0u);

    m = text_mark();
    (void)shim_uart_host_send_str("RESUME sensor\n");
    CHECK(wait_text(1000, m, "OK RESUME\n"));

    m = text_mark();
    (void)shim_uart_host_send_str("STAT?\n");
    CHECK(wait_text(1000, m, "OK STAT tasks=4"));

    /* Unknown task names. */
    m = text_mark();
    (void)shim_uart_host_send_str("FAULT HANG bogus\n");
    CHECK(wait_text(1000, m, "ERR BAD TASK\n"));

    m = text_mark();
    (void)shim_uart_host_send_str("RESUME bogus\n");
    CHECK(wait_text(1000, m, "ERR BAD TASK\n"));

    m = text_mark();
    (void)shim_uart_host_send_str("RESUME\n");
    CHECK(wait_text(1000, m, "ERR UNKNOWN CMD\n"));
}

/* Heartbeat-loss recovery in app.c: hang the watchdog task so hb_count
 * stops; the 1 s hb_check_cb timer then calls system_reset() (shimmed). */
static void scenario_watchdog_hang(void)
{
    size_t m;
    uint32_t r0;

    m = text_mark();
    r0 = shim_board_reset_count();
    (void)shim_uart_host_send_str("FAULT HANG watchdog\n");
    CHECK(wait_text(1000, m, "OK FAULT HANG\n"));

    /* Within ~2 s the checker must notice the stalled heartbeat and issue
     * a reset request. */
    {
        int fired = 0;

        for (int t = 0; t < 2500; t += 50) {
            if (shim_board_reset_count() > r0) {
                fired = 1;
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(50));
        }
        CHECK(fired);
    }

    /* Recovery: resume the watchdog; heartbeat increments again. */
    m = text_mark();
    (void)shim_uart_host_send_str("RESUME watchdog\n");
    CHECK(wait_text(1000, m, "OK RESUME\n"));
    CHECK(wait_count(2500, &g_obs.n_hb, g_obs.n_hb + 1u));
}

static void scenario_text_edge_cases(void)
{
    size_t m;

    /* '\r' before '\n' is ignored; the command still parses. */
    m = text_mark();
    (void)shim_uart_host_send_str("STAT?\r\n");
    CHECK(wait_text(1000, m, "OK STAT tasks=4"));

    /* Unknown command. */
    m = text_mark();
    (void)shim_uart_host_send_str("GIBBERISH\n");
    CHECK(wait_text(1000, m, "ERR UNKNOWN CMD\n"));

    /* Empty line: silently ignored (no reply, no crash). */
    m = text_mark();
    (void)shim_uart_host_send_str("\n");
    vTaskDelay(pdMS_TO_TICKS(200));
    CHECK(!text_has_since(m, "OK"));
    CHECK(!text_has_since(m, "ERR"));

    /* Over-long line (> LINE_MAX-1) is dropped; the next command works. */
    m = text_mark();
    (void)shim_uart_host_send_str(
        "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX\n");
    vTaskDelay(pdMS_TO_TICKS(200));
    (void)shim_uart_host_send_str("STAT?\n");
    CHECK(wait_text(1000, m, "OK STAT tasks=4"));
}

static void scenario_reset(void)
{
    size_t m;
    uint32_t r0 = shim_board_reset_count();

    m = text_mark();
    (void)shim_uart_host_send_str("RESET\n");
    CHECK(wait_text(1000, m, "OK RESET\n"));
    CHECK(shim_board_reset_count() == r0 + 1u);
}

static void test_task_fn(void *arg)
{
    (void)arg;

    printf("posix integration: starting scenario\n");

    scenario_streaming();
    scenario_stat();
    scenario_ackreq();
    scenario_fault_inject();
    scenario_stream_pause();
    scenario_hang_resume();
    scenario_watchdog_hang();
    scenario_text_edge_cases();
    scenario_reset();

    drain_tx();
    CHECK(g_parser.crc_errors == 0u);
    CHECK(g_parser.seq_gaps == 0u);

    printf("posix integration: %u checks, %u failures -> %s\n", g_checks,
           g_failures, g_failures == 0u ? "PASS" : "FAIL");
    printf("wire stats: frames_ok=%u crc_errors=%u resyncs=%u seq_gaps=%u "
           "(data=%u alarm=%u hb=%u ack=%u)\n",
           g_parser.frames_ok, g_parser.crc_errors, g_parser.resyncs,
           g_parser.seq_gaps, g_obs.n_data, g_obs.n_alarm, g_obs.n_hb,
           g_obs.n_ack);

    if (g_failures != 0u && getenv("DUMP_TEXT") != NULL) {
        printf("---- captured ASCII ----\n%s\n---- end ----\n", g_text);
    }

    fflush(stdout);
    vPortEndScheduler();

    /* vPortEndScheduler returns here on the POSIX port; park the task. */
    for (;;) {
        vTaskDelay(portMAX_DELAY);
    }
}

int main(void)
{
    parser_init(&g_parser, obs_cb, NULL);

    uart_init();
    app_tasks_create();

    BaseType_t ok = xTaskCreate(test_task_fn, "test", 4096, NULL,
                                configMAX_PRIORITIES - 2, NULL);
    if (ok != pdPASS) {
        fprintf(stderr, "failed to create test task\n");
        return 2;
    }

    vTaskStartScheduler();

    /* Reached after vPortEndScheduler(). */
    printf("scheduler stopped\n");
    return g_failures == 0u ? 0 : 1;
}
