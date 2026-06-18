/*
 * stimscheduler.c
 *
 *  Created on: Mar 4, 2025
 *      Author: afoy
 */

#include "stimscheduler.h"
#include "rhsinterface.h"
#include <string.h>
#include <stdlib.h>

RHSConfigParameters on_chip_parameters;
volatile StimSequence sequences[NUM_STIM_SEQUENCES];

static WriteCommandCircularBuffer scheduled_write_commands;
static uint8_t regs_needing_writes[MAX_REGS_NEEDING_WRITES] = {0};
static uint8_t regs_needing_writes_index = 0;
static bool odd_iteration = false;

#ifdef TRADITIONAL_FAST_SETTLE
static const uint8_t amp_settle_reg_address = 10;
#else
static const uint8_t amp_settle_reg_address = 12;
#endif
#ifdef CHARGE_RECOVERY_SWITCH
static const uint8_t charge_recovery_reg_address = 46;
#else
static const uint8_t charge_recovery_reg_address = 48;
#endif
static const uint8_t stim_on_address = 42;
static const uint8_t stim_pol_address = 44;
static const uint8_t compliance_monitor_address = 40;
static const uint8_t pos_stim_mag_address_offset = 96;
static const uint8_t neg_stim_mag_address_offset = 64;

static inline int16_t num_commands_in_buffer(void)
{
	if (scheduled_write_commands.read_index > scheduled_write_commands.write_index) {
		return scheduled_write_commands.write_index + MAX_COMMANDS_IN_BUFFER - scheduled_write_commands.read_index;
	} else {
		return scheduled_write_commands.write_index - scheduled_write_commands.read_index;
	}
}

static void set_last_command_update(void)
{
	uint8_t last_command_write_index;
	if (scheduled_write_commands.write_index == 0) {
		last_command_write_index = MAX_COMMANDS_IN_BUFFER - 1;
	} else {
		last_command_write_index = scheduled_write_commands.write_index - 1;
	}

	scheduled_write_commands.commands[last_command_write_index].update = true;
}


// Append command to bottom of buffer
static void write_command_to_buffer(ScheduledWriteCommand command)
{
	// Report error when attempting to write to a full buffer
	if (num_commands_in_buffer() >= MAX_COMMANDS_IN_BUFFER) {
		handle_error(CommandBufferOverflowError);
	}

	// Add command to current write index.
	scheduled_write_commands.commands[scheduled_write_commands.write_index] = command;

	// Increment index
	scheduled_write_commands.write_index++;

	// Wrap around when end of circular buffer index is reached.
	if (scheduled_write_commands.write_index >= MAX_COMMANDS_IN_BUFFER) {
		scheduled_write_commands.write_index = 0;
	}
}


// Pull command from top of buffer
static inline ScheduledWriteCommand read_command_from_buffer(void)
{
	// Report error when attempting to read from an empty buffer
	if (num_commands_in_buffer() <= 0) {
		handle_error(CommandBufferUnderflowError);
	}

	// Read command from current read index.
	ScheduledWriteCommand this_command = scheduled_write_commands.commands[scheduled_write_commands.read_index];

	// Increment index
	scheduled_write_commands.read_index++;

	// Wrap around when end of circular buffer index is reached.
	if (scheduled_write_commands.read_index >= MAX_COMMANDS_IN_BUFFER) {
		scheduled_write_commands.read_index = 0;
	}
	return this_command;
}


static inline void load_write_to_next(uint8_t aux_slot, uint8_t reg_address, bool u_flag, bool m_flag)
{
	next_aux_commands[aux_slot] = write_command(reg_address, get_register_value(&parameters, reg_address), u_flag, m_flag);
}

static inline void load_read_to_next(uint8_t aux_slot, uint8_t reg_address, bool u_flag, bool m_flag)
{
	next_aux_commands[aux_slot] = read_command(reg_address, u_flag, m_flag);
}


static void load_aux_commands(void)
{
	odd_iteration = odd_iteration ? false : true;

	int aux_slot = 0;
	compliance_aux_slot = -1;
	ScheduledWriteCommand this_command;
	while (aux_slot < AUX_COMMANDS_PER_SEQUENCE) {

		if (num_commands_in_buffer() >= 1) {
			// Load command from top of list to array of next aux commands
			this_command = read_command_from_buffer();

			load_write_to_next(aux_slot, this_command.reg_address, this_command.update, 0);

			// Move up to next aux slot
			aux_slot++;
		}

		// Fill all remaining empty aux command slots with redundant writes of important registers,
		// and reading/clearing of compliance monitor.
		// If empty, queue aux command slots such that:
		// 0: Alternate between: Write to Reg 42 (stim on) or Write to Reg 10/12 (fast settle)
		// 1: Alternate between: Write ro Reg 44 (stim pol) or Write to Reg 46/48 (charge recovery)
		// 2: Read compliance monitor: Read from Reg 40
		// 3: Write to Reg 42 (stim on) - if was preceded with compliance monitor read, flag CLEAR of compliance monitor
		else {
			switch (aux_slot) {
			case 0:
				load_write_to_next(aux_slot, odd_iteration ? stim_on_address : amp_settle_reg_address, false, false);
				break;
			case 1:
				load_write_to_next(aux_slot, odd_iteration ? stim_pol_address : charge_recovery_reg_address, false, false);
				break;
			case 2:
				load_read_to_next(aux_slot, compliance_monitor_address, false, false);
				compliance_aux_slot = aux_slot;
				break;
			case 3:
				load_write_to_next(aux_slot, stim_on_address, false, compliance_aux_slot >= 0);
				break;
			}

			// Move up to the next aux slot
			aux_slot++;
		}
	}
}


void initialize_stim_sequences(void)
{
	for (int i = 0; i < NUM_STIM_SEQUENCES; i++) {
		sequences[i].trigger_source = 0;
		sequences[i].channel = 0;
		sequences[i].loop_start = 0;
		sequences[i].loop_end = 0;
		sequences[i].loop_repeat = 0;
		sequences[i].num_segments = 0;
		for (int j = 0; j < MAX_SEGMENTS_PER_SEQUENCE; j++) {
			sequences[i].segments[j].length = 0;
			sequences[i].segments[j].magnitude = 0;
			sequences[i].segments[j].fast_settle = 0;
			sequences[i].segments[j].charge_recovery = 0;
		}
	}
}


void initialize_command_buffer(void)
{
	scheduled_write_commands.write_index = 0;
	scheduled_write_commands.read_index = 0;
	for (int i = 0; i < MAX_COMMANDS_IN_BUFFER; i++) {
		scheduled_write_commands.commands[i].reg_address = 0;
		scheduled_write_commands.commands[i].update = 0;
	}
}

// Compare contents of 'parameters' and 'on_chip_parameters' for all variables related to provided register address.
// If the provided register address differs between the two, return 1. If they're the same, return 0.
uint8_t register_differs(int reg_address) {

	switch (reg_address) {

	case 0:
		if (parameters.adc_buffer_bias != on_chip_parameters.adc_buffer_bias ||
				parameters.mux_bias != on_chip_parameters.mux_bias) {
			return 1;
		}
		break;

	case 1:
		if (parameters.digOutOD != on_chip_parameters.digOutOD ||
				parameters.digOut2 != on_chip_parameters.digOut2 ||
				parameters.digOut2HiZ != on_chip_parameters.digOut2HiZ ||
				parameters.digOut1 != on_chip_parameters.digOut1 ||
				parameters.digOut1HiZ != on_chip_parameters.digOut1HiZ ||
				parameters.weak_miso != on_chip_parameters.weak_miso ||
				parameters.twos_comp != on_chip_parameters.twos_comp ||
				parameters.abs_mode != on_chip_parameters.abs_mode ||
				parameters.DSP_en != on_chip_parameters.DSP_en ||
				parameters.DSP_cutoff_freq != on_chip_parameters.DSP_cutoff_freq) {
			return 1;
		}
		break;

	case 2:
		if (parameters.zcheck_select != on_chip_parameters.zcheck_select ||
				parameters.zcheck_DAC_power != on_chip_parameters.zcheck_DAC_power ||
				parameters.zcheck_load != on_chip_parameters.zcheck_load ||
				parameters.zcheck_scale != on_chip_parameters.zcheck_scale ||
				parameters.zcheck_en != on_chip_parameters.zcheck_en) {
			return 1;
		}
		break;

	case 4:
		if (parameters.rH1_sel2 != on_chip_parameters.rH1_sel2 ||
				parameters.rH1_sel1 != on_chip_parameters.rH1_sel1) {
			return 1;
		}
		break;

	case 5:
		if (parameters.rH2_sel2 != on_chip_parameters.rH2_sel2 ||
				parameters.rH2_sel1 != on_chip_parameters.rH2_sel1) {
			return 1;
		}
		break;

	case 6:
		if (parameters.rL_A_sel3 != on_chip_parameters.rL_A_sel3 ||
				parameters.rL_A_sel2 != on_chip_parameters.rL_A_sel2 ||
				parameters.rL_A_sel1 != on_chip_parameters.rL_A_sel1) {
			return 1;
		}
		break;

	case 7:
		if (parameters.rL_B_sel3 != on_chip_parameters.rL_B_sel3 ||
				parameters.rL_B_sel2 != on_chip_parameters.rL_B_sel2 ||
				parameters.rL_B_sel1 != on_chip_parameters.rL_B_sel1) {
			return 1;
		}
		break;

	case 8:
		if (memcmp(parameters.amp_pwr, on_chip_parameters.amp_pwr, sizeof(parameters.amp_pwr)) != 0) {
			return 1;
		}
		break;

	case 10:
		if (memcmp(parameters.amp_fast_settle, on_chip_parameters.amp_fast_settle, sizeof(parameters.amp_fast_settle)) != 0) {
			return 1;
		}
		break;

	case 12:
		if (memcmp(parameters.amp_f_l_select, on_chip_parameters.amp_f_l_select, sizeof(parameters.amp_f_l_select)) != 0) {
			return 1;
		}
		break;

	case 32:
		if (parameters.stim_enableA != on_chip_parameters.stim_enableA) {
			return 1;
		}
		break;

	case 33:
		if (parameters.stim_enableB != on_chip_parameters.stim_enableB) {
			return 1;
		}
		break;

	case 34:
		if (parameters.stim_step_sel3 != on_chip_parameters.stim_step_sel3 ||
				parameters.stim_step_sel2 != on_chip_parameters.stim_step_sel2 ||
				parameters.stim_step_sel1 != on_chip_parameters.stim_step_sel1) {
			return 1;
		}
		break;

	case 35:
		if (parameters.stimP_bias != on_chip_parameters.stimP_bias ||
				parameters.stimN_bias != on_chip_parameters.stimN_bias) {
			return 1;
		}
		break;

	case 36:
		if (parameters.charge_recovery_DAC != on_chip_parameters.charge_recovery_DAC) {
			return 1;
		}
		break;

	case 37:
		if (parameters.charge_recovery_current_limit_sel3 != on_chip_parameters.charge_recovery_current_limit_sel3 ||
				parameters.charge_recovery_current_limit_sel2 != on_chip_parameters.charge_recovery_current_limit_sel2 ||
				parameters.charge_recovery_current_limit_sel1 != on_chip_parameters.charge_recovery_current_limit_sel1) {
			return 1;
		}
		break;

	case 38:
		if (memcmp(parameters.dc_amp_pwr, on_chip_parameters.dc_amp_pwr, sizeof(parameters.dc_amp_pwr)) != 0) {
			return 1;
		}
		break;

	case 42:
		if (memcmp(parameters.stim_on, on_chip_parameters.stim_on, sizeof(parameters.stim_on)) != 0) {
			return 1;
		}
		break;

	case 44:
		if (memcmp(parameters.stim_pol, on_chip_parameters.stim_pol, sizeof(parameters.stim_pol)) != 0) {
			return 1;
		}
		break;

	case 46:
		if (memcmp(parameters.charge_recovery_switch, on_chip_parameters.charge_recovery_switch, sizeof(parameters.charge_recovery_switch)) != 0) {
			return 1;
		}
		break;

	case 48:
		if (memcmp(parameters.c_l_charge_recovery_en, on_chip_parameters.c_l_charge_recovery_en, sizeof(parameters.c_l_charge_recovery_en)) != 0) {
			return 1;
		}
		break;

	case 64 ... 79:
		if (parameters.neg_current_mag[reg_address - neg_stim_mag_address_offset] != on_chip_parameters.neg_current_mag[reg_address - neg_stim_mag_address_offset] ||
				parameters.neg_current_trim[reg_address - neg_stim_mag_address_offset] != on_chip_parameters.neg_current_trim[reg_address - neg_stim_mag_address_offset]) {
			return 1;
		}
		break;

	case 96 ... 111:
		if (parameters.pos_current_mag[reg_address - pos_stim_mag_address_offset] != on_chip_parameters.pos_current_mag[reg_address - pos_stim_mag_address_offset] ||
				parameters.pos_current_trim[reg_address - pos_stim_mag_address_offset] != on_chip_parameters.pos_current_trim[reg_address - pos_stim_mag_address_offset]) {
			return 1;
		}
		break;

	default:
		break;
	}
	return 0;
}


void sync_parameters_queue(void)
{
	if (regs_needing_writes_index == 0) {
		return;
	}
	bool duplicate_found;


	// Currently using a 2D for loop to find duplicates, for larger datasets hash map would
	// probably be faster. Or, if performance is critical, but having some duplicate WRITEs is acceptable,
	// it may be best to skip this duplicate-checking step and allow some duplicates to go through
	// in order to make this processing step execute faster.
	for (int i = 0; i < regs_needing_writes_index; i++) {
		duplicate_found = false;
		for (int j = 0; j < i; j++) {
			// If a duplicate register write is present, skip it
			if (regs_needing_writes[i] == regs_needing_writes[j]) {
				duplicate_found = true;
				break;
			}
		}
		if (duplicate_found) {
			continue;
		}

		//write_command_to_buffer((ScheduledWriteCommand) { .reg_address = regs_needing_writes[i], .update = ((regs_needing_writes_index - 1) == i)});
		// Set all commands with update = false as default
		write_command_to_buffer((ScheduledWriteCommand) { .reg_address = regs_needing_writes[i], .update = false});
	}

	// Only set update bit to true for the last command that was added in this timestep
	set_last_command_update();

	// Technically, to fully 'clear' regs_needing_writes, we should write 0 to all array elements from 0 to regs_needing_writes_index
	// before setting the index to 0, but as long as we're careful to only increment the index after writing the previous index element,
	// this is redundant and can be eliminated for efficiency
	regs_needing_writes_index = 0;
}


void sync_parameters_immediate(void)
{
	// Compare on_chip_parameters to parameters. For every discrepancy, encode relevant register addresses
	// into registers_to_sync and then, immediately write corresponding register(s)
	uint8_t registers_to_sync[112] = {0};

	// Keep track of last differing register
	// Flag each differing register in 'registers_to_sync'
	uint8_t last_differing_register = 255;
	for (int reg_address = 0; reg_address < 112; reg_address++) {
		if (register_differs(reg_address)) {
			last_differing_register = reg_address;
			registers_to_sync[reg_address] = 1;
		}
	}

	if (last_differing_register == 255) return;

	// Check 'registers_to_sync' for all differing registers, writing them immediately
	for (int i = 0; i < 112; i++) {
		if (registers_to_sync[i] == 1) {
			send_spi_command(write_command(i, get_register_value(&parameters, i), 0, 0));
			registers_to_sync[i] = 0;
		}
	}

	// Send dummy READ command with U flag
	send_spi_command(read_command(255, 1, 0));

	// Now that parameters are synced copy parameters to on_chip_parameters
	on_chip_parameters = parameters;
}


void set_parameters_for_segment(Segment segment, uint8_t channel)
{
	// Length - ignore

	// Magnitude
	if (segment.magnitude == 0) {
		if (parameters.stim_on[channel]) {
			set_stim_on(&parameters, channel, false);
			regs_needing_writes[regs_needing_writes_index++] = stim_on_address;
		}
	}

	else if (segment.magnitude > 0) {
		if (!parameters.stim_on[channel]) {
			set_stim_on(&parameters, channel, true);
			regs_needing_writes[regs_needing_writes_index++] = stim_on_address;
		}

		if (parameters.pos_current_mag[channel] != segment.magnitude) {
			set_pos_stim_magnitude(&parameters, channel, segment.magnitude, 0);
			regs_needing_writes[regs_needing_writes_index++] = pos_stim_mag_address_offset + channel;
		}

		if (parameters.stim_pol[channel] != true) {
			set_stim_polarity(&parameters, channel, 1);
			regs_needing_writes[regs_needing_writes_index++] = stim_pol_address;
		}
	}

	else if (segment.magnitude < 0) {
		if (!parameters.stim_on[channel]) {
			set_stim_on(&parameters, channel, true);
			regs_needing_writes[regs_needing_writes_index++] = stim_on_address;
		}

		if (parameters.neg_current_mag[channel] != segment.magnitude) {
			set_neg_stim_magnitude(&parameters, channel, abs(segment.magnitude), 0);
			regs_needing_writes[regs_needing_writes_index++] = neg_stim_mag_address_offset + channel;
		}

		if (parameters.stim_pol[channel] != 0) {
			set_stim_polarity(&parameters, channel, false);
			regs_needing_writes[regs_needing_writes_index++] = stim_pol_address;
		}
	}

	// Fast settle
#ifdef TRADITIONAL_FAST_SETTLE
	if (parameters.amp_fast_settle[channel] != segment.fast_settle) {
		set_amp_fast_settle(&parameters, channel, segment.fast_settle);
#else
	if (parameters.amp_f_l_select[channel] != segment.fast_settle) {
		set_amp_f_l_select(&parameters, channel, segment.fast_settle);
#endif
		regs_needing_writes[regs_needing_writes_index++] = amp_settle_reg_address;
	}

	// Charge recovery
#ifdef CHARGE_RECOVERY_SWITCH
	if (parameters.charge_recovery_switch[channel] != segment.charge_recovery) {
		set_charge_recovery_switch(&parameters, channel, segment.charge_recovery);
#else
	if (parameters.c_l_charge_recovery_en[channel] != segment.charge_recovery) {
		set_c_l_charge_recovery_enable(&parameters, channel, segment.charge_recovery);
#endif
		regs_needing_writes[regs_needing_writes_index++] = charge_recovery_reg_address;
	}
}

// Initialize sequence to begin executing starting at segment 1
static inline void begin_sequence(volatile StimSequence* const sequence)
{
	sequence->status.is_executing = 1;
	sequence->status.current_segment = 1;
	sequence->status.remaining_timesteps = sequence->segments[sequence->status.current_segment].length;
	sequence->status.current_repeat = sequence->loop_repeat;
}

// End sequence, setting it back to resting state segment 0 and de-flagging 'stim_executing'
static inline void end_sequence(volatile StimSequence* const sequence)
{
	sequence->status.is_executing = 0;
	set_parameters_for_segment(sequence->segments[0], sequence->channel);
}


static inline void advance_segment_timestep(volatile StimSequence* const sequence)
{
	if (!sequence->status.is_executing) {
		return;
	}

	// Determine what the next segment should be after this timestep.
	if (--sequence->status.remaining_timesteps == 0) {

		// Finished loop
		if (sequence->status.current_segment == sequence->loop_end) {

			// Finished last loop - increment segment
			if (sequence->status.current_repeat == 0) {
				sequence->status.current_segment++;
			}

			// Either infinite loop or not the last loop iteration
			else {
				// Loop back to start
				sequence->status.current_segment = sequence->loop_start;

				// For positive loop_repeat (finite loop ierations), decrement loop_repeat
				if (sequence->status.current_repeat > 0) {
					--sequence->status.current_repeat;
				}
			}

		}

		// Continue to the next segment in the loop
		else {
			sequence->status.current_segment++;
		}

		// If last defined segment has finished, back to segment 0 for final timestep
		// before flagging sequence as off
		if (sequence->status.current_segment == sequence->num_segments) {
			sequence->status.current_segment = 0;
		}

		sequence->status.remaining_timesteps = sequence->segments[sequence->status.current_segment].length;
	}
}

static inline void set_parameters_for_sequence(volatile StimSequence* const sequence)
{
	// If stim was triggered and sequence not previous executing, start sequence.
	if (!sequence->status.is_executing) {
		if (sequence->status.stim_triggered) {
			sequence->status.stim_triggered = false;
			begin_sequence(sequence);
		} else {
			return;
		}
	}

	// If stim was executing and is now back to segment 0, that means the sequence
	// has completed, so end sequence
	if (sequence->status.current_segment == 0) {
		end_sequence(sequence);
		return;
	}

	// Set parameters for this timestep.
	set_parameters_for_segment(sequence->segments[sequence->status.current_segment], sequence->channel);
}


void process_stim_sequences(void)
{
	for (int i = 0; i < NUM_STIM_SEQUENCES; i++) {
		set_parameters_for_sequence(&sequences[i]);
	}

	// Synchronize parameters to determine what, if any commands, must be issued for this timestep,
	// and add them to schedule queue.
	sync_parameters_queue();

	// Load aux commands for this timestep, working through schedule queue and loading up to
	// AUX_COMMANDS_PER_SEQUENCE (default 4) into MOSI sequence.
	load_aux_commands();

	// Determine what the next segment should be for each sequence after this timestep.
	for (int i = 0; i < NUM_STIM_SEQUENCES; i++) {
		advance_segment_timestep(&sequences[i]);
	}
}
