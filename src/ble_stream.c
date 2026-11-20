/*
 * BLE data pipe - implementation.
 */

#include "ble_stream.h"

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>

/* ------------------------------------------------------------------ UUIDs */

#define BT_UUID_IMU_SVC_VAL \
	BT_UUID_128_ENCODE(0x6e400001, 0xb5a3, 0xf393, 0xe0a9, 0xe50e24dcca9e)
#define BT_UUID_IMU_RX_VAL \
	BT_UUID_128_ENCODE(0x6e400002, 0xb5a3, 0xf393, 0xe0a9, 0xe50e24dcca9e)
#define BT_UUID_IMU_TX_VAL \
	BT_UUID_128_ENCODE(0x6e400003, 0xb5a3, 0xf393, 0xe0a9, 0xe50e24dcca9e)

static struct bt_uuid_128 uuid_svc = BT_UUID_INIT_128(BT_UUID_IMU_SVC_VAL);
static struct bt_uuid_128 uuid_rx  = BT_UUID_INIT_128(BT_UUID_IMU_RX_VAL);
static struct bt_uuid_128 uuid_tx  = BT_UUID_INIT_128(BT_UUID_IMU_TX_VAL);

/* ------------------------------------------------------------- ring buffer */

#define RING_SAMPLES 2048

/* how long to wait for a second sample before sending a lone one */
#define BATCH_WAIT_US 8000

static struct imu_sample ring[RING_SAMPLES];
static uint32_t ring_head;   /* advanced by the sampler thread */
static uint32_t ring_tail;   /* advanced by the BLE sender thread */
static uint32_t drop_count;
static K_MUTEX_DEFINE(ring_lock);

/* ---------------------------------------------------------- command queue */

#define CMD_MAX 48
#define CMD_DEPTH 4

K_MSGQ_DEFINE(cmd_q, CMD_MAX, CMD_DEPTH, 4);

/* -------------------------------------------------------------- BT state */

static struct bt_conn *current_conn;
static bool subscribed;
static volatile bool streaming;

static K_SEM_DEFINE(adv_restart_sem, 0, 1);

/* TX value attribute is attrs[4]:
 *   [0] primary service, [1] RX declaration, [2] RX value,
 *   [3] TX declaration, [4] TX value, [5] CCC
 */
#define TX_ATTR_INDEX 4

static void ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	ARG_UNUSED(attr);
	subscribed = (value == BT_GATT_CCC_NOTIFY);

	/* Stream as soon as somebody is listening. Recording is a host-side
	 * decision; the device just keeps the pipe full so the panel can show
	 * live data before the user presses record. */
	streaming = subscribed;

	printk("# BLE %s\n", subscribed ? "subscribed, streaming" : "unsubscribed, idle");
}

static ssize_t on_rx_write(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			   const void *buf, uint16_t len, uint16_t offset,
			   uint8_t flags)
{
	ARG_UNUSED(conn);
	ARG_UNUSED(attr);
	ARG_UNUSED(offset);
	ARG_UNUSED(flags);

	if (len == 0 || len >= CMD_MAX) {
		return len;
	}

	char cmd[CMD_MAX];

	memcpy(cmd, buf, len);
	cmd[len] = '\0';

	/* strip trailing CR/LF so the main-loop parser sees a bare command */
	while (len > 0 && (cmd[len - 1] == '\n' || cmd[len - 1] == '\r')) {
		cmd[--len] = '\0';
	}
	if (len == 0) {
		return len + 1;
	}

	if (k_msgq_put(&cmd_q, cmd, K_NO_WAIT) != 0) {
		printk("# WARN: command queue full, dropped '%s'\n", cmd);
	}

	return len;
}

BT_GATT_SERVICE_DEFINE(imu_svc,
	BT_GATT_PRIMARY_SERVICE(&uuid_svc),
	BT_GATT_CHARACTERISTIC(&uuid_rx.uuid,
			       BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
			       BT_GATT_PERM_WRITE, NULL, on_rx_write, NULL),
	BT_GATT_CHARACTERISTIC(&uuid_tx.uuid, BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_NONE, NULL, NULL, NULL),
	BT_GATT_CCC(ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

static const struct bt_data ad[] = {
	BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
	BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BT_DEVICE_NAME,
		sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};

static const struct bt_data sd[] = {
	BT_DATA_BYTES(BT_DATA_UUID128_ALL, BT_UUID_IMU_SVC_VAL),
};

/* BT_LE_ADV_CONN was removed in Zephyr 4.x; spell the parameters out. */
static const struct bt_le_adv_param adv_param = BT_LE_ADV_PARAM_INIT(
	BT_LE_ADV_OPT_CONN,
	BT_GAP_ADV_FAST_INT_MIN_2,
	BT_GAP_ADV_FAST_INT_MAX_2,
	NULL);

static void start_advertising(void)
{
	int err = bt_le_adv_start(&adv_param, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));

	if (err) {
		printk("# ERR: advertising failed (%d)\n", err);
	} else {
		printk("# advertising as \"%s\"\n", CONFIG_BT_DEVICE_NAME);
	}
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err) {
		printk("# ERR: connection failed 0x%02x\n", err);
		return;
	}

	current_conn = bt_conn_ref(conn);
	printk("# BLE connected\n");
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	ARG_UNUSED(conn);
	printk("# BLE disconnected (0x%02x)\n", reason);

	subscribed = false;
	streaming = false;
	if (current_conn) {
		bt_conn_unref(current_conn);
		current_conn = NULL;
	}
	k_sem_give(&adv_restart_sem);
}

static void recycled(void)
{
	k_sem_give(&adv_restart_sem);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
	.recycled = recycled,
};

/* ---------------------------------------------------------------- public */

bool ble_stream_connected(void) { return current_conn != NULL; }
bool ble_stream_subscribed(void) { return subscribed; }
bool ble_stream_streaming(void) { return streaming; }
void ble_stream_set_streaming(bool on) { streaming = on; }

uint32_t ble_stream_dropped(void)
{
	return drop_count;
}

uint16_t ble_stream_ring_used(void)
{
	uint32_t used;

	k_mutex_lock(&ring_lock, K_FOREVER);
	used = ring_head - ring_tail;
	k_mutex_unlock(&ring_lock);

	return (uint16_t)MIN(used, UINT16_MAX);
}

void ble_stream_push(const struct imu_sample *s)
{
	k_mutex_lock(&ring_lock, K_FOREVER);

	if ((ring_head - ring_tail) >= RING_SAMPLES) {
		/* Full: drop this sample rather than the oldest one, so the region
		 * the sender is about to transmit is never overwritten. */
		drop_count++;
		k_mutex_unlock(&ring_lock);
		return;
	}

	ring[ring_head % RING_SAMPLES] = *s;
	ring_head++;
	k_mutex_unlock(&ring_lock);
}

bool ble_stream_pop_command(char *out, size_t len)
{
	if (len < CMD_MAX) {
		return false;
	}
	return k_msgq_get(&cmd_q, out, K_NO_WAIT) == 0;
}

/* --------------------------------------------------------- sender thread */

static void ble_sender(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	static uint8_t buf[512];
	static uint16_t seq;
	static uint32_t last_send_us;

	while (1) {
		if (k_sem_take(&adv_restart_sem, K_NO_WAIT) == 0) {
			k_msleep(200);
			start_advertising();
		}

		if (!current_conn || !subscribed || !streaming) {
			k_sleep(K_MSEC(20));
			continue;
		}

		uint16_t mtu = bt_gatt_get_mtu(current_conn);
		size_t payload = (mtu > 3) ? (mtu - 3) : 0;

		if (payload <= BLE_FRAME_HDR + BLE_SAMPLE_SIZE) {
			k_sleep(K_MSEC(20));
			continue;
		}

		size_t max_samples = (payload - BLE_FRAME_HDR) / BLE_SAMPLE_SIZE;
		uint32_t avail;
		size_t take;

		k_mutex_lock(&ring_lock, K_FOREVER);
		avail = ring_head - ring_tail;
		take = MIN(avail, max_samples);

		if (take == 0) {
			k_mutex_unlock(&ring_lock);
			k_sleep(K_MSEC(2));
			continue;
		}

		uint16_t dropped_now = (uint16_t)MIN(drop_count, UINT16_MAX);

		/* Batch instead of firing one notification per sample. A connection
		 * event can only carry a couple of packets, so at 200 Hz and above
		 * one-sample packets cannot keep up. Wait for either a second sample
		 * or a few milliseconds, whichever comes first. */
		uint32_t now_us = k_cyc_to_us_floor32(k_cycle_get_32());

		if (take < 2 && (now_us - last_send_us) < BATCH_WAIT_US) {
			k_mutex_unlock(&ring_lock);
			k_sleep(K_MSEC(1));
			continue;
		}

		buf[0] = BLE_FRAME_SAMPLES;
		buf[1] = (uint8_t)(seq & 0xFF);
		buf[2] = (uint8_t)(seq >> 8);
		buf[3] = (uint8_t)(dropped_now & 0xFF);
		buf[4] = (uint8_t)(dropped_now >> 8);
		buf[5] = (uint8_t)take;

		size_t off = BLE_FRAME_HDR;

		for (size_t i = 0; i < take; i++) {
			const struct imu_sample *s = &ring[(ring_tail + i) % RING_SAMPLES];

			buf[off + 0] = (uint8_t)(s->t_us);
			buf[off + 1] = (uint8_t)(s->t_us >> 8);
			buf[off + 2] = (uint8_t)(s->t_us >> 16);
			buf[off + 3] = (uint8_t)(s->t_us >> 24);
			for (int k = 0; k < 3; k++) {
				buf[off + 4 + 2 * k] = (uint8_t)(s->a[k]);
				buf[off + 5 + 2 * k] = (uint8_t)((uint16_t)s->a[k] >> 8);
			}
			for (int k = 0; k < 3; k++) {
				buf[off + 10 + 2 * k] = (uint8_t)(s->g[k]);
				buf[off + 11 + 2 * k] = (uint8_t)((uint16_t)s->g[k] >> 8);
			}
			off += BLE_SAMPLE_SIZE;
		}

		k_mutex_unlock(&ring_lock);

		int err = bt_gatt_notify(NULL, &imu_svc.attrs[TX_ATTR_INDEX], buf, off);

		if (err == 0) {
			k_mutex_lock(&ring_lock, K_FOREVER);
			ring_tail += take;
			k_mutex_unlock(&ring_lock);
			seq++;
			last_send_us = k_cyc_to_us_floor32(k_cycle_get_32());
		} else {
			/* Controller buffers full - back off briefly and retry the same
			 * samples, they are still at the tail. */
			k_sleep(K_MSEC(3));
		}
	}
}

K_THREAD_DEFINE(ble_sender_tid, 2048, ble_sender, NULL, NULL, NULL, 5, 0, 0);

int ble_stream_start(void)
{
	int err = bt_enable(NULL);

	if (err) {
		printk("# ERR: bt_enable failed (%d)\n", err);
		return err;
	}
	printk("# BLE stack ready\n");
	start_advertising();
	return 0;
}
