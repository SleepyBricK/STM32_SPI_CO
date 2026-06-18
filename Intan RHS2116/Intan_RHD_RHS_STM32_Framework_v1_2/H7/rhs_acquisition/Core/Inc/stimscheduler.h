/*
 * stimscheduler.h
 *
 *  Created on: Mar 4, 2025
 *      Author: afoy
 */

#ifndef INC_STIMSCHEDULER_H_
#define INC_STIMSCHEDULER_H_

#include "rhsregisters.h"
#include "userconfig.h"

#define MAX_NUM_PULSES_PER_TRAIN 99
#define MAX_SEGMENTS_PER_SEQUENCE (6 * MAX_NUM_PULSES_PER_TRAIN)
#define MAX_COMMANDS_IN_BUFFER 128
#define MAX_REGS_NEEDING_WRITES 128

// Description of each horizontal line period, many of which make up a StimSequence.
typedef struct {
	uint32_t length; // Num timesteps (1 / sample rate) this segment lasts for.
	int32_t magnitude; // When multiplied by stim step size, magnitude of the current sourced during stimulation (can be positive or negative)
	bool fast_settle; // true if fast_settle is active, false if inactive.
	bool charge_recovery; // true if charge_recovery is active, false if inactive.
} Segment;

// Status variables used within software to monitor the state of a stim sequence.
// Should not be directly changed by the user.
typedef struct {
	bool stim_triggered; // true if sequence has been triggered, false if not
	uint8_t is_executing; // true if sequence is executing stimulation, false if not
	uint8_t current_segment; // Which of the user-defined segments the sequence is currently in
	uint32_t remaining_timesteps; // Num timesteps (1 / sample rate) remaining in the current segment
	uint32_t current_repeat; // Which of the user-defined loop repeats the sequence is currently in
} SequenceStatus;

typedef struct {
	// 1 - blue button interrupt ... 2 - some other GPIO (as yet implemented) ... 0 ... default (inactive)
	uint16_t trigger_source; // Where trigger initiating this stim sequence comes from
	uint8_t channel; // Which RHS chip channel this stim sequence occurs on
	uint32_t num_segments; // Total number of user-defined segments which make up this sequence

	// -1 for infinite, 0 for no loop, positive number for number of repeats
	// Note that the first run-through is not considered a loop - if you wanted a range of segments
	// to execute 5 times, you would specify loop_repeat of 4 since the first iteration is not a repeat.
	int32_t loop_repeat; // How many times a section of user-defined segments should loop.
	uint32_t loop_start; // Inclusive index of user-defined segments where a loop should begin.
	uint32_t loop_end; // Inclusive index of user-defined segments where a loop should end.

	SequenceStatus status; // Status variables used by software to monitor the state of this sequence.
	Segment segments [MAX_SEGMENTS_PER_SEQUENCE]; // Array of user-defined Segments.
} StimSequence;

typedef struct {
	uint8_t reg_address;
	bool update;
} ScheduledWriteCommand;

typedef struct {
	uint8_t read_index;
	uint8_t write_index;
	ScheduledWriteCommand commands [MAX_COMMANDS_IN_BUFFER];
} WriteCommandCircularBuffer;

typedef struct {
	uint8_t read_index;
	uint8_t write_index;
	uint8_t reg_address [256];
} RegsNeedingWritesCircularBuffer;

extern RHSConfigParameters on_chip_parameters;
extern volatile StimSequence sequences[NUM_STIM_SEQUENCES];

void initialize_stim_sequences(void);
void initialize_command_buffer(void);

void sync_parameters_queue(void);
void sync_parameters_immediate(void);
void set_parameters_for_segment(Segment segment, uint8_t channel);
void process_stim_sequences(void);

#endif /* INC_STIMSCHEDULER_H_ */
