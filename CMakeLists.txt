cmake_minimum_required(VERSION 3.22)

#
# This file is generated only once,
# and is not re-generated if converter is called multiple times.
#
# User is free to modify the file as much as necessary
#

# Setup compiler settings
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)


# Define the build type
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Debug")
endif()

# Set the project name
set(CMAKE_PROJECT_NAME WeActSTM32H743)
set(STM32CUBE_H7_ROOT "${CMAKE_SOURCE_DIR}/../Repository/STM32Cube_FW_H7_V1.13.0")

# Enable compile command to ease indexing with e.g. clangd
set(CMAKE_EXPORT_COMPILE_COMMANDS TRUE)

# Core project settings
project(${CMAKE_PROJECT_NAME})
message("Build type: " ${CMAKE_BUILD_TYPE})

# Enable CMake support for ASM and C languages
enable_language(C ASM)

# Create an executable object type
add_executable(${CMAKE_PROJECT_NAME})

# Add STM32CubeMX generated sources
add_subdirectory(cmake/stm32cubemx)

# Link directories setup
target_link_directories(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined library search paths
)

# Add sources to executable
target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    ${CMAKE_SOURCE_DIR}/Core/Src/intan_spi.c
    ${CMAKE_SOURCE_DIR}/Core/Src/intan_spi_diag.c
    ${CMAKE_SOURCE_DIR}/Core/Src/intan_spi4_hw.c
    ${CMAKE_SOURCE_DIR}/Core/Src/intan_app.c
    ${CMAKE_SOURCE_DIR}/Core/Src/intan_uart_cli.c
    ${CMAKE_SOURCE_DIR}/Core/Src/intan_stream.c
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

# Add include paths
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined include paths
    ${STM32CUBE_H7_ROOT}/Middlewares/ST/STM32_USB_Device_Library/Core/Inc
    ${STM32CUBE_H7_ROOT}/Drivers/STM32H7xx_HAL_Driver/Inc
)

# User: Intan RHS2116 on SPI2 (PE11 CS). OFF by default.
option(WITH_INTAN_HW "Intan RHS2116 soldered on SPI2" OFF)
# User: 32.768 kHz LSE on PC14/PC15. OFF if crystal not populated.
option(BOARD_HAS_LSE "Board has 32.768 kHz LSE on PC14/PC15" OFF)
# User: 480 MHz SYSCLK bench (VOS0). Default 240 MHz VSCALE2.
option(BOARD_SYSCLK_480 "Experimental 480 MHz SYSCLK for SPI bench" OFF)
if(BOARD_SYSCLK_480)
    message(STATUS "WeActSTM32H743: SYSCLK 480 MHz (VOS0 overdrive)")
else()
    message(STATUS "WeActSTM32H743: SYSCLK 240 MHz (VSCALE2)")
endif()

# Add project symbols (macros)
target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
    INTAN_HW_PRESENT=$<IF:$<BOOL:${WITH_INTAN_HW}>,1,0>
    BOARD_HAS_LSE=$<IF:$<BOOL:${BOARD_HAS_LSE}>,1,0>
    BOARD_SYSCLK_480=$<IF:$<BOOL:${BOARD_SYSCLK_480}>,1,0>
)

# Remove wrong libob.a library dependency when using cpp files
list(REMOVE_ITEM CMAKE_C_IMPLICIT_LINK_LIBRARIES ob)

# Add linked libraries
target_link_libraries(${CMAKE_PROJECT_NAME}
    stm32cubemx

    # Add user defined libraries
)
