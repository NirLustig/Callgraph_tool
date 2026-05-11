/*
 * varflow_chain.c
 *
 * Demonstrates deep variable alias tracking across multiple function calls.
 *
 * A single value starts as `raw_input` in main_chain() and is passed through
 * five successive pipeline stages, each using a different parameter name.
 * Variable Flow Mode should track the same underlying data through all six
 * name changes:
 *
 *   raw_input  (main_chain)
 *     -> data_in      (stage_preprocess)
 *       -> input_val  (stage_normalize)
 *         -> signal_value (stage_smooth)
 *           -> level      (stage_threshold)
 *             -> output_level (stage_output)
 *
 * Additionally, a second independent chain demonstrates a parallel branch:
 *
 *   base_rate  (compute_base)
 *     -> rate_in (apply_gain)
 *       -> gain_input (limit_range)
 *         -> bounded   (scale_result)
 */

#include <stdio.h>

/* ------------------------------------------------------------------ */
/* Chain 1: raw_input -> data_in -> input_val -> signal_value ->       */
/*           level -> output_level                                      */
/* ------------------------------------------------------------------ */

static void stage_output(int output_level) {
    int final_result = output_level;
    printf("Pipeline result: %d\n", final_result);
}

static void stage_threshold(int level) {
    int safe_level = level > 255 ? 255 : (level < 0 ? 0 : level);
    printf("Threshold applied: raw=%d, safe=%d\n", level, safe_level);
    stage_output(level);          /* level -> output_level */
}

static void stage_smooth(int signal_value) {
    int prev  = signal_value - 1;
    int next  = signal_value + 1;
    int avg   = (prev + signal_value + next) / 3;
    printf("Smooth: in=%d, avg=%d\n", signal_value, avg);
    stage_threshold(signal_value); /* signal_value -> level */
}

static void stage_normalize(int input_val) {
    double scale = 1.0 / 100.0;
    int    norm  = (int)(input_val * scale * 100);
    printf("Normalize: in=%d, norm=%d\n", input_val, norm);
    stage_smooth(input_val);       /* input_val -> signal_value */
}

static void stage_preprocess(int data_in) {
    int bias     = 5;
    int adjusted = data_in + bias;
    printf("Preprocess: in=%d, adjusted=%d\n", data_in, adjusted);
    stage_normalize(data_in);      /* data_in -> input_val */
}

void main_chain(void) {
    int raw_input  = 42;
    int iterations = 3;
    int i;
    printf("Starting pipeline: raw_input=%d, iterations=%d\n",
           raw_input, iterations);
    for (i = 0; i < iterations; i++) {
        stage_preprocess(raw_input); /* raw_input -> data_in */
        raw_input += 10;
    }
}

/* ------------------------------------------------------------------ */
/* Chain 2: base_rate -> rate_in -> gain_input -> bounded              */
/* ------------------------------------------------------------------ */

static void scale_result(int bounded) {
    int scaled = bounded * 4;
    printf("Scaled output: %d\n", scaled);
}

static void limit_range(int gain_input) {
    int bounded = gain_input > 100 ? 100 : gain_input;
    printf("Limit: in=%d, bounded=%d\n", gain_input, bounded);
    scale_result(gain_input);      /* gain_input -> bounded */
}

static void apply_gain(int rate_in) {
    int amplified = rate_in * 2;
    printf("Gain: in=%d, amplified=%d\n", rate_in, amplified);
    limit_range(rate_in);          /* rate_in -> gain_input */
}

void compute_base(void) {
    int base_rate = 25;
    int offset    = 3;
    base_rate += offset;
    printf("Base rate: %d\n", base_rate);
    apply_gain(base_rate);         /* base_rate -> rate_in */
}
