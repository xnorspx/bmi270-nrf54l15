/*
 * BMI270 IMU reader for Seeed XIAO nRF54L15 (Zephyr / NCS).
 *
 * Wiring:
 *   BMI270 SDA -> D4  (P1.10, I2C22)
 *   BMI270 SCL -> D5  (P1.11, I2C22)
 *   BMI270 INT -> D1  (P1.05)
 *   BMI270 VDD -> 3V3, GND -> GND, SDO -> GND (address 0x68)
 *
 * Console comes out of the on-board SAMD11 USB bridge, so the same USB-C cable
 * that flashes the board also shows this output.
 *
 * Commands (newline terminated):
 *   S,<hz>  set poll rate 1..1600      ?  status
 *   I       registers                  C  I2C scan
 *   H       toggle CSV header          Z  pause / resume
 *   R       re-init sensor
 *
 * Output: us,ax_mg,ay_mg,az_mg,gx_mdps,gy_mdps,gz_mdps
 */

#include <errno.h>
#include <stdlib.h>
#include <strings.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/printk.h>

#include "bmi270.h"
#include "ble_stream.h"

#define DEFAULT_POLL_HZ 100

/* ------------------------------------------------------------- hardware */

static const struct bmi270_cfg imu = {
	.i2c  = DEVICE_DT_GET(DT_NODELABEL(i2c22)),
	.addr = BMI270_ADDR_DEFAULT,
};

/* D1 = P1.05. Built by hand rather than through GPIO_DT_SPEC_GET: a bare
 * devicetree node without a compatible gets no gpio macros generated, so the
 * phandle-array would not be typed as a GPIO spec.
 *
 * INT must live on GPIO0 (D11, D12) or GPIO1 (D0..D5): GPIO2 (D6..D10,
 * D13..D15) has no gpiote-instance in this SoC, so the Zephyr GPIO driver
 * cannot configure an edge interrupt on it. */
static const struct gpio_dt_spec imu_int = {
	.port     = DEVICE_DT_GET(DT_NODELABEL(gpio1)),
	.pin      = 5,
	.dt_flags = GPIO_ACTIVE_HIGH,
};

static const struct device *const console = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

/* ---------------------------------------------------------------- state */

K_SEM_DEFINE(int_sem, 0, 1);

static struct gpio_callback int_cb_data;
static volatile uint32_t int_count;

static uint16_t poll_hz    = DEFAULT_POLL_HZ;
static uint32_t last_read_us;
static uint32_t last_sample_cnt;
static uint32_t last_int_ms;

static bool console_stream = false;   /* debug: also print CSV to the console */
static bool header     = true;
static bool sensor_ok;
static bool int_alive  = true;
static bool int_warned;

/* ------------------------------------------------------------- output */

static inline uint32_t us_now(void)
{
	return k_cyc_to_us_floor32(k_cycle_get_32());
}

static void emit_sample(const int16_t d[6])
{
	/* 8 g   -> 4096 LSB/g   -> mg   = raw * 1000 / 4096  = raw * 125 / 512
	 * 500 dps -> 65.536 LSB/dps -> mdps = raw * 15625 / 1024
	 * Integer only: printk has no float support by default. */
	long ax = (long)d[0] * 125 / 512;
	long ay = (long)d[1] * 125 / 512;
	long az = (long)d[2] * 125 / 512;
	long gx = (long)d[3] * 15625 / 1024;
	long gy = (long)d[4] * 15625 / 1024;
	long gz = (long)d[5] * 15625 / 1024;

	printk("%u,%ld,%ld,%ld,%ld,%ld,%ld\n",
	       (unsigned int)us_now(), ax, ay, az, gx, gy, gz);
}

static void print_header(void)
{
	printk("us,ax_mg,ay_mg,az_mg,gx_mdps,gy_mdps,gz_mdps\n");
}

/* ----------------------------------------------------------- interrupt */

static void imu_int_handler(const struct device *port,
			    struct gpio_callback *cb, gpio_port_pins_t pins)
{
	ARG_UNUSED(port);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);

	int_count = int_count + 1;
	k_sem_give(&int_sem);
}

/* ---------------------------------------------------------- commands */

static void print_status(void)
{
	printk("# sensor: %s\n", sensor_ok ? "initialised" : "NOT initialised - send R");
	printk("# poll=%u Hz  odr=%u Hz  accel=+-8g  gyro=+-500dps\n",
	       poll_hz, bmi270_effective_odr(poll_hz));
	printk("# ble: %s%s  streaming=%s  dropped=%u  ring=%u\n",
	       ble_stream_connected() ? "connected" : "advertising",
	       ble_stream_subscribed() ? "/subscribed" : "",
	       ble_stream_streaming() ? "on" : "off",
	       (unsigned int)ble_stream_dropped(), ble_stream_ring_used());
	printk("# int=%s (%u edges)  console_csv=%s\n",
	       sensor_ok ? (int_alive ? "working" : "timer fallback") : "n/a",
	       (unsigned int)int_count, console_stream ? "on" : "off");
	printk("# commands: START | STOP | S,<hz> | ? | I | C | R | L\n");
}

static void print_registers(void)
{
	uint8_t id = 0, st = 0, acc = 0, gyr = 0, pwr = 0;

	bmi270_read_reg(&imu, 0x00, &id);
	bmi270_read_reg(&imu, 0x21, &st);
	bmi270_read_reg(&imu, 0x40, &acc);
	bmi270_read_reg(&imu, 0x42, &gyr);
	bmi270_read_reg(&imu, 0x7D, &pwr);
	printk("# chip=0x%02X internal=0x%02X acc_conf=0x%02X gyr_conf=0x%02X pwr=0x%02X\n",
	       id, st, acc, gyr, pwr);
}

static void do_init(void)
{
	sensor_ok = (bmi270_init(&imu) == 0);
	if (!sensor_ok) {
		printk("# init FAILED - fix wiring, then send R\n");
		return;
	}

	bmi270_set_rate(&imu, poll_hz);

	/* forget any INT history from before the re-init */
	int_count      = 0;
	last_sample_cnt = 0;
	last_read_us   = us_now();
	last_int_ms    = k_uptime_get();
	int_alive      = true;
	int_warned     = false;
	k_sem_reset(&int_sem);

	printk("# re-init done, poll=%u Hz odr=%u Hz\n",
	       poll_hz, bmi270_effective_odr(poll_hz));
	if (header) {
		print_header();
	}
}

static void run_command(char *cmd)
{
	while (*cmd == ' ') {
		cmd++;
	}
	if (*cmd == '\0') {
		return;
	}

	/* multi-character commands first: both start with 'S', which is also the
	 * single-character "set rate" command */
	if (strncasecmp(cmd, "START", 5) == 0) {
		ble_stream_set_streaming(true);
		printk("# START - streaming samples over BLE\n");
		return;
	}
	if (strncasecmp(cmd, "STOP", 4) == 0) {
		ble_stream_set_streaming(false);
		printk("# STOP - streaming paused, ring=%u\n", ble_stream_ring_used());
		return;
	}

	switch (*cmd) {
	case '?':
	case 'P':
		print_status();
		break;

	case 'I':
		print_registers();
		break;

	case 'C':
		bmi270_scan(&imu);
		break;

	case 'H':
		header = !header;
		printk("# header %s\n", header ? "on" : "off");
		if (header) {
			print_header();
		}
		break;

	case 'L':
		console_stream = !console_stream;
		printk("# console csv %s\n", console_stream ? "on" : "off");
		break;

	case 'R':
		do_init();
		break;

	case 'S': {
		char *p = cmd + 1;

		while (*p == ' ' || *p == ',') {
			p++;
		}
		if (*p == '\0') {
			printk("# usage: S,<hz>\n");
			break;
		}

		long hz = strtol(p, NULL, 10);

		if (hz < 1 || hz > 1600) {
			printk("# ERR: rate must be 1..1600\n");
			break;
		}
		if (!sensor_ok) {
			printk("# ERR: sensor not initialised - send R first\n");
			break;
		}
		if (bmi270_set_rate(&imu, (uint16_t)hz) < 0) {
			printk("# ERR: I2C write failed\n");
			break;
		}
		poll_hz = (uint16_t)hz;
		printk("# poll=%u Hz, odr=%u Hz\n",
		       poll_hz, bmi270_effective_odr(poll_hz));
		break;
	}

	default:
		printk("# unknown command, try ?\n");
		break;
	}
}

static void handle_console(void)
{
	static char buf[32];
	static size_t len;
	uint8_t c;

	while (uart_poll_in(console, &c) == 0) {
		if (c == '\r') {
			continue;
		}
		if (c == '\n') {
			buf[len] = '\0';
			len = 0;
			run_command(buf);
		} else if (len < sizeof(buf) - 1) {
			buf[len++] = (char)c;
		}
	}
}

/* --------------------------------------------------------------- main */

int main(void)
{
	printk("\n# BMI270 reader - XIAO nRF54L15\n");

	if (!device_is_ready(imu.i2c)) {
		printk("# ERR: i2c22 not ready\n");
		return 0;
	}
	if (!gpio_is_ready_dt(&imu_int)) {
		printk("# ERR: INT gpio not ready\n");
		return 0;
	}

	gpio_pin_configure_dt(&imu_int, GPIO_INPUT);
	gpio_init_callback(&int_cb_data, imu_int_handler, BIT(imu_int.pin));
	gpio_add_callback(imu_int.port, &int_cb_data);
	gpio_pin_interrupt_configure_dt(&imu_int, GPIO_INT_EDGE_TO_ACTIVE);

	do_init();
	(void)ble_stream_start();

	while (1) {
		char bcmd[BLE_CMD_MAX];

		while (ble_stream_pop_command(bcmd, sizeof(bcmd))) {
			run_command(bcmd);
		}

		handle_console();

		if (!sensor_ok) {
			k_msleep(50);
			continue;
		}

		/* INT watchdog: no edge for 1 s means the pin is not wired, so fall
		 * back to rate-limiter polling instead of stalling forever. */
		if (int_alive && (k_uptime_get() - last_int_ms) > 1000) {
			int_alive  = false;
			if (!int_warned) {
				int_warned = true;
				printk("# WARN: no INT edge - polling on timer, check D0\n");
			}
		}

		uint32_t now    = us_now();
		uint32_t period = 1000000u / poll_hz;
		uint32_t since  = now - last_read_us;

		if (since < period) {
			/* sleep until the rate limiter expires; an INT edge wakes us early */
			(void)k_sem_take(&int_sem, K_USEC(period - since));
			continue;
		}

		bool fresh = int_alive ? (int_count != last_sample_cnt) : true;

		if (!fresh) {
			(void)k_sem_take(&int_sem, K_USEC(period));
			continue;
		}

		if (int_count != last_sample_cnt) {
			last_int_ms = k_uptime_get();
			if (!int_alive) {
				int_alive = true;
				printk("# INT recovered\n");
			}
		}
		last_sample_cnt = int_count;

		if (ble_stream_streaming() || console_stream) {
			int16_t d[6];

			if (bmi270_read_sample(&imu, d) == 0) {
				struct imu_sample s = {
					.t_us = us_now(),
					.a = { d[0], d[1], d[2] },
					.g = { d[3], d[4], d[5] },
				};

				ble_stream_push(&s);
				if (console_stream) {
					emit_sample(d);
				}
			}
		}

		last_read_us += period;
		if ((us_now() - last_read_us) >= period) {
			last_read_us = us_now();
		}
	}

	return 0;
}
