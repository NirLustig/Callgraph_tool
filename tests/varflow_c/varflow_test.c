/*
 * varflow_test.c
 * Expanded C code for debugging and verifying Variable Flow Mode.
 *
 * This file intentionally passes the same variables through several functions,
 * including local variables, pointers, globals, static variables, arrays,
 * structs-like grouped values through parameters, and cross-file calls.
 */
#include <stdio.h>
#include <stdbool.h>

/* Cross-file functions implemented in varflow_external.c */
extern int external_boost_counter(int sensor_value, int boost_factor, int *shared_counter);
extern double external_filter_signal(double signal_gain, double calibration_offset, double *running_energy);
extern void external_update_buffer(int *sample_buffer, int buffer_size, int injected_sample, int *overflow_flag);
extern double external_finalize_score(double filtered_signal, int safety_flag, double *energy_budget);

/* Global variables updated across multiple functions */
int g_call_count = 0;
double g_total = 0.0;
int g_shared_counter = 0;
double g_energy_budget = 250.0;

/* ------------------------------------------------------------------
 * clamp_value — bounds sensor_value and updates safety_flag by pointer
 * ------------------------------------------------------------------ */
int clamp_value(int sensor_value, int min_limit, int max_limit, int *safety_flag) {
    if (sensor_value < min_limit) {
        *safety_flag = 1;
        return min_limit;
    }
    if (sensor_value > max_limit) {
        *safety_flag = 1;
        return max_limit;
    }
    return sensor_value;
}

/* ------------------------------------------------------------------
 * normalize_signal — converts sensor_value into signal_gain
 * ------------------------------------------------------------------ */
double normalize_signal(int sensor_value, double calibration_offset) {
    double signal_gain = ((double)sensor_value + calibration_offset) / 100.0;
    g_total += signal_gain;
    g_call_count++;
    return signal_gain;
}

/* ------------------------------------------------------------------
 * accumulate_energy — modifies running_energy through a pointer
 * ------------------------------------------------------------------ */
void accumulate_energy(double signal_gain, double *running_energy, double energy_scale) {
    static double local_energy_cache = 0.0;

    local_energy_cache += signal_gain * energy_scale;
    *running_energy += local_energy_cache;
    g_energy_budget -= signal_gain;
    g_call_count++;
}

/* ------------------------------------------------------------------
 * update_sample_buffer — moves sample_index and sample_buffer forward
 * ------------------------------------------------------------------ */
void update_sample_buffer(int *sample_buffer, int buffer_size, int *sample_index, int sensor_value) {
    if (*sample_index >= buffer_size) {
        *sample_index = 0;
    }

    sample_buffer[*sample_index] = sensor_value;
    (*sample_index)++;
    g_shared_counter++;
}

/* ------------------------------------------------------------------
 * calculate_stability — repeatedly uses stability_score
 * ------------------------------------------------------------------ */
double calculate_stability(int *sample_buffer, int buffer_size, double signal_gain) {
    double stability_score = 0.0;
    int i;

    for (i = 0; i < buffer_size; i++) {
        stability_score += (double)sample_buffer[i] * signal_gain;
    }

    stability_score = stability_score / (double)buffer_size;
    g_total += stability_score;
    return stability_score;
}

/* ------------------------------------------------------------------
 * compute_control_output — combines several variable flows
 * ------------------------------------------------------------------ */
double compute_control_output(double filtered_signal,
                              double stability_score,
                              int boost_factor,
                              int safety_flag) {
    double control_output = filtered_signal + stability_score;

    if (safety_flag) {
        control_output *= 0.5;
    } else {
        control_output *= (double)boost_factor;
    }

    g_total += control_output;
    g_call_count++;
    return control_output;
}

/* ------------------------------------------------------------------
 * route_sensor_pipeline — main internal chain for selected variables
 * ------------------------------------------------------------------ */
double route_sensor_pipeline(int sensor_value,
                             double calibration_offset,
                             int boost_factor,
                             int *sample_buffer,
                             int buffer_size,
                             int *sample_index,
                             double *running_energy) {
    int safety_flag = 0;
    int min_limit = 5;
    int max_limit = 500;

    sensor_value = clamp_value(sensor_value, min_limit, max_limit, &safety_flag);
    update_sample_buffer(sample_buffer, buffer_size, sample_index, sensor_value);

    int boosted_value = external_boost_counter(sensor_value, boost_factor, &g_shared_counter);
    double signal_gain = normalize_signal(boosted_value, calibration_offset);

    accumulate_energy(signal_gain, running_energy, 1.25);
    double filtered_signal = external_filter_signal(signal_gain, calibration_offset, running_energy);

    double stability_score = calculate_stability(sample_buffer, buffer_size, filtered_signal);
    double control_output = compute_control_output(filtered_signal, stability_score, boost_factor, safety_flag);

    external_update_buffer(sample_buffer, buffer_size, boosted_value, &safety_flag);
    control_output = external_finalize_score(control_output, safety_flag, &g_energy_budget);

    return control_output;
}

/* ------------------------------------------------------------------
 * report_pipeline_state — reads final values and globals
 * ------------------------------------------------------------------ */
void report_pipeline_state(int sensor_value,
                           double calibration_offset,
                           int boost_factor,
                           double running_energy,
                           double control_output) {
    printf("sensor_value       = %d\n", sensor_value);
    printf("calibration_offset = %.2f\n", calibration_offset);
    printf("boost_factor       = %d\n", boost_factor);
    printf("running_energy     = %.2f\n", running_energy);
    printf("control_output     = %.2f\n", control_output);
    printf("g_shared_counter   = %d\n", g_shared_counter);
    printf("g_energy_budget    = %.2f\n", g_energy_budget);
    printf("g_total            = %.2f\n", g_total);
    printf("g_call_count       = %d\n", g_call_count);
}

int main(void) {
    int sensor_value = 42;
    double calibration_offset = 3.5;
    int boost_factor = 4;
    int sample_buffer[6] = {10, 12, 14, 16, 18, 20};
    int buffer_size = 6;
    int sample_index = 0;
    double running_energy = 0.0;

    double control_output = route_sensor_pipeline(sensor_value,
                                                  calibration_offset,
                                                  boost_factor,
                                                  sample_buffer,
                                                  buffer_size,
                                                  &sample_index,
                                                  &running_energy);

    sensor_value = sensor_value + 15;
    control_output = route_sensor_pipeline(sensor_value,
                                           calibration_offset,
                                           boost_factor,
                                           sample_buffer,
                                           buffer_size,
                                           &sample_index,
                                           &running_energy);

    report_pipeline_state(sensor_value,
                          calibration_offset,
                          boost_factor,
                          running_energy,
                          control_output);

    return 0;
}
