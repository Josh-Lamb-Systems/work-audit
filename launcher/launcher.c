/*
 * Work Audit launcher.
 *
 * macOS grants Screen Recording and Automation permissions to *apps*. This tiny
 * binary is the app ("Work Audit.app"): it starts bin/run.sh (the Python menu bar
 * app) as a child process and waits for it. Child processes inherit the app's
 * permissions, so you approve "Work Audit" once and can then update the Python
 * code freely without macOS asking again.
 */
#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef WA_ROOT
#define WA_ROOT ""
#endif

extern char **environ;
static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static int is_file(const char *p) {
    struct stat st;
    return stat(p, &st) == 0 && S_ISREG(st.st_mode);
}

int main(void) {
    char exe[PATH_MAX], real[PATH_MAX], buf[PATH_MAX], script[PATH_MAX], app[PATH_MAX];
    uint32_t size = sizeof(exe);
    script[0] = '\0';
    app[0] = '\0';

    if (_NSGetExecutablePath(exe, &size) == 0 && realpath(exe, real) != NULL) {
        /* real = <root>/Work Audit.app/Contents/MacOS/WorkAudit -> go up four levels */
        strncpy(buf, real, sizeof(buf) - 1);
        buf[sizeof(buf) - 1] = '\0';
        char *d = dirname(buf);             /* .../Contents/MacOS */
        char lvl1[PATH_MAX]; strncpy(lvl1, d, sizeof(lvl1) - 1); lvl1[sizeof(lvl1) - 1] = '\0';
        d = dirname(lvl1);                  /* .../Contents */
        char lvl2[PATH_MAX]; strncpy(lvl2, d, sizeof(lvl2) - 1); lvl2[sizeof(lvl2) - 1] = '\0';
        d = dirname(lvl2);                  /* .../Work Audit.app */
        strncpy(app, d, sizeof(app) - 1);
        app[sizeof(app) - 1] = '\0';
        char lvl3[PATH_MAX]; strncpy(lvl3, d, sizeof(lvl3) - 1); lvl3[sizeof(lvl3) - 1] = '\0';
        d = dirname(lvl3);                  /* <root> */
        snprintf(script, sizeof(script), "%s/bin/run.sh", d);
    }
    if (!is_file(script) && strlen(WA_ROOT) > 0) {
        snprintf(script, sizeof(script), "%s/bin/run.sh", WA_ROOT);
    }
    if (!is_file(script)) {
        fprintf(stderr, "Work Audit: cannot find bin/run.sh next to the app. Re-run install.sh.\n");
        return 1;
    }
    if (app[0]) setenv("WORKAUDIT_APP", app, 1);

    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);

    char *argv[] = {"/bin/bash", script, NULL};
    if (posix_spawn(&child, "/bin/bash", NULL, NULL, argv, environ) != 0) {
        perror("Work Audit: posix_spawn");
        return 1;
    }
    int status = 0;
    pid_t r;
    do {
        r = waitpid(child, &status, 0);
    } while (r < 0 && errno == EINTR);
    if (r < 0) return 1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
