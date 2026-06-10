# 코드 설명 보고서

## 목차
1. [전체 파일 구성](#1-전체-파일-구성)
2. [conveyor_node.c — 커널 드라이버](#2-conveyor_nodec--커널-드라이버)
3. [conveyor_event_daemon.py — MQTT 퍼블리셔](#3-conveyor_event_daemonpy--mqtt-퍼블리셔)
4. [conveyor_subscriber.py — MQTT 섭스크라이버](#4-conveyor_subscriberpy--mqtt-섭스크라이버)
5. [conveyor_web.py — 웹 대시보드](#5-conveyor_webpy--웹-대시보드)
6. [동시성 설계](#6-동시성-설계)
7. [설계 결정 및 트레이드오프](#7-설계-결정-및-트레이드오프)

---

## 1. 전체 파일 구성

```
conveyor_node_driver/
├── conveyor_node.c          # 리눅스 커널 모듈 (드라이버 본체)
├── conveyor_monitor.c       # 유저스페이스 테스트 도구 (poll + read)
├── conveyor_event_daemon.py # 드라이버 Pi: /dev → MQTT publish
├── broker/
│   ├── conveyor_subscriber.py  # 섭스크라이버 Pi: MQTT → 터미널 출력
│   └── conveyor_web.py         # 섭스크라이버 Pi: MQTT → HTTP 대시보드
├── deploy.sh                # 전체 배포 자동화 스크립트
└── Makefile                 # 크로스컴파일 / scp / 배포 타깃
```

---

## 2. conveyor_node.c — 커널 드라이버

### 2-1. 모듈 등록 방식: SPI 드라이버

```c
static struct spi_driver conveyor_driver = {
    .driver = {
        .name = "conveyor_node",
        .of_match_table = conveyor_of_match,   // Device Tree 매칭
    },
    .probe  = conveyor_probe,
    .remove = conveyor_remove,
    .id_table = conveyor_id,
};
module_spi_driver(conveyor_driver);
```

`module_spi_driver()`는 `module_init` / `module_exit`를 자동 생성하는 매크로다.  
SPI 버스에 `conveyor-node` 또는 `spidev` 이름의 디바이스가 등록되면 커널이 자동으로 `conveyor_probe()`를 호출한다.

---

### 2-2. probe() — 드라이버 초기화

`conveyor_probe()`는 모듈 바인딩 시 한 번 호출된다.

```c
static int conveyor_probe(struct spi_device *spi)
{
    // 1. 디바이스 구조체 할당 (kzalloc = kmalloc + memset 0)
    nd = kzalloc(sizeof(*nd), GFP_KERNEL);

    // 2. SPI 설정 (MODE_0, 8비트, 1MHz)
    spi->mode = SPI_MODE_0;
    spi->bits_per_word = 8;
    spi->max_speed_hz = spi_speed_hz;
    spi_setup(spi);

    // 3. GPIO 요청 및 방향 설정
    request_gpios(nd);

    // 4. 초기 tilt 상태 읽기
    value = gpio_get_value(nd->tilt_gpio);
    nd->tilt = (value == tilt_active_level) ? TILT_TILTED : TILT_LEVEL;

    // 5. workqueue, hrtimer 초기화
    INIT_DELAYED_WORK(&nd->tilt_eval_work, tilt_eval_work_fn);
    INIT_WORK(&nd->fusion_work, fusion_work_fn);
    hrtimer_init(&nd->sample_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);

    // 6. IRQ 등록 (tilt: 양엣지, echo: 양엣지)
    request_irq(nd->tilt_irq,  tilt_irq_handler,  IRQF_TRIGGER_RISING|FALLING, ...);
    request_irq(nd->ultra_irq, ultra_irq_handler, IRQF_TRIGGER_RISING|FALLING, ...);

    // 7. /dev/conveyor_node0 생성
    misc_register(&nd->miscdev);

    // 8. /proc/conveyor_node_stats 생성
    proc_create(PROC_NAME, 0444, NULL, &stats_proc_ops);

    // 9. 부팅 시 초기 이벤트 push 후 타이머 시작
    push_event(nd, REASON_TILT_CHANGED | REASON_BLOCKAGE_CHANGED);
    hrtimer_start(&nd->sample_timer, nd->sample_period, HRTIMER_MODE_REL);
}
```

**에러 처리**: goto 기반 언와인딩 패턴 (`err_misc → err_ultra_irq → err_tilt_irq → err_gpio → err_free`)으로, 초기화 도중 실패하면 이미 획득한 자원을 역순으로 해제한다.

---

### 2-3. IRQ 핸들러

#### tilt_irq_handler (하드웨어 인터럽트 컨텍스트)

```c
static irqreturn_t tilt_irq_handler(int irq, void *dev_id)
{
    // 인터럽트 컨텍스트: sleep 불가, 최소 작업만
    atomic64_inc(&nd->tilt_edges);
    nd->last_tilt_edge_ns = ktime_get_ns();

    // 디바운스: 50ms 뒤에 workqueue에서 실제 판정
    mod_delayed_work(system_wq, &nd->tilt_eval_work,
                     msecs_to_jiffies(tilt_debounce_ms));
    return IRQ_HANDLED;
}
```

스위치는 누를 때 여러 번 전기 노이즈(바운스)가 발생한다. `mod_delayed_work()`는 이미 예약된 work가 있으면 타이머를 재설정(리셋)하므로, 연속 바운스 중에는 마지막 엣지로부터 50ms 후에 한 번만 실행된다.

#### ultra_irq_handler — echo 상태 머신

초음파 측정은 4단계 phase로 관리된다.

```
phase 0/3 (대기)
    └─ trigger_ultrasonic() 호출 → phase = 1

phase 1 (트리거 완료, echo 대기)
    └─ RISING edge 수신 → echo_start = now, phase = 2

phase 2 (echo HIGH 구간 측정 중)
    └─ FALLING edge 수신
           ├─ us = now - echo_start
           ├─ 0 < us < 40000 이면: distance_cm = us / 58
           └─ phase = 3
```

```c
if (value && nd->echo_phase == 1) {        // phase==1일 때만 RISING 수용
    nd->echo_start = now;
    nd->echo_phase = 2;
} else if (!value && nd->echo_phase == 2) { // FALLING
    s64 us = ktime_to_us(ktime_sub(now, nd->echo_start));
    if (us > 0 && us < 40000)              // 40ms 초과는 노이즈로 폐기
        new_distance = (int)us / 58;       // 음속 340m/s → 58µs/cm
    nd->echo_phase = 3;
}
```

**거리 계산**: 음속 340m/s, 왕복이므로 편도 = us × 340/2 × 10⁻⁶ m = us/58 cm.

**설계 포인트**: `echo_phase == 1` 조건이 없으면 노이즈 RISING이 `echo_phase=2`로 고착시켜 이후 트리거가 모두 차단되는 버그가 발생했다. 40ms 타임아웃은 HC-SR04의 최대 echo 시간(38ms)에 2ms 마진을 더한 값이다.

---

### 2-4. fusion_work_fn — 센서 융합 판정

200ms 타이머마다 실행되는 핵심 로직이다.

```c
static void fusion_work_fn(struct work_struct *work)
{
    // 1. 광센서 ADC 읽기 (SPI, sleep 가능한 컨텍스트)
    light_value = adc_read_value(nd->spi, adc_channel);

    // 2. 공유 상태 스냅샷 (spinlock)
    spin_lock_irqsave(&nd->state_lock, flags);
    distance      = nd->distance_cm;
    prev_distance = nd->prev_distance_cm;
    distance_fresh = nd->distance_fresh;
    nd->distance_fresh = false;           // 소비 완료 표시

    // 3. EMA 필터 (광센서 노이즈 억제)
    // new_ema = old_ema × 7/8 + sample × 1/8
    nd->light_ema = nd->light_ema - (nd->light_ema >> 3) + light_value;
    nd->light_value = nd->light_ema >> 3;
    spin_unlock_irqrestore(&nd->state_lock, flags);

    // 4. 차단 판정
    diff = abs(distance - prev_distance);
    distance_stable = distance_fresh          // 새 측정값만 사용
                   && distance > 0
                   && distance < 50           // 탐지 범위 내
                   && diff <= 2;              // 이전 대비 2cm 이하 변화

    light_blocked = light_value < light_threshold;  // ADC < 1800
    raw_blocked   = distance_stable || light_blocked;

    // 5. streak 카운터 (오탐 방지)
    spin_lock_irqsave(&nd->state_lock, flags);
    nd->prev_distance_cm = distance;
    if (raw_blocked) { nd->block_streak++; nd->clear_streak = 0; }
    else             { nd->clear_streak++; nd->block_streak = 0; }

    if (nd->block_streak >= 5) new_blockage = BLOCK_BLOCKED; // 1초
    if (nd->clear_streak  >= 2) new_blockage = BLOCK_CLEAR;  // 400ms
    spin_unlock_irqrestore(&nd->state_lock, flags);

    // 6. FSM 상태 계산 + 이벤트 발행
    set_fused_state(nd, new_state, new_blockage, reason_flags);

    // 7. 다음 초음파 트리거
    trigger_ultrasonic(nd);
}
```

**`distance_fresh` 플래그의 역할**: 타이머가 echo 수신 전에 발동되면 `distance_cm`이 이전 값 그대로라 `diff=0`이 되어 `distance_stable=true`가 오탐된다. `distance_fresh`는 IRQ에서 새 거리 측정값이 들어왔을 때만 `true`로 세트되고, fusion 실행 시 `false`로 소비되어 이를 방지한다.

**EMA 필터 원리**:
```
light_ema는 실제 EMA값의 8배로 저장
업데이트: ema = ema - ema/8 + sample  →  ema = ema × 7/8 + sample × 1/8
출력:     light_value = ema / 8

시정수 = 8샘플 × 200ms = 1.6초
→ 50/60Hz 조명 깜빡임 완전 평탄화
```

---

### 2-5. EMA 필터 원리

**EMA 필터 원리**:
```
light_ema는 실제 EMA값의 8배로 저장
업데이트: ema = ema - ema/8 + sample  →  ema = ema × 7/8 + sample × 1/8
출력:     light_value = ema / 8

시정수 = 8샘플 × 200ms = 1.6초
→ 50/60Hz 조명 깜빡임 완전 평탄화
```

---

### 2-6. adc_read_value — MCP3208 SPI 통신

MCP3208은 12비트 8채널 SPI ADC다. 3바이트 명령/응답으로 동작한다.

```
MCP3208 SPI 프레임 (채널 0, Single-ended):
  TX: [0x06][0x00][0x00]
  RX: [don't care][0x0X][0xXX]
                     ↑      ↑
               상위 4비트  하위 8비트

결과: ((rx[1] & 0x0F) << 8) | rx[2]  →  0~4095
```

```c
// MCP3208 (12비트) 명령 구성
tx[0] = 0x06 | ((ch & 0x07) >> 2);  // START + SGL + D2
tx[1] = ((ch & 0x07) << 6);          // D1, D0, don't care

// 결과 파싱
return ((rx[1] & 0x0f) << 8) | rx[2];
```

---

### 2-7. push_event / set_fused_state — 이벤트 관리

```c
// set_fused_state: 상태가 실제로 바뀔 때만 이벤트 발행 (중복 억제)
static void set_fused_state(...)
{
    spin_lock_irqsave(&nd->state_lock, flags);
    if (nd->blockage != new_blockage) { changed = true; }
    if (nd->state    != new_state)    { changed = true; }
    spin_unlock_irqrestore(...);

    if (changed)
        push_event(nd, reason_flags);    // 유저스페이스 알림
    else
        atomic64_inc(&nd->suppressed_events);  // 통계만 증가
}

// push_event: 원형 큐에 이벤트 저장 + waitqueue 깨우기
static void push_event(...)
{
    // 큐 가득 차면 가장 오래된 항목 덮어씌움 (tail 전진)
    if (queue_full(nd))
        nd->q_tail = (nd->q_tail + 1) % EVENT_Q_SIZE;

    nd->q[nd->q_head] = ev;
    nd->q_head = (nd->q_head + 1) % EVENT_Q_SIZE;

    wake_up_interruptible(&nd->read_wq);  // read()에서 대기 중인 프로세스 깨움

    // CRITICAL_FAULT이면 LED ON
    gpio_set_value(nd->led_gpio, ev.state == CONV_CRITICAL_FAULT);
}
```

---

### 2-8. node_read — 유저스페이스 인터페이스

`cat /dev/conveyor_node0` 또는 `open() + read()`로 접근한다.

```c
static ssize_t node_read(struct file *file, char __user *buf, ...)
{
    // O_NONBLOCK이면 이벤트 없으면 즉시 -EAGAIN 반환
    // 블로킹이면 wait_event_interruptible()로 이벤트 대기

    // 원형 큐에서 이벤트 하나 꺼냄
    ev = nd->q[nd->q_tail];
    nd->q_tail = (nd->q_tail + 1) % EVENT_Q_SIZE;

    // 텍스트로 직렬화
    scnprintf(line, sizeof(line),
        "ts=%llu state=%s tilt=%s blockage=%s distance_cm=%d light=%d reason=0x%x\n",
        ...);

    copy_to_user(buf, line, len);  // 커널 → 유저 메모리 복사
}
```

`wait_event_interruptible()`은 이벤트가 없으면 CPU를 반납하고 슬립한다. `push_event()`의 `wake_up_interruptible()`이 호출될 때 깨어난다. 이 덕분에 `conveyor_event_daemon.py`의 `for line in dev:` 루프가 CPU 100% 폴링 없이 블로킹 대기한다.

---

## 3. conveyor_event_daemon.py — MQTT 퍼블리셔

드라이버 Pi에서 실행. `/dev/conveyor_node0` 이벤트를 MQTT로 발행한다.

### 3-1. 순수 Python stdlib MQTT 구현

`paho-mqtt` 같은 외부 패키지 없이 TCP 소켓으로 MQTT v3.1.1을 직접 구현했다. Pi에 패키지 설치가 불가능한 오프라인 환경을 위한 설계다.

```python
class MqttPublisher:
    def connect(self) -> None:
        # CONNECT 패킷 수동 조립
        # [0x10] = CONNECT 명령
        # bytes([4, 2, 0, 0]) = 프로토콜 버전4, clean-session, keepalive=0
        proto = _enc_str('MQTT') + bytes([4, 2, 0, 0])
        body  = proto + _enc_str(self.client_id)
        pkt   = bytes([0x10]) + _enc_remlen(len(body)) + body

        self._sock = socket.create_connection((self.host, self.port), timeout=10)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.sendall(pkt)

        # CONNACK 확인 (4바이트: 0x20, 0x02, 0x00, return_code)
        ack = _recvn(self._sock, 4)
        if ack[0] != 0x20 or ack[3] != 0:
            raise RuntimeError(f'CONNACK refused (rc={ack[3]})')

    def publish(self, topic: str, payload: str) -> None:
        # PUBLISH 패킷: [0x30] + remaining_len + topic + payload
        body = _enc_str(topic) + payload.encode()
        pkt  = bytes([0x30]) + _enc_remlen(len(body)) + body
        self._sock.sendall(pkt)
```

**keepalive=0**: MQTT keepalive를 비활성화해 이벤트가 없어도 브로커가 연결을 끊지 않는다. 컨베이어 이벤트는 가끔씩만 발생하기 때문에 필수적이다.

**가변 길이 인코딩 (`_enc_remlen`)**: MQTT remaining length는 1~4바이트 가변 인코딩을 사용한다. 각 바이트의 MSB가 1이면 다음 바이트가 있음을 의미한다.

```python
def _enc_remlen(n: int) -> bytes:
    out = []
    while True:
        byte = n & 0x7F          # 하위 7비트
        n >>= 7
        out.append(byte | (0x80 if n else 0))  # 계속되면 MSB=1
        if not n:
            return bytes(out)
```

### 3-2. 이벤트 파싱 및 발행 루프

```python
def main():
    while True:                                    # 브로커 재연결 루프
        with MqttPublisher(broker, port) as pub:
            with open('/dev/conveyor_node0', 'r') as dev:
                for line in dev:                   # 블로킹 read (이벤트 대기)
                    ev = parse_event(line)         # "key=value ..." → dict
                    ev['node_id'] = 'conveyor-1'
                    ev['created_at'] = time.time()
                    payload = json.dumps(ev)
                    pub.publish('iot/conveyor/state', payload)
```

`for line in dev:`는 커널의 `wait_event_interruptible()`과 짝을 이룬다. 새 이벤트가 없으면 파이썬 스레드는 `read()` 시스템콜에서 블로킹된다. 브로커 연결 실패 시 외부 while 루프에서 5초 후 재시도한다.

---

## 4. conveyor_subscriber.py — MQTT 섭스크라이버

섭스크라이버 Pi에서 실행. MQTT 메시지를 구독해 터미널에 출력한다.

### MQTT SUBSCRIBE 흐름

```python
def mqtt_messages(host, port, topic):
    while True:                                    # 재연결 루프
        sock = socket.create_connection((host, port))

        # CONNECT → CONNACK
        # SUBSCRIBE → SUBACK
        pkt_id = struct.pack('!H', 1)
        sub_payload = pkt_id + _enc_str(topic) + bytes([0])  # QoS=0
        sock.sendall(bytes([0x82]) + _enc_remlen(...) + sub_payload)
        _recvn(sock, 5)  # SUBACK

        sock.settimeout(60)
        while True:
            try:
                pkt_type = _recvn(sock, 1)[0] & 0xF0
                if pkt_type == 0x30:               # PUBLISH
                    tlen = struct.unpack('!H', data[:2])[0]
                    topic   = data[2:2+tlen].decode()
                    payload = data[2+tlen:].decode()
                    yield topic, payload
            except socket.timeout:
                sock.sendall(bytes([0xC0, 0x00]))  # PINGREQ
```

`socket.settimeout(60)`으로 60초 무응답 시 `socket.timeout` 예외가 발생한다. 이때 `PINGREQ(0xC0 0x00)`를 보내 연결이 살아있음을 브로커에 알린다.

---

## 5. conveyor_web.py — 웹 대시보드

섭스크라이버 Pi에서 실행. 포트 8080에서 HTTP 서버와 MQTT 클라이언트를 동시에 운용한다.

### 스레드 구조

```
메인 스레드
└─ HTTPServer.serve_forever()    ← 브라우저 요청 처리

_mqtt_reader 스레드 (daemon=True)
└─ MQTT 구독 루프
   └─ 메시지 수신 시 → _state 딕셔너리 갱신
                      → _sse_queues의 모든 클라이언트에 브로드캐스트
```

### Server-Sent Events (SSE)

브라우저가 `GET /events`를 열면 연결이 유지되고, 서버가 주도적으로 데이터를 전송한다.

```python
# 클라이언트 연결 시
q = queue.Queue()
_sse_queues.append(q)

# 이벤트 스트리밍
while True:
    data = q.get(timeout=25)         # 25초마다 keepalive 전송
    self.wfile.write(f'data: {data}\n\n'.encode())
    self.wfile.flush()

# MQTT 메시지 수신 시
for q in _sse_queues:
    q.put(json.dumps(event))         # 모든 브라우저 탭에 브로드캐스트
```

### 라우팅

| 경로 | 설명 |
|------|------|
| `GET /` | HTML 대시보드 (인라인 CSS/JS 포함) |
| `GET /events` | SSE 스트림 (text/event-stream) |
| `GET /api/state` | 현재 상태 JSON |

---

## 6. 동시성 설계

### 커널 드라이버의 락 구조

두 개의 spinlock으로 데이터 경쟁을 방지한다.

```
data_lock  : IRQ 핸들러 내부 원시 데이터 보호
             (last_tilt_edge_ns, echo_start, echo_phase 등)

state_lock : 융합된 상태 데이터 보호
             (tilt, blockage, state, distance_cm, light_value 등)
```

```
IRQ 핸들러 (하드웨어 컨텍스트)    workqueue (소프트웨어 컨텍스트)
─────────────────────────────     ─────────────────────────────
tilt_irq_handler                  tilt_eval_work_fn
  └─ data_lock 획득                 └─ data_lock / state_lock 획득
  └─ 원시 GPIO 값 저장              └─ tilt 상태 판정

ultra_irq_handler                 fusion_work_fn
  └─ data_lock 획득                 └─ state_lock 획득
  └─ echo 타이밍 계산               └─ 모든 센서 융합
  └─ state_lock 획득                └─ FSM 전이
  └─ distance_cm 갱신
```

`spinlock_irqsave`를 사용해 spinlock 보유 중 인터럽트를 비활성화한다. IRQ 핸들러와 workqueue가 같은 데이터를 접근할 때 데드락이 발생하지 않는다.

### 원형 이벤트 큐

크기 32의 정적 배열로 구성된 lock-free 스타일의 원형 큐다 (단, spinlock으로 직렬화됨).

```
head → 다음 쓸 위치
tail → 다음 읽을 위치

비어있음: head == tail
가득 참:  (head + 1) % 32 == tail

큐 가득 찰 경우: tail을 전진해 가장 오래된 이벤트 덮어씌움
→ 최신 이벤트 32개를 항상 유지
```

---

## 7. 설계 결정 및 트레이드오프

| 결정 | 이유 | 트레이드오프 |
|------|------|-------------|
| SPI 드라이버로 구현 | MCP3208 ADC를 커널에서 직접 제어 | spidev 언바인딩 필요 |
| workqueue 사용 (IRQ 핸들러에서 최소 작업) | IRQ 핸들러는 sleep 불가, SPI 읽기는 sleep 필요 | 약간의 지연 발생 |
| miscdevice (/dev/conveyor_node0) | 파일 인터페이스로 유저스페이스 연동 단순화 | 한 번에 하나의 이벤트만 읽힘 |
| hrtimer (high-resolution timer) | 정밀한 200ms 샘플링 주기 | jiffies 기반 timer보다 복잡 |
| echo-triggered fusion_work 제거 | 근거리 echo 100Hz 루프 방지 | echo 직후 판정 지연 최대 200ms |
| EMA 필터 (alpha=1/8) | 50/60Hz 조명 노이즈 억제 | 빠른 광 변화에 1.6초 지연 |
| distance_fresh 플래그 | 타이머 발동 시 stale 거리 오탐 방지 | echo 없으면 distance_stable=false 보장 |
| pure Python stdlib MQTT | 외부 패키지 설치 불필요 (오프라인 Pi) | MQTT 고급 기능(QoS 1/2 등) 미지원 |
| keepalive=0 | 이벤트 없는 긴 유휴 구간에서 연결 유지 | 브로커가 클라이언트 죽음을 감지 못함 |
| SSE (Server-Sent Events) | 단방향 실시간 스트림에 WebSocket보다 단순 | 서버→클라이언트 단방향만 가능 |
