cmake_minimum_required(VERSION 3.22)

# Toolchain должен быть задан до project(). Работает и без -DCMAKE_TOOLCHAIN_FILE=...
if(NOT CMAKE_TOOLCHAIN_FILE)
  set(CMAKE_TOOLCHAIN_FILE "${CMAKE_CURRENT_SOURCE_DIR}/cmake/gcc-arm-none-eabi.cmake"
      CACHE FILEPATH "GNU Arm Embedded toolchain")
endif()

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Debug")
endif()

set(CMAKE_PROJECT_NAME STM32H743)
set(STM32CUBE_H7_ROOT "${CMAKE_SOURCE_DIR}/../Repository/STM32Cube_FW_H7_V1.13.0")

set(CMAKE_EXPORT_COMPILE_COMMANDS TRUE)

project(${CMAKE_PROJECT_NAME})
message("Build type: " ${CMAKE_BUILD_TYPE})

enable_language(C ASM)

add_executable(${CMAKE_PROJECT_NAME})

add_subdirectory(cmake/stm32cubemx)

target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    ${CMAKE_SOURCE_DIR}/Core/Src/uart_diag.c
    ${CMAKE_SOURCE_DIR}/Core/Src/usb3300_ulpi_hw.c
    ${CMAKE_SOURCE_DIR}/Core/Src/usb_device.c
    ${CMAKE_SOURCE_DIR}/Core/Src/usbd_conf.c
    ${CMAKE_SOURCE_DIR}/Core/Src/usbd_desc.c
    ${CMAKE_SOURCE_DIR}/Core/Src/usbd_cdc_if.c
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_core.c
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ctlreq.c
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ioreq.c
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Class/CDC/Src/usbd_cdc.c
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pcd.c
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pcd_ex.c
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_ll_usb.c
)

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Inc
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Class/CDC/Inc
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Inc
)

list(REMOVE_ITEM CMAKE_C_IMPLICIT_LINK_LIBRARIES ob)

target_link_libraries(${CMAKE_PROJECT_NAME}
    stm32cubemx
)
