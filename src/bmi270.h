/*
 * BMI270 6-axis IMU driver - Zephyr I2C backend.
 *
 * The register sequences, the 8 KB configuration blob and the ODR table are the
 * same as the ESP32 version of this firmware; only the I2C calls changed.
 */

#pragma once

#include <stdint.h>
#include <zephyr/device.h>
#include <zephyr/drivers/i2c.h>

#define BMI270_ADDR_DEFAULT 0x68

struct bmi270_cfg {
	const struct device *i2c;
	uint16_t addr;
};

/* Chip id check, soft reset and the 8 KB config upload. */
int bmi270_init(const struct bmi270_cfg *cfg);

/* Pick the lowest ODR >= hz and program ACC_CONF / GYR_CONF. */
int bmi270_set_rate(const struct bmi270_cfg *cfg, uint16_t hz);

/* Read accel xyz + gyro xyz, raw 16-bit LSB. */
int bmi270_read_sample(const struct bmi270_cfg *cfg, int16_t out[6]);

/* Raw register access, for the status / scan commands. */
int bmi270_read_reg(const struct bmi270_cfg *cfg, uint8_t reg, uint8_t *val);
int bmi270_write_reg(const struct bmi270_cfg *cfg, uint8_t reg, uint8_t val);

/* Probe every address on the bus and print what answers. */
void bmi270_scan(const struct bmi270_cfg *cfg);

/* The ODR actually programmed for a requested rate. */
uint16_t bmi270_effective_odr(uint16_t hz);
