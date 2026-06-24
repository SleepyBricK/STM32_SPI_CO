cmake_minimum_required(VERSION 3.22)

project(WeActSTM32H743 LANGUAGES C ASM)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS TRUE)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE "Debug")
endif()

set(STM32CUBE_H7_ROOT "${CMAKE_SOURCE_DIR}/../Repository/STM32Cube_FW_H7_V1.13.0")

message(STATUS "Build type: ${CMAKE_BUILD_TYPE}")

execute_process(
  COMMAND git rev-parse --short=12 HEAD
  WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  OUTPUT_VARIABLE WEACT_GIT_HASH
  OUTPUT_STRIP_TRAILING_WHITESPACE
  ERROR_QUIET
)
if(NOT WEACT_GIT_HASH)
  set(WEACT_GIT_HASH "unknown")
endif()

add_executable(${PROJECT_NAME})
add_subdirectory(cmake/stm32cubemx)

option(WITH_INTAN_HW "Intan RHS2116 soldered on SPI2" OFF)
option(INTAN_CS_HW_NSS "Intan CS via SPI2_NSS PA11 (pulsed NSS)" ON)
option(BOARD_HAS_LSE "Board has 32.768 kHz LSE on PC14/PC15" OFF)
option(BOARD_SYSCLK_480 "480 MHz SYSCLK (VOS0); OFF = 240 MHz VSCALE2" ON)

if(BOARD_SYSCLK_480)
  message(STATUS "WeActSTM32H743: SYSCLK 480 MHz (VOS0 overdrive)")
else()
  message(STATUS "WeActSTM32H743: SYSCLK 240 MHz (VSCALE2)")
endif()

target_sources(${PROJECT_NAME} PRIVATE
  ${CMAKE_SOURCE_DIR}/Core/Src/intan_spi.c
  ${CMAKE_SOURCE_DIR}/Core/Src/intan_spi_diag.c
  ${CMAKE_SOURCE_DIR}/Core/Src/intan_spi4_hw.c
  ${CMAKE_SOURCE_DIR}/Core/Src/intan_app.c
  ${CMAKE_SOURCE_DIR}/Core/Src/intan_pattern.c
  ${CMAKE_SOURCE_DIR}/Core/Src/intan_uart_cli.c
  ${CMAKE_SOURCE_DIR}/Core/Src/intan_stream.c
  ${CMAKE_SOURCE_DIR}/Core/Src/intan_fw_acq.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usb_stream_frame.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usb_stream_ring.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usb_stream_service.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usb_commands.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usb_vendor_bulk.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usb3300_ulpi_hw.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usb_device.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usbd_conf.c
  ${CMAKE_SOURCE_DIR}/Core/Src/usbd_desc.c
  ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_core.c
  ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ctlreq.c
  ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ioreq.c
  ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pcd.c
  ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pcd_ex.c
  ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_ll_usb.c
)

target_include_directories(${PROJECT_NAME} PRIVATE
  ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Inc
  ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Inc
)

target_compile_definitions(${PROJECT_NAME} PRIVATE
  BUILD_TYPE=\"${CMAKE_BUILD_TYPE}\"
  BUILD_GIT_HASH=\"${WEACT_GIT_HASH}\"
  INTAN_HW_PRESENT=$<IF:$<BOOL:${WITH_INTAN_HW}>,1,0>
  INTAN_CS_HW_NSS=$<IF:$<BOOL:${INTAN_CS_HW_NSS}>,1,0>
  BOARD_HAS_LSE=$<IF:$<BOOL:${BOARD_HAS_LSE}>,1,0>
  BOARD_SYSCLK_480=$<IF:$<BOOL:${BOARD_SYSCLK_480}>,1,0>
)

list(REMOVE_ITEM CMAKE_C_IMPLICIT_LINK_LIBRARIES ob)
target_link_libraries(${PROJECT_NAME} stm32cubemx)
