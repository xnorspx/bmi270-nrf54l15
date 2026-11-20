/*
 * BLE data pipe for the BMI270 collector.
 *
 * Uses the Nordic UART Service UUIDs so generic BLE tools (nRF Connect, bleak)
 * can discover it, but the payload is a compact binary framing rather than text:
 *
 *   frame:  [type u8][seq u16le][dropped u16le][n u8][sample...]
 *   sample: [t_us u32le][ax i16][ay i16][az i16][gx i16][gy i16][gz i16]
 *
 * Raw LSB values are sent, not mg/mdps: the host owns the unit conversion, so
 * nothing is lost or rounded on the device. t_us is the device uptime, which
 * gives every sample a timestamp that is immune to host scheduling jitter.
 *
 * Commands arrive as newline-terminated ASCII on the RX characteristic and are
 * queued for the main loop, so I2C work never happens in the Bluetooth context.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BLE_FRAME_SAMPLES 0x01
#define BLE_FRAME_STATUS  0x02

#define BLE_SAMPLE_SIZE   16
#define BLE_FRAME_HDR     6
#define BLE_CMD_MAX       48

struct imu_sample {
	uint32_t t_us;
	int16_t  a[3];
	int16_t  g[3];
};

/* Bring up the stack, register the service and start advertising. */
int ble_stream_start(void);

bool ble_stream_connected(void);
bool ble_stream_subscribed(void);

/* Queue one sample. Drops the oldest sample when the ring is full and counts it,
 * so the host can see exactly how many samples were lost. */
void ble_stream_push(const struct imu_sample *s);

/* Gate the data path (START / STOP from the host). */
void ble_stream_set_streaming(bool on);
bool ble_stream_streaming(void);

uint32_t ble_stream_dropped(void);
uint16_t ble_stream_ring_used(void);

/* Pop a pending command line, or NULL when the queue is empty. */
bool ble_stream_pop_command(char *out, size_t len);
