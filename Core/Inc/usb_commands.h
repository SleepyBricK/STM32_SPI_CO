#ifndef USB_COMMANDS_H
#define USB_COMMANDS_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  USB_CMD_NONE = 0,
  USB_CMD_PING,
  USB_CMD_SYNTH_STREAM,
  USB_CMD_SPI_STREAM,
  USB_CMD_SPI_STREAM_REAL,
  USB_CMD_SPI_STREAM_RR8,
  USB_CMD_SPI_STREAM_RR8_REAL,
  USB_CMD_SPI_TO_RAM,
  USB_CMD_SPI_TO_RAM_FAST,
  USB_CMD_SPI_TO_RAM_RR8,
  USB_CMD_SPI_RATE,
  USB_CMD_SPI_RATE_FAST,
  USB_CMD_SPI_RATE_RR8,
  USB_CMD_STOP,
  USB_CMD_STATS,
  USB_CMD_UNKNOWN
} UsbCommandId;

typedef struct {
  UsbCommandId id;
  uint32_t arg0;
  uint32_t arg1;
  uint32_t arg2;
} UsbCommand;

UsbCommand UsbCommands_ParseLine(const char *line);

#ifdef __cplusplus
}
#endif

#endif /* USB_COMMANDS_H */
