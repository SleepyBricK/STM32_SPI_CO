# USB HS Streaming V2 для STM32H743VIT6 + USB3300

## Гайд для реализации USB-связи с чистого листа

Этот документ — техническое задание для новой версии USB-транспорта. Он написан так, чтобы внешний кодогенератор/ИИ-агент не додумывал архитектуру самостоятельно.

Цель: передавать поток `RESPONSE` от Intan RHS2116 через STM32H743VIT6 в host по USB 2.0 High-Speed без просадки относительно уже полученных **~713 kS/s по SPI**.

Финальная система:

```text
Intan RHS2116 -> SPI2 -> STM32H743VIT6 -> USB3300 ULPI -> USB HS bulk -> host
```

Целевой поток:

```text
713000 samples/s * 2 bytes = 1.426 MB/s payload
```

Для USB 2.0 High-Speed это маленький поток. Если скорость получается десятки kS/s, проблема почти наверняка в прошивке, буферах, USB state machine или host-reader, а не в USB-шине.

---

# 1. Аппаратная конфигурация

## 1.1 MCU

```text
STM32H743VIT6
Cortex-M7
LQFP100
```

## 1.2 Кварцы

```text
STM32 HSE     = 8 MHz
STM32 LSE     = 32.768 kHz
USB3300 XTAL  = 24 MHz
```

## 1.3 UART debug

```text
PB6 -> USART1_TX
PB7 -> USART1_RX
115200 8N1
No flow control
```

## 1.4 Intan SPI2

```text
PA9   -> SPI2_SCK
PB14  -> SPI2_MISO
PC1   -> SPI2_MOSI
PE11  -> INTAN_CS / GPIO Output
```

`PE11` использовать как **обычный GPIO-CS**, а не аппаратный NSS.

Логическая транзакция RHS2116:

```text
CS low
16 bit SPI
16 bit SPI
CS high
```

То есть один 32-битный transfer Intan реализуется как `2x16 bit` под одним CS.

## 1.5 USB3300 / ULPI

```text
PC0   -> OTG_HS_ULPI_STP
PC2_C -> OTG_HS_ULPI_DIR
PC3_C -> OTG_HS_ULPI_NXT
PA3   -> OTG_HS_ULPI_D0
PA5   -> OTG_HS_ULPI_CLK
PB0   -> OTG_HS_ULPI_D1
PB1   -> OTG_HS_ULPI_D2
PB10  -> OTG_HS_ULPI_D3
PB11  -> OTG_HS_ULPI_D4
PB12  -> OTG_HS_ULPI_D5
PB13  -> OTG_HS_ULPI_D6
PB5   -> OTG_HS_ULPI_D7
```

USB-тракт:

```text
STM32H743 <-> ULPI <-> USB3300 <-> USBLC6 <-> USB-C/host
```

## 1.6 SWD

```text
PA13 -> SWDIO
PA14 -> SWCLK
NRST -> Reset
GND
3.3V / VTref
```

## 1.7 BOOT0

```text
BOOT0 = отдельный pin 94
BOOT0 -> 10k -> GND
BOOT0 -> кнопка/джампер -> 3.3V
```

---

# 2. CubeMX / CubeIDE настройки

## 2.1 RCC

```text
HSE = Crystal/Ceramic Resonator
LSE = Crystal/Ceramic Resonator
HSE input = 8 MHz
LSE input = 32.768 kHz
```

## 2.2 SYSCLK

Для bring-up достаточно:

```text
SYSCLK = 240 MHz
PLL1: M1=4, N1=240, P1=2
```

Для benchmark можно:

```text
SYSCLK = 480 MHz
PLL1: M1=4, N1=480, P1=2
```

Для 480 MHz выставить корректный Voltage Scale. Если CubeMX ругается, исправлять Power Regulator Voltage Scale, но не ломать USB/SPI clocks.

## 2.3 SPI2 clock

Цель:

```text
SPI2 SCK = 25 MHz
```

Настроить `SPI1/2/3 Clock Mux = PLL2Q`.

```text
PLL2: M2=4, N2=100, Q2=2
PLL2Q = 100 MHz
SPI2 prescaler = 4
SPI2 SCK = 25 MHz
```

## 2.4 USB clock

USB3300 физически использует свой кварц 24 MHz. Если CubeMX требует clock для USB core:

```text
PLL3: M3=4, N3=96, Q3=4
PLL3Q = 48 MHz
USB Clock Mux = PLL3Q
```

## 2.5 USB_OTG_HS

```text
USB_OTG_HS:
  Mode = Device Only
  Physical Interface = External PHY
  VBUS sensing = Disabled
```

VBUS sensing отключён, потому что плата питается от VBUS: если VBUS отсутствует, плата просто не включена.

---

# 3. USB-протокол V2

## 3.1 Класс USB

Использовать **Vendor-specific bulk**, не CDC.

```text
bInterfaceClass = 0xFF
```

## 3.2 Endpoints

```text
EP0        Control
EP 0x01    Bulk OUT  host -> STM32 commands
EP 0x81    Bulk IN   STM32 -> host stream
```

High-Speed bulk max packet size:

```text
512 bytes
```

## 3.3 Команды на Bulk OUT

Минимальный набор:

```text
PING
SYNTH_STREAM n
STOP
STATS
```

Ответы на команды можно слать по тому же Bulk IN, но лучше не смешивать текстовые ответы и поток в одной активной stream-сессии без явного framing.

---

# 4. Формат кадра RHS1

## 4.1 Размер

```text
USB_STREAM_FRAME_SIZE      = 4096 bytes
USB_STREAM_HEADER_SIZE     = 32 bytes
USB_STREAM_FRAME_RESPONSES = 2032
```

Проверка:

```text
32 + 2032 * 2 = 4096
```

## 4.2 C-структура

```c
#pragma pack(push, 1)

typedef struct __attribute__((aligned(32))) {
    uint32_t magic;                 // 0x52485331 = 'RHS1'
    uint16_t version;               // 1
    uint16_t flags;                 // 0 for now

    uint32_t frame_seq;             // 0,1,2,...
    uint32_t first_sample_counter;  // global counter of first sample
    uint32_t sample_count;          // <= 2032

    uint32_t spi_overflow_count;
    uint32_t usb_overflow_count;
    uint32_t reserved;

    uint16_t response[2032];
} UsbStreamFrame;

#pragma pack(pop)

_Static_assert(sizeof(UsbStreamFrame) == 4096, "UsbStreamFrame must be 4096 bytes");
```

## 4.3 Synthetic payload

Для `SYNTH_STREAM`:

```c
response[i] = (first_sample_counter + i) & 0xFFFF;
```

Host обязан валидировать каждый sample.

---

# 5. Главный принцип новой реализации

Новая версия должна быть построена как producer-consumer pipeline:

```text
Synthetic/SPI producer -> RHS1 frame ring -> USB bulk consumer -> host
```

Правила:

```text
Producer не ждёт USB.
USB не ждёт producer, если есть READY frames.
Frame нельзя перезаписывать, пока он передаётся.
USB transmit не должен блокировать SPI.
```

---

# 6. Самое важное изменение относительно старой версии

## 6.1 Сначала попробовать full-frame USB transfer

В новой реализации **не нужно вручную резать 4096-byte frame на 512-byte chunks**, если USB low-level stack позволяет передать 4096 B одним transfer.

Правильная первая попытка:

```c
USBD_LL_Transmit(&hUsbDeviceHS, 0x81, (uint8_t *)frame, 4096);
```

или wrapper:

```c
USBD_VENDOR_BULK_TransmitFrame((uint8_t *)frame, sizeof(UsbStreamFrame));
```

Смысл:

```text
Endpoint MPS = 512 B.
Transfer length = 4096 B.
USB core сам разбивает transfer на 8 packets по 512 B.
DataIn должен означать завершение transfer.
```

Это убирает старый класс ошибок с пропуском ровно 512 bytes.

## 6.2 Если full-frame transfer не работает

Только тогда реализовать chunk pipeline с `queued_off` и `completed_off`. Не возвращаться к последовательной схеме:

```text
512 B -> ждать DataIn -> 512 B -> ждать DataIn
```

Она уже показала десятки kS/s и не годится для 713 kS/s.

---

# 7. Frame ring

## 7.1 Состояния кадра

```c
typedef enum {
    FRAME_FREE = 0,
    FRAME_FILLING,
    FRAME_READY,
    FRAME_TX_BUSY
} frame_state_t;
```

## 7.2 Буфер кадров

Рекомендуется:

```c
#define USB_FRAME_COUNT 64

static UsbStreamFrame g_frames[USB_FRAME_COUNT]
    __attribute__((section(".dma_buffer"), aligned(32)));

static volatile frame_state_t g_frame_state[USB_FRAME_COUNT];
```

Если RAM мало:

```c
#define USB_FRAME_COUNT 32
```

При 713 kS/s:

```text
1 frame = 2032 samples
2032 / 713000 = 2.85 ms
32 frames = 91 ms buffer
64 frames = 182 ms buffer
```

---

# 8. Full-frame TX state machine

## 8.1 TX state

```c
typedef struct {
    UsbStreamFrame *frame;
    uint8_t active;
} UsbTxState;

static UsbTxState g_usb_tx;
```

## 8.2 TX pump

```c
static void usb_stream_tx_pump(void)
{
    if (g_usb_tx.active) {
        return;
    }

    UsbStreamFrame *frame = stream_ring_get_next_ready();
    if (frame == NULL) {
        return;
    }

    stream_ring_mark_tx_busy(frame);

    usb_cache_clean_frame(frame); // no-op if .dma_buffer is non-cacheable

    USBD_StatusTypeDef st =
        USBD_VENDOR_BULK_TransmitFrame((uint8_t *)frame, sizeof(UsbStreamFrame));

    if (st != USBD_OK) {
        stream_ring_mark_ready(frame); // do not lose frame
        return;
    }

    g_usb_tx.frame = frame;
    g_usb_tx.active = 1;
}
```

## 8.3 DataIn complete

```c
void usb_stream_on_frame_tx_complete(void)
{
    if (!g_usb_tx.active || g_usb_tx.frame == NULL) {
        g_usb_error_count++;
        return;
    }

    stream_ring_mark_free(g_usb_tx.frame);

    g_usb_tx.frame = NULL;
    g_usb_tx.active = 0;

    usb_stream_tx_pump();
}
```

## 8.4 Инварианты

```text
Frame release только после DataIn complete.
Если TransmitFrame() вернул BUSY/FAIL, frame остаётся READY.
Frame в состоянии TX_BUSY нельзя перезаписывать.
```

---

# 9. Fallback: безопасный chunk pipeline

Использовать только если full-frame `USBD_LL_Transmit(..., 4096)` не работает.

## 9.1 TX state

```c
typedef struct {
    UsbStreamFrame *frame;

    uint32_t queued_off;       // поставлено в USB/software queue
    uint32_t completed_off;    // подтверждено DataIn

    uint8_t active;
    uint8_t chunks_in_flight;
} UsbChunkTxState;
```

## 9.2 Инварианты

```text
queued_off двигается только если chunk принят в vendor queue.
completed_off двигается только по DataIn.
Frame освобождается только completed_off >= 4096.
```

## 9.3 Pump

```c
#define USB_MPS 512
#define STREAM_MAX_CHUNKS_IN_FLIGHT 4

static void usb_chunk_tx_pump(void)
{
    if (!tx.active) {
        UsbStreamFrame *f = stream_ring_get_next_ready();
        if (!f) return;

        stream_ring_mark_tx_busy(f);

        tx.frame = f;
        tx.queued_off = 0;
        tx.completed_off = 0;
        tx.chunks_in_flight = 0;
        tx.active = 1;
    }

    while (tx.active &&
           tx.chunks_in_flight < STREAM_MAX_CHUNKS_IN_FLIGHT &&
           tx.queued_off < sizeof(UsbStreamFrame)) {

        uint32_t remaining = sizeof(UsbStreamFrame) - tx.queued_off;
        uint32_t len = remaining > USB_MPS ? USB_MPS : remaining;
        uint8_t *ptr = ((uint8_t *)tx.frame) + tx.queued_off;

        USBD_StatusTypeDef st = USBD_VENDOR_BULK_TransmitChunk(ptr, len);

        if (st != USBD_OK) {
            break;
        }

        tx.queued_off += len;
        tx.chunks_in_flight++;
    }
}
```

## 9.4 DataIn

```c
void usb_stream_on_chunk_tx_complete(uint32_t done_len)
{
    if (!tx.active) {
        g_usb_error_count++;
        return;
    }

    if (tx.chunks_in_flight == 0) {
        g_usb_error_count++;
        return;
    }

    tx.chunks_in_flight--;
    tx.completed_off += done_len;

    if (tx.completed_off >= sizeof(UsbStreamFrame)) {
        stream_ring_mark_free(tx.frame);

        tx.frame = NULL;
        tx.active = 0;
        tx.queued_off = 0;
        tx.completed_off = 0;
        tx.chunks_in_flight = 0;
    }

    usb_chunk_tx_pump();
}
```

## 9.5 Настройка pipeline depth

Тестировать по шагам:

```text
STREAM_MAX_CHUNKS_IN_FLIGHT = 2
потом 4
потом 8
```

Не начинать сразу с 8/16.

---

# 10. Vendor bulk layer

## 10.1 Минимальные функции

```c
USBD_StatusTypeDef USBD_VENDOR_BULK_TransmitFrame(uint8_t *buf, uint32_t len);
USBD_StatusTypeDef USBD_VENDOR_BULK_TransmitChunk(uint8_t *buf, uint32_t len);
void USBD_VENDOR_BULK_SetTxCompleteCallback(...);
```

Если используется только full-frame transfer, `TransmitChunk` не нужен.

## 10.2 Очередь

Если есть software queue, удалять элемент из очереди только после успешного `USBD_LL_Transmit`.

Правильно:

```c
item = queue_peek();
st = USBD_LL_Transmit(...);
if (st == USBD_OK) {
    queue_pop();
}
```

Неправильно:

```c
item = queue_pop();
st = USBD_LL_Transmit(...);
if (st != USBD_OK) {
    // item lost
}
```

## 10.3 EP busy

Если endpoint занят:

```text
Transmit возвращает BUSY.
Данные не терять.
Offset не двигать.
Frame не освобождать.
Повторить позже из pump.
```

---

# 11. Cache / MPU

## 11.1 Проблема

STM32H743 = Cortex-M7 с D-Cache. DMA/USB и CPU могут видеть разные данные, если буфер cacheable.

Симптомы:

```text
старые данные
битые данные
повторяющиеся frame
ложные mismatch
```

## 11.2 Рекомендуемое решение

Все stream buffers положить в non-cacheable MPU region:

```text
.dma_buffer -> RAM_D2 -> non-cacheable
```

Туда:

```text
UsbStreamFrame ring
SPI RX/TX buffers
USB DMA buffers
```

## 11.3 Если буфер cacheable

Перед USB IN:

```c
SCB_CleanDCache_by_Addr((uint32_t *)addr_aligned, len_aligned);
```

После SPI RX / USB OUT:

```c
SCB_InvalidateDCache_by_Addr((uint32_t *)addr_aligned, len_aligned);
```

Адрес и длина должны быть выровнены по 32 bytes.

---

# 12. SYNTH_STREAM

## 12.1 Команда

```text
SYNTH_STREAM n\n
```

## 12.2 Поведение STM32

```text
1. reset stream state
2. clear frame ring
3. clear counters
4. generate n samples
5. pack into RHS1 frames
6. send frames over Bulk IN
7. last frame may have sample_count < 2032
```

## 12.3 FillSynth

```c
static void UsbStreamFrame_FillSynth(
    UsbStreamFrame *f,
    uint32_t frame_seq,
    uint32_t first_sc,
    uint32_t count,
    uint32_t spi_ovf,
    uint32_t usb_ovf
) {
    f->magic = 0x52485331;
    f->version = 1;
    f->flags = 0;
    f->frame_seq = frame_seq;
    f->first_sample_counter = first_sc;
    f->sample_count = count;
    f->spi_overflow_count = spi_ovf;
    f->usb_overflow_count = usb_ovf;
    f->reserved = 0;

    for (uint32_t i = 0; i < count; i++) {
        f->response[i] = (uint16_t)((first_sc + i) & 0xFFFF);
    }

    for (uint32_t i = count; i < 2032; i++) {
        f->response[i] = 0;
    }
}
```

## 12.4 VerifySynth before TX

Проверять до отправки:

```c
static int UsbStreamFrame_VerifySynth(const UsbStreamFrame *f)
{
    static const uint32_t idxs[] = {
        0, 1, 239, 240, 241, 496, 1007, 1008, 1264, 2031
    };

    if (f->magic != 0x52485331) return -1;
    if (f->version != 1) return -2;
    if (f->sample_count > 2032) return -3;

    for (unsigned k = 0; k < sizeof(idxs)/sizeof(idxs[0]); k++) {
        uint32_t i = idxs[k];
        if (i >= f->sample_count) continue;

        uint16_t want = (uint16_t)((f->first_sample_counter + i) & 0xFFFF);
        if (f->response[i] != want) return -10;
    }

    return 0;
}
```

---

# 13. Main loop

Минимальный main loop:

```c
while (1) {
    UsbVendorBulk_ProcessOutCommands();
    UsbStreamService_Process();
    UsbStreamService_TxPump();
    Stats_PrintOncePerSecond();
}
```

Запрещено:

```text
UART logging из USB ISR
blocking wait внутри USB transmit
blocking wait внутри SPI producer
```

---

# 14. Real Intan integration

Не подключать реальный SPI path, пока synthetic USB не держит целевую скорость.

Потом схема:

```text
SPI2 DMA block complete
  -> extract 16-bit RESPONSE
  -> stream_push_response(response)
```

`stream_push_response()` должен только писать response в текущий frame.

Если нет свободного frame:

```text
не ждать USB
инкрементировать usb_overflow_count
для benchmark: можно stop-on-overflow
для финала: drop + counters
```

---

# 15. Host-side reader

## 15.1 Проверка скорости USB

```bash
lsusb -t
```

Должно быть:

```text
480M
```

Если `12M` — остановиться и чинить HS/ULPI.

## 15.2 libusb async

Использовать:

```text
endpoint = 0x81
transfer size = 4096
transfers in flight = 16..32
```

Host callback:

```text
1. проверить actual_length == 4096
2. проверить magic/version/frame_seq/sample_counter
3. положить frame в host ring
4. сразу re-submit transfer
```

Не писать UDP/file прямо в USB callback.

---

# 16. Тест-план

## 16.1 Ping

```bash
python3 tools/usb_intan_cmd.py PING
```

Ожидаемо:

```text
PONG
```

## 16.2 Short validation

```bash
python3 tools/usb_frame_bench.py -n 50000 --no-reset --runs 5
```

Ожидаемо:

```text
0 validation errors
usb_overflow=0
spi_overflow=0
```

## 16.3 Medium validation

```bash
python3 tools/usb_frame_bench.py -n 500000 --no-reset --runs 3
```

## 16.4 Long validation

```bash
python3 tools/usb_frame_bench.py -n 5000000 --no-reset --runs 1
```

## 16.5 Speed target

Для 4096-byte frames:

```text
713 kS/s -> около 351 frames/s
351 * 4096 = 1.438 MB/s по USB
```

Если synthetic stream меньше 500 kS/s — USB path ещё не готов.

---

# 17. Диагностика ошибок

## 17.1 Mismatch на i=240

```text
offset = 32 + 240*2 = 512
```

Это граница второго USB HS packet.

Вероятно:

```text
пропуск/повтор 512-byte chunk
```

## 17.2 Mismatch на i=1008

```text
offset = 32 + 1008*2 = 2048
```

Это граница пятого USB packet.

Если:

```text
got - want = 0x0100
```

значит пропущено:

```text
256 samples = 512 bytes
```

## 17.3 frame_seq скачет

Вероятно:

```text
frame потерян или буфер перезаписан до завершения TX
```

## 17.4 sample_counter скачет, frame_seq нормальный

Вероятно:

```text
ошибка producer/frame builder/overflow policy
```

## 17.5 usb_overflow растёт

```text
USB/host не успевает или frame ring мал
```

## 17.6 spi_overflow растёт

```text
SPI acquisition path не успевает
```

---

# 18. Структура файлов

Рекомендуемая новая структура:

```text
Core/Inc/usb_stream_frame.h
Core/Src/usb_stream_frame.c

Core/Inc/usb_stream_ring.h
Core/Src/usb_stream_ring.c

Core/Inc/usb_vendor_bulk.h
Core/Src/usb_vendor_bulk.c

Core/Inc/usb_stream_service.h
Core/Src/usb_stream_service.c

Core/Inc/usb_commands.h
Core/Src/usb_commands.c

Core/Inc/intan_stream.h
Core/Src/intan_stream.c
```

Назначение:

```text
usb_stream_frame.*   RHS1 struct, FillSynth, VerifySynth
usb_stream_ring.*    frame states/ring operations
usb_vendor_bulk.*    descriptors/endpoints/Bulk IN/Bulk OUT
usb_stream_service.* commands, synthetic producer, TX pump
usb_commands.*       text command parser
intan_stream.*       будущий SPI2/RHS2116 producer
```

---

# 19. Прямое задание для Composer/агента

Сформулировать задачу так:

```text
Написать новую USB streaming subsystem с нуля.

Железо:
STM32H743VIT6 + USB3300 ULPI.
USB_OTG_HS Device Only, External PHY.
Bulk IN EP 0x81, Bulk OUT EP 0x01.

Не использовать CDC.
Не использовать старую chunk state machine.
Не отправлять sample-by-sample.

Формат stream:
RHS1 frame = 4096 bytes.
Header = 32 bytes.
Payload = 2032 uint16 response.
Synthetic sample[i] = first_sample_counter + i.

Сначала реализовать SYNTH_STREAM n.
Для IN передачи сначала использовать full-frame transfer:
USBD_LL_Transmit(..., frame, 4096).
Frame release только после DataIn complete.
Если transmit busy/fail — frame не терять.

Если full-frame transfer не работает, реализовать chunk pipeline с queued_off/completed_off.
Никогда не двигать completed_off до DataIn.

Все stream buffers в .dma_buffer non-cacheable или делать DCache clean/invalidate.
UART logs только редко и не из ISR.
```

---

# 20. Критерии готовности

## Correctness

```text
50000 samples, 5 runs, 0 errors
500000 samples, 3 runs, 0 errors
5000000 samples, 1 run, 0 errors
```

## Speed

```text
SYNTH_STREAM >= 700 kS/s
```

## USB

```text
lsusb -t -> 480M
```

---

# 21. Финальный принцип

Правильная система:

```text
producer continuously fills frames
USB consumer continuously drains frames
host continuously has pending IN transfers
```

Неправильная система:

```text
512 B -> wait DataIn -> 512 B -> wait DataIn
```

Нужно добиться, чтобы для host один RHS1 frame всегда приходил как:

```text
ровно 4096 bytes
правильный header
правильный frame_seq
правильный first_sample_counter
правильный payload
```

Если это работает на `SYNTH_STREAM` со скоростью около 713 kS/s, только тогда подключать реальный SPI/Intan path.
