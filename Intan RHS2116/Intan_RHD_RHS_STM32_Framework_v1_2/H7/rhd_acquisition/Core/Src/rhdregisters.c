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

#include "rhdregisters.h"
#include "userconfig.h"
#include <math.h>


// Convert a size-16 array of bits (each bit allocated as bool) to a 16-bit word.
static inline uint16_t array_to_word(const bool* const a)
{
	uint16_t word = 0;

	for (int i = 0; i < 16; i++) {
		if (a[i]) word += (1 << i);
	}

	return word;
}


// Determine if a provided channel number is out of the valid range.
static inline bool channel_out_of_range(int channel)
{
	return (channel < 0 || channel >= MAX_NUM_CHANNELS_PER_CHIP);
}


// Drive auxiliary output low.
static inline void set_DigOut_low(RHDConfigParameters* const p)
{
	p->digOut = false;
	p->digOutHiZ = false;
}


// Drive auxiliary output high.
static inline void set_DigOut_high(RHDConfigParameters* const p)
{
	p->digOut = true;
	p->digOutHiZ = false;
}


// Set auxiliary digital output to high-impedance (HiZ) state.
static inline void set_DigOutHiZ(RHDConfigParameters* const p)
{
	p->digOut = false;
	p->digOutHiZ = true;
}


// Set the DSP offset removal filter cutoff frequency as closely to the requested
// newDspCutoffFreq (in Hz) as possible; returns the actual cutoff frequency (in Hz).
static inline double set_DSP_cutoff_freq(RHDConfigParameters* const p, double new_DSP_cutoff_freq)
{
	double f_cutoff[16] = {0};
	// Note: f_cutoff[0] = 0.0 here, but this index should not be used.
	for (int n = 1; n < 16; ++n) {
		double x = pow(2.0, (double) n);
		f_cutoff[n] = p->sample_rate * log(x / (x - 1.0)) / (2.0 * M_PI);
	}
	double log_new_DSP_cutoff_freq = log10(new_DSP_cutoff_freq);

	// Find the closest value to the requested cutoff frequency (on a logarithmic scale).
	if (new_DSP_cutoff_freq > f_cutoff[1]) {
		p->DSP_cutoff_freq = 1;
	} else if (new_DSP_cutoff_freq < f_cutoff[15]) {
		p->DSP_cutoff_freq = 15;
	} else {
		double min_log_diff = 10000000.0;
		for (int n = 1; n < 16; n++) {
			double log_f_cutoff = log10(f_cutoff[n]);
			if (fabs(log_new_DSP_cutoff_freq - log_f_cutoff) < min_log_diff) {
				min_log_diff = fabs(log_new_DSP_cutoff_freq - log_f_cutoff);
				p->DSP_cutoff_freq = n;
			}
		}
	}
	return f_cutoff[p->DSP_cutoff_freq];
}


// Return the current value of the DSP offset removal cutoff frequency (in Hz).
static inline double get_DSP_cutoff_freq(RHDConfigParameters* const p)
{
	double x = pow(2.0, (double) p->DSP_cutoff_freq);

	return p->sample_rate * log(x / (x - 1.0)) / (2.0 * M_PI);
}


// Power up or down selected amplifier on chip.
static inline void set_amp_powered(RHDConfigParameters* const p, int channel, bool powered)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->amp_pwr[channel] = powered;
}


// Power up all amplifiers on chip.
static inline void power_up_all_amps(RHDConfigParameters* const p)
{
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; ++channel) {
		p->amp_pwr[channel] = true;
	}
}


// Power down all amplifiers on chip.
static inline void power_down_all_amps(RHDConfigParameters* const p)
{
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; ++channel) {
		p->amp_pwr[channel] = false;
	}
}


// Return the value of the RH1 resistor (in ohms) corresponding to a particular upper bandwidth value (in Hz).
static inline double rH1_from_upper_bandwidth(double upper_bandwidth)
{
	const double log10f = log10(upper_bandwidth);

    return 0.9730 * pow(10.0, (8.0968 - 1.1892 * log10f + 0.04767 * log10f * log10f));
}


// Return the value of the RH2 resistor (in ohms) corresponding to a particular upper bandwidth value (in Hz).
static inline double rH2_from_upper_bandwidth(double upper_bandwidth)
{
    const double log10f = log10(upper_bandwidth);

    return 1.0191 * pow(10.0, (8.1009 - 1.0821 * log10f + 0.03383 * log10f * log10f));
}


// Return the value of the RL resistor (in ohms) corresponding to a particular lower bandwidth value (in Hz).
static inline double rL_from_lower_bandwidth(double lower_bandwidth)
{
    const double log10f = log10(lower_bandwidth);

    if (lower_bandwidth < 4.0) {
        return 1.0061 * pow(10.0, (4.9391 - 1.2088 * log10f + 0.5698 * log10f * log10f +
            0.1442 * log10f * log10f * log10f));
    } else {
        return 1.0061 * pow(10.0, (4.7351 - 0.5916 * log10f + 0.08482 * log10f * log10f));
    }
}


// Return the amplifier upper bandwidth (in Hz) corresponding to a particular value of the resistor RH1 (in Ohms).
static inline double upper_bandwidth_from_rH1(double rH1)
{
    const double a = 0.04767, b = -1.1892, c = 8.0968 - log10(rH1 / 0.9370);
    return pow(10.0, ((-b - sqrt(b * b - 4 * a * c)) / (2 * a)));
}


// Return the amplifier upper bandwidth (in Hz) corresponding to a particular value of the resistor RH2 (in Ohms).
static inline double upper_bandwidth_from_rH2(double rH2)
{
    const double a = 0.03383, b = -1.0821, c = 8.1009 - log10(rH2 / 1.0191);
    return pow(10.0, ((-b - sqrt(b * b - 4 * a * c)) / (2 * a)));
}


// Return the amplifier lower bandwidth (in Hz) corresponding to a particular value of the resistor RL (in Ohms).
static inline double lower_bandwidth_from_rL(double rL)
{
    double a, b, c;

    // Quadratic fit below is invalid for values of RL less than 5.1 kOhm.
    const double valid_rL = (rL < 5100.0) ? 5100.0 : rL;

    if (valid_rL < 30000.0) {
        a = 0.08482;
        b = -0.5916;
        c = 4.7351 - log10(valid_rL / 1.0061);
    } else {
        a = 0.3303;
        b = -1.2100;
        c = 4.9873 - log10(valid_rL / 1.0061);
    }

    return pow(10.0, ((-b - sqrt(b * b - 4 * a * c)) / (2 * a)));
}


// Set the on-chip RH1 and RH2 DAC values appropriately to set a particular amplifier
// upper bandwidth (in Hz).  Return an estimate of the actual upper bandwidth achieved.
static double set_upper_bandwidth(RHDConfigParameters* const p, double upper_bandwidth)
{
	const double RH1Base = 2200.0;
	const double RH1Dac1Unit = 600.0;
	const double RH1Dac2Unit = 29400.0;
	const int RH1Dac1Steps = 63;
	const int RH1Dac2Steps = 31;

	const double RH2Base = 8700.0;
	const double RH2Dac1Unit = 763.0;
	const double RH2Dac2Unit = 38400.0;
	const int RH2Dac1Steps = 63;
	const int RH2Dac2Steps = 31;

	// No upper bandwidths higher than 30 kHz.
	if (upper_bandwidth > 30000.0) {
		upper_bandwidth = 30000.0;
	}

	const double rH1_target = rH1_from_upper_bandwidth(upper_bandwidth);

	p->rH1_DAC1 = 0;
	p->rH1_DAC2 = 0;
	double rH1_actual = RH1Base;

	for (int i = 0; i < RH1Dac2Steps; ++i) {
		if (rH1_actual < rH1_target - (RH1Dac2Unit - RH1Dac1Unit / 2)) {
			rH1_actual += RH1Dac2Unit;
			++p->rH1_DAC2;
		}
	}

	for (int i = 0; i < RH1Dac1Steps; ++i) {
		if (rH1_actual < rH1_target - (RH1Dac1Unit / 2)) {
			rH1_actual += RH1Dac1Unit;
			++p->rH1_DAC1;
		}
	}

	double rH2_target = rH2_from_upper_bandwidth(upper_bandwidth);

	p->rH2_DAC1 = 0;
	p->rH2_DAC2 = 0;
	double rH2_actual = RH2Base;

	for (int i = 0; i < RH2Dac2Steps; ++i) {
		if (rH2_actual < rH2_target - (RH2Dac2Unit - RH2Dac1Unit / 2)) {
			rH2_actual += RH2Dac2Unit;
			++p->rH2_DAC2;
		}
	}

	for (int i = 0; i < RH2Dac1Steps; ++i) {
		if (rH2_actual < rH2_target - (RH2Dac1Unit / 2)) {
			rH2_actual += RH2Dac1Unit;
			++p->rH2_DAC1;
		}
	}

	const double actual_upper_bandwidth1 = upper_bandwidth_from_rH1(rH1_actual);
	const double actual_upper_bandwidth2 = upper_bandwidth_from_rH2(rH2_actual);

	// Upper bandwidth estimates calculated from actual RH1 value and actual RH2 value
	// should be very close; we will take their geometric means to get a single number.
	return sqrt(actual_upper_bandwidth1 * actual_upper_bandwidth2);
}


// Set the on-chip RL DAC values appropriately to set a particular amplifier lower bandwidth (in Hz).
// Return an estimate of the actual lower bandwidth achieved.
static double set_lower_bandwidth(RHDConfigParameters* const p, double lower_bandwidth)
{
    const double RLBase = 3500.0;
    const double RLDac1Unit = 175.0;
    const double RLDac2Unit = 12700.0;
    const double RLDac3Unit = 3000000.0;
    const int RLDac1Steps = 127;
    const int RLDac2Steps = 63;

    // No lower bandwidths higher than 1.5 kHz.
    if (lower_bandwidth > 1500.0) {
    	lower_bandwidth = 1500.0;
    }

    const double rL_target = rL_from_lower_bandwidth(lower_bandwidth);

    p->rL_DAC1 = 0;
    p->rL_DAC2 = 0;
    p->rL_DAC3 = 0;
    double rL_actual = RLBase;

    if (lower_bandwidth < 0.15) {
    	rL_actual += RLDac3Unit;
    	++p->rL_DAC3;
    }

    for (int i = 0; i < RLDac2Steps; ++i) {
    	if (rL_actual < rL_target - (RLDac2Unit - RLDac1Unit / 2)) {
    		rL_actual += RLDac2Unit;
    		++p->rL_DAC2;
    	}
    }

    for (int i = 0; i < RLDac1Steps; ++i) {
    	if (rL_actual < rL_target - (RLDac1Unit / 2)) {
    		rL_actual += RLDac1Unit;
    		++p->rL_DAC1;
    	}
    }

    return lower_bandwidth_from_rL(rL_actual);
}


// Determine suitable ADC buffer bias and mux bias values based on provided sample rate.
static inline void set_biases_based_on_sample_rate(uint8_t* const adc_buffer_bias, uint8_t* const mux_bias, double sample_rate)
{
	const double adc_sampling_rate = (CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE) * sample_rate;

	if (adc_sampling_rate <= 120000.0) {
		*adc_buffer_bias = 32;
		*mux_bias = 40;
	} else if (adc_sampling_rate <= 140000.0) {
		*adc_buffer_bias = 16;
		*mux_bias = 40;
	} else if (adc_sampling_rate <= 175000.0) {
		*adc_buffer_bias = 8;
		*mux_bias = 40;
	} else if (adc_sampling_rate <= 220000.0) {
		*adc_buffer_bias = 8;
		*mux_bias = 32;
	} else if (adc_sampling_rate <= 280000.0) {
		*adc_buffer_bias = 8;
		*mux_bias = 26;
	} else if (adc_sampling_rate <= 350000.0) {
		*adc_buffer_bias = 4;
		*mux_bias = 18;
	} else if (adc_sampling_rate <= 440000.0) {
		*adc_buffer_bias = 3;
		*mux_bias = 16;
	} else if (adc_sampling_rate <= 525000.0) {
		*adc_buffer_bias = 3;
		*mux_bias = 7;
	} else {
		*adc_buffer_bias = 2;
		*mux_bias = 4;
	}
}


// Select the series capacitor used to convert the voltage waveform generated by the on-chip DAC into an AC
// current waveform that stimulates a selected electrode for impedance testing (ZcheckCs100fF, ZcheckCs1pF, or Zcheck10pF).
void set_zcheck_scale(RHDConfigParameters* const p, ZcheckCs scale)
{
	switch (scale) {
	case ZcheckCs100fF:
		p->zcheck_scale = 0x00;		// Cs = 0.1 pF
		break;
	case ZcheckCs1pF:
		p->zcheck_scale = 0x01;		// Cs = 1.0 pF
		break;
	case ZcheckCs10pF:
		p->zcheck_scale = 0x03;
		break;
	}
}


// Select impedance testing of positive or negative amplifier inputs (RHD2216 only), based on
// the variable polarity (ZcheckPositiveInput or ZcheckNegativeInput).
void set_zcheck_polarity(RHDConfigParameters* const p, ZcheckPolarity polarity)
{
	p->zcheck_sel_pol = polarity == ZcheckNegativeInput; // Negative: 1, Positive: 0
}


// Select the amplifier channel for impedance testing.
int set_zcheck_channel(RHDConfigParameters* const p, int channel)
{
	if (channel_out_of_range(channel)) {
		return -1;
	}
	p->zcheck_select = channel;
	return p->zcheck_select;
}


// Set default values for parameters used to configure RAM registers on RHD2000 chip.
void set_default_rhd_settings(RHDConfigParameters *p)
{
	// Register 0: ADC Configuration and Amplifier Fast Settle
	// D[7:6]: ADC reference BW
	// D[5]: amp fast settle
	// D[4]: amp Vref enable
	// D[3:2]: ADC comparator bias
	// D[1:0]: ADC comparator select
	p->adc_reference_bw = 3;
	p->amp_fast_settle = false;
	p->amp_vref_enable = true;
	p->adc_comparator_bias = 3;
	p->adc_comparator_select = 2;

	/* NOTE: ADC buffer bias and MUX bias should be set to values based on the total ADC sampling rate. This is not
	 * the per-channel sampling rate, but rather determined by the minimum period of time between subsequent CONVERT
	 * commands. Please see pg. 27 of the Intan RHD2000 chip datasheet for a table of suitable ADC buffer bias and MUX
	 * bias values for ranges of sampling rates. When in doubt, the values suited for the highest sampling rates (ADC
	 * buffer bias of 2, MUX bias of 4) work for all sampling rates, while other values just offer slight improvements
	 * to power dissipation if the total ADC sampling rate is low enough for them to be used safely.
	 */

	// Register 1: Supply Sensor and ADC Buffer Bias Current
	// D[7]: X (No Function)
	// D[6]: VDD sense enable
	// D[5:0]: ADC buffer bias (set below in set_biases_based_on_sample_rate)
	p->vdd_sense_enable = true;

	// Register 2: MUX Bias Current
	// D[7:6]: X (No Function)
	// D[5:0]: MUX bias (set below in set_biases_based_on_sample_rate)
	set_biases_based_on_sample_rate(&p->adc_buffer_bias, &p->mux_bias, p->sample_rate);

	// Register 3: MUX Load, Temperature Sensor, and Auxiliary Digital Output
	// D[7:5]: MUX load
	// D[4]: tempS2
	// D[3]: tempS1
	// D[2]: tempen
	// D[1]: digout HiZ
	// D[0]: digout
	p->mux_load = 0;
	p->temp_S1 = false;
	p->temp_S2 = false;
	p->temp_en = false;
	set_DigOutHiZ(p);

	// Register 4: ADC Output Format and DSP Offset Removal
	// D[7]: weak MISO
	// D[6]: twoscomp
	// D[5]: absmode
	// D[4]: DSPen
	// D[3:0]: DSP cutoff freq
	p->weak_miso = true;
	p->twos_comp = false;
	p->abs_mode = false;
	p->DSP_en = true;
	set_DSP_cutoff_freq(p, 1.0);

	// Register 5: Impedance Check Control
	// D[7]: X (No Function)
	// D[6]: Zcheck DAC power
	// D[5]: Zcheck load
	// D[4:3]: Zcheck scale
	// D[2]: Zcheck conn all
	// D[1]: Zcheck sel pol
	// D[0]: Zcheck en
	p->zcheck_DAC_power = true;
	p->zcheck_load = false;
	set_zcheck_scale(p, ZcheckCs100fF);
	p->zcheck_conn_all = false;
	set_zcheck_polarity(p, ZcheckPositiveInput);
	p->zcheck_en = false;

	// Register 6: Impedance Check DAC
	// D[7:0]: Zcheck DAC

	// Register 7: Impedance Check Amplifier Select
	// D[7:6]: X (No Function)
	// D[5:0]: Zcheck select
	set_zcheck_channel(p, 0);

	// Registers 8-13: On-Chip Amplifier Bandwidth Select
	// R[8]D[7]: offchip RH1
	// R[8]D[6]: X (No Function)
	// R[8]D[5:0]: RH1 DAC1
	p->off_chip_RH1 = false;

	// R[9]D[7]: ADC aux1 en
	// R[9]D[6:5]: X (No Function)
	// R[9]D[4:0] RH1 DAC2
	p->adc_Aux1_en = true;

	// R[10]D[7]: offchip RH2
	// R[10]D[6]: X (No Function)
	// R[10]D[5:0]: RH2 DAC1
	p->off_chip_RH2 = false;

	// R[11]D[7]: ADC2 aux2 en
	// R[11]D[6:5]: X (No Function)
	// R[11]D[4:0]: RH2 DAC2
	p->adc_Aux2_en = true;

	// R[12]D[7]: offchip RL
	// R[12]D[6:0]: RL DAC1
	p->off_chip_RL = false;

	// R[13]D[7]: ADC3 aux3 en
	// R[13]D[6]: RL DAC3
	// R[13]D[5:0]: RL DAC2
	p->adc_Aux3_en = true;

	set_upper_bandwidth(p, 7500.0);
	set_lower_bandwidth(p, 1.0);

	// Register 14-17: Individual Amplifier Power
	// R[14]D[7:0]: apwr[7:0]
	// R[15]D[7:0]: apwr[15:8]
	// R[16]D[7:0]: apwr[23:16]
	// R[17]D[7:0]: apwr[31:0]
	power_up_all_amps(p);
}


// Return the value of a selected RAM register (0-17) on the RHD2000 chip,
// based on the current register variables in RHDConfigParameters.
uint16_t get_register_value(const RHDConfigParameters* const p, int reg)
{
	int regout;
	const int ZcheckDac = 128;  // midrange

	switch (reg) {
	case 0:
		regout = (p->adc_reference_bw << 6) + (p->amp_fast_settle << 5) + (p->amp_vref_enable << 4) +
			(p->adc_comparator_bias << 2) + p->adc_comparator_select;
		break;

	case 1:
		regout = (p->vdd_sense_enable << 6) + p->adc_buffer_bias;
		break;

	case 2:
		regout = p->mux_bias;
		break;

	case 3:
		regout = (p->mux_load << 5) + (p->temp_S2 << 4) + (p->temp_S1 << 3) + (p->temp_en << 2) +
			(p->digOutHiZ << 1) + p->digOut;
		break;

	case 4:
		regout = (p->weak_miso << 7) + (p->twos_comp << 6) + (p->abs_mode << 5) + (p->DSP_en << 4) +
			(p->DSP_cutoff_freq);
		break;

	case 5:
		regout = (p->zcheck_DAC_power << 6) + (p->zcheck_load << 5) + (p->zcheck_scale << 3) +
			(p->zcheck_conn_all << 2) + (p->zcheck_sel_pol << 1) + p->zcheck_en;
		break;

	case 6:
		regout = ZcheckDac;
		break;

	case 7:
		regout = p->zcheck_select;
		break;

	case 8:
		regout = (p->off_chip_RH1 << 7) + p->rH1_DAC1;
		break;

	case 9:
		regout = (p->adc_Aux1_en << 7) + p->rH1_DAC2;
		break;

	case 10:
		regout = (p->off_chip_RH2 << 7) + p->rH2_DAC1;
		break;

	case 11:
		regout = (p->adc_Aux2_en << 7) + p->rH2_DAC2;
		break;

	case 12:
		regout = (p->off_chip_RL << 7) + p->rL_DAC1;
		break;

	case 13:
		regout = (p->adc_Aux3_en << 7) + (p->rL_DAC3 << 6) + p->rL_DAC2;
		break;

	case 14:
		regout = (p->amp_pwr[7] << 7) + (p->amp_pwr[6] << 6) + (p->amp_pwr[5] << 5) + (p->amp_pwr[4] << 4) +
			(p->amp_pwr[3] << 3) + (p->amp_pwr[2] << 2) + (p->amp_pwr[1] << 1) + p->amp_pwr[0];
		break;

	case 15:
		regout = (p->amp_pwr[15] << 7) + (p->amp_pwr[14] << 6) + (p->amp_pwr[13] << 5) + (p->amp_pwr[12] << 4) +
			(p->amp_pwr[11] << 3) + (p->amp_pwr[10] << 2) + (p->amp_pwr[9] << 1) + p->amp_pwr[8];
		break;

	case 16:
		regout = (p->amp_pwr[23] << 7) + (p->amp_pwr[22] << 6) + (p->amp_pwr[21] << 5) + (p->amp_pwr[20] << 4) +
			(p->amp_pwr[19] << 3) + (p->amp_pwr[18] << 2) + (p->amp_pwr[17] << 1) + p->amp_pwr[16];
		break;

	case 17:
		regout = (p->amp_pwr[31] << 7) + (p->amp_pwr[30] << 6) + (p->amp_pwr[29] << 5) + (p->amp_pwr[28] << 4) +
			(p->amp_pwr[27] << 3) + (p->amp_pwr[26] << 2) + (p->amp_pwr[25] << 1) + p->amp_pwr[24];
		break;

	default:
		regout = 0;
	}
	return regout;
}
