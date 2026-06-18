/*
 * commonautosequences.c
 *
 *  Created on: Apr 4, 2025
 *      Author: afoy
 */

#include "commonautosequences.h"
#include "rhsinterface.h"

// Return index after any necessary increment
static void populate_segment(volatile StimSequence* const sequence, int* const segment_index,
		int length, int magnitude, bool fast_settle, bool charge_recovery)
{
	Segment segment = sequence->segments[*segment_index];

	// If desired segment length is 0, just return with no changes, skipping this non-existent segment
	if (length == 0) {
		return;
	}
	segment.length = length;
	segment.magnitude = magnitude;
	segment.fast_settle = fast_settle;
	segment.charge_recovery = charge_recovery;
	sequence->segments[*segment_index] = segment;
	(*segment_index)++;
	return;
}

static inline bool valid_amplitude(int16_t amplitude)
{
	if ((amplitude > 255) || (amplitude < -255)) {
		return false;
	} else {
		return true;
	}
}

void validate_biphasic(const BiphasicWaveform* const biphasic)
{
	// As soon as any rule is violated, flag this waveform as invalid
	bool valid = true;

	// channel must be between 0 and 15
	if (biphasic->channel >= MAX_NUM_CHANNELS_PER_CHIP) {
		valid = false;
	}

	// trigger_source must be between 0 and 15
	if (biphasic->trigger_source >= NUM_STIM_SEQUENCES) {
		valid = false;
	}

	// The following timing parameters must be between 0 and top of uint32_t range,
	// but no need to enforce, because they are declared as uint32_t:
	// post_trigger_delay
	// first_phase_duration
	// second_phase_duration
	// interphase_delay
	// refractory_period
	// pre_stim_amp_settle
	// post_stim_amp_settle
	// post_stim_charge_recovery_on
	// post_stim_charge_recovery_off

	// pulse_train_period must be between 0 and top of uint64_t range,
	// but no need to enforce, because it is declared as uint64_t

	// Resting amplitude must be between -255 and 255
	if (!valid_amplitude(biphasic->resting_amplitude)) {
		valid = false;
	}

	// First phase amplitude must be between -255 and 255
	if (!valid_amplitude(biphasic->first_phase_amplitude)) {
		valid = false;
	}

	// Second phase amplitude must be between -255 and 255
	if (!valid_amplitude(biphasic->second_phase_amplitude)) {
		valid = false;
	}

	// Num pulses must be between 1 and MAX_NUM_PULSES_PER_TRAIN
	if (biphasic->num_pulses < 1 || biphasic->num_pulses > MAX_NUM_PULSES_PER_TRAIN) {
		valid = false;
	}

	// If num_pulses == 1, pulse_train_period must be 0
	if (biphasic->num_pulses == 1 && biphasic->pulse_train_period != 0) {
		valid = false;
	}

	// For trains, pulse train period must be at least as large as the total period for each pulse in that train:
	// If maintain amp settle, at least as large as first phase + interphase + second phase
	// If not maintain amp settle, at least as large as first phase + interphase + second phase + post stim amp settle + pre stim amp settle
	if (biphasic->num_pulses > 1) {
		uint64_t min_pulse_duration;
		if (biphasic->maintain_amp_settle) {
			min_pulse_duration = biphasic->first_phase_duration + biphasic->interphase_delay + biphasic->second_phase_duration;
		} else {
			min_pulse_duration = biphasic->first_phase_duration + biphasic->interphase_delay + biphasic->second_phase_duration + biphasic->post_stim_amp_settle + biphasic->pre_stim_amp_settle;
		}
		if (biphasic->pulse_train_period < min_pulse_duration) {
			valid = false;
		}
	}

	// If enable_amp_settle is false, pre_stim_amp_settle must be 0
	if (!biphasic->enable_amp_settle && biphasic->pre_stim_amp_settle != 0) {
		valid = false;
	}

	// If enable_amp_settle is false, post_stim_amp_settle must be 0
	if (!biphasic->enable_amp_settle && biphasic->post_stim_amp_settle != 0) {
		valid = false;
	}

	// If enable_amp_settle is false, maintain_amp_settle must be false
	if (!biphasic->enable_amp_settle && biphasic->maintain_amp_settle) {
		valid = false;
	}

	// If enable_charge_recovery is false, post_stim_charge_recovery_on must be 0
	if (!biphasic->enable_charge_recovery && biphasic->post_stim_charge_recovery_on != 0) {
		valid = false;
	}

	// If enable_charge_recovery is false, post_stim_charge_recovery_off must be 0
	if (!biphasic->enable_charge_recovery && biphasic->post_stim_charge_recovery_off != 0) {
		valid = false;
	}

	// Post trigger delay must be greater than or equal to Pre stim amp settle
	if (biphasic->post_trigger_delay < biphasic->pre_stim_amp_settle) {
		valid = false;
	}

	// Post stim charge recovery off must be greater than or equal to Post stim charge recovery on
	if (biphasic->post_stim_charge_recovery_off < biphasic->post_stim_charge_recovery_on) {
		valid = false;
	}

	// Refractory period must be greater than or equal to:
	// 1) Post stim amp settle
	// 2) Post stim charge recovery on, AND
	// 3) Post stim charge recovery off
	if ((biphasic->refractory_period < biphasic->post_stim_amp_settle) ||
			(biphasic->refractory_period < biphasic->post_stim_charge_recovery_on) ||
			(biphasic->refractory_period < biphasic->post_stim_charge_recovery_off)) {
		valid = false;
	}

	// Pulse train period must be greater than or equal to:
	// First phase duration + Interphase delay + Second phase duration
	if (biphasic->num_pulses > 1) {
		uint64_t total_duration = biphasic->first_phase_duration + biphasic->interphase_delay + biphasic->second_phase_duration;
		if (biphasic->pulse_train_period < total_duration) {
			valid = false;
		}
	}

	if (!valid) {
		handle_error(InvalidStimWaveform);
	}
}


void create_biphasic_train(volatile StimSequence* const sequence, const BiphasicWaveform* const biphasic)
{
	// General parameters
	sequence->channel = biphasic->channel;
	sequence->trigger_source = biphasic->trigger_source;
	sequence->loop_repeat = biphasic->num_pulses - 2; // -1 because of 'repeat' not including the first pulse, and -1 because of last pulse requiring special treatment outside of loop
	sequence->loop_start = 0; // gets populated later
	sequence->loop_end = 0; // gets populated later

	// Default, resting segment
	int segment_index = 0;
	populate_segment(sequence, &segment_index, 1, biphasic->resting_amplitude, false, false);

	// POST TRIGGER DELAY
	if (biphasic->post_trigger_delay > 0) {
		int duration = biphasic->post_trigger_delay - biphasic->pre_stim_amp_settle;
		populate_segment(sequence, &segment_index, duration, biphasic->resting_amplitude, false, false);
	}

	// Start of loop (no maintain amp settle)
	if (!biphasic->maintain_amp_settle) {
		sequence->loop_start = segment_index;
	}

	// PRE STIM AMP SETTLE
	if (biphasic->pre_stim_amp_settle > 0) {
		populate_segment(sequence, &segment_index, biphasic->pre_stim_amp_settle, biphasic->resting_amplitude, true, false);
	}

	// Start of loop (maintain amp settle)
	if (biphasic->maintain_amp_settle) {
		sequence->loop_start = segment_index;
	}

	// FIRST PHASE
	populate_segment(sequence, &segment_index, biphasic->first_phase_duration, biphasic->first_phase_amplitude, biphasic->enable_amp_settle, false);

	// INTERPHASE
	if (biphasic->interphase_delay > 0) {
		populate_segment(sequence, &segment_index, biphasic->interphase_delay, biphasic->resting_amplitude, biphasic->enable_amp_settle, false);
	}

	// SECOND PHASE
	populate_segment(sequence, &segment_index, biphasic->second_phase_duration, biphasic->second_phase_amplitude, biphasic->enable_amp_settle, false);

	// POST STIM AMP SETTLE (no maintain)
	// Also, not applicable for the last repeat, where we have to sort between AS, CR1, and CR2, e.g. use populate_end_of_sequence
	if (!biphasic->maintain_amp_settle && biphasic->post_stim_amp_settle > 0) {
		populate_segment(sequence, &segment_index, biphasic->post_stim_amp_settle, biphasic->resting_amplitude, true, false);
	}

	// PULSE PERIOD - (first phase, interphase, second phase, post stim amp settle, pre stim amp settle) ... ignore post/pre stim amp settle durations if maintain amp settle
	int min_pulse_duration = biphasic->first_phase_duration + biphasic->interphase_delay + biphasic->second_phase_duration;
	if (!biphasic->maintain_amp_settle) {
		min_pulse_duration += biphasic->post_stim_amp_settle + biphasic->pre_stim_amp_settle;
	}
	int remaining_pulse_train_period = biphasic->pulse_train_period - min_pulse_duration;
	if (remaining_pulse_train_period > 0) {
		populate_segment(sequence, &segment_index, remaining_pulse_train_period, biphasic->resting_amplitude, biphasic->maintain_amp_settle, false);
	}

	// End of loop
	sequence->loop_end = segment_index - 1;

	// Last pulse in train needs to be handled individually, since potentially having charge recovery events divides the pulse up into different segments.
	// FINAL PRE STIM AMP SETTLE
	if (biphasic->pre_stim_amp_settle > 0) {
		populate_segment(sequence, &segment_index, biphasic->pre_stim_amp_settle, biphasic->resting_amplitude, true, false);
	}

	// FINAL FIRST PHASE
	populate_segment(sequence, &segment_index, biphasic->first_phase_duration, biphasic->first_phase_amplitude, biphasic->enable_amp_settle, false);

	// FINAL INTERPHASE
	if (biphasic->interphase_delay > 0) {
		populate_segment(sequence, &segment_index, biphasic->interphase_delay, biphasic->resting_amplitude, biphasic->enable_amp_settle, false);
	}

	// FINAL SECOND PHASE
	populate_segment(sequence, &segment_index, biphasic->second_phase_duration, biphasic->second_phase_amplitude, biphasic->enable_amp_settle, false);

	// Sort between post stim amp settle (AS), post stim charge recovery on (CR1), and post stim charge recovery off (CR2)
	populate_end_of_biphasic_sequence(sequence, &segment_index, biphasic);

}

void create_biphasic_pulse(volatile StimSequence* const sequence, const BiphasicWaveform* const biphasic)
{
	// General parameters
	sequence->channel = biphasic->channel;
	sequence->trigger_source = biphasic->trigger_source;
	sequence->loop_repeat = 0;
	sequence->loop_start = 0;
	sequence->loop_end = 0;

	// Default, resting segment
	int segment_index = 0;
	populate_segment(sequence, &segment_index, 1, biphasic->resting_amplitude, false, false);

	// POST TRIGGER DELAY
	if (biphasic->post_trigger_delay > 0) {
		int duration = biphasic->post_trigger_delay - biphasic->pre_stim_amp_settle;
		populate_segment(sequence, &segment_index, duration, biphasic->resting_amplitude, false, false);
	}

	// PRE STIM AMP SETTLE
	if (biphasic->pre_stim_amp_settle > 0) {
		populate_segment(sequence, &segment_index, biphasic->pre_stim_amp_settle, biphasic->resting_amplitude, true, false);
	}

	// FIRST PHASE
	populate_segment(sequence, &segment_index, biphasic->first_phase_duration, biphasic->first_phase_amplitude, biphasic->enable_amp_settle, false);

	// INTERPHASE
	if (biphasic->interphase_delay > 0) {
		populate_segment(sequence, &segment_index, biphasic->interphase_delay, biphasic->resting_amplitude, biphasic->enable_amp_settle, false);
	}

	// SECOND PHASE
	populate_segment(sequence, &segment_index, biphasic->second_phase_duration, biphasic->second_phase_amplitude, biphasic->enable_amp_settle, false);

	// Sort between post stim amp settle (AS), post stim charge recovery on (CR1), and post stim charge recovery off (CR2)
	populate_end_of_biphasic_sequence(sequence, &segment_index, biphasic);
}

// Sort between post stim amp settle (AS), post stim charge recovery on (CR1), and post stim charge recovery off (CR2)
void populate_end_of_biphasic_sequence(volatile StimSequence* const sequence, int* const segment_index, const BiphasicWaveform* const biphasic)
{
	int duration = 0;
	// No AS, no CR:
	// (1) refractory period
	// (2) end sequence
	if (!biphasic->enable_amp_settle && !biphasic->enable_charge_recovery) {
		duration = biphasic->refractory_period;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, false); // (1)

		sequence->num_segments = *segment_index; // (2) end sequence
		return;
	}

	// No AS, CR:
	// (1) period before CR1 ... if CR1 = 0, this should be skipped intelligently by populate_segment
	// (2) period after CR1, before CR2
	// (3) refractory period from after CR2
	// (4) end sequence
	if (!biphasic->enable_amp_settle && biphasic->enable_charge_recovery) {
		duration = biphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, false); // (1)

		duration = biphasic->post_stim_charge_recovery_off - biphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, true) ; // (2)

		duration = biphasic->refractory_period - biphasic->post_stim_charge_recovery_off;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, false); // (3)

		sequence->num_segments = *segment_index; // (4) end sequence
		return;
	}

	// AS, no CR:
	// (1) period before AS ... if AS = 0, this should be skipped intelligently by populate_segment
	// (2) refractory period from after AS
	// (3) end sequence
	if (biphasic->enable_amp_settle && !biphasic->enable_charge_recovery) {
		duration = biphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, true, false); // (1)

		duration = biphasic->refractory_period - biphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, false); // (2)

		sequence->num_segments = *segment_index; // (3) end sequence
		return;
	}


	// AS, CR:
	// AS, CR1, CR2: ... also consider AS = CR1, in which case (2) is skipped
	// (1) period before AS
	// (2) period after AS, before CR1
	// (3) period after CR1, before CR2
	// (4) refractory period from after CR2
	// (5) end sequence
	if (biphasic->post_stim_amp_settle <= biphasic->post_stim_charge_recovery_on) {
		duration = biphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, true, false); // (1)

		duration = biphasic->post_stim_charge_recovery_on - biphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, false); // (2)

		duration = biphasic->post_stim_charge_recovery_off - biphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, true); // (3)

		duration = biphasic->refractory_period - biphasic->post_stim_charge_recovery_off;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, false); // (4)

		sequence->num_segments = *segment_index; // (5) end sequence
		return;
	}

	// CR1, CR2, AS: ... also consider AS = CR2, in which case (3) is skipped
	// (1) period before CR1
	// (2) period after CR1, before CR2
	// (3) period after CR2, before AS
	// (4) refractory period from after AS
	// (5) end sequence
	if (biphasic->post_stim_charge_recovery_off <= biphasic->post_stim_amp_settle) {
		duration = biphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, true, false); // (1)

		duration = biphasic->post_stim_charge_recovery_off - biphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, true, true); // (2)

		duration = biphasic->post_stim_amp_settle - biphasic->post_stim_charge_recovery_off;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, true, false); // (3)

		duration = biphasic->refractory_period - biphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, false); // (4)

		sequence->num_segments = *segment_index; // (5) end sequence
		return;
	}

	// CR1, AS, CR2: ... assume AS != CR1 and AS != CR2
	// (1) period before CR1
	// (2) period after CR1, before AS
	// (3) period after AS, before CR2
	// (4) refractory period from after CR2
	// (5) end sequence
	if (biphasic->post_stim_charge_recovery_on < biphasic->post_stim_amp_settle && biphasic->post_stim_amp_settle < biphasic->post_stim_charge_recovery_off) {
		duration = biphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, true, false); // (1)

		duration = biphasic->post_stim_amp_settle - biphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, true, true); // (2)

		duration = biphasic->post_stim_charge_recovery_off - biphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, true); // (3)

		duration = biphasic->refractory_period - biphasic->post_stim_charge_recovery_off;
		populate_segment(sequence, segment_index, duration, biphasic->resting_amplitude, false, false); // (4)

		sequence->num_segments = *segment_index; // (5) end sequence
		return;
	}

	else {
		// Some unforeseen combination of amp settle and charge recovery durations occurred... should not happen if parameter validation was called before this
		while (1) {};
	}
}

void create_biphasic_waveform(volatile StimSequence* const sequence, const BiphasicWaveform* const biphasic)
{
	if (biphasic->num_pulses > 1) {
		create_biphasic_train(sequence, biphasic);
	} else {
		create_biphasic_pulse(sequence, biphasic);
	}
}


void validate_triphasic(const TriphasicWaveform* const triphasic)
{
	// As soon as any rule is violated, flag this waveform as invalid
	bool valid = true;

	// channel must be between 0 and 15
	if (triphasic->channel >= MAX_NUM_CHANNELS_PER_CHIP) {
		valid = false;
	}

	// trigger_source must be between 0 and 15
	if (triphasic->trigger_source >= NUM_STIM_SEQUENCES) {
		valid = false;
	}

	// The following timing parameters must be between 0 and top of uint32_t range,
	// but no need to enforce, because they are declared as uint32_t:
	// post_trigger_delay
	// first_phase_duration
	// second_phase_duration
	// third_phase_duration
	// refractory_period
	// pre_stim_amp_settle
	// post_stim_amp_settle
	// post_stim_charge_recovery_on
	// post_stim_charge_recovery_off

	// pulse_train_period must be between 0 and top of uint64_t range,
	// but no need to enforce, because it is declared as uint64_t

	// Resting amplitude must be between -255 and 255
	if (!valid_amplitude(triphasic->resting_amplitude)) {
		valid = false;
	}

	// First phase amplitude must be between -255 and 255
	if (!valid_amplitude(triphasic->first_phase_amplitude)) {
		valid = false;
	}

	// Second phase amplitude must be between -255 and 255
	if (!valid_amplitude(triphasic->second_phase_amplitude)) {
		valid = false;
	}

	// Third phase amplitude must be between -255 and 255
	if (!valid_amplitude(triphasic->third_phase_amplitude)) {
		valid = false;
	}

	// Num pulses must be between 1 and MAX_NUM_PULSES_PER_TRAIN
	if (triphasic->num_pulses < 1 || triphasic->num_pulses > MAX_NUM_PULSES_PER_TRAIN) {
		valid = false;
	}

	// If num_pulses == 1, pulse_train_period must be 0
	if (triphasic->num_pulses == 1 && triphasic->pulse_train_period != 0) {
		valid = false;
	}

	// For trains, pulse train period must be at least as large as the total period for each pulse in that train:
	// If maintain amp settle, at least as large as first phase + second phase + third phase
	// If not maintain amp settle, at least as large as first phase + second phase + third phase + post stim amp settle + pre stim amp settle
	if (triphasic->num_pulses > 1) {
		uint64_t min_pulse_duration;
		if (triphasic->maintain_amp_settle) {
			min_pulse_duration = triphasic->first_phase_duration + triphasic->second_phase_duration + triphasic->third_phase_duration;
		} else {
			min_pulse_duration = triphasic->first_phase_duration + triphasic->second_phase_duration + triphasic->third_phase_duration + triphasic->post_stim_amp_settle + triphasic->pre_stim_amp_settle;
		}
		if (triphasic->pulse_train_period < min_pulse_duration) {
			valid = false;
		}
	}

	// If enable_amp_settle is false, pre_stim_amp_settle must be 0
	if (!triphasic->enable_amp_settle && triphasic->pre_stim_amp_settle != 0) {
		valid = false;
	}

	// If enable_amp_settle is false, post_stim_amp_settle must be 0
	if (!triphasic->enable_amp_settle && triphasic->post_stim_amp_settle != 0) {
		valid = false;
	}

	// If enable_amp_settle is false, maintain_amp_settle must be false
	if (!triphasic->enable_amp_settle && triphasic->maintain_amp_settle) {
		valid = false;
	}

	// If enable_charge_recovery is false, post_stim_charge_recovery_on must be 0
	if (!triphasic->enable_charge_recovery && triphasic->post_stim_charge_recovery_on != 0) {
		valid = false;
	}

	// If enable_charge_recovery is false, post_stim_charge_recovery_off must be 0
	if (!triphasic->enable_charge_recovery && triphasic->post_stim_charge_recovery_off != 0) {
		valid = false;
	}

	// Post trigger delay must be greater than or equal to Pre stim amp settle
	if (triphasic->post_trigger_delay < triphasic->pre_stim_amp_settle) {
		valid = false;
	}

	// Post stim charge recovery off must be greater than or equal to Post stim charge recovery on
	if (triphasic->post_stim_charge_recovery_off < triphasic->post_stim_charge_recovery_on) {
		valid = false;
	}

	// Refractory period must be greater than or equal to:
	// 1) Post stim amp settle
	// 2) Post stim charge recovery on, AND
	// 3) Post stim charge recovery off
	if ((triphasic->refractory_period < triphasic->post_stim_amp_settle) ||
			(triphasic->refractory_period < triphasic->post_stim_charge_recovery_on) ||
			(triphasic->refractory_period < triphasic->post_stim_charge_recovery_off)) {
		valid = false;
	}

	// Pulse train period must be greater than or equal to:
	// First phase duration + Second phase duration + Third phase duration
	if (triphasic->num_pulses > 1) {
		uint64_t total_duration = triphasic->first_phase_duration + triphasic->second_phase_duration + triphasic->third_phase_duration;
		if (triphasic->pulse_train_period < total_duration) {
			valid = false;
		}
	}

	if (!valid) {
		handle_error(InvalidStimWaveform);
	}
}

void create_triphasic_waveform(volatile StimSequence* const sequence, const TriphasicWaveform* const triphasic)
{
	if (triphasic->num_pulses > 1) {
		create_triphasic_train(sequence, triphasic);
	} else {
		create_triphasic_pulse(sequence, triphasic);
	}
}

void create_triphasic_train(volatile StimSequence* const sequence, const TriphasicWaveform* const triphasic)
{
	// General parameters
	sequence->channel = triphasic->channel;
	sequence->trigger_source = triphasic->trigger_source;
	sequence->loop_repeat = triphasic->num_pulses - 2; // -1 because of 'repeat' not including the first pulse, and -1 because of last pulse requiring special treatment outside of loop
	sequence->loop_start = 0; // gets populated later
	sequence->loop_end = 0; // gets populated later

	// Default, resting segment
	int segment_index = 0;
	populate_segment(sequence, &segment_index, 1, triphasic->resting_amplitude, false, false);

	// POST TRIGGER DELAY
	if (triphasic->post_trigger_delay > 0) {
		int duration = triphasic->post_trigger_delay - triphasic->pre_stim_amp_settle;
		populate_segment(sequence, &segment_index, duration, triphasic->resting_amplitude, false, false);
	}

	// Start of loop (no maintain amp settle)
	if (!triphasic->maintain_amp_settle) {
		sequence->loop_start = segment_index;
	}

	// PRE STIM AMP SETTLE
	if (triphasic->pre_stim_amp_settle > 0) {
		populate_segment(sequence, &segment_index, triphasic->pre_stim_amp_settle, triphasic->resting_amplitude, true, false);
	}

	// Start of loop (maintain amp settle)
	if (triphasic->maintain_amp_settle) {
		sequence->loop_start = segment_index;
	}

	// FIRST PHASE
	populate_segment(sequence, &segment_index, triphasic->first_phase_duration, triphasic->first_phase_amplitude, triphasic->enable_amp_settle, false);

	// SECOND PHASE
	populate_segment(sequence, &segment_index, triphasic->second_phase_duration, triphasic->second_phase_amplitude, triphasic->enable_amp_settle, false);

	// THIRD PHASE
	populate_segment(sequence, &segment_index, triphasic->third_phase_duration, triphasic->third_phase_amplitude, triphasic->enable_amp_settle, false);

	// POST STIM AMP SETTLE (no maintain)
	// Also, not applicable for the last repeat, where we have to sort between AS, CR1, and CR2, e.g. use populate_end_of_sequence
	if (!triphasic->maintain_amp_settle && triphasic->post_stim_amp_settle > 0) {
		populate_segment(sequence, &segment_index, triphasic->post_stim_amp_settle, triphasic->resting_amplitude, true, false);
	}

	// PULSE PERIOD - (first phase, second phase, third phase, post stim amp settle, pre stim amp settle) ... ignore post/pre stim amp settle durations if maintain amp settle
	int min_pulse_duration = triphasic->first_phase_duration + triphasic->second_phase_duration + triphasic->third_phase_duration;
	if (!triphasic->maintain_amp_settle) {
		min_pulse_duration += triphasic->post_stim_amp_settle + triphasic->pre_stim_amp_settle;
	}
	int remaining_pulse_train_period = triphasic->pulse_train_period - min_pulse_duration;
	if (remaining_pulse_train_period > 0) {
		populate_segment(sequence, &segment_index, remaining_pulse_train_period, triphasic->resting_amplitude, triphasic->maintain_amp_settle, false);
	}

	// End of loop
	sequence->loop_end = segment_index - 1;

	// Last pulse in train needs to be handled individually, since potentially having charge recovery events divides the pulse up into different segments.
	// FINAL PRE STIM AMP SETTLE
	if (triphasic->pre_stim_amp_settle > 0) {
		populate_segment(sequence, &segment_index, triphasic->pre_stim_amp_settle, triphasic->resting_amplitude, true, false);
	}

	// FINAL FIRST PHASE
	populate_segment(sequence, &segment_index, triphasic->first_phase_duration, triphasic->first_phase_amplitude, triphasic->enable_amp_settle, false);

	// FINAL SECOND PHASE
	populate_segment(sequence, &segment_index, triphasic->second_phase_duration, triphasic->second_phase_amplitude, triphasic->enable_amp_settle, false);

	// FINAL SECOND PHASE
	populate_segment(sequence, &segment_index, triphasic->third_phase_duration, triphasic->third_phase_amplitude, triphasic->enable_amp_settle, false);

	// Sort between post stim amp settle (AS), post stim charge recovery on (CR1), and post stim charge recovery off (CR2)
	populate_end_of_triphasic_sequence(sequence, &segment_index, triphasic);
}

void create_triphasic_pulse(volatile StimSequence* const sequence, const TriphasicWaveform* const triphasic)
{
	// General parameters
	sequence->channel = triphasic->channel;
	sequence->trigger_source = triphasic->trigger_source;
	sequence->loop_repeat = 0;
	sequence->loop_start = 0;
	sequence->loop_end = 0;

	// Default, resting segment
	int segment_index = 0;
	populate_segment(sequence, &segment_index, 1, triphasic->resting_amplitude, false, false);

	// POST TRIGGER DELAY
	if (triphasic->post_trigger_delay > 0) {
		int duration = triphasic->post_trigger_delay - triphasic->pre_stim_amp_settle;
		populate_segment(sequence, &segment_index, duration, triphasic->resting_amplitude, false, false);
	}

	// PRE STIM AMP SETTLE
	if (triphasic->pre_stim_amp_settle > 0) {
		populate_segment(sequence, &segment_index, triphasic->pre_stim_amp_settle, triphasic->resting_amplitude, true, false);
	}

	// FIRST PHASE
	populate_segment(sequence, &segment_index, triphasic->first_phase_duration, triphasic->first_phase_amplitude, triphasic->enable_amp_settle, false);

	// SECOND PHASE
	populate_segment(sequence, &segment_index, triphasic->second_phase_duration, triphasic->second_phase_amplitude, triphasic->enable_amp_settle, false);

	// THIRD PHASE
	populate_segment(sequence, &segment_index, triphasic->third_phase_duration, triphasic->third_phase_amplitude, triphasic->enable_amp_settle, false);

	// Sort between post stim amp settle (AS), post stim charge recovery on (CR1), and post stim charge recovery off (CR2)
	populate_end_of_triphasic_sequence(sequence, &segment_index, triphasic);
}

void populate_end_of_triphasic_sequence(volatile StimSequence* const sequence, int * const segment_index, const TriphasicWaveform* const triphasic)
{
	int duration = 0;
	// No AS, no CR:
	// (1) refractory period
	// (2) end sequence
	if (!triphasic->enable_amp_settle && !triphasic->enable_charge_recovery) {
		duration = triphasic->refractory_period;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, false); // (1)

		sequence->num_segments = *segment_index; // (2) end sequence
		return;
	}

	// No AS, CR:
	// (1) period before CR1 ... if CR1 = 0, this should be skipped intelligently by populate_segment
	// (2) period after CR1, before CR2
	// (3) refractory period from after CR2
	// (4) end sequence
	if (!triphasic->enable_amp_settle && triphasic->enable_charge_recovery) {
		duration = triphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, false); // (1)

		duration = triphasic->post_stim_charge_recovery_off - triphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, true) ; // (2)

		duration = triphasic->refractory_period - triphasic->post_stim_charge_recovery_off;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, false); // (3)

		sequence->num_segments = *segment_index; // (4) end sequence
		return;
	}

	// AS, no CR:
	// (1) period before AS ... if AS = 0, this should be skipped intelligently by populate_segment
	// (2) refractory period from after AS
	// (3) end sequence
	if (triphasic->enable_amp_settle && !triphasic->enable_charge_recovery) {
		duration = triphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, true, false); // (1)

		duration = triphasic->refractory_period - triphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, false); // (2)

		sequence->num_segments = *segment_index; // (3) end sequence
		return;
	}


	// AS, CR:
	// AS, CR1, CR2: ... also consider AS = CR1, in which case (2) is skippedZ
	// (1) period before AS
	// (2) period after AS, before CR1
	// (3) period after CR1, before CR2
	// (4) refractory period from after CR2
	// (5) end sequence
	if (triphasic->post_stim_amp_settle <= triphasic->post_stim_charge_recovery_on) {
		duration = triphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, true, false); // (1)

		duration = triphasic->post_stim_charge_recovery_on - triphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, false); // (2)

		duration = triphasic->post_stim_charge_recovery_off - triphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, true); // (3)

		duration = triphasic->refractory_period - triphasic->post_stim_charge_recovery_off;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, false); // (4)

		sequence->num_segments = *segment_index; // (5) end sequence
		return;
	}

	// CR1, CR2, AS: ... also consider AS = CR2, in which case (3) is skipped
	// (1) period before CR1
	// (2) period after CR1, before CR2
	// (3) period after CR2, before AS
	// (4) refractory period from after AS
	// (5) end sequence
	if (triphasic->post_stim_charge_recovery_off <= triphasic->post_stim_amp_settle) {
		duration = triphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, true, false); // (1)

		duration = triphasic->post_stim_charge_recovery_off - triphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, true, true); // (2)

		duration = triphasic->post_stim_amp_settle - triphasic->post_stim_charge_recovery_off;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, true, false); // (3)

		duration = triphasic->refractory_period - triphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, false); // (4)

		sequence->num_segments = *segment_index; // (5) end sequence
		return;
	}

	// CR1, AS, CR2: ... assume AS != CR1 and AS != CR2
	// (1) period before CR1
	// (2) period after CR1, before AS
	// (3) period after AS, before CR2
	// (4) refractory period from after CR2
	// (5) end sequence
	if (triphasic->post_stim_charge_recovery_on < triphasic->post_stim_amp_settle && triphasic->post_stim_amp_settle < triphasic->post_stim_charge_recovery_off) {
		duration = triphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, true, false); // (1)

		duration = triphasic->post_stim_amp_settle - triphasic->post_stim_charge_recovery_on;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, true, true); // (2)

		duration = triphasic->post_stim_charge_recovery_off - triphasic->post_stim_amp_settle;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, true); // (3)

		duration = triphasic->refractory_period - triphasic->post_stim_charge_recovery_off;
		populate_segment(sequence, segment_index, duration, triphasic->resting_amplitude, false, false); // (4)

		sequence->num_segments = *segment_index; // (5) end sequence
		return;
	}

	else {
		// Some unforeseen combination of amp settle and charge recovery durations occurred... should not happen if parameter validation was called before this
		while (1) {};
	}
}
