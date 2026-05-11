/*
 * varflow_test.c
 * Realistic C code for debugging and verifying Variable Flow Mode.
 *
 * Contains: global var, static var, locals, assignments, reassignments,
 *           loops, conditionals, function arguments, return values.
 */
#include <stdio.h>
#include <stdbool.h>

/* Global: shared call counter across all functions */
int g_call_count = 0;

/* Global: accumulator updated by multiple functions */
double g_total = 0.0;

/* ------------------------------------------------------------------
 * compute_sum — sum integers 1..n; uses loop + local accumulator
 * ------------------------------------------------------------------ */
int compute_sum(int n) {
    int sum = 0;
    int i;
    for (i = 1; i <= n; i++) {
        sum += i;
    }
    g_call_count++;
    return sum;
}

/* ------------------------------------------------------------------
 * factorial — recursive-style iterative factorial; uses long long result
 * ------------------------------------------------------------------ */
long long factorial(int n) {
    long long result = 1;
    int i;
    if (n < 0) {
        return -1;
    }
    for (i = 1; i <= n; i++) {
        result *= i;
    }
    g_call_count++;
    return result;
}

/* ------------------------------------------------------------------
 * is_prime — primality test; uses static counter + local flag
 * ------------------------------------------------------------------ */
bool is_prime(int n) {
    static int prime_checks = 0;   /* static: persists between calls */
    bool found_divisor = false;
    int i;

    prime_checks++;

    if (n < 2) {
        return false;
    }
    for (i = 2; i * i <= n; i++) {
        if (n % i == 0) {
            found_divisor = true;
            break;
        }
    }
    return !found_divisor;
}

/* ------------------------------------------------------------------
 * compute_score — weighted scoring; multiple branches update bonus
 * ------------------------------------------------------------------ */
double compute_score(int raw_score, double weight) {
    double base     = (double)raw_score;
    double adjusted = base * weight;
    double bonus    = 0.0;

    if (raw_score > 100) {
        bonus = 10.0;
    } else if (raw_score > 50) {
        bonus = 5.0;
    } else {
        bonus = 1.0;
    }

    double final_score = adjusted + bonus;
    g_total += final_score;
    g_call_count++;
    return final_score;
}

/* ------------------------------------------------------------------
 * process_data — array stats; reuses 'total', 'average', 'i', 'val'
 * ------------------------------------------------------------------ */
double process_data(int *values, int count) {
    int    total   = 0;
    int    i;
    double average = 0.0;
    int    max_val = values[0];
    int    min_val = values[0];

    for (i = 0; i < count; i++) {
        int val = values[i];
        total  += val;
        if (val > max_val) max_val = val;
        if (val < min_val) min_val = val;
    }

    if (count > 0) {
        average = (double)total / (double)count;
    }

    g_total += average;
    g_call_count++;
    return average;
}

/* ------------------------------------------------------------------
 * count_primes — counts primes in [2, limit]; calls is_prime
 * ------------------------------------------------------------------ */
int count_primes(int limit) {
    int count = 0;
    int i;
    for (i = 2; i <= limit; i++) {
        if (is_prime(i)) {
            count++;
        }
    }
    g_call_count++;
    return count;
}

/* ------------------------------------------------------------------
 * main — ties everything together; uses most variables
 * ------------------------------------------------------------------ */
int main(void) {
    int    n         = 10;
    int    data[]    = {3, 7, 2, 9, 1, 5, 8, 4, 6, 10};
    int    data_size = 10;
    double weight    = 1.5;

    /* Basic computations */
    int       sum  = compute_sum(n);
    long long fact = factorial(n);

    /* Prime counting */
    int prime_count = count_primes(n);

    /* Array processing */
    double avg = process_data(data, data_size);

    /* Score using sum as raw_score */
    double score = compute_score(sum, weight);

    /* Reassign weight and recompute for comparison */
    weight = 2.0;
    double score2 = compute_score(sum, weight);

    /* Conditional output */
    if (score2 > score) {
        printf("Higher weight raised score from %.2f to %.2f\n", score, score2);
    } else {
        printf("Scores: %.2f and %.2f\n", score, score2);
    }

    printf("Sum 1..%d         = %d\n",   n, sum);
    printf("Factorial(%d)      = %lld\n", n, fact);
    printf("Primes up to %d   = %d\n",   n, prime_count);
    printf("Average of data   = %.2f\n", avg);
    printf("g_total           = %.2f\n", g_total);
    printf("g_call_count      = %d\n",   g_call_count);

    return 0;
}
