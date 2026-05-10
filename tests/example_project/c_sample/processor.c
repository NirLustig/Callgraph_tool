/* processor.c — C sample project: data processing functions */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define MAX_BUF 256

static int validate_input(const char *data) {
    if (!data || strlen(data) == 0) {
        return 0;
    }
    return 1;
}

static char *copy_buffer(const char *src) {
    char *buf = (char *)malloc(MAX_BUF);
    if (!buf) return NULL;
    strncpy(buf, src, MAX_BUF - 1);
    buf[MAX_BUF - 1] = '\0';
    return buf;
}

int process(const char *data, char *out_buf, int buf_size) {
    if (!validate_input(data)) {
        fprintf(stderr, "Invalid input\n");
        return -1;
    }

    char *tmp = copy_buffer(data);
    if (!tmp) return -1;

    int len = (int)strlen(tmp);
    snprintf(out_buf, buf_size, "processed(%d): %s", len, tmp);
    free(tmp);
    return len;
}

void helper_c(const char *msg) {
    printf("[helper] %s\n", msg);
}
