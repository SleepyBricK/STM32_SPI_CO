/*
  Intan Technologies RHS STM32 Firmware Framework
  Version 1.2

  Copyright (c) 2025 Intan Technologies

  This file is part of the Intan Technologies RHS STM32 Firmware Framework.

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

volatile uint32_t command_sequence_MOSI[CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE] = {0};
volatile uint32_t command_sequence_MISO[CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE] = {0};
volatile uint32_t next_aux_commands[AUX_COMMANDS_PER_SEQUENCE] = {0};

uint16_t sample_counter = 0;
uint32_t *sample_memory = NULL;
uint32_t per_channel_sample_memory_capacity = 20000;

uint32_t aux_command_list[AUX_COMMANDS_PER_SEQUENCE][AUX_COMMAND_LIST_LENGTH] = {{0}};

volatile bool main_loop_active 		    = false;
volatile bool uart_ready                = true;
volatile bool sample_interrupt_occurred = false;
volatile bool main_pin_status 		    = false;

RHSConfigParameters parameters;

volatile int8_t compliance_aux_slot = -1;
static uint8_t aux_command_index = 0;
static volatile TransferState command_transfer_state = TRANSFER_COMPLETE;
static volatile bool reception_in_progress = false;
static int8_t prev_compliance_aux_slot = -1;

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


// Begin receiving MISO data (RHS -> SPI -> DMA -> memory) and transmitting MOSI data (memory -> DMA -> SPI -> RHS).
static void transfer_sequence_spi_dma(void)
{
#ifdef USE_HAL
	// HAL handles all of SPI DMA transfer with this single function call.

	// Note: this HAL function call seems to not be consistent in how long it takes, causing some jitter between Interrupt_Monitor_Pin (GPIO) and SPI signals.
	// However, SPI/DMA signals seem to be consistent with each other, so this shouldn't affect functionality.

	if (HAL_SPI_TransmitReceive_DMA(&SPI, (uint8_t*)command_sequence_MOSI, (uint8_t*)command_sequence_MISO,
			CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE) != HAL_OK)
	{
		Error_Handler();
	}

#else
	begin_spi_rx(LL_DMA_MEMORY_INCREMENT, (uint32_t) command_sequence_MISO, CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);
	begin_spi_tx(LL_DMA_MEMORY_INCREMENT, (uint32_t) command_sequence_MOSI, CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);
#endif
}


static int8_t find_compliance(void)
{
	static const uint32_t COMPLIANCE_READ_MASK    = 0xcfffffff; // 0b11001111111111111111111111111111
	static const uint32_t COMPLIANCE_READ_COMMAND = 0xc0280000; // 0b11000000001010000000000000000000
	for (int i = 0; i < AUX_COMMANDS_PER_SEQUENCE; i++) {
		if ((command_sequence_MOSI[AUX_OFFSET + i] & COMPLIANCE_READ_MASK) == COMPLIANCE_READ_COMMAND) {
			return i;
		}
	}
	return -1;
}


// Use compliance_aux_slot and prev_compliance_aux_slot to track where an expected result for a
// READ result of compliance register should appear in command_sequence_MISO. Reminder - each
// command in MOSI has its result appear 2 commands later in MISO, so it's possible that
// a compliance READ towards the end of a sequence will appear in the next sequence due to the
// 2-command pipeline delay.
// Return 32-bit result of READ command, or all 1s if no READ occurred indicating no compliance
// data is present.
static uint32_t locate_compliance_result(void)
{
	uint32_t compliance_read_data = 0xffffffff;
	const int8_t found_compliance_aux_slot = find_compliance();

	// If no compliance command found, just update prev_compliance_aux_slot and exit early
	if (found_compliance_aux_slot == -1) {
		prev_compliance_aux_slot = -1;
		return compliance_read_data;
	}

	// If prev_compliance_aux_slot is valid (was found in previous timestep) and delayed,
	// then its result should be present at beginning of this timestep's MISO
	if (prev_compliance_aux_slot != -1 && result_is_delayed(prev_compliance_aux_slot + AUX_OFFSET)) {
		compliance_read_data = command_sequence_MISO[prev_compliance_aux_slot - 2];
	}

	// If current result is delayed, but not previous, don't act on this timestep's MISO
	else if (result_is_delayed(found_compliance_aux_slot + AUX_OFFSET)) {

	}

	// If this timestep had a compliance read in MOSI, but early enough that its result is
	// also in this timestep's MISO, then just read compliance data directly from MISO.
	else {
		compliance_read_data = command_sequence_MISO[found_compliance_aux_slot + 2 + AUX_OFFSET];
	}

	// Update previous slot
	prev_compliance_aux_slot = found_compliance_aux_slot;

	// If data is corrupted, trigger an error - possibly a logic error in identifying a compliance
	// read and tracking down its result in MISO, or general SPI integrity
	if (compliance_read_data != 0xffffffff && (compliance_read_data & 0xffff0000) != 0) {
		handle_error(InvalidComplianceReading);
	}

	return compliance_read_data;
}



// Locate, if present, results from compliance register reads, and if results
// are found, pass them to user-defined function handle_compliance_result().
static void process_compliance_data(void)
{
	uint32_t compliance_read_data = locate_compliance_result();

	// If read data is 0xffffffff (default, unitialized value), there is no
	// compliance data to read, so do nothing to GPIOs and just return.
	if (compliance_read_data == 0xffffffff) {
		return;
	}

	handle_compliance_result((uint16_t) compliance_read_data);
}


// Callback function that executes when both Transmission and Reception of SPI have completed.
static void spi_txrx_cplt_callback(void)
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

		// Look for and process results of any compliance-monitor READ commands.
		process_compliance_data();

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


// Send provided 32-bit word 'tx_data' over SPI, and pass resultant 32-bit received work by reference.
// Note that the pipelined nature of the SPI communication has a 2-command delay,
// so the obtained result corresponds to the command from 2 transactions earlier.
static void send_receive_spi_command(uint32_t tx_data, uint32_t* const rx_data)
{
	reception_in_progress = true;
#ifdef USE_HAL

	if (HAL_SPI_TransmitReceive_DMA(&SPI, (uint8_t*) &tx_data, (uint8_t*)rx_data, 1) != HAL_OK)
	{
		Error_Handler();
	}

#else
	begin_spi_rx(LL_DMA_MEMORY_NOINCREMENT, (uint32_t) rx_data, 1);
	begin_spi_tx(LL_DMA_MEMORY_NOINCREMENT, (uint32_t) &tx_data, 1);
#endif
	while (reception_in_progress) {}
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
	//memcpy((uint32_t*) &command_sequence_MOSI[AUX_OFFSET], (uint32_t*) next_aux_commands, AUX_COMMANDS_PER_SEQUENCE * sizeof(uint32_t));

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

#ifdef AUTO_STIM_CMD_MODE
	// Writes aux commands to command_sequence_MOSI.
	// Contents of the aux commands, by default, are specified as:
	// 1. WRITE to Stim On (42) or Amp Settle (10 or 12), alternating between these two every other timestep.
	// 2. WRITE to Stim Polarity (44) or Charge Recovery (46 or 48), alternating between these two every other timestep.
	// 3. READ from Compliance Monitor (40)
	// 4. WRITE to Stim On (42) - if was preceded with compliance monitor read, also flag CLEAR of compliance monitor.
	// As stim sequences are executed, and it may be necessary to write to registers to, for instance,
	// set the stimulation magnitudes or polarities for a channel, these writes will supercede the 4 default
	// commands listed above, starting at slot 1.
	process_stim_sequences();
#else
	// Write aux commands to command_sequence_MOSI, advancing one sample through aux_command_list.
	// Cycles through pre-defined aux commands in aux_command_list
	cycle_aux_commands();
#endif

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
	// Specify 6 SCLK cycles between each 32-bit SPI word in which NSS is driven high
	// (necessary to conform to RHS chip datasheet).
	// NOTE - Changing SPI setting Master Inter-Data Idleness in .ioc does NOT
	// seem to actually cause initialization to set this parameter for LL, so
	// it's important to specify this here.
	// In contrast, HAL does seem to correctly initialize based on .ioc.
	LL_SPI_SetInterDataIdleness(SPI, LL_SPI_ID_IDLENESS_06CYCLE);

	// Specify that NSS (CS) should remain high between each command sequence.
	LL_SPI_EnableGPIOControl(SPI);

	// Specify direction for SPI bus.
	LL_SPI_SetTransferDirection(SPI, LL_SPI_FULL_DUPLEX);

	// Configure Tx DMA stream settings
	LL_DMA_ConfigTransfer(DMA, DMA_TX_CHANNEL, LL_DMA_DIRECTION_MEMORY_TO_PERIPH | // Configure TX DMA stream, MOSI from memory to SPI
			LL_DMA_PRIORITY_VERYHIGH | // Assign very high priority
			LL_DMA_MODE_NORMAL | // Use non-circular DMA mode
			LL_DMA_PERIPH_NOINCREMENT | // Do not increment peripheral address after each transfer - should always write to SPI data register
			LL_DMA_MEMORY_INCREMENT | // Default to increment memory address after each transfer to iterate through array - may be overwritten for individual transfers
			LL_DMA_PDATAALIGN_WORD | LL_DMA_MDATAALIGN_WORD); // Data aligned at full words (32 bits)

	// Configure Tx DMA stream addresses
	LL_DMA_ConfigAddresses(DMA, DMA_TX_CHANNEL, // Configure TX DMA stream
			(uint32_t) command_sequence_MOSI, // Default to transfer data from command_sequence_MOSI array's memory address - may be overwritten for individual transfers
			LL_SPI_DMA_GetTxRegAddr(SPI), // Transfer data to SPI data register
			LL_DMA_GetDataTransferDirection(DMA, DMA_TX_CHANNEL)); // Transfer from memory to peripheral

	// Default to data length of full command sequence - may be overwritten for individual transfers
	LL_DMA_SetDataLength(DMA, DMA_TX_CHANNEL, CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);

	// Assign TX DMA stream to correct DMAMUX request
	LL_DMA_SetPeriphRequest(DMA, DMA_TX_CHANNEL, LL_DMAMUX1_REQ_SPI3_TX);

	// Configure Rx DMA stream settings
	LL_DMA_ConfigTransfer(DMA, DMA_RX_CHANNEL, LL_DMA_DIRECTION_PERIPH_TO_MEMORY | // Configure RX DMA stream
			LL_DMA_PRIORITY_VERYHIGH | // Assign very high priority
			LL_DMA_MODE_NORMAL | // Use non-circular DMA mode
			LL_DMA_PERIPH_NOINCREMENT | // Do not increment peripheral address after each transfer - should always read from SPI data register
			LL_DMA_MEMORY_INCREMENT | // Default to increment memory address after each transfer to iterate through array - may be overwritten for individual transfers
			LL_DMA_PDATAALIGN_WORD | LL_DMA_MDATAALIGN_WORD); // Data aligned at full words (32 bits)

	// Configure Rx DMA stream addresses
	LL_DMA_ConfigAddresses(DMA, DMA_RX_CHANNEL, // Configure RX DMA stream
			LL_SPI_DMA_GetRxRegAddr(SPI), // Transfer data from SPI data register
			(uint32_t) command_sequence_MISO, // Default to transfer data to command_sequence_MISO array's memory address - may be overwritten for individual transfers
			LL_DMA_GetDataTransferDirection(DMA, DMA_RX_CHANNEL)); // Transfer from peripheral to memory

	// Default to data length of full command sequence - may be overwritten for individual transfers
	LL_DMA_SetDataLength(DMA, DMA_RX_CHANNEL, CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE);

	// Assign RX DMA stream to correct DMAMUX request
	LL_DMA_SetPeriphRequest(DMA, DMA_RX_CHANNEL, LL_DMAMUX1_REQ_SPI3_RX);
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


// Determine suitable values to be written to registers
// (based on default acquisition values from RHX software).
// These suitable default values are saved to RHSConfigParameters argument.
// Write these values to registers.
void write_initial_reg_values(RHSConfigParameters* const p)
{
	// Determine suitable values to be written for each of the registers.
	p->sample_rate = calculate_sample_rate();
	set_default_rhs_settings(p);

	uint16_t registers[112];
	for (int i = 0; i < 112; i++) {
		registers[i] = get_register_value(p, i);
	}

	// Send a few dummy commands in case chip is still powering up.
	send_spi_command(read_command(255, false, false));
	send_spi_command(read_command(255, false, false));

	// Send CLEAR command to set ADC calibration
	send_spi_command(clear_command());

	// Write suitable default values for RHS registers.
	for (int i = 0; i < 112; i++) {
		// Skip registers 9, 11, 13-31, 39, 41, 43, 45, 47, 49, 51-63, 80-95
		if ((i == 9) ||
				(i == 11) ||
				(i >= 13 && i <= 31) ||
				(i == 39) ||
				(i == 41) ||
				(i == 43) ||
				(i == 45) ||
				(i == 47) ||
				(i == 49) ||
				(i >= 51 && i <= 63) ||
				(i >= 80 && i <= 95)) {
			continue;
		}
		send_spi_command(write_command(i, registers[i], false, false));
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


// Send provided 32-bit word 'tx_data' over SPI, ignoring resultant 32-bit received word.
void send_spi_command(uint32_t tx_data)
{
	uint32_t dummy_data = 0;
	send_receive_spi_command(tx_data, &dummy_data);
}


// Create a list of CONVERT_COMMANDS_PER_SEQUENCE (default 16) CONVERT commands,
// and load them into command_sequence_MOSI.
// If the channel_numbers_to_convert parameter is NULL,
// create CONVERT_COMMANDS_PER_SEQUENCE commands from channel 0 (default 0 - 15).
// Otherwise, populate the CONVERT commands in the order specified by channel_numbers_to_convert.
void create_convert_sequence(const uint8_t* const channel_numbers_to_convert)
{
	// If no list of channel numbers is provided,
	// then assume CONVERT should occur for channels 0 - CONVERT_COMMANDS_PER_SEQUENCE.
	if (channel_numbers_to_convert == NULL) {
		for (int i = 0; i < CONVERT_COMMANDS_PER_SEQUENCE; i++) {
			bool d_bit = false;
#ifdef SAMPLE_DC_AMPS
			d_bit = true;
#endif
			command_sequence_MOSI[i] = convert_command(i, false, false, d_bit, false);
		}
	}

	// Otherwise, assume CONVERT should occur for only the channel numbers listed
	// in channel_numbers_to_convert, in the order they appear in the list.
	else {
		for (int i = 0; i < CONVERT_COMMANDS_PER_SEQUENCE; i++) {
			bool d_bit = false;
#ifdef SAMPLE_DC_AMPS
			d_bit = true;
#endif
			command_sequence_MOSI[i] = convert_command(channel_numbers_to_convert[i], false, false, d_bit, false);
		}
	}
}


// Create a list of num_commands commands to program most RAM registers on an RHS2116 chip, read those values
// back to confirm programming, and read ROM registers.
// If update_stim_params, update stimulation amplitudes and other charge-recovery parameters.
// Return the length of the command list.
int create_command_list_RHS_register_config(const RHSConfigParameters* const p, uint32_t* const command_list, bool update_stim_params, int num_commands)
{
	int command_index = 0;
	// Start with a few dummy commands in case chip is still powering up.
	command_list[command_index++] = read_command(255, false, false);
	command_list[command_index++] = read_command(255, false, false);


	// Program 53 RAM registers.
	command_list[command_index++] = write_command(0, get_register_value(p, 0), false, false);
	command_list[command_index++] = write_command(1, get_register_value(p, 1), false, false);
	command_list[command_index++] = write_command(2, get_register_value(p, 2), false, false);
	// Don't program Register 3 (Impedance Check DAC) here; create DAC waveform in another command stream.
	command_list[command_index++] = write_command(4, get_register_value(p, 4), false, false);
	command_list[command_index++] = write_command(5, get_register_value(p, 5), false, false);
	command_list[command_index++] = write_command(6, get_register_value(p, 6), false, false);
	command_list[command_index++] = write_command(7, get_register_value(p, 7), false, false);
	command_list[command_index++] = write_command(8, get_register_value(p, 8), false, false);
	command_list[command_index++] = write_command(10, get_register_value(p, 10), false, false);
	command_list[command_index++] = write_command(12, get_register_value(p, 12), false, false);
	command_list[command_index++] = write_command(32, get_register_value(p, 32), false, false);
	command_list[command_index++] = write_command(33, get_register_value(p, 33), false, false);
	if (update_stim_params) {
		command_list[command_index++] = write_command(34, get_register_value(p, 34), false, false);
		command_list[command_index++] = write_command(35, get_register_value(p, 35), false, false);
		command_list[command_index++] = write_command(36, get_register_value(p, 36), false, false);
		command_list[command_index++] = write_command(37, get_register_value(p, 37), false, false);
	} else {
		command_list[command_index++] = read_command(255, false, false);
		command_list[command_index++] = read_command(255, false, false);
		command_list[command_index++] = read_command(255, false, false);
		command_list[command_index++] = read_command(255, false, false);
	}
	command_list[command_index++] = write_command(38, get_register_value(p, 38), false, false);
	// Register 40 (Compliance Monitor) is read only; clear it here:
	command_list[command_index++] = write_command(40, false, false, true);
	command_list[command_index++] = write_command(42, get_register_value(p, 42), false, false);
	command_list[command_index++] = write_command(44, get_register_value(p, 44), false, false);
	command_list[command_index++] = write_command(46, get_register_value(p, 46), false, false);
	command_list[command_index++] = write_command(48, get_register_value(p, 48), false, false);
	// Register 50 (Fault Current Detect) is read only.

	if (update_stim_params) {
		for (int reg = 64; reg < 80; ++reg) {
			command_list[command_index++] = write_command(reg, get_register_value(p, reg), false, false);
		}
		for (int reg = 96; reg < 111; ++reg) {
			command_list[command_index++] = write_command(reg, get_register_value(p, reg), false, false);
		}
		// Update all triggered registers with the last WRITE command.
		command_list[command_index++] = write_command(111, get_register_value(p, 111), true, false);
	} else {
		for (int i = 0; i < 32; ++i) {
			command_list[command_index++] = read_command(255, false, false);
		}
	}

	// Read 5 ROM registers.
	command_list[command_index++] = read_command(255, false, false);
	command_list[command_index++] = read_command(254, false, false);
	command_list[command_index++] = read_command(253, false, false);
	command_list[command_index++] = read_command(252, false, false);
	command_list[command_index++] = read_command(251, false, false);

	// Read back 56 RAM registers to confirm programming.
	command_list[command_index++] = read_command(0, false, false);
	command_list[command_index++] = read_command(1, false, false);
	command_list[command_index++] = read_command(2, false, false);
	command_list[command_index++] = read_command(3, false, false);
	command_list[command_index++] = read_command(4, false, false);
	command_list[command_index++] = read_command(5, false, false);
	command_list[command_index++] = read_command(6, false, false);
	command_list[command_index++] = read_command(7, false, false);
	command_list[command_index++] = read_command(8, false, false);
	command_list[command_index++] = read_command(10, false, false);
	command_list[command_index++] = read_command(12, false, false);
	command_list[command_index++] = read_command(32, false, false);
	command_list[command_index++] = read_command(33, false, false);
	command_list[command_index++] = read_command(34, false, false);
	command_list[command_index++] = read_command(35, false, false);
	command_list[command_index++] = read_command(36, false, false);
	command_list[command_index++] = read_command(37, false, false);
	command_list[command_index++] = read_command(38, false, false);
	command_list[command_index++] = read_command(40, false, false);
	command_list[command_index++] = read_command(42, false, false);
	command_list[command_index++] = read_command(44, false, false);
	command_list[command_index++] = read_command(46, false, false);
	command_list[command_index++] = read_command(48, false, false);
	command_list[command_index++] = read_command(50, false, false);
	for (int reg = 64; reg < 80; ++reg) {
		command_list[command_index++] = read_command(reg, false, false);
	}
	for (int reg = 96; reg < 112; ++reg) {
		command_list[command_index++] = read_command(reg, false, false);
	}

	// ADC Calibration should not be performed on RHS2116; rather, CLEAR command should be sent.
	command_list[command_index++] = clear_command();

	// End with dummy commands.
	for (int i = 0; i < 10; ++i) {
		command_list[command_index++] = read_command(255, false, false);
	}

	return command_index;
}


// Read all registers from chip without changing any values.
int create_command_list_RHS_register_read(uint32_t* const command_list)
{
	int command_index = 0;

	// Start with a few dummy commands in case chip is still powering up.
	command_list[command_index++] = read_command(255, false, false);
	command_list[command_index++] = read_command(255, false, false);

	for (int i = 0; i < 54; i++) {
		command_list[command_index++] = read_command(255, false, false);
	}

	// Read 5 ROM registers
	command_list[command_index++] = read_command(255, false, false);
	command_list[command_index++] = read_command(254, false, false);
	command_list[command_index++] = read_command(253, false, false);
	command_list[command_index++] = read_command(252, false, false);
	command_list[command_index++] = read_command(251, false, false);

	// Read back 56 RAM registers to confirm programming
	command_list[command_index++] = read_command(0, false, false);
	command_list[command_index++] = read_command(1, false, false);
	command_list[command_index++] = read_command(2, false, false);
	command_list[command_index++] = read_command(3, false, false);
	command_list[command_index++] = read_command(4, false, false);
	command_list[command_index++] = read_command(5, false, false);
	command_list[command_index++] = read_command(6, false, false);
	command_list[command_index++] = read_command(7, false, false);
	command_list[command_index++] = read_command(8, false, false);
	command_list[command_index++] = read_command(10, false, false);
	command_list[command_index++] = read_command(12, false, false);
	command_list[command_index++] = read_command(32, false, false);
	command_list[command_index++] = read_command(33, false, false);
	command_list[command_index++] = read_command(34, false, false);
	command_list[command_index++] = read_command(35, false, false);
	command_list[command_index++] = read_command(36, false, false);
	command_list[command_index++] = read_command(37, false, false);
	command_list[command_index++] = read_command(38, false, false);
	command_list[command_index++] = read_command(40, false, false);
	command_list[command_index++] = read_command(42, false, false);
	command_list[command_index++] = read_command(44, false, false);
	command_list[command_index++] = read_command(46, false, false);
	command_list[command_index++] = read_command(48, false, false);
	command_list[command_index++] = read_command(50, false, false);

	for (int reg = 64; reg < 80; ++reg) {
		command_list[command_index++] = read_command(reg, false, false);
	}
	for (int reg = 96; reg < 112; ++reg) {
		command_list[command_index++] = read_command(reg, false, false);
	}
	command_list[command_index++] = read_command(255, false, false);

	// End with dummy commands.
	for (int i = 0; i < 10; ++i) {
		command_list[command_index++] = read_command(255, false, false);
	}

    return command_index;
}


// Set positive and negative stimulation magnitude and trim parameters for a single channel.
// Return the length of the command list (which should be 128).
int create_command_list_set_stim_magnitudes(RHSConfigParameters* const p, uint32_t* const command_list,
		int channel, int pos_mag, int pos_trim, int neg_mag, int neg_trim)
{
	int command_index = 0;

	// Start with two dummy commands
	command_list[command_index++] = read_command(255, false, false);
	command_list[command_index++] = read_command(255, false, false);

	set_pos_stim_magnitude(p, channel, pos_mag, pos_trim);
	set_neg_stim_magnitude(p, channel, neg_mag, neg_trim);

	// Now, configure RAM registers.
	command_list[command_index++] = write_command(96 + channel, get_register_value(p, 96 + channel), true, false); // positive register; update
	command_list[command_index++] = write_command(64 + channel, get_register_value(p, 64 + channel), true, false); // negative register; update

	// More dummy commands to make 128 total commands
	while (command_index < 128) {
		command_list[command_index++] = read_command(255, false, false);
	}

	return command_index;
}


// Set positive and negative stimulation magnitude and trim parameters for all channels.
// Return the length of the command list (which should be 128).
int create_command_list_set_stim_magnitudes_all_channels(RHSConfigParameters* const p, uint32_t* const command_list,
		int pos_mag, int pos_trim, int neg_mag, int neg_trim)
{
	int command_index = 0;

	// Start with two dummy commands
	command_list[command_index++] = read_command(255, false, false);
	command_list[command_index++] = read_command(255, false, false);

	for (int channel = 0; channel < 16; channel++) {
		set_pos_stim_magnitude(p, channel, pos_mag, pos_trim);
		set_neg_stim_magnitude(p, channel, neg_mag, neg_trim);
		command_list[command_index++] = write_command(96 + channel, get_register_value(p, 96 + channel), true, false); // positive register; update
		command_list[command_index++] = write_command(64 + channel, get_register_value(p, 64 + channel), true, false); // negative register; update
	}

	// More dummy commands to make 128 total commands
	while (command_index < 128) {
		command_list[command_index++] = read_command(255, false, false);
	}

	return command_index;
}


// Set charge recovery current limit and target voltage (Registers 36 and 37).
// Return the length of the command list (which should be 128).
int create_command_list_config_charge_recovery(RHSConfigParameters* const p, uint32_t* const command_list, ChargeRecoveryCurrentLimit current_limit, double target_voltage)
{
	int command_index = 0;

	// Start with two dummy commands.
	command_list[command_index++] = read_command(255, false, false);
	command_list[command_index++] = read_command(255, false, false);

	set_charge_recovery_current_limit(p, current_limit); 	// Set charge recovery current limit.
	set_charge_recovery_target_voltage(p, target_voltage); // Set charge recovery target voltage.

	// Now, configure RAM registers.
	command_list[command_index++] = write_command(36, get_register_value(p, 36), false, false);
	command_list[command_index++] = write_command(37, get_register_value(p, 37), false, false);

	// More dummy commands to make 128 total commands.
	while (command_index < 128) {
		command_list[command_index++] = read_command(255, false, false);
	}

	return command_index;
}


// Create a list of dummy commands with a specific command.
// Return the length of the command list (which should be n).
int create_command_list_dummy(const RHSConfigParameters* const p, uint32_t* const command_list, int n, uint32_t cmd)
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
int create_command_list_zcheck_DAC(const RHSConfigParameters* const p, uint32_t* const command_list, double frequency, double amplitude)
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

	unsigned int dac_register = 3;
	if (frequency == 0.0) {
		for (int i = 0; i < AUX_COMMAND_LIST_LENGTH; ++i) {
			command_list[command_index++] = write_command(dac_register, 128, false, false);
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
				command_list[command_index++] = write_command(dac_register, value, false, false);
				t += 1.0 / p->sample_rate;
			}
		}
	}

	return command_index;
}


#ifdef USE_HAL
// HAL calls this function when both Tx and Rx have completed.
void HAL_SPI_TxRxCpltCallback(SPI_HandleTypeDef* hspi)
{
	if (hspi == &SPI) {
		spi_txrx_cplt_callback();
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
// Begin receive transfer from SPI to memory.
void begin_spi_rx(uint32_t mem_increment, uint32_t mem_address, uint32_t num_words)
{
	LL_SPI_DisableDMAReq_RX(SPI);
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

	LL_SPI_EnableIT_OVR(SPI);

	LL_SPI_SetTransferSize(SPI, num_words);
	LL_SPI_EnableDMAReq_RX(SPI);
	LL_SPI_Enable(SPI);
}

// Begin transmit transfer from memory to SPI
void begin_spi_tx(uint32_t mem_increment, uint32_t mem_address, uint32_t num_words)
{
	LL_SPI_DisableDMAReq_TX(SPI);
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

	LL_SPI_EnableIT_UDR(SPI);
	LL_SPI_EnableIT_MODF(SPI);

	LL_SPI_SetTransferSize(SPI, num_words);
	LL_SPI_EnableDMAReq_TX(SPI);
	LL_SPI_Enable(SPI);
	LL_SPI_StartMasterTransfer(SPI);
}


// End receive transfer from SPI to memory
void end_spi_rx(void)
{
	// Clear EOT and SUSP flags in IFCR register.
	LL_SPI_ClearFlag_EOT(SPI);
	LL_SPI_ClearFlag_SUSP(SPI);
	LL_SPI_ClearFlag_TXTF(SPI);

	// Clear SPE bit in CR1 register.
	LL_SPI_Disable(SPI);

	// Disable SPI interrupts in IER register.
	LL_SPI_WriteReg(SPI, IER, 0U);

	// Clear RXDMAEN bit from CFG1 register.
	LL_SPI_DisableDMAReq_RX(SPI);

	// Disable DMA channel.
	LL_DMA_DisableStream(DMA, DMA_RX_CHANNEL);
}


// End transmit transfer from memory to SPI
void end_spi_tx(void)
{
	// Clear EOT, TXTF, and SUSP flags in IFCR register.
	LL_SPI_ClearFlag_EOT(SPI);
	LL_SPI_ClearFlag_SUSP(SPI);
	LL_SPI_ClearFlag_TXTF(SPI);

	// Clear SPE bit in CR1 register.
	LL_SPI_Disable(SPI);

	// Disable SPI interrupts in IER register.
	LL_SPI_WriteReg(SPI, IER, 0U);

	// Clear TXDMAEN bit from CFG1 register.
	LL_SPI_DisableDMAReq_TX(SPI);

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
			LL_SPI_EnableIT_EOT(SPI);
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
		LL_SPI_EnableIT_EOT(SPI);
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


// When a SPI interrupt is triggered, this function executes.
// Writes Main Monitor pin low, detects communication errors,
// and if transfer is complete cleanly exits transfer routine and calls user-facing callback function.
void spi_interrupt_routine(void)
{
	// Indicate main loop is not currenty processing.
	write_pin(Main_Monitor_GPIO_Port, Main_Monitor_Pin, false);
	main_pin_status = false;

	if (LL_SPI_IsActiveFlag_EOT(SPI)) {

		end_spi_tx();
		end_spi_rx();

		// Call transfer complete callback, user-facing function for both HAL and LL when transfer is complete.
		spi_txrx_cplt_callback();
	}

	// Check for any SPI errors.
	if (LL_SPI_IsActiveFlag_OVR(SPI)) {
		handle_error(RxSPIError); // OVR - Overrun
	}

	// Check for any SPI errors.
	if (LL_SPI_IsActiveFlag_UDR(SPI) || LL_SPI_IsActiveFlag_MODF(SPI)) {
		handle_error(TxSPIError); // UDR - Underrun ... MODF - Mode Fault
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

void process_trigger(uint8_t trigger_source)
{
	// For all sequences that are not currently executing and have a matching trigger_source, set stim_triggered to 1
	for (int i = 0; i < NUM_STIM_SEQUENCES; i++) {
		if (sequences[i].trigger_source != trigger_source) {
			continue;
		}

		if (!sequences[i].status.is_executing) {
			sequences[i].status.stim_triggered = true;
		}
	}
}
