#ifndef USB_DEVICE_H
#define USB_DEVICE_H

#ifdef __cplusplus
extern "C" {
#endif

void USB_DEVICE_Init(void);
void USB_DEVICE_FinalizeAttach(void);
void USB_DEVICE_PollEvents(void);

#ifdef __cplusplus
}
#endif

#endif /* USB_DEVICE_H */
