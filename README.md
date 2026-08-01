# Irudo

A C2 framework based on  AI LLM

***

# Introduction

A C2 remote control framework based on AI large language model.

Users communicate with the large language model via C2 and forward commands issued by the model to the Bots for execution.

All Bots connect to a common C2 and are controlled by the same user/LLM, much like the Irudo aliens in the Ultraman series.

***

# AI Command & Control Tool

基于 LLM 的远程控制工具，通过自定义二进制 TCP 协议通信。

完整设计文档：[`设计文档.md`](./设计文档.md)

## 功能特性

- **C2 / Agent 双端架构**：C2 端负责 LLM 对话与指令路由，远程端无头执行（无本地 CLI/Web）
- **多 Agent 支持**：C2 可同时纳管多个远程端，每个 Agent 独立 LLM 会话，切换后历史保留
- **LLM 驱动命令执行**：AI 输出 JSON 命令 → C2 路由 → 远程端执行 → 结果回灌 LLM 续轮
- **授权控制**：命令执行前可要求授权（CLI `/y`/`/n` 提示，Web 弹窗），或自动授权
- **文件操作**：文件读写/编辑/复制/移动/上传下载（512 字节分包 + 结束标记）
- **远程关机**：下发 `shutdown` 指令关闭远程端进程（非系统关机）
- **心跳保活**：远程端周期性心跳，C2 watchdog 超时自动剔除失联 Agent
- **CLI 与 Web 双界面**：Web 支持 Agent 切换、文件管理、授权弹窗、深浅主题

## 架构

```
C2 端（控制端）                            远程端（被控端 / Agent）
┌──────────────────┐                  ┌──────────────────┐
│   CLI / Web      │                  │   Agent daemon   │
│ ┌──────────────┐ │  TCP + 二进制协议 │ ┌──────────────┐ │
│ │ ModelModule  │ │ ◄──────────────► │ │   Handler    │ │
│ │  (LLM 对话)  │ │   request_id     │ │  (指令解析)  │ │
│ └──────┬───────┘ │   TLV 参数       │ └──────┬───────┘ │
│        │         │   单一字符串结果   │        │         │
│ ┌──────┴───────┐ │                  │ ┌──────┴───────┐ │
│ │  Forwarder   │ │                  │ │  LocalExec   │ │
│ └──────┬───────┘ │                  │ │  + FileOps   │ │
│        │         │                  │ └──────────────┘ │
│ ┌──────┴───────┐ │                  └──────────────────┘
│ │ NetworkSrv   │ │
│ └──────────────┘ │
└──────────────────┘
```

## 目录结构

```
XXX/
├── 设计文档.md                # 详细设计
├── README.md                  # 本文件
├── requirements.txt
├── config_c2.example.json     # C2 配置示例
├── config_remote.example.json # 远程端配置示例
├── start_c2.bat / start_c2.sh
├── start_remote.bat / start_remote.sh
├── web/
│   └── index.html             # C2 Web UI
├── common/                    # 共享：协议
│   └── protocol.py
├── src/                       # C2 端
│   ├── c2/
│   │   ├── agent_registry.py  # Agent 状态 + per-Agent 历史
│   │   ├── network_server.py  # TCP 监听 + 鉴权 + 心跳 watchdog
│   │   ├── forwarder.py       # 指令转发 + 控制包
│   │   └── file_transfer.py   # 上传/下载
│   ├── llm.py                 # LLM 调用 + JSON 命令解析
│   ├── command.py             # 动作路由 + 安全检查
│   ├── controller.py
│   ├── config.py
│   ├── model.py               # 多轮对话 + per-Agent 历史
│   ├── main.py                # C2 入口（CLI/Web）
│   └── web_server.py          # C2 Web（FastAPI + NetworkServer 共存）
└── remote/                    # 远程端（独立包）
    ├── agent_client.py        # TCP 拨号 + 心跳 + 重连
    ├── handler.py             # 包解析 + 指令路由 + 文件传输
    ├── local_executor.py      # 文件/Shell 执行
    ├── file_transfer.py       # 数据包收发
    └── main.py                # 远程端入口
```

## 环境要求

- Python 3.9+
- 远程端与 C2 端可互相访问 TCP 端口（默认 C2 监听 `8881`，Web 面板 `8880`）

## 安装

```bash
pip install -r requirements.txt
```

## 配置

复制示例配置并按需修改：

```bash
cp config_c2.example.json config_c2.json      # 填入 LLM API 地址/密钥/模型
cp config_remote.example.json config_remote.json  # 填入 C2 地址/agent id/token
```

**C2 关键项**（`config_c2.json`）：
- `api_base` / `api_key` / `model`：LLM API（OpenAI 兼容）
- `listen_host` / `listen_port`：Web 面板监听（默认 `127.0.0.1:8880`）
- `c2_host` / `c2_port`：C2 网络监听，供远程端连接（默认 `0.0.0.0:8881`）
- `c2_auth_tokens`：预共享 token 列表，远程端需匹配
- `system_prompt`：提示词（`{system_name}` 占位符由 C2 按激活 Agent 的 OS 动态替换）

**远程端关键项**（`config_remote.json`）：`c2_address`、`agent_id`、`auth_token`。

## 启动

### C2 端

```bash
# CLI 模式
python -m src.main --mode cli --config config_c2.json

# Web 模式
python -m src.main --mode web --config config_c2.json
```

启动脚本：

```bash
./start_c2.sh cli     # Linux/macOS
start_c2.bat cli      # Windows（cli 或 web）
```

### 远程端

**配置文件方式**：

```bash
python -m remote.main --config config_remote.json
```

**纯命令行方式**（无需任何文件）：

```bash
python -m remote.main \
    --c2-address 192.168.1.100:8881 \
    --agent-id server-01 \
    --auth-token token-for-server-01
```

**混合方式**（配置文件提供默认值，命令行覆盖）：

```bash
python -m remote.main --config base.json --c2-address other:8881
```

### Web 界面

启动 Web 模式后，浏览器打开 `http://<listen_host>:<listen_port>`：

- 顶部：Agent 下拉切换 + 当前 Agent 状态
- 聊天区：与 AI 对话，命令执行结果以终端风格块展示
- **Command**：直接对当前 Agent 执行 shell 命令
- **Files**：文件管理器（针对当前 Agent 的远程文件系统）
- **Controls**：主题切换、授权模式、重置会话、关闭 Agent

## CLI 命令

| 命令                       | 作用                                            |
| -------------------------- | ----------------------------------------------- |
| `/agents`                  | 列出所有已连接 Agent                            |
| `/target <agent_id>`       | 切换当前激活 Agent（同时切换 LLM 会话）         |
| `/y-all` / `/n-all`        | 自动授权 / 每次询问                             |
| `/reset`                   | 重置当前 Agent 的会话历史                      |
| `/upload <local> <dest>`   | 上传本地文件至当前 Agent                       |
| `/download <src>`          | 从当前 Agent 下载文件，保存到程序目录          |
| `/shutdown`                | 关闭当前 Agent（断开连接并退出进程）           |
| `/help` / `/quit`          | 帮助 / 退出                                      |

授权提示时输入 `/y` / `/n` / `/y-all` / `/n-all`。

## 关键协议

- 包头（16B）：`request_id (8B) | body_len (4B) | reserved (3B) | cmd (1B)`
- 请求包身：TLV 链式 `uint32 length + UTF-8 data`
- 响应包身：单一 UTF-8 字符串（无 TLV 切分）
- 文件传输：数据包复用 16B 头，`cmd` 字段换为结束标记（0=续传，1=末包），≤ 512 字节/包
- 控制指令：`register` / `heartbeat` / `disconnect` / `shutdown`（0x85）

详见 `设计文档.md` §3。

## 测试

```bash
# 方式一：先起 C2（任意模式），再起远程端，用 /agents 确认上线
python -m src.main --mode cli --config config_c2.json
python -m remote.main --config config_remote.json

# 方式二：纯命令行远程端
python -m remote.main --c2-address 127.0.0.1:8881 --agent-id server-01 --auth-token token-for-server-01
```

## 免责声明

本工具仅供个人/受信环境使用，未做生产级防护。C2 Web 面板默认仅监听本机；如暴露公网，请自行加 SSH 隧道或反向代理。详见 `设计文档.md` §9。

