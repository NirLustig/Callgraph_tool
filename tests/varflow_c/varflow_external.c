/*
 * varflow_external.c
 * Second C file used by varflow_test.c to test variable flow across files.
 *
 * The functions here continue the same variable chains from the main file.
 */
#include <stdio.h>

extern int g_call_count;
extern double g_total;
extern int g_shared_counter;
extern double g_energy_budget;

/* ------------------------------------------------------------------
 * external_boost_counter — receives sensor_value and returns boosted_value
 * ------------------------------------------------------------------ */
int external_boost_counter(int sensor_value, int boost_factor, int *shared_counter) {
    static int external_counter_cache = 0;
    int boosted_value = sensor_value * boost_factor;

    external_counter_cache += boosted_value;
    *shared_counter += external_counter_cache;
    g_shared_counter += 1;
    g_call_count++;

    return boosted_value;
}

/* ------------------------------------------------------------------
 * external_filter_signal — continues signal_gain into filtered_signal
 * ------------------------------------------------------------------ */
double external_filter_signal(double signal_gain, double calibration_offset, double *running_energy) {
    double filter_gain = 0.85;
    double filtered_signal = (signal_gain * filter_gain) - calibration_offset;

    if (filtered_signal < 0.0) {
        filtered_signal = 0.0;
    }

    *running_energy += filtered_signal;
    g_total += filtered_signal;
    g_call_count++;

    return filtered_signal;
}

/* ------------------------------------------------------------------
 * external_update_buffer — mutates sample_buffer and overflow_flag
 * ------------------------------------------------------------------ */
void external_update_buffer(int *sample_buffer, int buffer_size, int injected_sample, int *overflow_flag) {
    int i;
    int shifted_sample = injected_sample;

    for (i = buffer_size - 1; i > 0; i--) {
        sample_buffer[i] = sample_buffer[i - 1];
    }

    sample_buffer[0] = shifted_sample;

    if (shifted_sample > 1000) {
        *overflow_flag = 1;
    }

    g_call_count++;
}

/* ------------------------------------------------------------------
 * external_finalize_score — adjusts control_output using energy_budget
 * ------------------------------------------------------------------ */
double external_finalize_score(double filtered_signal, int safety_flag, double *energy_budget) {
    double final_score = filtered_signal;

    if (safety_flag) {
        final_score *= 0.75;
        *energy_budget -= 15.0;
    } else {
        final_score += 5.0;
        *energy_budget -= final_score * 0.1;
    }

    g_total += final_score;
    g_call_count++;

    return final_score;
}
