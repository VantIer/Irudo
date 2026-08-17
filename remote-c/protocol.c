/* protocol.c - wire protocol encoding/decoding and helpers. */
#include "agent.h"

/* connection-wide stream encryption state (ChaCha20; implemented below).
 * Forward-declared here because send_all() / recv_more() use them. */
static int g_encrypted = 0;
static void crypto_encrypt_buf(const uint8_t *in, uint8_t *out, size_t n);
static void crypto_decrypt_buf(uint8_t *buf, size_t n);

/* ===================================================================== */
/* small helpers                                                         */
/* ===================================================================== */

char *xstrdup(const char *s) {
    size_t n = strlen(s) + 1;
    char *d = (char *)malloc(n);
    if (d) memcpy(d, s, n);
    return d;
}

char *printf_str(const char *fmt, ...) {
    char buf[65536];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n < 0) return xstrdup("(format error)");
    if ((size_t)n < sizeof buf) return xstrdup(buf);
    /* exact-size allocation for very long output */
    char *s = (char *)malloc((size_t)n + 1);
    if (!s) return xstrdup("(out of memory)");
    va_start(ap, fmt);
    vsnprintf(s, (size_t)n + 1, fmt, ap);
    va_end(ap);
    return s;
}

static void bb_grow(bytebuf_t *b, size_t need) {
    size_t newcap = b->cap ? b->cap : 8192;
    while (newcap < need) newcap *= 2;
    uint8_t *nd = (uint8_t *)realloc(b->data, newcap);
    if (nd) { b->data = nd; b->cap = newcap; }
}

void bb_init(bytebuf_t *b) { b->data = NULL; b->len = 0; b->cap = 0; b->off = 0; }
void bb_free(bytebuf_t *b) { free(b->data); b->data = NULL; b->len = b->cap = b->off = 0; }

int bb_append(bytebuf_t *b, const uint8_t *d, size_t n) {
    if (b->off > 0) {
        /* compact the consumed prefix to the front before appending */
        if (b->off >= b->len) {
            b->off = 0;
            b->len = 0;
        } else {
            memmove(b->data, b->data + b->off, b->len - b->off);
            b->len -= b->off;
            b->off = 0;
        }
    }
    if (b->len + n > b->cap) bb_grow(b, b->len + n);
    if (b->len + n > b->cap) return -1; /* alloc failed */
    memcpy(b->data + b->len, d, n);
    b->len += n;
    return 0;
}

static uint64_t read_u64_le(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 7; i >= 0; i--) v = (v << 8) | p[i];
    return v;
}

static uint32_t read_u32_le(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void write_u32_le(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v & 0xff);
    p[1] = (uint8_t)((v >> 8) & 0xff);
    p[2] = (uint8_t)((v >> 16) & 0xff);
    p[3] = (uint8_t)((v >> 24) & 0xff);
}

static void write_u64_le(uint8_t *p, uint64_t v) {
    for (int i = 0; i < 8; i++) { p[i] = (uint8_t)(v & 0xff); v >>= 8; }
}

static void write_header(uint8_t *buf, uint64_t req_id, uint32_t body_len, uint8_t cmd) {
    memset(buf, 0, PACKET_HEADER_LEN);
    write_u64_le(buf, req_id);
    write_u32_le(buf + 8, body_len);
    buf[15] = cmd;
}

int bb_take_packet(bytebuf_t *b, uint64_t *req_id, uint8_t *cmd,
                   const uint8_t **body, uint32_t *body_len) {
    if (b->len - b->off < PACKET_HEADER_LEN) return 0;
    uint64_t rid = read_u64_le(b->data + b->off);
    uint32_t blen = read_u32_le(b->data + b->off + 8);
    if (b->len - b->off < PACKET_HEADER_LEN + blen) return 0;
    *req_id = rid;
    *cmd = b->data[b->off + 15];
    *body = b->data + b->off + PACKET_HEADER_LEN;
    *body_len = blen;
    b->off += PACKET_HEADER_LEN + blen;
    if (b->off >= b->len) {
        b->off = 0;
        b->len = 0;
    }
    return 1;
}

/* ===================================================================== */
/* TLV                                                                   */
/* ===================================================================== */

int tlv_encode(const char *const *params, int n, uint8_t **out, uint32_t *out_len) {
    size_t total = 0;
    int i;
    for (i = 0; i < n; i++) total += 4 + strlen(params[i]);
    uint8_t *buf = (uint8_t *)malloc(total ? total : 1);
    if (!buf) return -1;
    size_t off = 0;
    for (i = 0; i < n; i++) {
        size_t sl = strlen(params[i]);
        if (sl > UINT32_MAX) { free(buf); return -1; }
        write_u32_le(buf + off, (uint32_t)sl);
        off += 4;
        memcpy(buf + off, params[i], sl);
        off += sl;
    }
    *out = buf;
    *out_len = (uint32_t)total;
    return 0;
}

int tlv_decode(const uint8_t *body, uint32_t body_len, char ***out_params, int *out_count) {
    uint32_t off = 0;
    int n = 0;
    while (off < body_len) {
        if (off + 4 > body_len) return -1;
        uint32_t l = read_u32_le(body + off);
        if (off + 4 + l > body_len) return -1;
        off += 4 + l;
        n++;
    }
    char **arr = (char **)calloc((size_t)n + 1, sizeof(char *));
    if (!arr) return -1;
    off = 0;
    int k = 0;
    while (off < body_len) {
        uint32_t l = read_u32_le(body + off);
        off += 4;
        char *s = (char *)malloc((size_t)l + 1);
        if (!s) { tlv_free(arr, k); return -1; }
        memcpy(s, body + off, l);
        s[l] = 0;
        arr[k++] = s;
        off += l;
    }
    arr[k] = NULL;
    *out_params = arr;
    *out_count = n;
    return 0;
}

void tlv_free(char **params, int n) {
    if (!params) return;
    for (int i = 0; i < n; i++) free(params[i]);
    free(params);
}

/* ===================================================================== */
/* packet builders                                                       */
/* ===================================================================== */

uint8_t *build_request(uint64_t req_id, uint8_t cmd,
                       const char *const *params, int n, uint32_t *out_len) {
    uint8_t *body = NULL;
    uint32_t blen = 0;
    if (tlv_encode(params, n, &body, &blen) != 0) return NULL;
    uint32_t total = PACKET_HEADER_LEN + blen;
    uint8_t *buf = (uint8_t *)malloc(total);
    if (!buf) { free(body); return NULL; }
    write_header(buf, req_id, blen, cmd);
    if (blen) memcpy(buf + PACKET_HEADER_LEN, body, blen);
    free(body);
    *out_len = total;
    return buf;
}

uint8_t *build_response(uint64_t req_id, uint8_t cmd,
                        const char *result, size_t reslen, uint32_t *out_len) {
    uint32_t total = PACKET_HEADER_LEN + (uint32_t)reslen;
    uint8_t *buf = (uint8_t *)malloc(total);
    if (!buf) return NULL;
    write_header(buf, req_id, (uint32_t)reslen, cmd);
    if (reslen) memcpy(buf + PACKET_HEADER_LEN, result, reslen);
    *out_len = total;
    return buf;
}

uint8_t *build_data_packet(uint64_t req_id, uint8_t end_flag,
                           const uint8_t *data, size_t data_len, uint32_t *out_len) {
    uint32_t total = PACKET_HEADER_LEN + (uint32_t)data_len;
    uint8_t *buf = (uint8_t *)malloc(total);
    if (!buf) return NULL;
    write_header(buf, req_id, (uint32_t)data_len, end_flag);
    if (data_len) memcpy(buf + PACKET_HEADER_LEN, data, data_len);
    *out_len = total;
    return buf;
}

/* ===================================================================== */
/* wire io                                                               */
/* ===================================================================== */

int send_all(sockfd_t sock, const uint8_t *buf, size_t n) {
    size_t off = 0;
    if (g_encrypted) {
        uint8_t *enc = (uint8_t *)malloc(n ? n : 1);
        if (!enc) return -1;
        crypto_encrypt_buf(buf, enc, n);
        while (off < n) {
            int w = (int)send(sock, (const char *)enc + off, (int)(n - off), 0);
            if (w <= 0) { free(enc); return -1; }
            off += (size_t)w;
        }
        free(enc);
        return 0;
    }
    while (off < n) {
        int w = (int)send(sock, (const char *)buf + off, (int)(n - off), 0);
        if (w <= 0) return -1;
        off += (size_t)w;
    }
    return 0;
}

int wait_sock(sockfd_t sock, int timeout_ms) {
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(sock, &rfds);
    struct timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    return (int)select((int)sock + 1, &rfds, NULL, NULL, &tv);
}

int recv_more(sockfd_t sock, bytebuf_t *b) {
    char chunk[8192];
    int n = (int)recv(sock, chunk, sizeof chunk, 0);
    if (n <= 0) return 0; /* closed or error */
    if (g_encrypted) crypto_decrypt_buf((uint8_t *)chunk, (size_t)n);
    if (bb_append(b, (const uint8_t *)chunk, (size_t)n) != 0) return -1;
    return 1;
}

int take_packet_blocking(sockfd_t sock, bytebuf_t *b,
                         uint64_t *req_id, uint8_t *cmd,
                         const uint8_t **body, uint32_t *body_len, int timeout_ms) {
    for (;;) {
        int have = bb_take_packet(b, req_id, cmd, body, body_len);
        if (have != 0) return have;
        if (wait_sock(sock, timeout_ms) <= 0) return 0; /* timeout */
        int r = recv_more(sock, b);
        if (r <= 0) return -1;
    }
}

/* ===================================================================== */
/* misc helpers                                                          */
/* ===================================================================== */

int path_exists(const char *p) {
#ifdef _WIN32
    DWORD a = GetFileAttributesA(p);
    return a != INVALID_FILE_ATTRIBUTES;
#else
    struct stat st;
    return stat(p, &st) == 0;
#endif
}

int is_dir(const char *p) {
#ifdef _WIN32
    DWORD a = GetFileAttributesA(p);
    if (a == INVALID_FILE_ATTRIBUTES) return 0;
    return (a & FILE_ATTRIBUTE_DIRECTORY) != 0;
#else
    struct stat st;
    if (stat(p, &st) != 0) return 0;
    return S_ISDIR(st.st_mode);
#endif
}

static int mkdir_one(const char *p) {
#ifdef _WIN32
    return _mkdir(p);
#else
    return mkdir(p, 0755);
#endif
}
void mkdir_p(const char *path) {
    char tmp[PROTO_MAX_PATH];
    size_t len = strlen(path);
    if (len == 0 || len >= sizeof tmp) return;
    memcpy(tmp, path, len + 1);
    /* drop trailing separator */
    while (len > 1 && (tmp[len - 1] == '/' || tmp[len - 1] == '\\')) tmp[--len] = 0;
    char *p = tmp;
    if (len >= 2 && tmp[1] == ':') p = tmp + 2; /* skip "C:" */
    if (*p == '/' || *p == '\\') p++;           /* skip leading sep */
    for (; *p; p++) {
        if (*p == '/' || *p == '\\') {
            char saved = *p;
            *p = 0;
            mkdir_one(tmp);
            *p = saved;
        }
    }
    mkdir_one(tmp);
}

void make_parent_dirs(const char *path) {
    char tmp[PROTO_MAX_PATH];
    size_t len = strlen(path);
    if (len == 0 || len >= sizeof tmp) return;
    memcpy(tmp, path, len + 1);
    char *sep = NULL;
    for (char *p = tmp; *p; p++) if (*p == '/' || *p == '\\') sep = p;
    if (sep) {
        *sep = 0;
        mkdir_p(tmp);
    }
}

char *detect_os(void) {
#ifdef _WIN32
    return xstrdup("Windows");
#elif defined(__APPLE__)
    return xstrdup("macOS");
#else
    return xstrdup("Linux");
#endif
}

int get_hostname(char *buf, size_t n) {
#ifdef _WIN32
    DWORD sz = (DWORD)n;
    if (!GetComputerNameA(buf, &sz)) return -1;
    return 0;
#else
    if (gethostname(buf, n) != 0) return -1;
    buf[n - 1] = 0;
    return 0;
#endif
}

int64_t now_ms(void) {
#ifdef _WIN32
    return (int64_t)GetTickCount64();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
#endif
}

void sleep_sec(double s) {
#ifdef _WIN32
    Sleep((DWORD)(s * 1000.0));
#else
    struct timespec ts;
    ts.tv_sec = (time_t)s;
    ts.tv_nsec = (long)((s - (double)ts.tv_sec) * 1e9);
    nanosleep(&ts, NULL);
#endif
}

void sb_init(strbuf_t *b) {
    b->data = (char *)malloc(128);
    if (b->data) { b->data[0] = 0; }
    b->len = 0;
    b->cap = b->data ? 128 : 0;
}

void sb_free(strbuf_t *b) { free(b->data); b->data = NULL; b->len = b->cap = 0; }

int sb_append(strbuf_t *b, const char *d, size_t n) {
    if (b->len + n + 1 > b->cap) {
        size_t nc = b->cap ? b->cap : 128;
        while (nc < b->len + n + 1) nc *= 2;
        char *nd = (char *)realloc(b->data, nc);
        if (!nd) return -1;
        b->data = nd;
        b->cap = nc;
    }
    if (n) memcpy(b->data + b->len, d, n);
    b->len += n;
    b->data[b->len] = 0;
    return 0;
}

int sb_printf(strbuf_t *b, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    char tmp[2048];
    int n = vsnprintf(tmp, sizeof tmp, fmt, ap);
    va_end(ap);
    if (n < 0) return -1;
    if ((size_t)n < sizeof tmp) return sb_append(b, tmp, (size_t)n);
    /* very large formatted chunk: build dynamically */
    va_list ap2;
    va_start(ap2, fmt);
    char *big = (char *)malloc((size_t)n + 1);
    if (!big) { va_end(ap2); return -1; }
    vsnprintf(big, (size_t)n + 1, fmt, ap2);
    va_end(ap2);
    int rc = sb_append(b, big, (size_t)n);
    free(big);
    return rc;
}

char *sb_take(strbuf_t *b) {
    char *d = b->data ? b->data : xstrdup("");
    b->data = NULL;
    b->len = b->cap = 0;
    return d;
}

/* ===================================================================== */
/* SHA-256 (self-contained, public domain style)                          */
/* ===================================================================== */

static const uint32_t SHA256_K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

static uint32_t sha256_rotr(uint32_t x, int n) {
    return (x >> n) | (x << (32 - n));
}

static void sha256_transform(uint32_t state[8], const uint8_t data[64]) {
    uint32_t w[64];
    int i;
    for (i = 0; i < 16; i++) {
        w[i] = ((uint32_t)data[i * 4] << 24) | ((uint32_t)data[i * 4 + 1] << 16)
             | ((uint32_t)data[i * 4 + 2] << 8) | (uint32_t)data[i * 4 + 3];
    }
    for (i = 16; i < 64; i++) {
        uint32_t s0 = sha256_rotr(w[i - 15], 7) ^ sha256_rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = sha256_rotr(w[i - 2], 17) ^ sha256_rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = state[0], b = state[1], c = state[2], d = state[3];
    uint32_t e = state[4], f = state[5], g = state[6], h = state[7];
    for (i = 0; i < 64; i++) {
        uint32_t S1 = sha256_rotr(e, 6) ^ sha256_rotr(e, 11) ^ sha256_rotr(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + S1 + ch + SHA256_K[i] + w[i];
        uint32_t S0 = sha256_rotr(a, 2) ^ sha256_rotr(a, 13) ^ sha256_rotr(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + maj;
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

static void sha256_digest_raw(const uint8_t *data, size_t len, uint32_t state[8]) {
    state[0] = 0x6a09e667; state[1] = 0xbb67ae85;
    state[2] = 0x3c6ef372; state[3] = 0xa54ff53a;
    state[4] = 0x510e527f; state[5] = 0x9b05688c;
    state[6] = 0x1f83d9ab; state[7] = 0x5be0cd19;
    uint64_t bitlen = (uint64_t)len * 8;
    uint8_t block[64];
    const uint8_t *p = data;
    size_t left = len;

    /* process full 64-byte blocks */
    while (left >= 64) {
        sha256_transform(state, p);
        p += 64;
        left -= 64;
    }

    /* final block: pad with 0x80, zeros, then 8-byte big-endian bit length */
    memset(block, 0, sizeof block);
    memcpy(block, p, left);
    block[left] = 0x80;
    if (left >= 56) {
        sha256_transform(state, block);
        memset(block, 0, sizeof block);
    }
    for (int i = 0; i < 8; i++) {
        block[63 - i] = (uint8_t)(bitlen >> (i * 8));
    }
    sha256_transform(state, block);
}

void sha256_digest(const void *data, size_t len, uint8_t out[32]) {
    uint32_t state[8];
    sha256_digest_raw((const uint8_t *)data, len, state);
    for (int i = 0; i < 8; i++) {
        out[i * 4]     = (uint8_t)(state[i] >> 24);
        out[i * 4 + 1] = (uint8_t)(state[i] >> 16);
        out[i * 4 + 2] = (uint8_t)(state[i] >> 8);
        out[i * 4 + 3] = (uint8_t)(state[i]);
    }
}

void sha256_hex(const void *data, size_t len, char out[65]) {
    uint8_t d[32];
    sha256_digest(data, len, d);
    static const char hexc[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        out[i * 2]     = hexc[d[i] >> 4];
        out[i * 2 + 1] = hexc[d[i] & 0xf];
    }
    out[64] = 0;
}

/* ===================================================================== */
/* ChaCha20 (RFC 7539: 256-bit key, 96-bit nonce, 32-bit counter)        */
/* Adapted from CycloneCRYPTO Open chacha20 (样例代码/chacha20.c).        */
/* ===================================================================== */

typedef struct {
    uint32_t state[16];
    uint8_t keystream[64];
    size_t pos;
} Chacha20Context;

static uint32_t c20_load32le(const uint8_t *p) {
    return ((uint32_t)p[0]) | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void c20_store32le(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

static uint32_t c20_rol(uint32_t x, int n) {
    return (x << n) | (x >> (32 - n));
}

#define C20_QR(a, b, c, d) do {                     \
    (a) += (b); (d) ^= (a); (d) = c20_rol((d), 16); \
    (c) += (d); (b) ^= (c); (b) = c20_rol((b), 12); \
    (a) += (b); (d) ^= (a); (d) = c20_rol((d), 8);  \
    (c) += (d); (b) ^= (c); (b) = c20_rol((b), 7);  \
} while (0)

static void c20_block(Chacha20Context *ctx) {
    uint32_t w[16];
    int i;
    for (i = 0; i < 16; i++) w[i] = ctx->state[i];
    for (i = 0; i < 10; i++) {
        C20_QR(w[0], w[4], w[8], w[12]);
        C20_QR(w[1], w[5], w[9], w[13]);
        C20_QR(w[2], w[6], w[10], w[14]);
        C20_QR(w[3], w[7], w[11], w[15]);
        C20_QR(w[0], w[5], w[10], w[15]);
        C20_QR(w[1], w[6], w[11], w[12]);
        C20_QR(w[2], w[7], w[8], w[13]);
        C20_QR(w[3], w[4], w[9], w[14]);
    }
    for (i = 0; i < 16; i++) {
        w[i] += ctx->state[i];
        c20_store32le(ctx->keystream + i * 4, w[i]);
    }
    ctx->state[12]++;
    if (ctx->state[12] == 0) ctx->state[13]++;
    ctx->pos = 0;
}

static void c20_init(Chacha20Context *ctx, const uint8_t key[32], const uint8_t nonce[12]) {
    static const uint32_t consts[4] = { 0x61707865U, 0x3320646EU, 0x79622D32U, 0x6B206574U };
    int i;
    ctx->state[0] = consts[0]; ctx->state[1] = consts[1];
    ctx->state[2] = consts[2]; ctx->state[3] = consts[3];
    for (i = 0; i < 8; i++) ctx->state[4 + i] = c20_load32le(key + i * 4);
    ctx->state[12] = 0;
    ctx->state[13] = c20_load32le(nonce);
    ctx->state[14] = c20_load32le(nonce + 4);
    ctx->state[15] = c20_load32le(nonce + 8);
    ctx->pos = 0;
}

static void c20_crypt(Chacha20Context *ctx, const uint8_t *in, uint8_t *out, size_t n) {
    size_t off = 0;
    while (off < n) {
        if (ctx->pos == 0 || ctx->pos >= 64) c20_block(ctx);
        size_t take = (n - off) < (64 - ctx->pos) ? (n - off) : (64 - ctx->pos);
        for (size_t i = 0; i < take; i++) out[off + i] = in[off + i] ^ ctx->keystream[ctx->pos + i];
        ctx->pos += take;
        off += take;
    }
}

/* ===================================================================== */
/* connection-wide stream encryption state                               */
/* ===================================================================== */

static Chacha20Context g_tx, g_rx;

void crypto_enable_agent(const uint8_t key[32]) {
    static const uint8_t nonce_c2_to_agent[12] = { 0,0,0,0,0,0,0,0,0,0,0,0 };
    static const uint8_t nonce_agent_to_c2[12] = { 1,0,0,0,0,0,0,0,0,0,0,0 };
    c20_init(&g_tx, key, nonce_agent_to_c2); /* agent outbound */
    c20_init(&g_rx, key, nonce_c2_to_agent); /* agent inbound  */
    g_encrypted = 1;
}

void crypto_disable(void) {
    g_encrypted = 0;
}

static void crypto_encrypt_buf(const uint8_t *in, uint8_t *out, size_t n) {
    if (!g_encrypted) { memmove(out, in, n); return; }
    c20_crypt(&g_tx, in, out, n);
}

static void crypto_decrypt_buf(uint8_t *buf, size_t n) {
    if (!g_encrypted) return;
    c20_crypt(&g_rx, buf, buf, n);
}
