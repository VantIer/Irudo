/* exec_cmd.c - cross-platform shell command execution with timeout.
 * Captures combined stdout+stderr. Windows output is converted from the
 * ANSI codepage to UTF-8 (mirroring the Python agent's cp936 handling).
 */
#include "agent.h"

static int safety_check(const char *cmd) {
    /* lower-case scan for forbidden patterns */
    static const char *forbidden[] = { "rm -rf /" };
    size_t n = strlen(cmd);
    for (size_t i = 0; i + 8 <= n; i++) {
        int match = 1;
        for (size_t j = 0; j < sizeof(forbidden[0]) - 1; j++) {
            char c = cmd[i + j];
            if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
            if (c != forbidden[0][j]) { match = 0; break; }
        }
        if (match) return 0;
    }
    return 1;
}

#ifdef _WIN32

static char *acp_to_utf8(const char *s) {
    int wlen = MultiByteToWideChar(CP_ACP, 0, s, -1, NULL, 0);
    if (wlen <= 0) return xstrdup(s);
    wchar_t *w = (wchar_t *)malloc((size_t)wlen * sizeof(wchar_t));
    if (!w) return xstrdup(s);
    MultiByteToWideChar(CP_ACP, 0, s, -1, w, wlen);
    int ulen = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    char *u = (char *)malloc((size_t)ulen);
    if (!u) { free(w); return xstrdup(s); }
    WideCharToMultiByte(CP_UTF8, 0, w, -1, u, ulen, NULL, NULL);
    free(w);
    return u;
}

char *run_cmd(const char *cmd, int timeout_sec) {
    if (!cmd || !*cmd) return xstrdup("Error: Empty command");
    if (!safety_check(cmd)) return xstrdup("Error: Command blocked due to safety concerns");

    SECURITY_ATTRIBUTES sa;
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    sa.lpSecurityDescriptor = NULL;
    HANDLE hOutRd = NULL, hOutWr = NULL;
    if (!CreatePipe(&hOutRd, &hOutWr, &sa, 0))
        return printf_str("Error: cannot create pipe");
    SetHandleInformation(hOutRd, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOA si;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = hOutWr;
    si.hStdError = hOutWr;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);

    PROCESS_INFORMATION pi;
    memset(&pi, 0, sizeof pi);

    char cmdline[32768];
    _snprintf(cmdline, sizeof cmdline, "cmd.exe /c %s", cmd);
    cmdline[sizeof cmdline - 1] = 0;

    BOOL ok = CreateProcessA(NULL, cmdline, NULL, NULL, TRUE,
                             CREATE_NO_WINDOW, NULL, NULL, &si, &pi);
    CloseHandle(hOutWr);
    if (!ok) {
        CloseHandle(hOutRd);
        return xstrdup("Error: cannot start process");
    }

    strbuf_t out;
    sb_init(&out);
    char buf[8192];
    HANDLE hs[2] = { hOutRd, pi.hProcess };
    int killed = 0;
    int done_out = 0, proc_done = 0;
    int64_t deadline = now_ms() + (int64_t)timeout_sec * 1000;

    while (!(done_out && proc_done)) {
        if (now_ms() >= deadline) {
            TerminateProcess(pi.hProcess, 1);
            killed = 1;
            break;
        }
        DWORD w = WaitForMultipleObjects(2, hs, FALSE, 200);
        if (w == WAIT_OBJECT_0) {
            for (;;) {
                DWORD avail = 0, n = 0;
                if (!PeekNamedPipe(hOutRd, NULL, 0, NULL, &avail, NULL)) { done_out = 1; break; }
                if (avail == 0) break;
                n = (avail > (DWORD)sizeof buf) ? (DWORD)sizeof buf : avail;
                if (!ReadFile(hOutRd, buf, n, &n, NULL) || n == 0) { done_out = 1; break; }
                sb_append(&out, buf, n);
            }
            if (WaitForSingleObject(pi.hProcess, 0) == WAIT_OBJECT_0) proc_done = 1;
        } else if (w == WAIT_OBJECT_0 + 1) {
            proc_done = 1;
        } else if (w == WAIT_TIMEOUT) {
            /* nothing */
        } else {
            done_out = 1;
            proc_done = 1;
        }
    }

    DWORD code = 0;
    if (!killed) {
        WaitForSingleObject(pi.hProcess, 5000);
        GetExitCodeProcess(pi.hProcess, &code);
    }
    CloseHandle(hOutRd);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);

    if (killed) {
        sb_free(&out);
        return printf_str("Error: Command timed out after %d seconds", timeout_sec);
    }

    char *raw = sb_take(&out);
    char *utf8 = acp_to_utf8(raw);
    free(raw);
    char *result;
    if (utf8 && utf8[0]) {
        result = utf8;
    } else {
        if (code != 0) result = printf_str("Exit code: %lu", (unsigned long)code);
        else result = xstrdup("Command executed successfully (no output)");
        if (utf8) free(utf8);
    }
    return result;
}

#else /* POSIX */

char *run_cmd(const char *cmd, int timeout_sec) {
    if (!cmd || !*cmd) return xstrdup("Error: Empty command");
    if (!safety_check(cmd)) return xstrdup("Error: Command blocked due to safety concerns");

    int p[2];
    if (pipe(p) != 0) return printf_str("Error: %s", strerror(errno));

    pid_t pid = fork();
    if (pid < 0) {
        close(p[0]);
        close(p[1]);
        return printf_str("Error: %s", strerror(errno));
    }
    if (pid == 0) {
        /* child */
        close(p[0]);
        dup2(p[1], STDOUT_FILENO);
        dup2(p[1], STDERR_FILENO);
        close(p[1]);
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
    close(p[1]);

    strbuf_t out;
    sb_init(&out);
    char buf[8192];
    int killed = 0;
    int64_t deadline = now_ms() + (int64_t)timeout_sec * 1000;

    for (;;) {
        int64_t remain = deadline - now_ms();
        if (remain <= 0) {
            kill(pid, SIGKILL);
            killed = 1;
            break;
        }
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(p[0], &rfds);
        struct timeval tv;
        tv.tv_sec = (long)(remain / 1000);
        tv.tv_usec = (long)((remain % 1000) * 1000);
        int sel = (int)select(p[0] + 1, &rfds, NULL, NULL, &tv);
        if (sel < 0) {
            if (errno == EINTR) continue;
            kill(pid, SIGKILL);
            killed = 1;
            break;
        }
        if (sel == 0) {
            kill(pid, SIGKILL);
            killed = 1;
            break;
        }
        ssize_t r = read(p[0], buf, sizeof buf);
        if (r < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (r == 0) break; /* EOF */
        sb_append(&out, buf, (size_t)r);
    }
    close(p[0]);

    int status = 0;
    waitpid(pid, &status, 0);

    if (killed) {
        sb_free(&out);
        return printf_str("Error: Command timed out after %d seconds", timeout_sec);
    }
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;

    char *text = sb_take(&out);
    char *result;
    if (text && text[0]) {
        result = text;
    } else {
        if (code != 0) result = printf_str("Exit code: %d", code);
        else result = xstrdup("Command executed successfully (no output)");
        free(text);
    }
    return result;
}

#endif
