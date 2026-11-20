/*
 * BMI270 6-axis IMU driver - Zephyr I2C backend.
 */

#include "bmi270.h"

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "bmi270_config_file.h"

/* ------------------------------------------------------------- registers */

#define REG_CHIP_ID       0x00
#define REG_ACC_X_LSB     0x0C  /* 12 bytes: acc xyz then gyr xyz, LSB first */
#define REG_INTERNAL_ST   0x21
#define REG_ACC_CONF      0x40
#define REG_ACC_RANGE     0x41
#define REG_GYR_CONF      0x42
#define REG_GYR_RANGE     0x43
#define REG_INT1_IO_CTRL  0x53
#define REG_INT_MAP_DATA  0x58
#define REG_INIT_CTRL     0x59
#define REG_INIT_ADDR_0   0x5B
#define REG_INIT_DATA     0x5E
#define REG_PWR_CONF      0x7C
#define REG_PWR_CTRL      0x7D
#define REG_CMD           0x7E

#define CMD_SOFT_RESET    0xB6
#define CHIP_ID_BMI270    0x24

/* ACC_CONF base: bit7 acc_filter_perf | bits6:4 acc_bwp(=2)  */
#define ACC_CONF_BASE     0xA0
/* GYR_CONF base: bit7 gyr_filter_perf | bit6 gyr_noise_perf | bits5:4 bwp(=2).
 * Without bit6 the gyro runs in power-optimised (noisier) mode. */
#define GYR_CONF_BASE     0xE0

#define ACC_RANGE_CODE    0x02  /* +-8 g   */
#define GYR_RANGE_CODE    0x02  /* +-500 dps */

/* ------------------------------------------------------------------ ODR */

struct odr_step {
	uint16_t hz;
	uint8_t code;
};

static const struct odr_step odr_table[] = {
	{  25, 0x06 }, {  50, 0x07 }, { 100, 0x08 }, { 200, 0x09 },
	{ 400, 0x0A }, { 800, 0x0B }, { 1600, 0x0C },
};

#define ODR_TABLE_LEN (sizeof(odr_table) / sizeof(odr_table[0]))

static uint8_t odr_code_for(uint16_t hz)
{
	for (size_t i = 0; i < ODR_TABLE_LEN; i++) {
		if (odr_table[i].hz >= hz) {
			return odr_table[i].code;
		}
	}
	return odr_table[ODR_TABLE_LEN - 1].code;
}

static uint16_t odr_hz_of(uint8_t code)
{
	for (size_t i = 0; i < ODR_TABLE_LEN; i++) {
		if (odr_table[i].code == code) {
			return odr_table[i].hz;
		}
	}
	return 0;
}

uint16_t bmi270_effective_odr(uint16_t hz)
{
	if (hz < 1) {
		hz = 1;
	}
	return odr_hz_of(odr_code_for(hz));
}

/* ------------------------------------------------------------------- I2C */

int bmi270_write_reg(const struct bmi270_cfg *cfg, uint8_t reg, uint8_t val)
{
	return i2c_reg_write_byte(cfg->i2c, cfg->addr, reg, val);
}

int bmi270_read_reg(const struct bmi270_cfg *cfg, uint8_t reg, uint8_t *val)
{
	return i2c_reg_read_byte(cfg->i2c, cfg->addr, reg, val);
}

/* nRF TWIM moves data with EasyDMA, and EasyDMA cannot read a buffer that lives
 * in flash. BMI270_CONFIG_FILE is `static const` (flash), so a direct burst write
 * from it fails with -ENOSPC. Copy each chunk into a RAM buffer first. */
static int write_block(const struct bmi270_cfg *cfg, uint8_t reg,
		       const uint8_t *data, size_t len)
{
	uint8_t buf[1 + 32];

	if (len > sizeof(buf) - 1) {
		return -EINVAL;
	}
	buf[0] = reg;
	memcpy(&buf[1], data, len);

	return i2c_write(cfg->i2c, buf, len + 1, cfg->addr);
}

static int read_block(const struct bmi270_cfg *cfg, uint8_t reg,
		      uint8_t *data, size_t len)
{
	return i2c_burst_read(cfg->i2c, cfg->addr, reg, data, len);
}

/* ------------------------------------------------------------------ init */

int bmi270_init(const struct bmi270_cfg *cfg)
{
	uint8_t id = 0;
	int ret;

	ret = bmi270_read_reg(cfg, REG_CHIP_ID, &id);
	if (ret < 0) {
		printk("# ERR: I2C read failed (%d) - check SDA/SCL/3V3/GND\n", ret);
		return ret;
	}
	if (id != CHIP_ID_BMI270) {
		printk("# ERR: chip id 0x%02X, expected 0x%02X (BMI270)\n",
		       id, CHIP_ID_BMI270);
		return -ENODEV;
	}
	printk("# chip id 0x%02X OK\n", id);

	/* 1. soft reset, then wait for the command register to clear */
	ret = bmi270_write_reg(cfg, REG_CMD, CMD_SOFT_RESET);
	if (ret < 0) {
		printk("# ERR: soft reset write failed (%d)\n", ret);
		return ret;
	}
	k_busy_wait(2000);

	bool reset_done = false;

	for (int i = 0; i < 20; i++) {
		uint8_t st = 0xFF;

		if (bmi270_read_reg(cfg, REG_CMD, &st) == 0 && st == 0x00) {
			reset_done = true;
			break;
		}
		k_msleep(1);
	}
	if (!reset_done) {
		printk("# ERR: soft reset did not complete\n");
		return -EIO;
	}

	/* 2. advanced power save off, config load off.
	 * Datasheet: wait >= 450 us after clearing adv_power_save before touching
	 * another register, otherwise the upload below fails intermittently. */
	bmi270_write_reg(cfg, REG_PWR_CONF, 0x00);
	k_busy_wait(500);
	bmi270_write_reg(cfg, REG_INIT_CTRL, 0x00);

	/* 3. upload the 8 KB configuration blob, 32 bytes at a time */
	for (uint16_t i = 0; i < BMI270_CONFIG_FILE_SIZE; i += 32) {
		uint8_t addr[2];

		addr[0] = (uint8_t)((i / 2) & 0x0F); /* bits 0..3 of word address */
		addr[1] = (uint8_t)((i / 2) >> 4);   /* bits 4..11                 */

		ret = write_block(cfg, REG_INIT_ADDR_0, addr, sizeof(addr));
		if (ret < 0) {
			printk("# ERR: init address write failed at %u (%d)\n", i, ret);
			return ret;
		}
		ret = write_block(cfg, REG_INIT_DATA, &BMI270_CONFIG_FILE[i], 32);
		if (ret < 0) {
			printk("# ERR: config upload failed at %u (%d)\n", i, ret);
			return ret;
		}
	}

	/* 4. enable load, then poll for the ASIC to finish initialising */
	ret = bmi270_write_reg(cfg, REG_INIT_CTRL, 0x01);
	if (ret < 0) {
		printk("# ERR: config load enable failed (%d)\n", ret);
		return ret;
	}

	k_msleep(20);
	uint8_t internal_status = 0;
	bool load_ok = false;

	for (int i = 0; i < 18; i++) {
		if (bmi270_read_reg(cfg, REG_INTERNAL_ST, &internal_status) == 0 &&
		    (internal_status & 0x0F) == 0x01) {
			load_ok = true;
			break;
		}
		k_msleep(10);
	}
	if (!load_ok) {
		printk("# ERR: config load failed, internal status 0x%02X\n",
		       internal_status);
		return -EIO;
	}
	printk("# config loaded, internal status ok\n");

	/* 5. full scale */
	bmi270_write_reg(cfg, REG_ACC_RANGE, ACC_RANGE_CODE);
	bmi270_write_reg(cfg, REG_GYR_RANGE, GYR_RANGE_CODE);

	/* 6. data-ready interrupt on INT1: push-pull, active high, output on */
	bmi270_write_reg(cfg, REG_INT1_IO_CTRL, 0x08 | 0x02);
	bmi270_write_reg(cfg, REG_INT_MAP_DATA, 0x04);

	/* 7. enable accel + gyro */
	ret = bmi270_write_reg(cfg, REG_PWR_CTRL, 0x04 | 0x02);
	if (ret < 0) {
		printk("# ERR: power ctrl write failed (%d)\n", ret);
		return ret;
	}

	k_msleep(50);
	return 0;
}

int bmi270_set_rate(const struct bmi270_cfg *cfg, uint16_t hz)
{
	if (hz < 1) {
		hz = 1;
	}
	if (hz > 1600) {
		hz = 1600;
	}

	uint8_t code = odr_code_for(hz);
	int ret = bmi270_write_reg(cfg, REG_ACC_CONF, ACC_CONF_BASE | code);

	if (ret < 0) {
		return ret;
	}
	return bmi270_write_reg(cfg, REG_GYR_CONF, GYR_CONF_BASE | code);
}

int bmi270_read_sample(const struct bmi270_cfg *cfg, int16_t out[6])
{
	uint8_t b[12];
	int ret = read_block(cfg, REG_ACC_X_LSB, b, sizeof(b));

	if (ret < 0) {
		return ret;
	}
	for (int i = 0; i < 6; i++) {
		out[i] = (int16_t)((uint16_t)b[2 * i] | ((uint16_t)b[2 * i + 1] << 8));
	}
	return 0;
}

void bmi270_scan(const struct bmi270_cfg *cfg)
{
	int found = 0;

	printk("# scanning I2C ...\n");
	for (uint8_t a = 0x08; a < 0x78; a++) {
		uint8_t dummy;

		if (i2c_reg_read_byte(cfg->i2c, a, REG_CHIP_ID, &dummy) == 0) {
			printk("#   device at 0x%02X%s\n", a,
			       (a == cfg->addr) ? "  <- expected BMI270" : "");
			found++;
		}
	}
	if (found == 0) {
		printk("#   nothing responded - check pull-ups, pins and sensor power\n");
	} else {
		printk("# %d device(s) found\n", found);
	}
}
