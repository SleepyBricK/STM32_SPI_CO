/*
  Intan Technologies RHD STM32 Firmware Framework
  Version 1.2

  Copyright (c) 2025 Intan Technologies

  This file is part of the Intan Technologies RHD STM32 Firmware Framework.

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the “Software”), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in
  all copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
  THE SOFTWARE.


  See <http://www.intantech.com> for documentation and product information.
  
 */

#include "userfunctions.h"
#include <math.h>

volatile uint16_t command_sequence_MOSI[CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE] = {0};
volatile uint32_t command_sequence_MISO[CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE] = {0};
volatile uint16_t next_aux_commands[AUX_COMMANDS_PER_SEQUENCE] = {0};

uint16_t sample_counter = 0;
uint16_t *sample_memory = NULL;
uint32_t per_channel_sample_memory_capacity = 20000;

uint16_t aux_command_list[AUX_COMMANDS_PER_SEQUENCE][AUX_COMMAND_LIST_LENGTH] = {{0}};

volatile bool main_loop_active 		    = false;
volatile bool uart_ready                = true;
volatile bool sample_interrupt_occurred = false;
volatile bool main_pin_status 		    = false;

RHDConfigParameters parameters;

static uint8_t aux_command_index = 0;
static volatile TransferState command_transfer_state = TRANSFER_COMPLETE;
static volatile bool reception_in_progress = false;

static inline bool extract_error_code_bit(ErrorCode error_code, int bit)
{
	return (error_code & (0b1 << bit)) >> bit;
}


// Return true if a command entered into MOSI at the specified position will, due to the 2-command
// pipeline delay, have its result appear at MISO during the next timestep.
// Otherwise, return false.
static inline bool result_is_delayed(int8_t command_slot)
{
	return (command_slot + 2 >= CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);
}


// Begin receiving MISO data (RHD -> SPI -> DMA -> memory) and transmitting MOSI data (memory -> DMA -> SPI -> RHD).
static void transfer_sequence_spi_dma(void)
{
#ifdef USE_HAL
	// HAL handles all of SPI DMA transfer with this single function call.

	// Note: this HAL function call seems to not be consistent in how long it takes, causing some jitter between Interrupt_Monitor_Pin (GPIO) and SPI signals.
	// However, SPI/DMA signals seem to be consistent with each other, so this shouldn't affect functionality.
	if (HAL_SPI_Receive_DMA(&RECEIVE_SPI, (uint8_t*)command_sequence_MISO,
			CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE) != HAL_OK)
	{
		Error_Handler();
	}

	if (HAL_SPI_Transmit_DMA(&TRANSMIT_SPI, (uint8_t*)command_sequence_MOSI,
			CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE) != HAL_OK)
	{
		Error_Handler();
	}
#else
	begin_spi_rx(LL_DMA_MEMORY_INCREMENT, (uint32_t) command_sequence_MISO, CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);
	begin_spi_tx(LL_DMA_MEMORY_INCREMENT, (uint32_t) command_sequence_MOSI, CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);
#endif
}


// Callback function that executes when Reception of SPI has completed.
static void spi_rx_cplt_callback(void)
{
	// If main loop is active, drive Main_Monitor_Pin low, write data to memory, transmit data in realtime, and update command_transfer_state
	if (main_loop_active) {
		// Indicate main loop is not currently processing by writing Main_Monitor_Pin Low.
		write_pin(Main_Monitor_GPIO_Port, Main_Monitor_Pin, false);
		main_pin_status = false;

		// User-specified function - here is where specified channel(s) can be written to memory.
		write_data_to_memory();

		// User-specified function - here is where user can transmit data in real time every sample period.
		transmit_data_realtime();

		// Copy the MOSI commands currently in next_aux_commands into actual MOSI memory accessible via DMA.
		copy_next_aux_commands_to_MOSI();

		// Update state variable to show that transfer has completed.
		command_transfer_state = TRANSFER_COMPLETE;
	}

	// If main loop is not active, that indicates just a single SPI DMA transfer has occurred, so set reception_in_progress to 0
	else {
#ifdef USE_HAL
#else
		end_spi_rx();
#endif
		reception_in_progress = false;
	}
}


// Callback function to show that an SPI error occurred.
static inline void spi_error_callback(void)
{
	command_transfer_state = TRANSFER_ERROR;
}


// Send provided 16-bit word 'tx_data' over SPI, and pass resultant 16-bit received work by reference.
// Note that the pipelined nature of the SPI communication has a 2-command delay,
// so the obtained results correspond to the command from 2 transactions earlier.
static void send_receive_spi_command(uint16_t tx_data, uint16_t* const rx_data_A, uint16_t* const rx_data_B)
{
	uint32_t rx_data = 0;
	reception_in_progress = true;
#ifdef USE_HAL
	if (HAL_SPI_Receive_DMA(&RECEIVE_SPI, (uint8_t*) &rx_data, 1) != HAL_OK)
	{
		Error_Handler();
	}

	if (HAL_SPI_Transmit_DMA(&TRANSMIT_SPI, (uint8_t*) &tx_data, 1) != HAL_OK)
	{
		Error_Handler();
	}

#else
	begin_spi_rx(LL_DMA_MEMORY_NOINCREMENT, (uint32_t) &rx_data, 1);
	begin_spi_tx(LL_DMA_MEMORY_NOINCREMENT, (uint32_t) &tx_data, 1);
#endif
	while (reception_in_progress) {}
	extract_ddr_words(rx_data, rx_data_A, rx_data_B);
}


// Every sample period, cycle circularly through aux_command_list, copying this sample's AUX commands to the
// 'next_aux_commands' array, which will ultimately be copied to the end of command_sequence_MOSI array when DMA
// is not accessing the memory, so that for the next timestep, these commands will be transmitted via DMA to SPI.
static inline void cycle_aux_commands(void)
{
	for (int i = 0; i < AUX_COMMANDS_PER_SEQUENCE; i++) {
		next_aux_commands[i] = aux_command_list[i][aux_command_index];
	}
	if (++aux_command_index >= AUX_COMMAND_LIST_LENGTH) {
		aux_command_index = 0;
	}

	// Note that if any command(s) are to be used with a command list different from AUX_COMMAND_LIST_LENGTH,
	// the above code should be commented out, and the last AUX_COMMANDS_PER_SEQUENCE to go to command_sequence_MOSI
	// should be written here. For example, if impedance check DAC control is used, zcheck_DAC_command_list_length
	// should replace AUX_COMMAND_LIST_LENGTH and zcheck_DAC_command_slot_position should be used to correctly index
	// commands from the proper aux_command_list slot.
}


// Handle communication error.
// Write ERROR_DETECTED_PIN (by default, red LED) High.
// Write each bit of a 4-bit error code to a pin so that by measuring pins, user can determine the error code.
// Enter an infinite loop, halting execution and allowing user to measure error pins.
void handle_error(ErrorCode error_code)
{
	// No error, just return.
	if (error_code == 0) return;

	// Write ERROR_DETECTED_PIN (by default red LED) to communicate that an error occurred.
	write_pin(ERROR_DETECTED_PORT, ERROR_DETECTED_PIN, true);

	// Write 4 bits of error code to 4 pins.
	write_pin(ErrorCode_Bit_0_GPIO_Port, ErrorCode_Bit_0_Pin, extract_error_code_bit(error_code, 0));
	write_pin(ErrorCode_Bit_1_GPIO_Port, ErrorCode_Bit_1_Pin, extract_error_code_bit(error_code, 1));
	write_pin(ErrorCode_Bit_2_GPIO_Port, ErrorCode_Bit_2_Pin, extract_error_code_bit(error_code, 2));
	write_pin(ErrorCode_Bit_3_GPIO_Port, ErrorCode_Bit_3_Pin, extract_error_code_bit(error_code, 3));

	// Enter infinite loop.
	while(1);
}


// Safely copy 'next_aux_commands' to end of command_sequence_MOSI so that these aux commands will be transmitted via DMA
// to SPI. It's important that this function is only called when SPI/DMA is not currently accessing command_sequence_MOSI,
// so only call use this before the main loop starts or immediately after the previous SPI/DMA transfer has completed.
void copy_next_aux_commands_to_MOSI(void)
{
	// Even though this being called after DMA transfer is done means that command_sequence_MOSI's "volatile" qualifier
	// can be safely ignored (only a concern if modifying memory during DMA transfer), using memcpy with a volatile
	// parameter is not recommended, so manually copy all commands from next_aux_commands to command_sequence_MOSI
	// one at a time.

	// Basically doing a memcpy, but element by element:
	//memcpy((uint16_t*) &command_sequence_MOSI[AUX_OFFSET], (uint16_t*) next_aux_commands, AUX_COMMANDS_PER_SEQUENCE * sizeof(uint16_t));

	for (int i = 0; i < AUX_COMMANDS_PER_SEQUENCE; i++) {
		command_sequence_MOSI[AUX_OFFSET + i] = next_aux_commands[i];
	}
}


void sample_processing_routine(void)
{
	sample_interrupt_occurred = false;

	// Indicate main loop is not currently processing by writing Main_Monitor_Pin Low.
	// Main loop will write Main_Monitor_Pin when processing returns to main, so the duty cycle of this pin
	// can be measured to estimate what percentage of clock cycles are available for main processing.
	write_pin(Main_Monitor_GPIO_Port, Main_Monitor_Pin, false);
	main_pin_status = false;

	// Indicate start of timer interrupt by writing Interrupt_Monitor_Pin High.
	// At the end of this function, Interrupt_Monitor_Pin will be written Low (though, keep in mind that
	// this only indicates that the DMA transfer has been initiated - DMA will continue running either until
	// its SPI command sequence concludes, or the next interrupt occurs, causing a SampleClip error).
	write_pin(Interrupt_Monitor_GPIO_Port, Interrupt_Monitor_Pin, true);

	// If previous DMA transfer has not completed, SPI communication from previous sample has not finished.
	// This is a critical error that will halt execution. To avoid this, all processing from previous interrupt
	// must conclude sooner (most likely, this would be waiting on SPI transfer completion, in which case
	// fewer channels can be included in the command sequence, or the SPI communication itself must be sped up).
	if (command_transfer_state == TRANSFER_WAIT) {
		handle_error(SampleClip);
	}

	// Update variable indicating to wait until SPI DMA transfer completes.
	command_transfer_state = TRANSFER_WAIT;

	transfer_sequence_spi_dma();

	// Write aux commands to command_sequence_MOSI, advancing one sample through aux_command_list.
	// Cycles through pre-defined aux commands in aux_command_list
	cycle_aux_commands();

	// SPI DMA transfer has begun, so write Interrupt_Monitor_Pin Low and exit interrupt function,
	// returning to processing main loop.
	write_pin(Interrupt_Monitor_GPIO_Port, Interrupt_Monitor_Pin, false);

	if (sample_interrupt_occurred) {
		handle_error(SampleClip);
	}
}


// Set up general SPI/DMA configuration.
// HAL automatically does this for each Send/Receive with SPI/DMA,
// so this function only has an LL implementation.
// Some of these settings (data length, memory location, and memory increment state)
// will be overwritten on a transfer-by-transfer basis, but the general configurations
// like transfer directions, peripheral addresses, and DMAMUX request ID can be permanently set here.
void initialize_spi_with_dma(void)
{
#ifdef USE_HAL
	return;
#else
	// Specify 6 SCLK cycles between each 16-bit SPI word in which NSS is driven high
	// (necessary to conform to RHD chip datasheet).
	// NOTE - Changing SPI setting Master Inter-Data Idleness in .ioc does NOT
	// seem to actually cause initialization to set this parameter for LL, so
	// it's important to specify this here.
	// In contrast, HAL does seem to correctly initialize based on .ioc.
	LL_SPI_SetInterDataIdleness(TRANSMIT_SPI, LL_SPI_ID_IDLENESS_06CYCLE);

	// Specify that NSS (CS) should remain high between each command sequence.
	LL_SPI_EnableGPIOControl(TRANSMIT_SPI);

	// Specify transmit and receive directions for both SPI buses.
	LL_SPI_SetTransferDirection(TRANSMIT_SPI, LL_SPI_SIMPLEX_TX);
	LL_SPI_SetTransferDirection(RECEIVE_SPI, LL_SPI_SIMPLEX_RX);

	// Configure Tx DMA stream settings
	LL_DMA_ConfigTransfer(DMA, DMA_TX_CHANNEL, LL_DMA_DIRECTION_MEMORY_TO_PERIPH | // Configure TX DMA stream, MOSI from memory to TRANSMIT_SPI
			LL_DMA_PRIORITY_VERYHIGH | // Assign very high priority
			LL_DMA_MODE_NORMAL | // Use non-circular DMA mode
			LL_DMA_PERIPH_NOINCREMENT | // Do not increment peripheral address after each transfer - should always write to TRANSMIT_SPI data register
			LL_DMA_MEMORY_INCREMENT | // Default to increment memory address after each transfer to iterate through array - may be overwritten for individual transfers
			LL_DMA_PDATAALIGN_HALFWORD | LL_DMA_MDATAALIGN_HALFWORD); // Data aligned at half-words (16 bits)

	// Configure Tx DMA stream addresses
	LL_DMA_ConfigAddresses(DMA, DMA_TX_CHANNEL, // Configure TX DMA stream
			(uint32_t) command_sequence_MOSI, // Default to transfer data from command_sequence_MOSI array's memory address - may be overwritten for individual transfers
			LL_SPI_DMA_GetTxRegAddr(TRANSMIT_SPI), // Transfer data to TRANSMIT_SPI data register
			LL_DMA_GetDataTransferDirection(DMA, DMA_TX_CHANNEL)); // Transfer from memory to peripheral

	// Default to data length of full command sequence - may be overwritten for individual transfers
	LL_DMA_SetDataLength(DMA, DMA_TX_CHANNEL, CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);

	// Assign TX DMA stream to correct DMAMUX request
	LL_DMA_SetPeriphRequest(DMA, DMA_TX_CHANNEL, LL_DMAMUX1_REQ_SPI3_TX);

	// Configure Rx DMA stream settings
	LL_DMA_ConfigTransfer(DMA, DMA_RX_CHANNEL, LL_DMA_DIRECTION_PERIPH_TO_MEMORY | // Configure RX DMA stream
			LL_DMA_PRIORITY_VERYHIGH | // Assign very high priority
			LL_DMA_MODE_NORMAL | // Use non-circular DMA mode
			LL_DMA_PERIPH_NOINCREMENT | // Do not increment peripheral address after each transfer - should always read from RECEIVE_SPI data register
			LL_DMA_MEMORY_INCREMENT | // Default to increment memory address after each transfer to iterate through array - may be overwritten for individual transfers
			LL_DMA_PDATAALIGN_WORD | LL_DMA_MDATAALIGN_WORD); // Data aligned at words (32 bits)

	// Configure Rx DMA stream addresses
	LL_DMA_ConfigAddresses(DMA, DMA_RX_CHANNEL, // Configure RX DMA stream
			LL_SPI_DMA_GetRxRegAddr(RECEIVE_SPI), // Transfer data from RECEIVE_SPI data register
			(uint32_t) command_sequence_MISO, // Default to transfer data to command_sequence_MISO array's memory address - may be overwritten for individual transfers
			LL_DMA_GetDataTransferDirection(DMA, DMA_RX_CHANNEL)); // Transfer from peripheral to memory

	// Default to data length of full command sequence - may be overwritten for individual transfers
	LL_DMA_SetDataLength(DMA, DMA_RX_CHANNEL, CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);

	// Assign RX DMA stream to correct DMAMUX request
	LL_DMA_SetPeriphRequest(DMA, DMA_RX_CHANNEL, LL_DMAMUX1_REQ_SPI1_RX);
#endif
}

// Write SPI/DMA registers to cleanly disable once DMA transfer ends.
// HAL automatically does this for each Send/Receive with SPI/DMA,
// so this function only has an LL implementation.
void end_spi_with_dma(void)
{
#ifdef USE_HAL
#else
	end_spi_rx();
	end_spi_tx();
#endif
}


// Start timers used to generate Receive SCLK signal used to read DDR MISO, triggered with delay from Transmit CS.
void initialize_ddr_sclk_timers(void)
{
#ifdef USE_HAL
  HAL_TIM_OC_Start(&RECEIVE_SCLK_TIM, TIM_CHANNEL_1);
  HAL_TIM_OC_Start(&CS_DELAY_TIM, TIM_CHANNEL_3);
#else
  LL_TIM_CC_EnableChannel(RECEIVE_SCLK_TIM, LL_TIM_CHANNEL_CH1);
  LL_TIM_EnableAllOutputs(RECEIVE_SCLK_TIM);
  LL_TIM_EnableCounter(RECEIVE_SCLK_TIM);

  LL_TIM_CC_EnableChannel(CS_DELAY_TIM, LL_TIM_CHANNEL_CH3);
  LL_TIM_EnableCounter(CS_DELAY_TIM);
#endif
}


// End timers used to generate Receive SCLK signal used to read DDR MISO, triggered with delay from Transmit CS.
void end_ddr_sclk_timers(void)
{
#ifdef USE_HAL
  HAL_TIM_OC_Stop(&RECEIVE_SCLK_TIM, TIM_CHANNEL_1);
  HAL_TIM_OC_Stop(&CS_DELAY_TIM, TIM_CHANNEL_3);
#else
  LL_TIM_DisableCounter(RECEIVE_SCLK_TIM);
  LL_TIM_DisableAllOutputs(RECEIVE_SCLK_TIM);
  LL_TIM_CC_DisableChannel(RECEIVE_SCLK_TIM, LL_TIM_CHANNEL_CH1);

  LL_TIM_DisableCounter(CS_DELAY_TIM);
  LL_TIM_CC_DisableChannel(CS_DELAY_TIM, LL_TIM_CHANNEL_CH3);
#endif
}


// Use 'magic bits' de interleave method inspired by Jeroen Baert's blog post:
// "Morton encoding/decoding through bit interleaving: Implementations" to achieve
// high performance method of separate every other bit from a 32-bit input.
static inline uint16_t morton_deinterleave(uint32_t x)
{
    x = x & 0x55555555; // Use mask to clear all even bits
    x = (x | (x >> 1)) & 0x33333333; // Shift-right by 1, duplicate, and mask data so that valid data is grouped into 2s (00xx00xx00xx...)
    x = (x | (x >> 2)) & 0x0F0F0F0F; // Shift-right by 2, duplicate, and mask data so that valid data is grouped into 4s (0000xxxx0000xxxx...)
    x = (x | (x >> 4)) & 0x00FF00FF; // Shift-right by 4, duplicate, and mask data so that valid data is grouped into 8s (00000000xxxxxxxx00000000xxxxxxxx...)
    x = (x | (x >> 8)) & 0x0000FFFF; // Shift-right by 8, duplicate, and mask data so that 16 MSB data is 0s, 16 LSB data is valid data
    return (uint16_t) x; // Cast 32-bit 0000_0000_0000_0000_xxxx_xxxx_xxxx_xxxx data into 16-bit xxxx_xxxx_xxxx_xxxx data
}


// Separate a 32-bit merged word (interleaved stream A and stream B data) into 2 distinct 16-bit words.
void extract_ddr_words(uint32_t merged_word, uint16_t* const word_A, uint16_t* const word_B)
{

	// A slow, but straightforward, implementation to separate every other bit from a 32-bit word into
	// 2 16-bit words A and B.
	// Iterates through 16 pairs of bits in a 32-bit word, separating out every odd and even bit into A and B.
//	for (int i = 0; i < 16; i++) {
//		uint16_t bit_B = (uint16_t) ((merged_word >> (2*i)) & 1);
//		uint16_t bit_A = (uint16_t) ((merged_word >> (2*i + 1)) & 1);
//		*word_A |= bit_A << i;
//		*word_B |= bit_B << i;
//	}

	// A much faster, but less obvious method uses 'magic bit' masks to copy, shift, and mask bits in several steps
	// to achieve the same result in fewer operations.
	*word_A = morton_deinterleave(merged_word); // Data stream A is all add
	*word_B = morton_deinterleave(merged_word >> 1);
}


// Determine suitable values to be written to registers
// (based on default acquisition values from RHX software).
// These suitable default values are saved to RHDConfigParameters argument.
// Write these values to registers, and calibrate and run for 9 commands to fully initialize chip.
void write_initial_reg_values(RHDConfigParameters* const p)
{
	// Determine suitable values to be written for each of the registers.
	p->sample_rate = calculate_sample_rate();
	set_default_rhd_settings(p);

	uint16_t registers[22];
	for (int i = 0; i < 22; i++) {
		registers[i] = get_register_value(p, i);
	}

	// Send a few dummy commands in case chip is still powering up.
	send_spi_command(read_command(63));
	send_spi_command(read_command(63));


	// Write suitable default values for RHD registers.
	for (int i = 0; i < 22; i++) {
		send_spi_command(write_command(i, registers[i]));
	}

	// Calibrate and run for 9 commands.
	send_spi_command(calibrate_command());
	for (int i = 0; i < 9; i++) {
		send_spi_command(read_command(40));
	}
}


// Check timer clock input, clock division, prescaling, and counter period
// to determine the rate at which INTERRUPT_TIM interrupts occur (sample rate).
// Note that this reads clock and timer configuration register values during runtime,
// so this function should adapt to any changes made to the .ioc.
double calculate_sample_rate(void)
{
	uint32_t apb1_timer_freq, ckd_value, psc_value, counter_period;

#ifdef USE_HAL
	apb1_timer_freq = HAL_RCC_GetPCLK1Freq() * 2; // Timer clock inputs on the H7 are multiplied x2 from peripheral clock frequency, which this function reports.
	ckd_value = INTERRUPT_TIM.Init.ClockDivision;
	psc_value = INTERRUPT_TIM.Init.Prescaler;
	counter_period = INTERRUPT_TIM.Init.Period;
#else
	LL_RCC_ClocksTypeDef RCC_Clocks;
	LL_RCC_GetSystemClocksFreq(&RCC_Clocks);
	apb1_timer_freq = RCC_Clocks.PCLK1_Frequency * 2; // Timer clock inputs on the H7 are multiplied x2 from peripheral clock frequency, which this function reports.
	ckd_value = LL_TIM_GetClockDivision(INTERRUPT_TIM);
	psc_value = LL_TIM_GetPrescaler(INTERRUPT_TIM);
	counter_period = LL_TIM_GetAutoReload(INTERRUPT_TIM);
#endif

	double ckd_factor = 1.0;
	if (ckd_value == 0b01) {
		ckd_factor = 2;
	} else if (ckd_value == 0b10) {
		ckd_factor = 4;
	}

	double psc_factor = psc_value + 1;

	double input_frequency = apb1_timer_freq / (ckd_factor * psc_factor);
	return input_frequency / counter_period;
}


// Send provided 16-bit word 'tx_data' over SPI, ignoring resultant 16-bit received word.
void send_spi_command(uint16_t tx_data)
{
	uint16_t dummy_data_A = 0;
	uint16_t dummy_data_B = 0;
	send_receive_spi_command(tx_data, &dummy_data_A, &dummy_data_B);
}


// Create a list of CONVERT_COMMANDS_PER_SEQUENCE (default 32) CONVERT commands,
// and load them into command_sequence_MOSI.
// If the channel_numbers_to_convert parameter is NULL,
// create CONVERT_COMMANDS_PER_SEQUENCE commands from channel 0 (default 0 - 31).
// Otherwise, populate the CONVERT commands in the order specified by channel_numbers_to_convert.
void create_convert_sequence(const uint8_t* const channel_numbers_to_convert)
{
	// If no list of channel numbers is provided,
	// then assume CONVERT should occur for channels 0 - CONVERT_COMMANDS_PER_SEQUENCE.
	if (channel_numbers_to_convert == NULL) {
		for (int i = 0; i < CONVERT_COMMANDS_PER_SEQUENCE; i++) {
			command_sequence_MOSI[i] = convert_command(i, false);
		}
	}

	// Otherwise, assume CONVERT should occur for only the channel numbers listed
	// in channel_numbers_to_convert, in the order they appear in the list.
	else {
		for (int i = 0; i < CONVERT_COMMANDS_PER_SEQUENCE; i++) {
			command_sequence_MOSI[i] = convert_command(channel_numbers_to_convert[i], false);
		}
	}
}


// Create a list of num_commands commands to program most RAM registers on an RHD2000 chip, read those values
// back to confirm programming, read ROM registers, and (if calibrate == true) run ADC calibration.
// Return the number of populated commands. num_commands must be 60 or greater.
int create_command_list_RHD_register_config(const RHDConfigParameters* const p, uint16_t* const command_list, bool calibrate, int num_commands)
{
	int command_index = 0;
	// Start with a few dummy commands in case chip is still powering up.
	command_list[command_index++] = read_command(63);
	command_list[command_index++] = read_command(63);


	// Program RAM registers.
	for (int reg = 0; reg < 22; ++reg) {
		// Don't program Register 3 (MUX Load, Temperature Sensor, and Auxiliary Digital Output)
		// or 6 (Impedance Check DAC) here;
		// control temperature sensor and DAC waveforms in other command streams.
		if (reg == 3 || reg == 6) continue;
		command_list[command_index++] = write_command(reg, get_register_value(p, reg));
	}


	// Read ROM registers.
	command_list[command_index++] = read_command(63);
	command_list[command_index++] = read_command(62);
	command_list[command_index++] = read_command(61);
	command_list[command_index++] = read_command(60);
	command_list[command_index++] = read_command(59);

	// Read chip name from ROM.
	command_list[command_index++] = read_command(48);
	command_list[command_index++] = read_command(49);
	command_list[command_index++] = read_command(50);
	command_list[command_index++] = read_command(51);
	command_list[command_index++] = read_command(52);
	command_list[command_index++] = read_command(53);
	command_list[command_index++] = read_command(54);
	command_list[command_index++] = read_command(55);

	// Read Intan name from ROM.
	command_list[command_index++] = read_command(40);
	command_list[command_index++] = read_command(41);
	command_list[command_index++] = read_command(42);
	command_list[command_index++] = read_command(43);
	command_list[command_index++] = read_command(44);

	// Read back RAM registers to confirm programming.
	for (int reg = 0; reg < 22; ++reg) {
		command_list[command_index++] = read_command(reg);
		// Note that registers 18-21 are only 'visible' on MISO B, so if register values are being used, be sure to use MISO B read values.
	}

	// Optionally, run ADC calibration (should only be run once after board is plugged in).
	if (calibrate) {
		command_list[command_index++] = calibrate_command();
	} else {
		command_list[command_index++] = read_command(63);
	}

	// End with a dummy command.
	command_list[command_index++] = read_command(63);

	for (int i = 0; i < (num_commands - 64); ++i) {
		command_list[command_index++] = read_command(63);
	}
	return command_index;
}


// Create a list of RHD commands to sample auxiliary ADC inputs 1-3 at 1/4 the amplifier sampling rate.
// The reading of a ROM register is interleaved to allow for data frame alignment.
// Return the length of the command list. num_commands should be evenly divisible by four.
int create_command_list_RHD_sample_aux_ins(uint16_t* const command_list, int num_commands)
{
	if (num_commands < 4) return -1;

	int command_index = 0;

	for (int i = 0; i < (num_commands / 4) - 2; ++i) {
		command_list[command_index++] = convert_command(32, false); // sample AuxIn1.
		command_list[command_index++] = convert_command(33, false); // sample AuxIn2.
		command_list[command_index++] = convert_command(34, false); // sample AuxIn3.
		command_list[command_index++] = read_command(40); // read ROM register; should return 0x0049.
	}

	// Last two times:
	command_list[command_index++] = convert_command(32, false); // sample AuxIn1.
	command_list[command_index++] = convert_command(33, false); // sample AuxIn2.
	command_list[command_index++] = convert_command(34, false); // sample AuxIn3.
	command_list[command_index++] = read_command(48); // read supply voltage sensor.

	command_list[command_index++] = convert_command(32, false); // sample AuxIn1.
	command_list[command_index++] = convert_command(33, false); // sample AuxIn2.
	command_list[command_index++] = convert_command(34, false); // sample AuxIn3.
	command_list[command_index++] = convert_command(40, false); // read ROM register; should return 0x0049.
	return command_index;
}


// Create a list of commands to update RHD Register 3 (controlling the auxiliary
// digital output pin) every sampling period.
// Return the length of the command list.
int create_command_list_RHD_update_DigOut(const RHDConfigParameters* const p, uint16_t* const command_list, int num_commands)
{
	if (num_commands < 1) return -1;

	int command_index = 0;

	for (int i = 0; i < num_commands; i++) {
		command_list[command_index++] = write_command(3, get_register_value(p, 3));
	}

	return command_index;
}


// Create a list of dummy commands with a specific command.
// Return the length of the command list (which should be n).
int create_command_list_dummy(const RHDConfigParameters* const p, uint16_t* const command_list, int n, uint16_t cmd)
{
	int command_index = 0;

	for (int i = 0; i < n; i++) {
		command_list[command_index++] = cmd;
	}

	return command_index;
}


// Create a list of up to AUX_COMMAND_LIST_LENGTH commands to generate a sine wave of particular frequency (in Hz) and
// amplitude (in DAC steps, 0-128) using the on-chip impedance testing voltage DAC.  If frequency is set to zero,
// a DC baseline waveform is created.
// Return the length of the command list.
int create_command_list_zcheck_DAC(const RHDConfigParameters* const p, uint16_t* const command_list, double frequency, double amplitude)
{
	int command_index = 0;

	if ((amplitude < 0.0) || (amplitude > 128.0)) {
		// Error: Amplitude out of range
		return -1;
	}
	if (frequency < 0.0) {
		// Error: Negative frequency not allowed
		return -1;
	} else if (frequency > p->sample_rate / 4.0) {
		// Error: Frequency too high relative to sampling rate
		return -1;
	}

	unsigned int dac_register = 6;
	if (frequency == 0.0) {
		for (int i = 0; i < AUX_COMMAND_LIST_LENGTH; ++i) {
			command_list[command_index++] = write_command(dac_register, 128);
		}
	} else {
		int period = (int) floor(p->sample_rate / frequency + 0.5);
		if (period > AUX_COMMAND_LIST_LENGTH) {
			// Error: Frequency too low
			return -1;
		} else {
			double t = 0.0;
			for (int i = 0; i < period; ++i) {
				int value = (int) floor(amplitude * sin((2 * M_PI) * frequency * t) + 128.0 + 0.5);
				if (value < 0) {
					value = 0;
				} else if (value > 255) {
					value = 255;
				}
				command_list[command_index++] = write_command(dac_register, value);
				t += 1.0 / p->sample_rate;
			}
		}
	}

	return command_index;
}


#ifdef USE_HAL
// HAL calls this function when Rx has completed.
void HAL_SPI_RxCpltCallback(SPI_HandleTypeDef *hspi)
{
	if (hspi == &RECEIVE_SPI) {
		spi_rx_cplt_callback();
	}
}


// HAL calls this function when an error in the SPI communication has been detected.
void HAL_SPI_ErrorCallback(SPI_HandleTypeDef* hspi)
{
	spi_error_callback();
}


// HAL calls this function when UART Tx has completed.
void HAL_UART_TxCpltCallback(UART_HandleTypeDef* UartHandle)
{
	uart_ready = true;
}


#else
// Begin receive transfer from RECEIVE_SPI to memory.
void begin_spi_rx(uint32_t mem_increment, uint32_t mem_address, uint32_t num_words)
{
	LL_SPI_DisableDMAReq_RX(RECEIVE_SPI);
	LL_DMA_DisableStream(DMA, DMA_RX_CHANNEL);

	LL_DMA_SetMemoryIncMode(DMA, DMA_RX_CHANNEL, mem_increment);
	LL_DMA_SetMemoryAddress(DMA, DMA_RX_CHANNEL, mem_address);

	LL_DMA_SetDataLength(DMA, DMA_RX_CHANNEL, num_words);

	LL_DMA_ClearFlag_TC0(DMA);
	LL_DMA_ClearFlag_TE0(DMA);
	LL_DMA_ClearFlag_DME0(DMA);

	LL_DMA_EnableIT_TC(DMA, DMA_RX_CHANNEL);
	LL_DMA_EnableIT_TE(DMA, DMA_RX_CHANNEL);
	LL_DMA_EnableIT_DME(DMA, DMA_RX_CHANNEL);

	LL_DMA_EnableStream(DMA, DMA_RX_CHANNEL);

	LL_SPI_EnableIT_OVR(RECEIVE_SPI);

	LL_SPI_SetTransferSize(RECEIVE_SPI, num_words);
	LL_SPI_EnableDMAReq_RX(RECEIVE_SPI);
	LL_SPI_Enable(RECEIVE_SPI);
}

// Begin transmit transfer from memory to TRANSMIT_SPI
void begin_spi_tx(uint32_t mem_increment, uint32_t mem_address, uint32_t num_words)
{
	LL_SPI_DisableDMAReq_TX(TRANSMIT_SPI);
	LL_DMA_DisableStream(DMA, DMA_TX_CHANNEL);

	LL_DMA_SetMemoryIncMode(DMA, DMA_TX_CHANNEL, mem_increment);
	LL_DMA_SetMemoryAddress(DMA, DMA_TX_CHANNEL, mem_address);

	LL_DMA_SetDataLength(DMA, DMA_TX_CHANNEL, num_words);

	LL_DMA_ClearFlag_TC1(DMA);
	LL_DMA_ClearFlag_TE1(DMA);
	LL_DMA_ClearFlag_DME1(DMA);

	LL_DMA_EnableIT_TC(DMA, DMA_TX_CHANNEL);
	LL_DMA_EnableIT_TE(DMA, DMA_TX_CHANNEL);
	LL_DMA_EnableIT_DME(DMA, DMA_TX_CHANNEL);

	LL_DMA_EnableStream(DMA, DMA_TX_CHANNEL);

	LL_SPI_EnableIT_UDR(TRANSMIT_SPI);
	LL_SPI_EnableIT_MODF(TRANSMIT_SPI);

	LL_SPI_SetTransferSize(TRANSMIT_SPI, num_words);
	LL_SPI_EnableDMAReq_TX(TRANSMIT_SPI);
	LL_SPI_Enable(TRANSMIT_SPI);
	LL_SPI_StartMasterTransfer(TRANSMIT_SPI);
}


// End receive transfer from SPI to memory.
void end_spi_rx(void)
{
	// Clear EOT and SUSP flags in IFCR register.
	LL_SPI_ClearFlag_EOT(RECEIVE_SPI);
	LL_SPI_ClearFlag_SUSP(RECEIVE_SPI);
	LL_SPI_ClearFlag_TXTF(RECEIVE_SPI);

	// Clear SPE bit in CR1 register.
	LL_SPI_Disable(RECEIVE_SPI);

	// Disable SPI interrupts in IER register.
	LL_SPI_WriteReg(RECEIVE_SPI, IER, 0U);

	// Clear RXDMAEN bit from CFG1 register.
	LL_SPI_DisableDMAReq_RX(RECEIVE_SPI);

	// Disable DMA channel.
	LL_DMA_DisableStream(DMA, DMA_RX_CHANNEL);
}


// End transmit transfer from memory to SPI
void end_spi_tx(void)
{
	// Clear EOT, TXTF, and SUSP flags in IFCR register.
	LL_SPI_ClearFlag_EOT(TRANSMIT_SPI);
	LL_SPI_ClearFlag_SUSP(TRANSMIT_SPI);
	LL_SPI_ClearFlag_TXTF(TRANSMIT_SPI);

	// Clear SPE bit in CR1 register.
	LL_SPI_Disable(TRANSMIT_SPI);

	// Disable SPI interrupts in IER register.
	LL_SPI_WriteReg(TRANSMIT_SPI, IER, 0U);

	// Clear TXDMAEN bit from CFG1 register.
	LL_SPI_DisableDMAReq_TX(TRANSMIT_SPI);

	// Disable DMA channel.
	LL_DMA_DisableStream(DMA, DMA_TX_CHANNEL);
}


// When a DMA receive interrupt is triggered, this function executes.
// Writes Main Monitor pin low, detects communication errors, monitors non-error interrupt flags
// to enable EOT end of transfer interrupt when approaching finishing transfer.
void dma_interrupt_routine_rx(void)
{
	// Indicate main loop is not currently processing.
	write_pin(Main_Monitor_GPIO_Port, Main_Monitor_Pin, false);
	main_pin_status = false;

	// Check for DMA errors (TE, DME for Rx).
	// If any are found, set error LED and error code GPIOs.
	if (LL_DMA_IsActiveFlag_TE0(DMA) || LL_DMA_IsActiveFlag_DME0(DMA)) {
		handle_error(RxDMAError);
	}

	if (LL_DMA_IsActiveFlag_TC0(DMA)) {
		if (LL_DMA_IsEnabledIT_TC(DMA, DMA_RX_CHANNEL)) {

			// U5: Write CBR1 0 (BNDT): Set block # of data bytes to transfer from source
			// H7: Write S0NDTR 0 (NDT): Set # of data items to transfer
			LL_DMA_SetDataLength(DMA, DMA_RX_CHANNEL, 0);
			LL_DMA_ClearFlag_TC0(DMA);
			LL_SPI_EnableIT_EOT(RECEIVE_SPI);
		}
	}
}


// When a DMA transmit interrupt is triggered, this function executes.
// Writes Main Monitor pin low, detects communication errors, monitors non-error interrupt flags
// to enable EOT end of transfer interrupt when approaching finishing transfer.
void dma_interrupt_routine_tx(void)
{
	// Indicate main loop is not currently processing.
	write_pin(Main_Monitor_GPIO_Port, Main_Monitor_Pin, false);
	main_pin_status = false;

	// Check for DMA errors (TE, DME for Tx).
	// If any are found, set error LED and error code GPIOs.
	// Note that FE may be flagged at some point, but FIFO is not used so this can be ignored.
	if (LL_DMA_IsActiveFlag_TE1(DMA) || LL_DMA_IsActiveFlag_DME1(DMA)) {
		handle_error(TxDMAError);
	}

	if (LL_DMA_IsActiveFlag_TC1(DMA)) {
		LL_DMA_ClearFlag_TC1(DMA);
		LL_SPI_EnableIT_EOT(TRANSMIT_SPI);
	}
}


// When a DMA USART transmit interrupt is triggered, this function executes.
// Writes Main Monitor pin low, detects communication errors, monitors non-error interrupt flags
// and clears flags.
void dma_interrupt_routine_usart_tx(void)
{
	// Indicate main loop is not currently processing.
	write_pin(Main_Monitor_GPIO_Port, Main_Monitor_Pin, false);
	main_pin_status = false;

	// Check for DMA errors (DTE for Tx).
	// If any are found, set error LED and error code GPIOs.
	if (LL_DMA_IsActiveFlag_DME2(DMA)) {
		handle_error(TxDMAError);
	}

	// Note that FE may be flagged at some point, but FIFO is not used so this can be ignored.
	LL_DMA_ClearFlag_FE2(DMA);

	// Do nothing if HT is flagged, just clear flag and continue.
	if (LL_DMA_IsActiveFlag_HT2(DMA)) {
		LL_DMA_ClearFlag_HT2(DMA);
	}

	// If DMA TC is flagged, enable USART TC IT which should occur shortly after.
	if (LL_DMA_IsActiveFlag_TC2(DMA)) {
		if (LL_DMA_IsEnabledIT_TC(DMA, DMA_USART_CHANNEL)) {
			LL_USART_EnableIT_TC(USART);
			LL_DMA_ClearFlag_TC2(DMA);
		}
	}
}


// When a SPI receive interrupt is triggered, this function executes.
// Writes Main Monitor pin low, detects communication errors,
// and if transfer is complete cleanly exits transfer routine and calls user-facing callback function.
void spi_interrupt_routine_rx(void)
{
	// Indicate main loop is not currenty processing.
	write_pin(Main_Monitor_GPIO_Port, Main_Monitor_Pin, false);
	main_pin_status = false;

	if (LL_SPI_IsActiveFlag_EOT(RECEIVE_SPI)) {

		end_spi_rx();

		// Call transfer complete callback, user-facing function for both HAL and LL when transfer is complete.
		spi_rx_cplt_callback();
	}

	// Check for any SPI errors.
	if (LL_SPI_IsActiveFlag_OVR(RECEIVE_SPI)) {
		handle_error(RxSPIError); // OVR - Overrun
	}
}


// When a SPI transmit interrupt is triggered, this function executes.
// Writes Main Monitor pin low, detects communication errors,
// and if transfer is complete cleanly exits transfer routine.
void spi_interrupt_routine_tx(void)
{
	// Indicate main loop is not currently processing.
	write_pin(Main_Monitor_GPIO_Port, Main_Monitor_Pin, false);
	main_pin_status = false;

	if (LL_SPI_IsActiveFlag_EOT(TRANSMIT_SPI)) {

		end_spi_tx();

		// Check for any SPI errors.
		if (LL_SPI_IsActiveFlag_UDR(TRANSMIT_SPI) || LL_SPI_IsActiveFlag_MODF(TRANSMIT_SPI)) {
			handle_error(TxSPIError); // UDR - Underrun ... MODF - Mode Fault
		}
	}
}


// When a UART transmit interrupt is triggered, this function executes.
// If TC is flagged, set uart_ready variable to true and disable this interrupt.
// This should only execute shortly after DMA TC occurs, which enables this interrupt.
void uart_interrupt_routine(void)
{
	if (!LL_USART_IsActiveFlag_TC(USART)) {
		return;
	}

	LL_USART_ClearFlag_CM(USART);
	LL_USART_ClearFlag_EOB(USART);
	LL_USART_ClearFlag_FE(USART);
	LL_USART_ClearFlag_IDLE(USART);
	LL_USART_ClearFlag_LBD(USART);
	LL_USART_ClearFlag_NE(USART);
	LL_USART_ClearFlag_ORE(USART);
	LL_USART_ClearFlag_PE(USART);
	LL_USART_ClearFlag_RTO(USART);
	LL_USART_ClearFlag_TCBGT(USART);
	LL_USART_ClearFlag_TXFE(USART);
	LL_USART_ClearFlag_UDR(USART);
	LL_USART_ClearFlag_nCTS(USART);
	LL_USART_ClearFlag_TC(USART);

	uart_ready = true;
	LL_USART_DisableIT_TC(USART);
}
#endif
