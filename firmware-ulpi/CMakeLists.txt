cmake_minimum_required(VERSION 3.22)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Debug")
endif()

set(PARENT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/..")
set(CMAKE_PROJECT_NAME WeActSTM32H743_ULPI)
set(STM32CUBE_H7_ROOT "${PARENT_DIR}/../Repository/STM32Cube_FW_H7_V1.13.0")

set(CMAKE_EXPORT_COMPILE_COMMANDS TRUE)

include(${PARENT_DIR}/cmake/gcc-arm-none-eabi.cmake)
string(REGEX REPLACE "-T \"[^\"]+\"" "-T \"${PARENT_DIR}/STM32H743XX_FLASH.ld\"" CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS}")

project(${CMAKE_PROJECT_NAME})
message("Build type: " ${CMAKE_BUILD_TYPE})

enable_language(C ASM)

add_executable(${CMAKE_PROJECT_NAME})

set(ULPI_CORE "${CMAKE_CURRENT_SOURCE_DIR}/Core")
set(PARENT_CORE "${PARENT_DIR}/Core")

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    ${ULPI_CORE}/Inc
    ${PARENT_CORE}/Inc
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Inc
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Inc/Legacy
    ${PARENT_DIR}/Drivers/CMSIS/Device/ST/STM32H7xx/Include
    ${PARENT_DIR}/Drivers/CMSIS/Include
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Inc
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Inc
)

target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
    USE_PWR_LDO_SUPPLY
    USE_HAL_DRIVER
    STM32H743xx
    $<$<CONFIG:Debug>:DEBUG>
)

target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    ${ULPI_CORE}/Src/main.c
    ${ULPI_CORE}/Src/gpio_min.c
    ${ULPI_CORE}/Src/usart.c
    ${ULPI_CORE}/Src/stm32h7xx_it.c
    ${ULPI_CORE}/Src/stm32h7xx_hal_msp.c
    ${ULPI_CORE}/Src/usb_app_bulk.c
    ${ULPI_CORE}/Src/usbd_desc.c
    ${PARENT_CORE}/Src/usb3300_ulpi_hw.c
    ${PARENT_CORE}/Src/usb_device.c
    ${PARENT_CORE}/Src/usbd_conf.c
    ${PARENT_CORE}/Src/usbd_vendor_bulk.c
    ${PARENT_CORE}/Src/system_stm32h7xx.c
    ${PARENT_CORE}/Src/sysmem.c
    ${PARENT_CORE}/Src/syscalls.c
    ${PARENT_DIR}/startup_stm32h743xx.s
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_cortex.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_rcc.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_rcc_ex.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_flash.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_flash_ex.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_gpio.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_hsem.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_dma.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_dma_ex.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_mdma.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr_ex.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_exti.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart.c
    ${PARENT_DIR}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart_ex.c
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pcd.c
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pcd_ex.c
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_ll_usb.c
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_core.c
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ctlreq.c
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ioreq.c
)

list(REMOVE_ITEM CMAKE_C_IMPLICIT_LINK_LIBRARIES ob)

target_link_libraries(${CMAKE_PROJECT_NAME} m)

set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES
    ADDITIONAL_CLEAN_FILES ${CMAKE_PROJECT_NAME}.map
)
