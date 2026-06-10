// SPDX-License-Identifier: GPL-2.0
/*
 * Conveyor safety-node driver.
 *
 * Integrated demo driver using three project parts:
 * - tilt GPIO interrupt
 * - ultrasonic trigger/echo interrupt
 * - MCP3208/MCP3008 photocell SPI ADC sampling
 *
 * Raw sensor activity is filtered in kernel space. User space is woken only
 * when the fused conveyor state changes.
 */

#include <linux/atomic.h>
#include <linux/bitops.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/gpio.h>
#include <linux/hrtimer.h>
#include <linux/interrupt.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/poll.h>
#include <linux/proc_fs.h>
#include <linux/slab.h>
#include <linux/spi/spi.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/version.h>
#include <linux/wait.h>
#include <linux/workqueue.h>

#define DRIVER_NAME "conveyor_node"
#define DEVICE_NAME "conveyor_node0"
#define PROC_NAME "conveyor_node_stats"
#define EVENT_Q_SIZE 32

#define REASON_TILT_CHANGED      BIT(0)
#define REASON_BLOCKAGE_CHANGED  BIT(1)
#define REASON_LIGHT_BLOCKED     BIT(2)
#define REASON_DISTANCE_STABLE   BIT(3)

enum tilt_state {
	TILT_LEVEL = 0,
	TILT_TILTED,
};

enum blockage_state {
	BLOCK_CLEAR = 0,
	BLOCK_BLOCKED,
};

enum conveyor_state {
	CONV_RUNNING_OK = 0,
	CONV_BLOCKAGE_ALERT,
	CONV_STRUCTURAL_FAULT,
	CONV_CRITICAL_FAULT,
};

struct conveyor_event {
	u64 ts_ns;
	enum conveyor_state state;
	enum tilt_state tilt;
	enum blockage_state blockage;
	int distance_cm;
	int light_value;
	u32 reason_flags;
};

struct conveyor_node_dev {
	struct spi_device *spi;

	int tilt_gpio;
	int tilt_irq;
	int ultra_trig_gpio;
	int ultra_echo_gpio;
	int ultra_irq;
	int led_gpio;

	atomic64_t irq_total;
	atomic64_t tilt_edges;
	atomic64_t ultra_edges;
	atomic64_t fusion_runs;
	atomic64_t suppressed_events;
	atomic64_t user_events;

	u64 last_tilt_edge_ns;
	u64 last_ultra_rise_ns;
	u64 last_ultra_fall_ns;
	int last_tilt_gpio;
	int last_echo_gpio;
	ktime_t echo_start;
	int echo_phase;
	spinlock_t data_lock;

	enum tilt_state tilt;
	enum blockage_state blockage;
	enum conveyor_state state;
	spinlock_t state_lock;

	int distance_cm;
	int prev_distance_cm;
	bool distance_fresh;
	int light_value;
	int light_ema;       /* scaled by 8: actual = light_ema >> 3 */
	unsigned int block_streak;
	unsigned int clear_streak;

	struct delayed_work tilt_eval_work;
	struct work_struct fusion_work;
	struct hrtimer sample_timer;
	ktime_t sample_period;

	struct conveyor_event q[EVENT_Q_SIZE];
	unsigned int q_head;
	unsigned int q_tail;
	spinlock_t q_lock;
	wait_queue_head_t read_wq;

	struct miscdevice miscdev;
	struct proc_dir_entry *proc_entry;
};

static int tilt_gpio = 27;
module_param(tilt_gpio, int, 0444);
MODULE_PARM_DESC(tilt_gpio, "BCM GPIO connected to tilt switch output");

static int tilt_active_level = 1;
module_param(tilt_active_level, int, 0644);
MODULE_PARM_DESC(tilt_active_level, "GPIO level that means tilted");

static unsigned int tilt_debounce_ms = 50;
module_param(tilt_debounce_ms, uint, 0644);
MODULE_PARM_DESC(tilt_debounce_ms, "Tilt debounce delay in ms");

static int ultra_trig_gpio = 23;
module_param(ultra_trig_gpio, int, 0444);
MODULE_PARM_DESC(ultra_trig_gpio, "BCM GPIO connected to ultrasonic trigger");

static int ultra_echo_gpio = 24;
module_param(ultra_echo_gpio, int, 0444);
MODULE_PARM_DESC(ultra_echo_gpio, "BCM GPIO connected to ultrasonic echo");

static int ultra_max_dist_cm = 50;
module_param(ultra_max_dist_cm, int, 0644);
MODULE_PARM_DESC(ultra_max_dist_cm, "Maximum distance range for blockage detection");

static int ultra_tolerance_cm = 2;
module_param(ultra_tolerance_cm, int, 0644);
MODULE_PARM_DESC(ultra_tolerance_cm, "Distance stability tolerance for blockage detection");

static unsigned int blockage_confirm_count = 20; /* 20 × 200ms = 4s */
module_param(blockage_confirm_count, uint, 0644);
MODULE_PARM_DESC(blockage_confirm_count, "Consecutive blocked samples required");

static unsigned int blockage_clear_count = 2;
module_param(blockage_clear_count, uint, 0644);
MODULE_PARM_DESC(blockage_clear_count, "Consecutive clear samples required");

static int adc_channel = 0;
module_param(adc_channel, int, 0644);
MODULE_PARM_DESC(adc_channel, "MCP3208/MCP3008 ADC channel");

static int adc_bits = 12;
module_param(adc_bits, int, 0644);
MODULE_PARM_DESC(adc_bits, "ADC resolution: 12 for MCP3208, 10 for MCP3008");

static int light_threshold = 1800;
module_param(light_threshold, int, 0644);
MODULE_PARM_DESC(light_threshold, "Photocell blockage threshold");

static int light_blocked_when_below = 1;
module_param(light_blocked_when_below, int, 0644);
MODULE_PARM_DESC(light_blocked_when_below, "1 if lower ADC value means blocked");

static int led_gpio = 5;
module_param(led_gpio, int, 0644);
MODULE_PARM_DESC(led_gpio, "BCM GPIO used for critical alarm LED");

static int sample_period_ms = 200;
module_param(sample_period_ms, int, 0644);
MODULE_PARM_DESC(sample_period_ms, "Periodic fusion sampling interval");

static int spi_speed_hz = 1000000;
module_param(spi_speed_hz, int, 0644);
MODULE_PARM_DESC(spi_speed_hz, "SPI speed for ADC");

static struct conveyor_node_dev *gdev;

static const char *tilt_name(enum tilt_state state)
{
	return state == TILT_TILTED ? "TILTED" : "LEVEL";
}

static const char *blockage_name(enum blockage_state state)
{
	return state == BLOCK_BLOCKED ? "BLOCKED" : "CLEAR";
}

static const char *conveyor_name(enum conveyor_state state)
{
	switch (state) {
	case CONV_RUNNING_OK:
		return "RUNNING_OK";
	case CONV_BLOCKAGE_ALERT:
		return "BLOCKAGE_ALERT";
	case CONV_STRUCTURAL_FAULT:
		return "STRUCTURAL_FAULT";
	case CONV_CRITICAL_FAULT:
		return "CRITICAL_FAULT";
	default:
		return "UNKNOWN";
	}
}

static bool queue_empty(struct conveyor_node_dev *nd)
{
	return nd->q_head == nd->q_tail;
}

static bool queue_full(struct conveyor_node_dev *nd)
{
	return ((nd->q_head + 1) % EVENT_Q_SIZE) == nd->q_tail;
}

static enum conveyor_state evaluate_state(enum tilt_state tilt,
					  enum blockage_state blockage)
{
	if (tilt == TILT_LEVEL && blockage == BLOCK_CLEAR)
		return CONV_RUNNING_OK;
	if (tilt == TILT_LEVEL && blockage == BLOCK_BLOCKED)
		return CONV_BLOCKAGE_ALERT;
	if (tilt == TILT_TILTED && blockage == BLOCK_CLEAR)
		return CONV_STRUCTURAL_FAULT;
	return CONV_CRITICAL_FAULT;
}

static int adc_read_value(struct spi_device *spi, int ch)
{
	unsigned char tx[3] = { 0, 0, 0 };
	unsigned char rx[3] = { 0, 0, 0 };
	struct spi_transfer transfer;
	int ret;

	memset(&transfer, 0, sizeof(transfer));
	ch &= 0x07;

	if (adc_bits == 10) {
		tx[0] = 0x01;
		tx[1] = (0x08 | ch) << 4;
	} else {
		tx[0] = 0x06 | ((ch & 0x07) >> 2);
		tx[1] = ((ch & 0x07) << 6);
	}

	transfer.tx_buf = tx;
	transfer.rx_buf = rx;
	transfer.len = 3;

	ret = spi_sync_transfer(spi, &transfer, 1);
	if (ret < 0)
		return ret;

	if (adc_bits == 10)
		return ((rx[1] & 0x03) << 8) | rx[2];
	return ((rx[1] & 0x0f) << 8) | rx[2];
}

static bool light_is_blocked(int value)
{
	if (value < 0)
		return false;
	return light_blocked_when_below ? value < light_threshold :
					  value > light_threshold;
}

static void push_event(struct conveyor_node_dev *nd, u32 reason_flags)
{
	struct conveyor_event ev;
	unsigned long flags;

	spin_lock_irqsave(&nd->state_lock, flags);
	ev.ts_ns = ktime_get_ns();
	ev.state = nd->state;
	ev.tilt = nd->tilt;
	ev.blockage = nd->blockage;
	ev.distance_cm = nd->distance_cm;
	ev.light_value = nd->light_value;
	ev.reason_flags = reason_flags;
	spin_unlock_irqrestore(&nd->state_lock, flags);

	spin_lock_irqsave(&nd->q_lock, flags);
	if (queue_full(nd))
		nd->q_tail = (nd->q_tail + 1) % EVENT_Q_SIZE;
	nd->q[nd->q_head] = ev;
	nd->q_head = (nd->q_head + 1) % EVENT_Q_SIZE;
	spin_unlock_irqrestore(&nd->q_lock, flags);

	atomic64_inc(&nd->user_events);
	wake_up_interruptible(&nd->read_wq);

	gpio_set_value(nd->led_gpio, ev.state == CONV_CRITICAL_FAULT);

	pr_info(DRIVER_NAME ": state=%s tilt=%s blockage=%s distance=%d light=%d reason=0x%x\n",
		conveyor_name(ev.state), tilt_name(ev.tilt),
		blockage_name(ev.blockage), ev.distance_cm, ev.light_value,
		ev.reason_flags);
}

static void set_fused_state(struct conveyor_node_dev *nd,
			    enum conveyor_state new_state,
			    enum blockage_state new_blockage, u32 reason_flags)
{
	unsigned long flags;
	bool changed = false;

	spin_lock_irqsave(&nd->state_lock, flags);
	if (nd->blockage != new_blockage) {
		nd->blockage = new_blockage;
		reason_flags |= REASON_BLOCKAGE_CHANGED;
		changed = true;
	}
	if (nd->state != new_state) {
		nd->state = new_state;
		changed = true;
	}
	spin_unlock_irqrestore(&nd->state_lock, flags);

	if (changed)
		push_event(nd, reason_flags);
	else
		atomic64_inc(&nd->suppressed_events);
}

static void trigger_ultrasonic(struct conveyor_node_dev *nd)
{
	unsigned long flags;

	spin_lock_irqsave(&nd->data_lock, flags);
	if (nd->echo_phase != 0 && nd->echo_phase != 3) {
		spin_unlock_irqrestore(&nd->data_lock, flags);
		return;
	}
	nd->echo_phase = 1;
	spin_unlock_irqrestore(&nd->data_lock, flags);

	gpio_set_value(nd->ultra_trig_gpio, 1);
	udelay(10);
	gpio_set_value(nd->ultra_trig_gpio, 0);
}

static void fusion_work_fn(struct work_struct *work)
{
	struct conveyor_node_dev *nd;
	enum conveyor_state new_state;
	enum blockage_state new_blockage;
	enum tilt_state tilt;
	int light_value;
	int distance;
	int prev_distance;
	int diff;
	bool distance_fresh;
	bool distance_stable;
	bool light_blocked;
	bool raw_blocked;
	u32 reason_flags = 0;
	unsigned long flags;

	nd = container_of(work, struct conveyor_node_dev, fusion_work);
	atomic64_inc(&nd->fusion_runs);

	light_value = adc_read_value(nd->spi, adc_channel);
	if (light_value < 0)
		pr_err(DRIVER_NAME ": adc read failed: %d\n", light_value);

	spin_lock_irqsave(&nd->state_lock, flags);
	tilt = nd->tilt;
	distance = nd->distance_cm;
	prev_distance = nd->prev_distance_cm;
	distance_fresh = nd->distance_fresh;
	nd->distance_fresh = false;
	if (light_value >= 0) {
		if (unlikely(!nd->light_ema))
			nd->light_ema = light_value << 3;
		else
			nd->light_ema = nd->light_ema - (nd->light_ema >> 3) + light_value;
		nd->light_value = nd->light_ema >> 3;
		light_value = nd->light_value;
	}
	spin_unlock_irqrestore(&nd->state_lock, flags);

	diff = distance - prev_distance;
	if (diff < 0)
		diff = -diff;

	distance_stable = distance_fresh && distance > 0 &&
			  distance < ultra_max_dist_cm &&
			  diff <= ultra_tolerance_cm;
	light_blocked = light_is_blocked(light_value);
	raw_blocked = distance_stable || light_blocked;

	spin_lock_irqsave(&nd->state_lock, flags);
	nd->prev_distance_cm = distance;
	if (raw_blocked) {
		nd->block_streak++;
		nd->clear_streak = 0;
	} else {
		nd->clear_streak++;
		nd->block_streak = 0;
	}

	new_blockage = nd->blockage;
	if (nd->block_streak >= blockage_confirm_count)
		new_blockage = BLOCK_BLOCKED;
	else if (nd->clear_streak >= blockage_clear_count)
		new_blockage = BLOCK_CLEAR;

	new_state = evaluate_state(tilt, new_blockage);
	if (light_blocked)
		reason_flags |= REASON_LIGHT_BLOCKED;
	if (distance_stable)
		reason_flags |= REASON_DISTANCE_STABLE;
	spin_unlock_irqrestore(&nd->state_lock, flags);

	set_fused_state(nd, new_state, new_blockage, reason_flags);
	trigger_ultrasonic(nd);
}

static void tilt_eval_work_fn(struct work_struct *work)
{
	struct conveyor_node_dev *nd;
	enum tilt_state new_tilt;
	enum conveyor_state new_state;
	int value;
	unsigned long flags;
	bool changed = false;

	nd = container_of(to_delayed_work(work), struct conveyor_node_dev,
			  tilt_eval_work);

	value = gpio_get_value(nd->tilt_gpio);
	new_tilt = (value == tilt_active_level) ? TILT_TILTED : TILT_LEVEL;

	spin_lock_irqsave(&nd->data_lock, flags);
	nd->last_tilt_gpio = value;
	spin_unlock_irqrestore(&nd->data_lock, flags);

	spin_lock_irqsave(&nd->state_lock, flags);
	if (nd->tilt != new_tilt) {
		nd->tilt = new_tilt;
		changed = true;
	}
	new_state = evaluate_state(nd->tilt, nd->blockage);
	if (changed)
		nd->state = new_state;
	spin_unlock_irqrestore(&nd->state_lock, flags);

	if (changed)
		push_event(nd, REASON_TILT_CHANGED);
	else
		atomic64_inc(&nd->suppressed_events);

	schedule_work(&nd->fusion_work);
}

static irqreturn_t tilt_irq_handler(int irq, void *dev_id)
{
	struct conveyor_node_dev *nd = dev_id;
	unsigned long flags;

	atomic64_inc(&nd->irq_total);
	atomic64_inc(&nd->tilt_edges);

	spin_lock_irqsave(&nd->data_lock, flags);
	nd->last_tilt_edge_ns = ktime_get_ns();
	nd->last_tilt_gpio = gpio_get_value(nd->tilt_gpio);
	spin_unlock_irqrestore(&nd->data_lock, flags);

	mod_delayed_work(system_wq, &nd->tilt_eval_work,
			 msecs_to_jiffies(tilt_debounce_ms));

	return IRQ_HANDLED;
}

static irqreturn_t ultra_irq_handler(int irq, void *dev_id)
{
	struct conveyor_node_dev *nd = dev_id;
	ktime_t now = ktime_get();
	int value = gpio_get_value(nd->ultra_echo_gpio);
	int new_distance = -1;
	unsigned long flags;

	atomic64_inc(&nd->irq_total);
	atomic64_inc(&nd->ultra_edges);

	spin_lock_irqsave(&nd->data_lock, flags);
	nd->last_echo_gpio = value;
	if (value && nd->echo_phase == 1) {
		nd->echo_start = now;
		nd->last_ultra_rise_ns = ktime_to_ns(now);
		nd->echo_phase = 2;
	} else if (!value && nd->echo_phase == 2) {
		s64 us = ktime_to_us(ktime_sub(now, nd->echo_start));
		nd->last_ultra_fall_ns = ktime_to_ns(now);
		if (us > 0 && us < 40000)   /* HC-SR04 max echo ~38ms */
			new_distance = (int)us / 58;
		nd->echo_phase = 3;
	}
	spin_unlock_irqrestore(&nd->data_lock, flags);

	if (new_distance >= 0) {
		spin_lock_irqsave(&nd->state_lock, flags);
		nd->distance_cm = new_distance;
		nd->distance_fresh = true;
		spin_unlock_irqrestore(&nd->state_lock, flags);
	}

	return IRQ_HANDLED;
}

static enum hrtimer_restart sample_timer_fn(struct hrtimer *timer)
{
	struct conveyor_node_dev *nd;

	nd = container_of(timer, struct conveyor_node_dev, sample_timer);
	schedule_work(&nd->fusion_work);
	hrtimer_forward_now(timer, nd->sample_period);

	return HRTIMER_RESTART;
}

static ssize_t node_read(struct file *file, char __user *buf, size_t count,
			 loff_t *ppos)
{
	struct conveyor_node_dev *nd = container_of(file->private_data,
						   struct conveyor_node_dev,
						   miscdev);
	struct conveyor_event ev;
	unsigned long flags;
	char line[192];
	int len;
	int ret;

	if (count == 0)
		return 0;

	if (file->f_flags & O_NONBLOCK) {
		spin_lock_irqsave(&nd->q_lock, flags);
		ret = queue_empty(nd) ? -EAGAIN : 0;
		spin_unlock_irqrestore(&nd->q_lock, flags);
		if (ret)
			return ret;
	} else {
		ret = wait_event_interruptible(nd->read_wq, ({
			bool ready;

			spin_lock_irqsave(&nd->q_lock, flags);
			ready = !queue_empty(nd);
			spin_unlock_irqrestore(&nd->q_lock, flags);
			ready;
		}));
		if (ret)
			return ret;
	}

	spin_lock_irqsave(&nd->q_lock, flags);
	if (queue_empty(nd)) {
		spin_unlock_irqrestore(&nd->q_lock, flags);
		return -EAGAIN;
	}
	ev = nd->q[nd->q_tail];
	nd->q_tail = (nd->q_tail + 1) % EVENT_Q_SIZE;
	spin_unlock_irqrestore(&nd->q_lock, flags);

	len = scnprintf(line, sizeof(line),
			"ts=%llu state=%s tilt=%s blockage=%s distance_cm=%d light=%d reason=0x%x\n",
			ev.ts_ns, conveyor_name(ev.state), tilt_name(ev.tilt),
			blockage_name(ev.blockage), ev.distance_cm,
			ev.light_value, ev.reason_flags);

	if (count < len)
		return -EINVAL;
	if (copy_to_user(buf, line, len))
		return -EFAULT;

	return len;
}

static __poll_t node_poll(struct file *file, poll_table *wait)
{
	struct conveyor_node_dev *nd = container_of(file->private_data,
						   struct conveyor_node_dev,
						   miscdev);
	unsigned long flags;
	__poll_t mask = 0;

	poll_wait(file, &nd->read_wq, wait);

	spin_lock_irqsave(&nd->q_lock, flags);
	if (!queue_empty(nd))
		mask |= POLLIN | POLLRDNORM;
	spin_unlock_irqrestore(&nd->q_lock, flags);

	return mask;
}

static const struct file_operations node_fops = {
	.owner = THIS_MODULE,
	.read = node_read,
	.poll = node_poll,
	.llseek = no_llseek,
};

static ssize_t stats_read(struct file *file, char __user *buf, size_t count,
			  loff_t *ppos)
{
	struct conveyor_node_dev *nd = gdev;
	char tmp[1024];
	unsigned long flags;
	enum conveyor_state state;
	enum tilt_state tilt;
	enum blockage_state blockage;
	int distance;
	int light;
	int last_tilt_gpio;
	int last_echo_gpio;
	int len;

	if (!nd)
		return 0;

	spin_lock_irqsave(&nd->state_lock, flags);
	state = nd->state;
	tilt = nd->tilt;
	blockage = nd->blockage;
	distance = nd->distance_cm;
	light = nd->light_value;
	spin_unlock_irqrestore(&nd->state_lock, flags);

	spin_lock_irqsave(&nd->data_lock, flags);
	last_tilt_gpio = nd->last_tilt_gpio;
	last_echo_gpio = nd->last_echo_gpio;
	spin_unlock_irqrestore(&nd->data_lock, flags);

	len = scnprintf(tmp, sizeof(tmp),
			"state: %s\n"
			"tilt: %s\n"
			"blockage: %s\n"
			"distance_cm: %d\n"
			"light_value: %d\n"
			"tilt_gpio: %d\n"
			"ultra_trig_gpio: %d\n"
			"ultra_echo_gpio: %d\n"
			"led_gpio: %d\n"
			"last_tilt_gpio: %d\n"
			"last_echo_gpio: %d\n"
			"irq_total: %llu\n"
			"tilt_edges: %llu\n"
			"ultra_edges: %llu\n"
			"fusion_runs: %llu\n"
			"suppressed_events: %llu\n"
			"user_events: %llu\n",
			conveyor_name(state), tilt_name(tilt),
			blockage_name(blockage), distance, light, nd->tilt_gpio,
			nd->ultra_trig_gpio, nd->ultra_echo_gpio, nd->led_gpio,
			last_tilt_gpio, last_echo_gpio,
			atomic64_read(&nd->irq_total),
			atomic64_read(&nd->tilt_edges),
			atomic64_read(&nd->ultra_edges),
			atomic64_read(&nd->fusion_runs),
			atomic64_read(&nd->suppressed_events),
			atomic64_read(&nd->user_events));

	return simple_read_from_buffer(buf, count, ppos, tmp, len);
}

#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 6, 0)
static const struct proc_ops stats_proc_ops = {
	.proc_read = stats_read,
};
#else
static const struct file_operations stats_proc_ops = {
	.owner = THIS_MODULE,
	.read = stats_read,
};
#endif

static int request_gpios(struct conveyor_node_dev *nd)
{
	int ret;

	ret = gpio_request(nd->tilt_gpio, DRIVER_NAME "_tilt");
	if (ret)
		return ret;
	ret = gpio_direction_input(nd->tilt_gpio);
	if (ret)
		goto err_tilt;

	ret = gpio_request(nd->ultra_trig_gpio, DRIVER_NAME "_ultra_trig");
	if (ret)
		goto err_tilt;
	ret = gpio_direction_output(nd->ultra_trig_gpio, 0);
	if (ret)
		goto err_trig;

	ret = gpio_request(nd->ultra_echo_gpio, DRIVER_NAME "_ultra_echo");
	if (ret)
		goto err_trig;
	ret = gpio_direction_input(nd->ultra_echo_gpio);
	if (ret)
		goto err_echo;

	ret = gpio_request(nd->led_gpio, DRIVER_NAME "_led");
	if (ret)
		goto err_echo;
	ret = gpio_direction_output(nd->led_gpio, 0);
	if (ret)
		goto err_led;

	return 0;

err_led:
	gpio_free(nd->led_gpio);
err_echo:
	gpio_free(nd->ultra_echo_gpio);
err_trig:
	gpio_free(nd->ultra_trig_gpio);
err_tilt:
	gpio_free(nd->tilt_gpio);
	return ret;
}

static void free_gpios(struct conveyor_node_dev *nd)
{
	gpio_set_value(nd->led_gpio, 0);
	gpio_free(nd->led_gpio);
	gpio_free(nd->ultra_echo_gpio);
	gpio_free(nd->ultra_trig_gpio);
	gpio_free(nd->tilt_gpio);
}

static int conveyor_probe(struct spi_device *spi)
{
	struct conveyor_node_dev *nd;
	int ret;
	int value;

	nd = kzalloc(sizeof(*nd), GFP_KERNEL);
	if (!nd)
		return -ENOMEM;

	nd->spi = spi;
	nd->tilt_gpio = tilt_gpio;
	nd->ultra_trig_gpio = ultra_trig_gpio;
	nd->ultra_echo_gpio = ultra_echo_gpio;
	nd->led_gpio = led_gpio;
	nd->echo_phase = 3;
	if (sample_period_ms <= 0)
		sample_period_ms = 200;
	nd->sample_period = ktime_set(0, sample_period_ms * 1000000L);

	spin_lock_init(&nd->data_lock);
	spin_lock_init(&nd->state_lock);
	spin_lock_init(&nd->q_lock);
	init_waitqueue_head(&nd->read_wq);

	spi->mode = SPI_MODE_0;
	spi->bits_per_word = 8;
	spi->max_speed_hz = spi_speed_hz;
	ret = spi_setup(spi);
	if (ret)
		goto err_free;

	ret = request_gpios(nd);
	if (ret)
		goto err_free;

	value = gpio_get_value(nd->tilt_gpio);
	nd->last_tilt_gpio = value;
	nd->tilt = (value == tilt_active_level) ? TILT_TILTED : TILT_LEVEL;
	nd->blockage = BLOCK_CLEAR;
	nd->state = evaluate_state(nd->tilt, nd->blockage);

	INIT_DELAYED_WORK(&nd->tilt_eval_work, tilt_eval_work_fn);
	INIT_WORK(&nd->fusion_work, fusion_work_fn);
	hrtimer_init(&nd->sample_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	nd->sample_timer.function = sample_timer_fn;

	nd->tilt_irq = gpio_to_irq(nd->tilt_gpio);
	if (nd->tilt_irq < 0) {
		ret = nd->tilt_irq;
		goto err_gpio;
	}

	nd->ultra_irq = gpio_to_irq(nd->ultra_echo_gpio);
	if (nd->ultra_irq < 0) {
		ret = nd->ultra_irq;
		goto err_gpio;
	}

	ret = request_irq(nd->tilt_irq, tilt_irq_handler,
			  IRQF_TRIGGER_RISING | IRQF_TRIGGER_FALLING,
			  DRIVER_NAME "_tilt", nd);
	if (ret)
		goto err_gpio;

	ret = request_irq(nd->ultra_irq, ultra_irq_handler,
			  IRQF_TRIGGER_RISING | IRQF_TRIGGER_FALLING,
			  DRIVER_NAME "_ultra", nd);
	if (ret)
		goto err_tilt_irq;

	nd->miscdev.minor = MISC_DYNAMIC_MINOR;
	nd->miscdev.name = DEVICE_NAME;
	nd->miscdev.fops = &node_fops;
	ret = misc_register(&nd->miscdev);
	if (ret)
		goto err_ultra_irq;

	nd->proc_entry = proc_create(PROC_NAME, 0444, NULL, &stats_proc_ops);
	if (!nd->proc_entry) {
		ret = -ENOMEM;
		goto err_misc;
	}

	spi_set_drvdata(spi, nd);
	gdev = nd;
	push_event(nd, REASON_TILT_CHANGED | REASON_BLOCKAGE_CHANGED);
	hrtimer_start(&nd->sample_timer, nd->sample_period, HRTIMER_MODE_REL);

	pr_info(DRIVER_NAME ": loaded state=%s tilt_gpio=%d ultra=%d/%d adc_ch=%d\n",
		conveyor_name(nd->state), nd->tilt_gpio, nd->ultra_trig_gpio,
		nd->ultra_echo_gpio, adc_channel);

	return 0;

err_misc:
	misc_deregister(&nd->miscdev);
err_ultra_irq:
	free_irq(nd->ultra_irq, nd);
err_tilt_irq:
	free_irq(nd->tilt_irq, nd);
err_gpio:
	free_gpios(nd);
err_free:
	kfree(nd);
	return ret;
}

static int conveyor_remove(struct spi_device *spi)
{
	struct conveyor_node_dev *nd = spi_get_drvdata(spi);

	if (!nd)
		return 0;

	gdev = NULL;
	hrtimer_cancel(&nd->sample_timer);
	cancel_delayed_work_sync(&nd->tilt_eval_work);
	cancel_work_sync(&nd->fusion_work);
	proc_remove(nd->proc_entry);
	misc_deregister(&nd->miscdev);
	free_irq(nd->ultra_irq, nd);
	free_irq(nd->tilt_irq, nd);
	free_gpios(nd);
	kfree(nd);

	pr_info(DRIVER_NAME ": unloaded\n");
	return 0;
}

static const struct of_device_id conveyor_of_match[] = {
	{ .compatible = "simple,conveyor-node" },
	{ }
};
MODULE_DEVICE_TABLE(of, conveyor_of_match);

static const struct spi_device_id conveyor_id[] = {
	{ "conveyor-node", 0 },
	{ "spidev", 0 },
	{ }
};
MODULE_DEVICE_TABLE(spi, conveyor_id);

static struct spi_driver conveyor_driver = {
	.driver = {
		.name = DRIVER_NAME,
		.of_match_table = conveyor_of_match,
	},
	.probe = conveyor_probe,
	.remove = conveyor_remove,
	.id_table = conveyor_id,
};

module_spi_driver(conveyor_driver);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Cho Youngtak team integration");
MODULE_DESCRIPTION("Integrated conveyor safety-node state driver");
