/*
 * commonautosequences.h
 *
 *  Created on: Apr 4, 2025
 *      Author: afoy
 */

#ifndef INC_COMMONAUTOSEQUENCES_H_
#define INC_COMMONAUTOSEQUENCES_H_

#include "stimscheduler.h"

// Amplitudes are measured in stim current steps
// Durations are measured in timesteps
typedef struct {
	// Channel/trigger
	uint8_t channel;
	uint8_t trigger_source;

	// Pulse train
	uint32_t num_pulses;
	uint64_t pulse_train_period;

	// Current amplitude
	int16_t resting_amplitude;
	int16_t first_phase_amplitude;
	int16_t second_phase_amplitude;

	// Stimulation timing
	uint32_t post_trigger_delay;
	uint32_t first_phase_duration;
	uint32_t interphase_delay;
	uint32_t second_phase_duration;
	uint32_t refractory_period;

	// Amp settle
	bool enable_amp_settle;
	uint32_t pre_stim_amp_settle;
	uint32_t post_stim_amp_settle;
	bool maintain_amp_settle;

	// Charge recovery
	bool enable_charge_recovery;
	uint32_t post_stim_charge_recovery_on;
	uint32_t post_stim_charge_recovery_off;
} BiphasicWaveform;


typedef struct {
	// Channel/trigger
	uint8_t channel;
	uint8_t trigger_source;

	// Pulse train
	uint32_t num_pulses;
	uint64_t pulse_train_period;

	// Current amplitude
	int16_t resting_amplitude;
	int16_t first_phase_amplitude;
	int16_t second_phase_amplitude;
	int16_t third_phase_amplitude;

	// Stimulation timing
	uint32_t post_trigger_delay;
	uint32_t first_phase_duration;
	uint32_t second_phase_duration;
	uint32_t third_phase_duration;
	uint32_t refractory_period;

	// Amp settle
	bool enable_amp_settle;
	uint32_t pre_stim_amp_settle;
	uint32_t post_stim_amp_settle;
	bool maintain_amp_settle;

	// Charge recovery
	bool enable_charge_recovery;
	uint32_t post_stim_charge_recovery_on;
	uint32_t post_stim_charge_recovery_off;
} TriphasicWaveform;

void validate_biphasic(const BiphasicWaveform* const biphasic);
void validate_triphasic(const TriphasicWaveform* const triphasic);

void create_biphasic_waveform(volatile StimSequence* const sequence, const BiphasicWaveform* const biphasic);
void create_biphasic_train(volatile StimSequence* const sequence, const BiphasicWaveform* const biphasic);
void create_biphasic_pulse(volatile StimSequence* const sequence, const BiphasicWaveform* const biphasic);
void populate_end_of_biphasic_sequence(volatile StimSequence* const sequence,  int * const segment_index, const BiphasicWaveform* const biphasic);

void create_triphasic_waveform(volatile StimSequence* const sequence, const TriphasicWaveform* const triphasic);
void create_triphasic_train(volatile StimSequence* const sequence, const TriphasicWaveform* const triphasic);
void create_triphasic_pulse(volatile StimSequence* const sequence, const TriphasicWaveform* const triphasic);
void populate_end_of_triphasic_sequence(volatile StimSequence* const sequence, int * const segment_index, const TriphasicWaveform* const triphasic);

#endif /* INC_COMMONAUTOSEQUENCES_H_ */
