/* main.c — C sample project entry point */
#include <stdio.h>
#include <string.h>
#include "processor.h"

#define OUT_SIZE 512

static void init(void) {
    printf("Initializing...\n");
    helper_c("system ready");
}

static void run(const char *input) {
    char out[OUT_SIZE];
    int result = process(input, out, OUT_SIZE);
    if (result < 0) {
        fprintf(stderr, "Processing failed\n");
        return;
    }
    printf("Result: %s\n", out);
}

int main(int argc, char *argv[]) {
    init();
    const char *data = (argc > 1) ? argv[1] : "default input";
    run(data);
    return 0;
}
