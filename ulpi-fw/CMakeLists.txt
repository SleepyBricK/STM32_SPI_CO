cmake_minimum_required(VERSION 3.22)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Debug")
endif()

set(CMAKE_PROJECT_NAME WeActULPI)
set(CUBE_H7 "${CMAKE_CURRENT_SOURCE_DIR}/../../Repository/STM32Cube_FW_H7_V1.13.0")
set(HAL_LOCAL "${CMAKE_CURRENT_SOURCE_DIR}/../Drivers")

set(CMAKE_EXPORT_COMPILE_COMMANDS TRUE)

include(${CMAKE_CURRENT_SOURCE_DIR}/cmake/gcc-arm-none-eabi.cmake)

project(${CMAKE_PROJECT_NAME})
message("Build type: " ${CMAKE_BUILD_TYPE})

enable_language(C ASM)

add_executable(${CMAKE_PROJECT_NAME})

set(APP_CORE "${CMAKE_CURRENT_SOURCE_DIR}/Core")

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    ${APP_CORE}/Inc
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Inc
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Inc/Legacy
    ${HAL_LOCAL}/CMSIS/Device/ST/STM32H7xx/Include
    ${HAL_LOCAL}/CMSIS/Include
    ${CUBE_H7}/Middlewares/ST/STM32_USB_Device_Library/Core/Inc
    ${CUBE_H7}/Drivers/STM32H7xx_HAL_Driver/Inc
)

target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
    USE_PWR_LDO_SUPPLY
    USE_HAL_DRIVER
    STM32H743xx
    HSE_VALUE=8000000U
    $<$<CONFIG:Debug>:DEBUG>
)

target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    ${APP_CORE}/Src/main.c
    ${APP_CORE}/Src/gpio.c
    ${APP_CORE}/Src/usart.c
    ${APP_CORE}/Src/stm32h7xx_it.c
    ${APP_CORE}/Src/stm32h7xx_hal_msp.c
    ${APP_CORE}/Src/system_stm32h7xx.c
    ${APP_CORE}/Src/sysmem.c
    ${APP_CORE}/Src/syscalls.c
    ${APP_CORE}/Src/usb3300_ulpi_hw.c
    ${APP_CORE}/Src/usb_device.c
    ${APP_CORE}/Src/usbd_conf.c
    ${APP_CORE}/Src/usbd_desc.c
    ${APP_CORE}/Src/usbd_vendor_bulk.c
    ${APP_CORE}/Src/usb_cmd.c
    ${APP_CORE}/Src/usb_diag.c
    ${CMAKE_CURRENT_SOURCE_DIR}/startup_stm32h743xx.s
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_cortex.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_rcc.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_rcc_ex.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_flash.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_flash_ex.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_gpio.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_hsem.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_dma.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_dma_ex.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_mdma.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr_ex.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_exti.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart.c
    ${HAL_LOCAL}/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart_ex.c
    ${CUBE_H7}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pcd.c
    ${CUBE_H7}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pcd_ex.c
    ${CUBE_H7}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_ll_usb.c
    ${CUBE_H7}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_core.c
    ${CUBE_H7}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ctlreq.c
    ${CUBE_H7}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ioreq.c
)

list(REMOVE_ITEM CMAKE_C_IMPLICIT_LINK_LIBRARIES ob)
target_link_libraries(${CMAKE_PROJECT_NAME} m)

set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES
    ADDITIONAL_CLEAN_FILES ${CMAKE_PROJECT_NAME}.map
)
