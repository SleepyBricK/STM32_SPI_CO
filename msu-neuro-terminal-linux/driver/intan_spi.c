/*
 * Intan RHS2116 SPI Driver for Linux
 *
 * Полностью повторяет логику оригинального Python-проекта:
 * - GPIO 226 (PH2): питание Intan, включается до SPI
 * - 3 отдельных SPI-транзакции (как spidev xfer2)
 * - READ 255 для Chip ID, формат [0xC0, reg, 0x00, 0x00]
 *
 * IOCTL: бенчмарк транзакций. SPI фиксирован на 25 МГц (лимит Intan).
 *
 * SPDX-License-Identifier: GPL-2.0
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/spi/spi.h>
#include <linux/gpio/consumer.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/delay.h>
#include <linux/ktime.h>
#include <linux/ioctl.h>
#include <linux/slab.h>
#include <linux/vmalloc.h>
#include <linux/kthread.h>
#include <linux/wait.h>
#include <linux/spinlock.h>
#include <linux/jiffies.h>

#define INTAN_CHIP_ID_REG	255
#define INTAN_RHS2116_CHIP_ID	32
#define INTAN_POWER_DELAY_MS	100	/* как time.sleep(0.1) в Python */
#define INTAN_MAX_FREQ_HZ	25000000	/* Intan RHS2116 лимит */

#define INTAN_MINOR		MISC_DYNAMIC_MINOR
#define INTAN_DEV_NAME		"intan"

/* IOCTL */
#define INTAN_IOC_MAGIC		'I'
#define INTAN_IOC_READ_REG	_IOWR(INTAN_IOC_MAGIC, 4, struct intan_read_reg_arg)
#define INTAN_IOC_WRITE_REG	_IOW(INTAN_IOC_MAGIC, 5, struct intan_write_reg_arg)
#define INTAN_IOC_CONVERT	_IOWR(INTAN_IOC_MAGIC, 6, struct intan_convert_arg)
#define INTAN_IOC_MEASURE_IMPEDANCE _IOWR(INTAN_IOC_MAGIC, 7, struct intan_impedance_arg)
#define INTAN_IOC_SINGLE_STEP	_IOW(INTAN_IOC_MAGIC, 8, struct intan_single_step_arg)
#define INTAN_IOC_RUN_PATTERN	_IOWR(INTAN_IOC_MAGIC, 9, struct intan_run_pattern_arg)
#define INTAN_IOC_STREAM_CONFIG	_IOW(INTAN_IOC_MAGIC, 10, struct intan_stream_config)
#define INTAN_IOC_GET_RING_LAYOUT _IOR(INTAN_IOC_MAGIC, 11, struct intan_ring_layout)
#define INTAN_IOC_START_STREAM	_IO(INTAN_IOC_MAGIC, 12)
#define INTAN_IOC_STOP_STREAM	_IO(INTAN_IOC_MAGIC, 13)
#define INTAN_IOC_GET_STREAM_STATUS _IOR(INTAN_IOC_MAGIC, 14, struct intan_stream_status)
#define INTAN_IOC_STREAM_READ_PACKET _IOWR(INTAN_IOC_MAGIC, 15, struct intan_stream_read_packet_arg)
#define INTAN_IOC_BENCHMARK	_IOWR(INTAN_IOC_MAGIC, 3, struct intan_benchmark_result)

#define INTAN_IMPEDANCE_MAX_AVERAGES	1000
#define INTAN_IMPEDANCE_MAX_SAMPLES	2048
#define INTAN_PATTERN_MAX_OPS	16384
#define INTAN_STREAM_MAX_CHANNELS	16
#define INTAN_STREAM_RING_SLOTS		1024
#define INTAN_STREAM_PACKET_MAX_BYTES	1200
#define INTAN_STREAM_MAGIC		0x334E5449U
#define INTAN_STREAM_VERSION		3
#define INTAN_STREAM_MAX_RATE_HZ	40000
#define INTAN_STREAM_PACKET_TARGET_LATENCY_NS	1000000ULL
#define INTAN_STREAM_PACKET_TARGET_LATENCY_SEQ_NS	5000000ULL
#define INTAN_STREAM_MAX_TRANSFERS	600
/*
 * Zcheck fast-path loop rate observed on Orange Pi H618 is closer to
 * 11-12 kHz than to the old 30 kHz assumption. This estimate is used only
 * for selecting samples_per_period; effective_frequency_millihz is still
 * measured from wall-clock time and returned to userspace.
 */
#define INTAN_IMPEDANCE_SAMPLE_RATE_EST_HZ	11500
#define INTAN_IMPEDANCE_MIN_PERIODS	8
#define INTAN_IMPEDANCE_MIN_SPP		6

struct intan_impedance_point {
	s64 sin_accum;
	s64 cos_accum;
};

struct intan_impedance_arg {
	u8 channel;
	u8 scale_bits;	/* 0=0.1pF, 1=1pF, 3=10pF */
	u16 num_averages;	/* input: requested averages, output: actual averages */
	u16 num_samples;	/* input: minimum samples, output: actual samples per average */
	u16 frequency_hz;	/* requested Zcheck frequency */
	u16 samples_per_period;	/* output */
	u32 effective_frequency_millihz;	/* output */
	u16 _reserved;		/* padding for 8-byte alignment */
	struct intan_impedance_point points[INTAN_IMPEDANCE_MAX_AVERAGES];
};

struct intan_read_reg_arg {
	u8 reg;
	u8 _pad;
	u16 value;
};

struct intan_write_reg_arg {
	u8 reg;
	u8 u_flag;
	u8 m_flag;
	u8 _pad;
	u16 value;
};

/* CONVERT: flags: bit0=h_flag (HPF reset), bit1=d_flag (DC read) */
struct intan_convert_arg {
	u8 channel;	/* 0-15 или 63 (auto) */
	u8 flags;
	u16 value;	/* результат ADC */
};

struct intan_single_step_arg {
	u8 tx[4];
};

enum intan_pattern_opcode {
	INTAN_PATTERN_OP_WRITE_REG = 1,
	INTAN_PATTERN_OP_READ_REG = 2,
	INTAN_PATTERN_OP_CLEAR_ADC = 3,
	INTAN_PATTERN_OP_DELAY = 4,
	INTAN_PATTERN_OP_CLEAR_COMPLIANCE = 5,
};

/*
 * Batch opcode ABI:
 * - WRITE: reg/value input, flags bit0=U bit1=M
 * - READ: reg input, value output
 * - DELAY: count SPI single-step iterations
 * - CLEAR/CLEAR_COMPLIANCE: other fields ignored
 */
struct intan_pattern_op {
	u8 opcode;
	u8 reg;
	u8 flags;
	u8 reserved;
	u16 value;
	u16 count;
};

struct intan_run_pattern_arg {
	u32 num_ops;
	u32 completed_ops;	/* output: количество успешно выполненных операций */
	u64 ops_ptr;		/* userspace pointer to struct intan_pattern_op[num_ops] */
};

struct intan_stream_config {
	u32 sample_rate_hz;	/* per-channel sample rate */
	u16 channel_count;
	u16 flags;
	u8 channels[INTAN_STREAM_MAX_CHANNELS];
	u32 ring_slot_count;
	u32 reserved;
};

struct intan_ring_layout {
	u32 slot_count;
	u32 slot_bytes;
	u32 packet_max_bytes;
	u32 flags;
};

struct intan_stream_status {
	u32 running;
	u32 configured;
	u32 last_errno;
	u32 reserved;
	u64 sequence;
	u64 samples_produced;
	u64 packets_produced;
	u64 ring_overruns;
	u64 spi_errors;
};

struct intan_stream_read_packet_arg {
	u32 timeout_ms;
	u32 buffer_size;
	u32 packet_size;
	u32 sequence;
	u64 data_ptr;
};

struct intan_stream_packet_header {
	__le32 magic;
	__le16 version;
	__le16 header_size;
	__le32 sequence;
	__le64 timestamp_ns;
	__le16 channel_count;
	__le16 sample_count;
	__le16 flags;
	__le16 reserved;
	u8 channels[INTAN_STREAM_MAX_CHANNELS];
} __packed;

struct intan_benchmark_result {
	unsigned int count;		/* число транзакций (1 транзакция = 1 READ = 3 SPI transfer) */
	unsigned long elapsed_ns;	/* время в наносекундах */
	unsigned int freq_hz;		/* текущая частота */
};

struct intan_stream_slot {
	u32 len;
	u32 sequence;
	u8 data[INTAN_STREAM_PACKET_MAX_BYTES];
};

struct intan_priv {
	struct spi_device *spi;
	struct gpio_desc *power_gpio;
	struct mutex lock;
	struct mutex stream_ctl_lock;
	spinlock_t stream_ring_lock;
	wait_queue_head_t stream_waitq;
	struct task_struct *stream_thread;
	struct intan_stream_config stream_cfg;
	struct intan_stream_status stream_status;
	struct intan_stream_slot *stream_ring;
	struct spi_transfer *stream_transfers;
	u8 (*stream_tx)[4];
	u8 (*stream_rx)[4];
	u32 stream_write_idx;
	u32 stream_read_idx;
	u32 stream_ring_count;
	bool stream_seq_primed;
	bool stream_configured;
	bool stream_running;
	struct miscdevice miscdev;
};

static void intan_wait_until_ns(u64 deadline_ns);

/*
 * READ — точно как в stimulate_channel0.py:
 *   cmd = [0xC0, reg_addr & 0xFF, 0x00, 0x00]
 *   resp1 = spi.transfer(cmd)
 *   resp2 = spi.transfer([0x00, 0x00, 0x00, 0x00])
 *   resp3 = spi.transfer([0x00, 0x00, 0x00, 0x00])
 *   reg_value = (resp3[2] << 8) | resp3[3]
 *
 * Три ОТДЕЛЬНЫЕ транзакции (каждый transfer = отдельный CS-импульс).
 */
static int intan_read_reg(struct spi_device *spi, u8 reg_addr, u16 *value)
{
	u8 tx[4];
	u8 rx[4];
	struct spi_transfer t = {
		.tx_buf = tx,
		.rx_buf = rx,
		.len = 4,
	};
	int ret;

	/* Фаза 1 */
	tx[0] = 0xC0;
	tx[1] = reg_addr & 0xFF;
	tx[2] = 0x00;
	tx[3] = 0x00;
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;

	/* Фаза 2 */
	tx[0] = 0x00;
	tx[1] = 0x00;
	tx[2] = 0x00;
	tx[3] = 0x00;
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;

	/* Фаза 3 */
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;

	*value = (rx[2] << 8) | rx[3];
	return 0;
}

/*
 * WRITE — как в stimulate_channel0.py write_intan_register
 */
static int intan_write_reg(struct spi_device *spi, u8 reg_addr, u16 value,
			   u8 u_flag, u8 m_flag)
{
	u8 tx[4];
	u8 rx[4];
	struct spi_transfer t = {
		.tx_buf = tx,
		.rx_buf = rx,
		.len = 4,
	};
	int ret;

	tx[0] = 0x80 | (u_flag << 5) | (m_flag << 4);
	tx[1] = reg_addr & 0xFF;
	tx[2] = (value >> 8) & 0xFF;
	tx[3] = value & 0xFF;
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;

	tx[0] = tx[1] = tx[2] = tx[3] = 0x00;
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;
	ret = spi_sync_transfer(spi, &t, 1);
	return ret;
}

/* CONVERT: channel 0-15 или 63 (auto), flags: bit0=h, bit1=d */
static int intan_convert(struct spi_device *spi, u8 channel, u8 flags, u16 *value)
{
	u8 tx[4];
	u8 rx[4];
	u8 d_flag = (flags >> 1) & 1;
	u8 h_flag = flags & 1;
	struct spi_transfer t = { .tx_buf = tx, .rx_buf = rx, .len = 4 };
	int ret;

	tx[0] = (d_flag << 3) | (h_flag << 2);
	tx[1] = channel & 0x3F;
	tx[2] = 0x00;
	tx[3] = 0x00;
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;
	tx[0] = tx[1] = tx[2] = tx[3] = 0x00;
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;
	*value = (rx[0] << 8) | rx[1];
	return 0;
}

/* Raw 4-byte command, 3 SPI transfers (для CLEAR и т.п.) */
static int intan_raw_cmd(struct spi_device *spi, const u8 *cmd4)
{
	u8 tx[4];
	u8 rx[4];
	struct spi_transfer t = { .tx_buf = tx, .rx_buf = rx, .len = 4 };
	int ret;

	tx[0] = cmd4[0];
	tx[1] = cmd4[1];
	tx[2] = cmd4[2];
	tx[3] = cmd4[3];
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;
	tx[0] = tx[1] = tx[2] = tx[3] = 0x00;
	ret = spi_sync_transfer(spi, &t, 1);
	if (ret)
		return ret;
	return spi_sync_transfer(spi, &t, 1);
}

static int intan_single_step(struct spi_device *spi, const u8 *cmd4)
{
	u8 tx[4];
	u8 rx[4];
	struct spi_transfer t = { .tx_buf = tx, .rx_buf = rx, .len = 4 };

	memcpy(tx, cmd4, 4);
	return spi_sync_transfer(spi, &t, 1);
}

static int intan_clear_adc(struct spi_device *spi)
{
	static const u8 clear_cmd[4] = { 0x6A, 0x00, 0x00, 0x00 };

	return intan_raw_cmd(spi, clear_cmd);
}

static int intan_clear_compliance_monitor(struct spi_device *spi)
{
	static const u8 clear_cmd[4] = { 0xD0, 0xFF, 0x00, 0x00 };

	return intan_raw_cmd(spi, clear_cmd);
}

static int intan_run_pattern_op(struct spi_device *spi, struct intan_pattern_op *op)
{
	static const u8 delay_cmd[4] = { 0xC0, 0xFF, 0x00, 0x00 };
	unsigned int i;
	u16 value;
	int ret;

	switch (op->opcode) {
	case INTAN_PATTERN_OP_WRITE_REG:
		return intan_write_reg(spi, op->reg, op->value,
				       op->flags & BIT(0),
				       !!(op->flags & BIT(1)));

	case INTAN_PATTERN_OP_READ_REG:
		ret = intan_read_reg(spi, op->reg, &value);
		if (ret)
			return ret;
		op->value = value;
		return 0;

	case INTAN_PATTERN_OP_CLEAR_ADC:
		return intan_clear_adc(spi);

	case INTAN_PATTERN_OP_DELAY:
		for (i = 0; i < op->count; i++) {
			int ret = intan_single_step(spi, delay_cmd);

			if (ret)
				return ret;
		}
		return 0;

	case INTAN_PATTERN_OP_CLEAR_COMPLIANCE:
		return intan_clear_compliance_monitor(spi);

	default:
		return -EINVAL;
	}
}

static int intan_run_pattern(struct spi_device *spi, struct intan_pattern_op *ops,
			     u32 num_ops, u32 *completed_ops)
{
	u32 idx;
	int ret;

	*completed_ops = 0;
	for (idx = 0; idx < num_ops; idx++) {
		ret = intan_run_pattern_op(spi, &ops[idx]);
		if (ret)
			return ret;
		*completed_ops = idx + 1;
	}

	return 0;
}

static bool intan_stream_channels_sequential(const struct intan_stream_config *cfg)
{
	unsigned int i;

	if (cfg->channel_count == 0)
		return false;
	for (i = 1; i < cfg->channel_count; i++) {
		if (cfg->channels[i] != cfg->channels[0] + i)
			return false;
	}

	return true;
}

static int intan_stream_validate_config(const struct intan_stream_config *cfg)
{
	unsigned int i;

	if (cfg->channel_count == 0 || cfg->channel_count > INTAN_STREAM_MAX_CHANNELS)
		return -EINVAL;
	if (cfg->sample_rate_hz == 0 || cfg->sample_rate_hz > INTAN_STREAM_MAX_RATE_HZ)
		return -EINVAL;
	for (i = 0; i < cfg->channel_count; i++) {
		if (cfg->channels[i] >= INTAN_STREAM_MAX_CHANNELS)
			return -EINVAL;
	}

	return 0;
}

static void intan_stream_reset_ring(struct intan_priv *priv)
{
	unsigned long flags;

	spin_lock_irqsave(&priv->stream_ring_lock, flags);
	priv->stream_write_idx = 0;
	priv->stream_read_idx = 0;
	priv->stream_ring_count = 0;
	spin_unlock_irqrestore(&priv->stream_ring_lock, flags);
}

static int intan_stream_prime_sequential_pipeline(struct intan_priv *priv)
{
	static const u8 conv_auto_cmd[4] = { 0x00, 0x3F, 0x00, 0x00 };
	int ret;

	if (!intan_stream_channels_sequential(&priv->stream_cfg) || priv->stream_cfg.channel_count <= 1)
		return 0;
	if (priv->stream_seq_primed)
		return 0;

	ret = intan_single_step(priv->spi, conv_auto_cmd);
	if (ret)
		return ret;
	ret = intan_single_step(priv->spi, conv_auto_cmd);
	if (ret)
		return ret;
	priv->stream_seq_primed = true;
	return 0;
}

static void intan_stream_enqueue_packet(struct intan_priv *priv, const u8 *packet,
					u32 packet_len, u32 sequence)
{
	unsigned long flags;
	struct intan_stream_slot *slot;

	spin_lock_irqsave(&priv->stream_ring_lock, flags);
	if (priv->stream_ring_count == INTAN_STREAM_RING_SLOTS) {
		priv->stream_read_idx = (priv->stream_read_idx + 1) % INTAN_STREAM_RING_SLOTS;
		priv->stream_ring_count--;
		priv->stream_status.ring_overruns++;
	}

	slot = &priv->stream_ring[priv->stream_write_idx];
	slot->len = packet_len;
	slot->sequence = sequence;
	memcpy(slot->data, packet, packet_len);
	priv->stream_write_idx = (priv->stream_write_idx + 1) % INTAN_STREAM_RING_SLOTS;
	priv->stream_ring_count++;
	spin_unlock_irqrestore(&priv->stream_ring_lock, flags);

	wake_up_interruptible(&priv->stream_waitq);
}

static u32 intan_stream_max_samples_per_packet(const struct intan_stream_config *cfg)
{
	u32 payload_bytes;
	u32 max_samples;

	payload_bytes = INTAN_STREAM_PACKET_MAX_BYTES - sizeof(struct intan_stream_packet_header);
	max_samples = payload_bytes / (cfg->channel_count * sizeof(u16));
	return max_t(u32, 1, max_samples);
}

static u32 intan_stream_target_samples_per_packet(const struct intan_stream_config *cfg)
{
	u64 target_samples;
	u32 max_samples;
	u64 target_latency_ns = INTAN_STREAM_PACKET_TARGET_LATENCY_NS;

	max_samples = intan_stream_max_samples_per_packet(cfg);
	if (intan_stream_channels_sequential(cfg) && cfg->channel_count > 1)
		target_latency_ns = INTAN_STREAM_PACKET_TARGET_LATENCY_SEQ_NS;
	target_samples = div_u64(((u64)cfg->sample_rate_hz * target_latency_ns) +
				 999999999ULL,
				 1000000000ULL);
	return clamp_t(u32, (u32)max_t(u64, 1, target_samples), 1, max_samples);
}

static u32 intan_stream_build_packet(const struct intan_stream_config *cfg, u32 sequence,
				     u64 timestamp_ns, const u16 *values,
				     u16 sample_count, u8 *packet)
{
	struct intan_stream_packet_header *hdr;
	__le16 *payload;
	unsigned int i, total_values;
	u16 flags = 0;

	memset(packet, 0, INTAN_STREAM_PACKET_MAX_BYTES);
	hdr = (struct intan_stream_packet_header *)packet;
	if (intan_stream_channels_sequential(cfg))
		flags |= BIT(0);
	if (sample_count > 1)
		flags |= BIT(1);
	hdr->magic = cpu_to_le32(INTAN_STREAM_MAGIC);
	hdr->version = cpu_to_le16(INTAN_STREAM_VERSION);
	hdr->header_size = cpu_to_le16(sizeof(*hdr));
	hdr->sequence = cpu_to_le32(sequence);
	hdr->timestamp_ns = cpu_to_le64(timestamp_ns);
	hdr->channel_count = cpu_to_le16(cfg->channel_count);
	hdr->sample_count = cpu_to_le16(sample_count);
	hdr->flags = cpu_to_le16(flags);
	memcpy(hdr->channels, cfg->channels, cfg->channel_count);

	payload = (__le16 *)(packet + sizeof(*hdr));
	total_values = sample_count * cfg->channel_count;
	for (i = 0; i < total_values; i++)
		payload[i] = cpu_to_le16(values[i]);

	return sizeof(*hdr) + (total_values * sizeof(__le16));
}

static int intan_stream_capture_batch(struct intan_priv *priv, u16 *values,
				      u16 sample_count)
{
	struct intan_stream_config *cfg = &priv->stream_cfg;
	struct spi_transfer *transfers = priv->stream_transfers;
	u8 (*tx)[4] = priv->stream_tx;
	u8 (*rx)[4] = priv->stream_rx;
	unsigned int transfer_count;
	unsigned int total_commands;
	unsigned int sample_idx;
	unsigned int channel_idx;
	unsigned int i;
	int ret;

	if (sample_count == 0)
		return -EINVAL;
	if (sample_count == 1 && cfg->channel_count == 1)
		return intan_convert(priv->spi, cfg->channels[0], 0, &values[0]);

	if (intan_stream_channels_sequential(cfg) && cfg->channel_count > 1) {
		total_commands = sample_count * cfg->channel_count;
		transfer_count = total_commands + 2;
		if (transfer_count > INTAN_STREAM_MAX_TRANSFERS)
			return -EINVAL;
		memset(transfers, 0, sizeof(*transfers) * transfer_count);
		memset(tx, 0, sizeof(*tx) * transfer_count);
		memset(rx, 0, sizeof(*rx) * transfer_count);

		for (i = 0; i < transfer_count; i++) {
			u8 channel = 63;

			if (i < total_commands) {
				channel = ((i % cfg->channel_count) == 0) ? cfg->channels[0] : 63;
			}
			tx[i][0] = 0x00;
			tx[i][1] = channel & 0x3F;
			tx[i][2] = 0x00;
			tx[i][3] = 0x00;
			transfers[i].tx_buf = tx[i];
			transfers[i].rx_buf = rx[i];
			transfers[i].len = 4;
			transfers[i].cs_change = (i + 1 < transfer_count);
		}

		ret = spi_sync_transfer(priv->spi, transfers, transfer_count);
		if (ret)
			return ret;

		for (sample_idx = 0; sample_idx < sample_count; sample_idx++) {
			for (channel_idx = 0; channel_idx < cfg->channel_count; channel_idx++) {
				unsigned int rx_idx = 2 + (sample_idx * cfg->channel_count) + channel_idx;

				values[(sample_idx * cfg->channel_count) + channel_idx] =
					(rx[rx_idx][0] << 8) | rx[rx_idx][1];
			}
		}

		return 0;
	}

	/*
	 * Intan CONVERT is pipelined: response for command N arrives on transfer N+2.
	 * For a batch of S frames with K channels we therefore acquire:
	 *   2 priming CONVERT(63) + (S * K) explicit CONVERT(channel) + 2 tail CONVERT(63)
	 * = (S * K) + 4 SPI transfers instead of S * 3 * K full intan_convert() calls.
	 */
	total_commands = sample_count * cfg->channel_count;
	transfer_count = total_commands + 4;
	if (transfer_count > INTAN_STREAM_MAX_TRANSFERS)
		return -EINVAL;
	memset(transfers, 0, sizeof(*transfers) * transfer_count);
	memset(tx, 0, sizeof(*tx) * transfer_count);
	memset(rx, 0, sizeof(*rx) * transfer_count);

	for (i = 0; i < transfer_count; i++) {
		u8 channel = 63;

		if (i >= 2 && i < total_commands + 2)
			channel = cfg->channels[(i - 2) % cfg->channel_count] & 0x3F;
		tx[i][0] = 0x00;
		tx[i][1] = channel;
		tx[i][2] = 0x00;
		tx[i][3] = 0x00;
		transfers[i].tx_buf = tx[i];
		transfers[i].rx_buf = rx[i];
		transfers[i].len = 4;
		transfers[i].cs_change = (i + 1 < transfer_count);
	}

	ret = spi_sync_transfer(priv->spi, transfers, transfer_count);
	if (ret)
		return ret;

	for (sample_idx = 0; sample_idx < sample_count; sample_idx++) {
		for (channel_idx = 0; channel_idx < cfg->channel_count; channel_idx++) {
			unsigned int rx_idx = 4 + (sample_idx * cfg->channel_count) + channel_idx;

			values[(sample_idx * cfg->channel_count) + channel_idx] =
				(rx[rx_idx][0] << 8) | rx[rx_idx][1];
		}
	}

	return 0;
}

static int intan_stream_thread_fn(void *data)
{
	struct intan_priv *priv = data;
	u8 *packet;
	u16 *batch_values;
	u32 target_batch_samples;
	u32 max_batch_samples;
	u32 sequence = 0;
	u16 batch_samples;
	u64 interval_ns;
	u64 next_deadline_ns;

	interval_ns = div_u64(1000000000ULL, priv->stream_cfg.sample_rate_hz);
	max_batch_samples = intan_stream_max_samples_per_packet(&priv->stream_cfg);
	target_batch_samples = intan_stream_target_samples_per_packet(&priv->stream_cfg);
	batch_samples = target_batch_samples;
	next_deadline_ns = ktime_get_ns();
	packet = kmalloc(INTAN_STREAM_PACKET_MAX_BYTES, GFP_KERNEL);
	batch_values = kmalloc_array(INTAN_STREAM_PACKET_MAX_BYTES / sizeof(u16),
				    sizeof(*batch_values),
				    GFP_KERNEL);
	if (!packet || !batch_values) {
		priv->stream_status.last_errno = ENOMEM;
		kfree(packet);
		kfree(batch_values);
		WRITE_ONCE(priv->stream_running, false);
		wake_up_interruptible(&priv->stream_waitq);
		return -ENOMEM;
	}

	while (!kthread_should_stop()) {
		int ret;
		u64 timestamp_ns;
		u32 packet_len;

		if (!READ_ONCE(priv->stream_running))
			break;

		intan_wait_until_ns(next_deadline_ns);
		timestamp_ns = ktime_get_real_ns();
		next_deadline_ns += (interval_ns * batch_samples);

		mutex_lock(&priv->lock);
		ret = intan_stream_capture_batch(priv, batch_values, batch_samples);
		mutex_unlock(&priv->lock);
		if (ret) {
			priv->stream_status.spi_errors++;
			priv->stream_status.last_errno = -ret;
			msleep(1);
			continue;
		}

		packet_len = intan_stream_build_packet(&priv->stream_cfg, sequence,
						      timestamp_ns,
						      batch_values,
						      batch_samples,
						      packet);
		intan_stream_enqueue_packet(priv, packet, packet_len, sequence);
		sequence++;
		priv->stream_status.sequence = sequence;
		priv->stream_status.samples_produced += batch_samples;
		priv->stream_status.packets_produced++;
	}

	kfree(packet);
	kfree(batch_values);
	WRITE_ONCE(priv->stream_running, false);
	wake_up_interruptible(&priv->stream_waitq);
	return 0;
}

static int intan_stream_start(struct intan_priv *priv)
{
	int ret;

	if (!priv->stream_configured)
		return -EINVAL;
	if (priv->stream_running)
		return 0;

	intan_stream_reset_ring(priv);
	priv->stream_seq_primed = false;
	memset(&priv->stream_status, 0, sizeof(priv->stream_status));
	priv->stream_status.configured = 1;
	mutex_lock(&priv->lock);
	ret = intan_stream_prime_sequential_pipeline(priv);
	mutex_unlock(&priv->lock);
	if (ret) {
		priv->stream_status.last_errno = -ret;
		return ret;
	}
	priv->stream_running = true;
	priv->stream_thread = kthread_run(intan_stream_thread_fn, priv, "intan_stream");
	if (IS_ERR(priv->stream_thread)) {
		int ret = PTR_ERR(priv->stream_thread);

		priv->stream_thread = NULL;
		priv->stream_running = false;
		priv->stream_status.last_errno = -ret;
		return ret;
	}
	priv->stream_status.running = 1;
	return 0;
}

static void intan_stream_stop(struct intan_priv *priv)
{
	struct task_struct *thread;

	thread = priv->stream_thread;
	priv->stream_thread = NULL;
	priv->stream_running = false;
	priv->stream_seq_primed = false;
	priv->stream_status.running = 0;
	wake_up_interruptible(&priv->stream_waitq);
	if (thread)
		kthread_stop(thread);
}

/* Синус 64 точки: 128 + 127*sin(2π*i/64), округлено */
static const u8 intan_sine64[64] = {
	128, 140, 152, 164, 176, 187, 198, 209, 218, 227, 235, 242, 248, 253, 255, 255,
	255, 255, 253, 248, 242, 235, 227, 218, 209, 198, 187, 176, 164, 152, 140, 128,
	116, 104, 92, 80, 69, 58, 47, 38, 29, 21, 14, 8, 3, 1, 1, 1,
	1, 1, 3, 8, 14, 21, 29, 38, 47, 58, 69, 80, 92, 104, 116, 128,
};

static void intan_wait_until_ns(u64 deadline_ns)
{
	for (;;) {
		u64 now_ns = ktime_get_ns();
		u64 remaining_ns;

		if (now_ns >= deadline_ns)
			break;
		remaining_ns = deadline_ns - now_ns;
		if (remaining_ns >= 100000ULL) {
			u64 remaining_us = div_u64(remaining_ns, 1000ULL);

			usleep_range((unsigned long)remaining_us,
				     (unsigned long)(remaining_us + 20ULL));
		} else if (remaining_ns > 5000ULL) {
			udelay((unsigned long)((remaining_ns - 2000ULL) / 1000ULL));
		} else {
			ndelay((unsigned long)remaining_ns);
		}
	}
}

static void intan_select_impedance_profile(u16 frequency_hz, u16 requested_samples,
					      u16 *samples_per_period, u16 *num_periods)
{
	u16 spp;
	u16 min_periods = INTAN_IMPEDANCE_MIN_PERIODS;

	switch (frequency_hz) {
	case 100:
		/*
		 * 100 Hz needs a very coarse pacing step on H618. Even 32 spp
		 * still does not hold timing reliably here, so use 8 spp with
		 * an explicit 1.25 ms cadence per sample in the paced path.
		 */
		spp = 8;
		min_periods = 5;
		break;
	case 300:
		spp = 32;
		min_periods = 5;
		break;
	case 1000:
		spp = 11;
		min_periods = 8;
		break;
	case 2000:
		spp = 5;
		min_periods = 10;
		break;
	default:
		spp = max_t(u16, INTAN_IMPEDANCE_MIN_SPP,
			    DIV_ROUND_CLOSEST(INTAN_IMPEDANCE_SAMPLE_RATE_EST_HZ,
					      frequency_hz));
		break;
	}

	spp = min_t(u16, spp, INTAN_IMPEDANCE_MAX_SAMPLES);
	*samples_per_period = spp;
	*num_periods = max_t(u16, min_periods,
			     DIV_ROUND_UP(requested_samples, spp));
}

static int intan_measure_impedance(struct spi_device *spi, struct intan_impedance_arg *arg)
{
	u8 channel = arg->channel & 0x0F;
	u8 scale_bits = arg->scale_bits;
	u16 requested_averages = arg->num_averages;
	u16 requested_samples = arg->num_samples;
	u16 frequency_hz = arg->frequency_hz;
	u16 num_averages;
	u16 num_samples;
	u16 samples_per_period;
	u16 num_periods;
	u16 reg2;
	bool paced_mode = false;
	bool slow_100hz_mode = false;
	s32 period_sum_s = 0;
	s32 period_sum_c = 0;
	unsigned int avg_idx;
	unsigned int sample_idx;
	unsigned int phase_idx;
	u64 elapsed_ns;
	u64 eff_freq_millihz;
	u64 target_step_ns = 0;
	u64 next_step_ns = 0;
	ktime_t t0, t1;
	int ret;

	if (requested_averages == 0 || requested_averages > INTAN_IMPEDANCE_MAX_AVERAGES)
		return -EINVAL;
	if (requested_samples == 0 || requested_samples > INTAN_IMPEDANCE_MAX_SAMPLES)
		return -EINVAL;
	if (frequency_hz == 0)
		return -EINVAL;
	if (scale_bits != 0 && scale_bits != 1 && scale_bits != 3)
		return -EINVAL;

	/*
	 * A few frequencies are calibrated explicitly because the measured loop
	 * rate is not linear enough across the full range to rely on a single
	 * estimate. For everything else we still fall back to the generic rule.
	 */
	intan_select_impedance_profile(frequency_hz, requested_samples,
					 &samples_per_period, &num_periods);
	num_averages = requested_averages;
	num_samples = min_t(u16, INTAN_IMPEDANCE_MAX_SAMPLES,
			    num_periods * samples_per_period);
	slow_100hz_mode = (frequency_hz == 100 && samples_per_period == 8);
	paced_mode = (frequency_hz <= 300 || frequency_hz == 1000 || frequency_hz == 2000);
	if (paced_mode && !slow_100hz_mode) {
		u64 denom = (u64)frequency_hz * (u64)samples_per_period;

		if (denom == 0)
			return -EINVAL;
		target_step_ns = div_u64(1000000000ULL + (denom / 2), denom);
	}
	for (phase_idx = 0; phase_idx < samples_per_period; phase_idx++) {
		unsigned int idx = (phase_idx * ARRAY_SIZE(intan_sine64)) /
				   samples_per_period;

		if (idx >= ARRAY_SIZE(intan_sine64))
			idx = ARRAY_SIZE(intan_sine64) - 1;
		period_sum_s += (s32)intan_sine64[idx] - 128;
		period_sum_c += (s32)intan_sine64[(idx + 16) & 63] - 128;
	}

	/* CLEAR */
	ret = intan_clear_adc(spi);
	if (ret)
		return ret;

	/* WRITE 2 0x0040 — включение Zcheck DAC */
	ret = intan_write_reg(spi, 2, 0x0040, 0, 0);
	if (ret)
		return ret;

	/* WRITE 3 0x0080 — нейтральное */
	ret = intan_write_reg(spi, 3, 0x0080, 0, 0);
	if (ret)
		return ret;

	reg2 = (channel << 8) | (1 << 6) | (1 << 0) | (scale_bits << 3);
	ret = intan_write_reg(spi, 2, reg2, 0, 0);
	if (ret)
		return ret;

	/*
	 * Даём Zcheck DAC и аналоговому тракту немного времени стабилизироваться.
	 * Это повторяет идею из рабочего Python-пути.
	 */
	msleep(20);

	/* Сбор batched averages: sine DAC + CONVERT с частотно-зависимым числом точек на период */
	t0 = ktime_get();
	if (paced_mode && !slow_100hz_mode)
		next_step_ns = ktime_get_ns();
	for (avg_idx = 0; avg_idx < num_averages; avg_idx++) {
		s64 sin_accum = 0;
		s64 cos_accum = 0;

		for (sample_idx = 0; sample_idx < num_samples; sample_idx++) {
			u8 dac_val;
			u16 adc_val;
			s32 centered;
			s32 sin_basis;
			s32 cos_basis;
			unsigned int phase_idx = sample_idx % samples_per_period;
			unsigned int idx = (phase_idx * ARRAY_SIZE(intan_sine64)) /
					   samples_per_period;

			if (idx >= ARRAY_SIZE(intan_sine64))
				idx = ARRAY_SIZE(intan_sine64) - 1;
			if (paced_mode && !slow_100hz_mode) {
				intan_wait_until_ns(next_step_ns);
				next_step_ns += target_step_ns;
			}
			dac_val = intan_sine64[idx];

			ret = intan_write_reg(spi, 3, dac_val & 0xFF, 0, 0);
			if (ret)
				goto err_disable;
			ret = intan_convert(spi, channel, 0, &adc_val);
			if (ret)
				goto err_disable;

			centered = (s32)adc_val - 32768;
			sin_basis = ((s32)intan_sine64[idx] - 128) * (s32)samples_per_period - period_sum_s;
			cos_basis = ((s32)intan_sine64[(idx + 16) & 63] - 128) * (s32)samples_per_period - period_sum_c;
			sin_accum += (s64)centered * sin_basis;
			cos_accum += (s64)centered * cos_basis;

			if (slow_100hz_mode && sample_idx + 1 < num_samples)
				usleep_range(980, 1080);
		}

		arg->points[avg_idx].sin_accum = sin_accum;
		arg->points[avg_idx].cos_accum = cos_accum;
	}
	t1 = ktime_get();
	elapsed_ns = ktime_to_ns(ktime_sub(t1, t0));
	if (elapsed_ns == 0)
		elapsed_ns = 1;
	/*
	 * elapsed_ns may exceed 2^32 on slow low-frequency runs. div_u64()
	 * truncates the divisor to 32 bits, which corrupts effective frequency
	 * once the batch lasts longer than ~4.29 s. Use div64_u64() here.
	 */
	eff_freq_millihz = div64_u64((u64)num_averages * (u64)num_samples * 1000000000000ULL,
				     elapsed_ns);
	eff_freq_millihz = div_u64(eff_freq_millihz, samples_per_period);
	arg->num_averages = num_averages;
	arg->num_samples = num_samples;
	arg->samples_per_period = samples_per_period;
	arg->effective_frequency_millihz = (u32)eff_freq_millihz;
	arg->_reserved = 0;

	/* Отключить Zcheck и вернуть DAC в нейтраль */
	ret = intan_write_reg(spi, 2, reg2 & 0xFFFE, 0, 0);
	if (ret)
		return ret;
	ret = intan_write_reg(spi, 3, 0x0080, 0, 0);
	if (ret)
		return ret;
	return 0;

err_disable:
	intan_write_reg(spi, 2, reg2 & 0xFFFE, 0, 0);
	intan_write_reg(spi, 3, 0x0080, 0, 0);
	return ret;
}

static ssize_t intan_read(struct file *filp, char __user *buf, size_t count,
			  loff_t *ppos)
{
	struct intan_priv *priv = filp->private_data;
	u16 chip_id;
	int ret;

	if (count < 2)
		return -EINVAL;

	mutex_lock(&priv->lock);
	ret = intan_read_reg(priv->spi, INTAN_CHIP_ID_REG, &chip_id);
	mutex_unlock(&priv->lock);

	if (ret)
		return ret;

	if (copy_to_user(buf, &chip_id, 2))
		return -EFAULT;

	return 2;
}

static ssize_t intan_write(struct file *filp, const char __user *buf,
			   size_t count, loff_t *ppos)
{
	struct intan_priv *priv = filp->private_data;
	u8 tx[4];
	u8 rx[4];
	struct spi_transfer t = {
		.tx_buf = tx,
		.rx_buf = rx,
		.len = 4,
	};
	int ret;

	if (count != 4)
		return -EINVAL;

	if (copy_from_user(tx, buf, 4))
		return -EFAULT;

	mutex_lock(&priv->lock);
	ret = spi_sync_transfer(priv->spi, &t, 1);
	if (!ret) {
		tx[0] = tx[1] = tx[2] = tx[3] = 0x00;
		ret = spi_sync_transfer(priv->spi, &t, 1);
	}
	if (!ret)
		ret = spi_sync_transfer(priv->spi, &t, 1);
	mutex_unlock(&priv->lock);

	return ret ? ret : 4;
}

static long intan_ioctl(struct file *filp, unsigned int cmd, unsigned long arg)
{
	struct intan_priv *priv = filp->private_data;
	struct intan_benchmark_result bench;
	struct intan_read_reg_arg read_arg;
	struct intan_write_reg_arg write_arg;
	struct intan_single_step_arg single_step_arg;
	struct intan_run_pattern_arg pattern_arg;
	struct intan_stream_config stream_cfg;
	struct intan_ring_layout ring_layout;
	struct intan_stream_read_packet_arg read_packet_arg;
	ktime_t t0, t1;
	u16 val;
	int ret;

	if (cmd == INTAN_IOC_MEASURE_IMPEDANCE) {
		struct intan_impedance_arg *imp_arg;

		imp_arg = kzalloc(sizeof(*imp_arg), GFP_KERNEL);
		if (!imp_arg)
			return -ENOMEM;
		if (copy_from_user(imp_arg, (void __user *)arg, sizeof(*imp_arg))) {
			kfree(imp_arg);
			return -EFAULT;
		}
		mutex_lock(&priv->lock);
		ret = intan_measure_impedance(priv->spi, imp_arg);
		mutex_unlock(&priv->lock);
		if (ret) {
			kfree(imp_arg);
			return ret;
		}
		if (copy_to_user((void __user *)arg, imp_arg, sizeof(*imp_arg))) {
			kfree(imp_arg);
			return -EFAULT;
		}
		kfree(imp_arg);
		return 0;
	}

	switch (cmd) {
	case INTAN_IOC_READ_REG:
		if (copy_from_user(&read_arg, (void __user *)arg, sizeof(read_arg)))
			return -EFAULT;
		mutex_lock(&priv->lock);
		ret = intan_read_reg(priv->spi, read_arg.reg, &read_arg.value);
		mutex_unlock(&priv->lock);
		if (ret)
			return ret;
		if (copy_to_user((void __user *)arg, &read_arg, sizeof(read_arg)))
			return -EFAULT;
		return 0;

	case INTAN_IOC_WRITE_REG:
		if (copy_from_user(&write_arg, (void __user *)arg, sizeof(write_arg)))
			return -EFAULT;
		mutex_lock(&priv->lock);
		ret = intan_write_reg(priv->spi, write_arg.reg, write_arg.value,
				      write_arg.u_flag, write_arg.m_flag);
		mutex_unlock(&priv->lock);
		return ret;

	case INTAN_IOC_CONVERT: {
		struct intan_convert_arg conv;

		if (copy_from_user(&conv, (void __user *)arg, sizeof(conv)))
			return -EFAULT;
		mutex_lock(&priv->lock);
		ret = intan_convert(priv->spi, conv.channel, conv.flags, &conv.value);
		mutex_unlock(&priv->lock);
		if (ret)
			return ret;
		if (copy_to_user((void __user *)arg, &conv, sizeof(conv)))
			return -EFAULT;
		return 0;
	}

	case INTAN_IOC_SINGLE_STEP:
		if (copy_from_user(&single_step_arg, (void __user *)arg, sizeof(single_step_arg)))
			return -EFAULT;
		mutex_lock(&priv->lock);
		ret = intan_single_step(priv->spi, single_step_arg.tx);
		mutex_unlock(&priv->lock);
		return ret;

	case INTAN_IOC_RUN_PATTERN: {
		struct intan_pattern_op *ops;
		size_t ops_bytes;

		if (copy_from_user(&pattern_arg, (void __user *)arg, sizeof(pattern_arg)))
			return -EFAULT;
		if (pattern_arg.num_ops == 0 || pattern_arg.num_ops > INTAN_PATTERN_MAX_OPS)
			return -EINVAL;
		if (!pattern_arg.ops_ptr)
			return -EINVAL;
		ops_bytes = array_size(pattern_arg.num_ops, sizeof(*ops));

		ops = kmalloc_array(pattern_arg.num_ops, sizeof(*ops), GFP_KERNEL);
		if (!ops)
			return -ENOMEM;
		if (copy_from_user(ops,
				   u64_to_user_ptr(pattern_arg.ops_ptr),
				   ops_bytes)) {
			kfree(ops);
			return -EFAULT;
		}

		mutex_lock(&priv->lock);
		ret = intan_run_pattern(priv->spi, ops, pattern_arg.num_ops,
					&pattern_arg.completed_ops);
		mutex_unlock(&priv->lock);

		if (copy_to_user(u64_to_user_ptr(pattern_arg.ops_ptr), ops, ops_bytes)) {
			kfree(ops);
			return -EFAULT;
		}
		kfree(ops);
		if (copy_to_user((void __user *)arg, &pattern_arg, sizeof(pattern_arg)))
			return -EFAULT;
		return ret;
	}

	case INTAN_IOC_STREAM_CONFIG:
		if (copy_from_user(&stream_cfg, (void __user *)arg, sizeof(stream_cfg)))
			return -EFAULT;
		ret = intan_stream_validate_config(&stream_cfg);
		if (ret)
			return ret;
		mutex_lock(&priv->stream_ctl_lock);
		if (priv->stream_running) {
			mutex_unlock(&priv->stream_ctl_lock);
			return -EBUSY;
		}
		priv->stream_cfg = stream_cfg;
		priv->stream_configured = true;
		memset(&priv->stream_status, 0, sizeof(priv->stream_status));
		priv->stream_status.configured = 1;
		intan_stream_reset_ring(priv);
		mutex_unlock(&priv->stream_ctl_lock);
		return 0;

	case INTAN_IOC_GET_RING_LAYOUT:
		ring_layout.slot_count = INTAN_STREAM_RING_SLOTS;
		ring_layout.slot_bytes = INTAN_STREAM_PACKET_MAX_BYTES;
		ring_layout.packet_max_bytes = INTAN_STREAM_PACKET_MAX_BYTES;
		ring_layout.flags = 0;
		if (copy_to_user((void __user *)arg, &ring_layout, sizeof(ring_layout)))
			return -EFAULT;
		return 0;

	case INTAN_IOC_START_STREAM:
		mutex_lock(&priv->stream_ctl_lock);
		ret = intan_stream_start(priv);
		mutex_unlock(&priv->stream_ctl_lock);
		return ret;

	case INTAN_IOC_STOP_STREAM:
		mutex_lock(&priv->stream_ctl_lock);
		intan_stream_stop(priv);
		mutex_unlock(&priv->stream_ctl_lock);
		return 0;

	case INTAN_IOC_GET_STREAM_STATUS:
		mutex_lock(&priv->stream_ctl_lock);
		if (copy_to_user((void __user *)arg, &priv->stream_status,
				 sizeof(priv->stream_status))) {
			mutex_unlock(&priv->stream_ctl_lock);
			return -EFAULT;
		}
		mutex_unlock(&priv->stream_ctl_lock);
		return 0;

	case INTAN_IOC_STREAM_READ_PACKET: {
		u8 packet[INTAN_STREAM_PACKET_MAX_BYTES];
		u32 packet_len = 0;
		u32 packet_sequence = 0;
		long wait_ret;
		unsigned long flags;

		if (copy_from_user(&read_packet_arg, (void __user *)arg, sizeof(read_packet_arg)))
			return -EFAULT;
		if (!read_packet_arg.data_ptr || read_packet_arg.buffer_size < INTAN_STREAM_PACKET_MAX_BYTES)
			return -EINVAL;

		wait_ret = wait_event_interruptible_timeout(
			priv->stream_waitq,
			priv->stream_ring_count > 0 || !priv->stream_running,
			read_packet_arg.timeout_ms ?
				msecs_to_jiffies(read_packet_arg.timeout_ms) :
				msecs_to_jiffies(1));
		if (wait_ret < 0)
			return wait_ret;
		if (wait_ret == 0)
			return -EAGAIN;

		spin_lock_irqsave(&priv->stream_ring_lock, flags);
		if (priv->stream_ring_count == 0) {
			bool running = priv->stream_running;

			spin_unlock_irqrestore(&priv->stream_ring_lock, flags);
			return running ? -EAGAIN : -EPIPE;
		}

		packet_len = priv->stream_ring[priv->stream_read_idx].len;
		packet_sequence = priv->stream_ring[priv->stream_read_idx].sequence;
		memcpy(packet, priv->stream_ring[priv->stream_read_idx].data, packet_len);
		priv->stream_read_idx = (priv->stream_read_idx + 1) % INTAN_STREAM_RING_SLOTS;
		priv->stream_ring_count--;
		spin_unlock_irqrestore(&priv->stream_ring_lock, flags);

		if (copy_to_user(u64_to_user_ptr(read_packet_arg.data_ptr), packet, packet_len))
			return -EFAULT;
		read_packet_arg.packet_size = packet_len;
		read_packet_arg.sequence = packet_sequence;
		if (copy_to_user((void __user *)arg, &read_packet_arg, sizeof(read_packet_arg)))
			return -EFAULT;
		return 0;
	}

	case INTAN_IOC_BENCHMARK:
		if (copy_from_user(&bench, (void __user *)arg, sizeof(bench)))
			return -EFAULT;
		if (bench.count == 0 || bench.count > 1000000)
			return -EINVAL;

		mutex_lock(&priv->lock);
		t0 = ktime_get();
		{
			unsigned int i;
			for (i = 0; i < bench.count; i++) {
				ret = intan_read_reg(priv->spi, INTAN_CHIP_ID_REG, &val);
				if (ret) {
					mutex_unlock(&priv->lock);
					return ret;
				}
			}
		}
		t1 = ktime_get();
		mutex_unlock(&priv->lock);

		(void)val; /* подавить unused */
		bench.elapsed_ns = (unsigned long)ktime_to_ns(ktime_sub(t1, t0));
		bench.freq_hz = INTAN_MAX_FREQ_HZ;  /* фиксировано 25 МГц */

		if (copy_to_user((void __user *)arg, &bench, sizeof(bench)))
			return -EFAULT;
		return 0;

	default:
		return -ENOTTY;
	}
}

static int intan_open(struct inode *inode, struct file *filp)
{
	struct miscdevice *misc = filp->private_data;
	struct intan_priv *priv = container_of(misc, struct intan_priv, miscdev);

	filp->private_data = priv;

	/* Питание включаем только при подключении (open); при старте ОПи питание нестабильно */
	if (priv->power_gpio) {
		gpiod_set_value_cansleep(priv->power_gpio, 1);
		msleep(INTAN_POWER_DELAY_MS);
	}
	return 0;
}

static int intan_release(struct inode *inode, struct file *filp)
{
	struct intan_priv *priv = filp->private_data;

	mutex_lock(&priv->stream_ctl_lock);
	intan_stream_stop(priv);
	mutex_unlock(&priv->stream_ctl_lock);
	if (priv && priv->power_gpio)
		gpiod_set_value_cansleep(priv->power_gpio, 0);
	return 0;
}

static const struct file_operations intan_fops = {
	.owner		= THIS_MODULE,
	.open		= intan_open,
	.release	= intan_release,
	.read		= intan_read,
	.write		= intan_write,
	.unlocked_ioctl	= intan_ioctl,
};

static int intan_probe(struct spi_device *spi)
{
	struct intan_priv *priv;
	u16 chip_id;
	int ret;

	priv = devm_kzalloc(&spi->dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	priv->spi = spi;
	mutex_init(&priv->lock);
	mutex_init(&priv->stream_ctl_lock);
	spin_lock_init(&priv->stream_ring_lock);
	init_waitqueue_head(&priv->stream_waitq);
	spi_set_drvdata(spi, priv);
	priv->stream_ring = vzalloc(array_size(INTAN_STREAM_RING_SLOTS, sizeof(*priv->stream_ring)));
	if (!priv->stream_ring)
		return -ENOMEM;
	priv->stream_transfers = kcalloc(INTAN_STREAM_MAX_TRANSFERS,
					 sizeof(*priv->stream_transfers),
					 GFP_KERNEL);
	if (!priv->stream_transfers) {
		vfree(priv->stream_ring);
		return -ENOMEM;
	}
	priv->stream_tx = kcalloc(INTAN_STREAM_MAX_TRANSFERS,
				  sizeof(*priv->stream_tx),
				  GFP_KERNEL);
	if (!priv->stream_tx) {
		kfree(priv->stream_transfers);
		vfree(priv->stream_ring);
		return -ENOMEM;
	}
	priv->stream_rx = kcalloc(INTAN_STREAM_MAX_TRANSFERS,
				  sizeof(*priv->stream_rx),
				  GFP_KERNEL);
	if (!priv->stream_rx) {
		kfree(priv->stream_tx);
		kfree(priv->stream_transfers);
		vfree(priv->stream_ring);
		return -ENOMEM;
	}

	/* Фиксируем 25 МГц (лимит Intan RHS2116) */
	spi->max_speed_hz = INTAN_MAX_FREQ_HZ;

	/* GPIO питания (PH2): включается только в open(), выключается в release() */
	priv->power_gpio = devm_gpiod_get_optional(&spi->dev, "power",
						   GPIOD_OUT_LOW);
	if (IS_ERR(priv->power_gpio)) {
		dev_err(&spi->dev, "Не удалось получить GPIO питания: %ld\n",
			PTR_ERR(priv->power_gpio));
		return PTR_ERR(priv->power_gpio);
	}

	if (priv->power_gpio) {
		gpiod_set_value_cansleep(priv->power_gpio, 1);
		msleep(INTAN_POWER_DELAY_MS);
	} else {
		dev_warn(&spi->dev, "GPIO питания не задан в DT, используйте intan_power_on.sh\n");
	}

	/* Проверка Chip ID (READ 255); после проверки питание выключим до первого open() */
	ret = intan_read_reg(spi, INTAN_CHIP_ID_REG, &chip_id);
	if (ret) {
		dev_err(&spi->dev, "Ошибка чтения регистра 255: %d\n", ret);
		goto err_power_off;
	}

	if (chip_id != INTAN_RHS2116_CHIP_ID) {
		dev_warn(&spi->dev,
			 "Chip ID 0x%04X (ожидается 0x%04X для RHS2116)\n",
			 chip_id, INTAN_RHS2116_CHIP_ID);
	}

	/* Питание выключаем после проверки: включается только при open() (подключение к Intan) */
	if (priv->power_gpio)
		gpiod_set_value_cansleep(priv->power_gpio, 0);

	priv->miscdev.minor = INTAN_MINOR;
	priv->miscdev.name = INTAN_DEV_NAME;
	priv->miscdev.fops = &intan_fops;
	priv->miscdev.parent = &spi->dev;

	ret = misc_register(&priv->miscdev);
	if (ret) {
		dev_err(&spi->dev, "Ошибка регистрации misc: %d\n", ret);
		goto err_power_off;
	}

	dev_info(&spi->dev, "Intan RHS2116 (Chip ID 0x%04X), /dev/%s\n",
		 chip_id, INTAN_DEV_NAME);

	return 0;

err_power_off:
	if (priv->power_gpio)
		gpiod_set_value_cansleep(priv->power_gpio, 0);
	return ret;
}

static void intan_remove(struct spi_device *spi)
{
	struct intan_priv *priv = spi_get_drvdata(spi);

	mutex_lock(&priv->stream_ctl_lock);
	intan_stream_stop(priv);
	mutex_unlock(&priv->stream_ctl_lock);
	misc_deregister(&priv->miscdev);
	kfree(priv->stream_rx);
	kfree(priv->stream_tx);
	kfree(priv->stream_transfers);
	vfree(priv->stream_ring);
	if (priv->power_gpio)
		gpiod_set_value_cansleep(priv->power_gpio, 0);
}

static const struct of_device_id intan_of_match[] = {
	{ .compatible = "msu,intan-rhs2116" },
	{ /* sentinel */ }
};
MODULE_DEVICE_TABLE(of, intan_of_match);

static const struct spi_device_id intan_spi_id[] = {
	{ "intan-rhs2116", 0 },
	{ /* sentinel */ }
};
MODULE_DEVICE_TABLE(spi, intan_spi_id);

static struct spi_driver intan_spi_driver = {
	.driver = {
		.name	= "intan-rhs2116",
		.of_match_table = intan_of_match,
	},
	.probe		= intan_probe,
	.remove		= intan_remove,
	.id_table	= intan_spi_id,
};

module_spi_driver(intan_spi_driver);

MODULE_AUTHOR("MSU Neuro Terminal");
MODULE_DESCRIPTION("SPI driver for Intan RHS2116 (совместим с Python-проектом)");
MODULE_LICENSE("GPL v2");
