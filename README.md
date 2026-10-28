# Agent Credit AutoCheckin

Agent额度自动签到脚本（Trae/Qoder），纯 Python 零依赖，支持青龙/呆呆面板。

为 AI 编程 Agent 自动领取每日额度，可直接在青龙、呆呆面板或任意 Python 3.8+ 环境运行。

## 支持的工具

| 工具 | 每日额度 | 脚本 |
| :--- | :--- | :--- |
| **Trae Work** | 签到积分 + 可选抽奖 | `trae/trae_checkin.py` |
| **Qoder** | 活动领取（每日 10:00 刷新） | `qoder/qoder_checkin.py` |

两者都支持：多账号、Token 自动续期、失败才推送、退出码语义。

> 欢迎补充其他 Agent 的适配（Windsurf / Cursor / CodeBuddy 等），结构照着现有两个目录建即可。

## 快速开始

### Trae

```bash
export TRAE_TOKEN='eyJ...'          # access_token
export TRAE_UID='123456789012345'   # 账号 user_id
python3 trae/trae_checkin.py
```

### Qoder

```bash
export QODER_TOKEN='token#备注'
python3 qoder/qoder_checkin.py
```

## 环境变量

### Trae

| 变量 | 必填 | 说明 |
| :--- | :---: | :--- |
| `TRAE_TOKEN` | ✅ | access_token（不带 `Cloud-IDE-JWT ` 前缀也行） |
| `TRAE_UID` | ✅ | 账号 user_id，用于派生稳定的设备身份 |
| `TRAE_REFRESH_TOKEN` | ⬜ | refresh_token，配了才能自动续期 |
| `TRAE_EXPIRES_AT` | ⬜ | access_token 到期时间戳（秒或毫秒），配了才能判断「是否快过期」 |
| `TRAE_TOKEN_N` / `TRAE_UID_N` | ⬜ | 多账号，N = 1..20 |
| `TRAE_NOTIFY` | ⬜ | 推送策略：`fail`（默认，仅失败推）/ `always` / `0` |
| `TRAE_LOTTERY` | ⬜ | 设 `1` 时签到后顺带抽奖 |
| `TRAE_BALANCE` | ⬜ | 设 `0` 跳过积分查询 |
| `TRAE_DEBUG` | ⬜ | 设 `1` 输出诊断日志 |
| `TRAE_STATE_DIR` | ⬜ | 状态文件目录（默认脚本同级 `data/`） |
| `TRAE_TIMEOUT` | ⬜ | 单次请求超时毫秒（默认 `15000`） |
| `TRAE_API_HOST` / `TRAE_API_HOSTS` | ⬜ | 覆盖 API 主机（默认 `https://api.trae.cn`） |
| `TRAE_IDE_VERSION` | ⬜ | 客户端版本号（默认 `1.107.1`） |
| `TRAE_NAME` / `TRAE_NAME_N` | ⬜ | 账号显示名（默认「账号N」） |

### Qoder

| 变量 | 必填 | 说明 |
| :--- | :---: | :--- |
| `QODER_TOKEN` | ✅ | 格式 `token#备注`，多账号换行分隔 |
| `QODER_NOTIFY` | ⬜ | 推送策略：`fail`（默认）/ `always` / `0` |
| `QODER_FORCE` | ⬜ | 设 `1` 忽略 10:00 刷新判断（一般不要用） |
| `QODER_DRY_RUN` | ⬜ | 设 `1` 演练模式：只查询不领取 |
| `QODER_MACHINE_ID` | ⬜ | 设备标识，不设则由 token 派生 |
| `QODER_MACHINE_TOKEN` / `QODER_MACHINE_TYPE` | ⬜ | 设备鉴权头，实测中国版不需要 |
| `QODER_STATE_DIR` | ⬜ | 状态文件目录（默认脚本同级 `data/`） |

> **只支持中国版**。国际版（`openapi.qoder.sh`）的免费账号没有每日领取权益
> —— 实测 `userQuota.remaining=0`，活动列表里也没有 `CLAIM_BENEFIT` 条目，
> 所以脚本把站点写死为中国版。代码里保留的 `SITES["intl"]` 仅作未来适配留档，
> 当前无任何入口切换。

### 共用

| 变量 | 说明 |
| :--- | :--- |
| `SERVERCHAN_SENDKEY` | Server酱推送（`sctp` 开头自动走方糖通道） |

## 设计要点

### 设备身份确定性派生（Trae）

Trae 服务端按设备维度限制「每设备每天一次」。脚本用 `user_id` 确定性派生三个请求头，保证同一账号每天设备身份稳定，避免风控：

```
X-Device-Id      = seededDigits(15, uid, "devid")
X-Market-User-Id = 稳定 UUID v4
Vscode-Sessionid = seededStream(uid, "sess", 32).hex()
```

算法：`SHA256(utf8("salt:seed") ++ counterBE32)` 串联，实现在 `trae_checkin.py` 的
`seeded_stream()` / `derive_device_id15()` 等函数里。

移植自 JS 版时做过逐字符比对验证：同一个 `user_id` 在 JS 与 Python 两侧产出完全相同的
三个头，确保服务端不会把它当成陌生设备。

### 签到积分回读（Trae）

`claim` 响应**不含积分数**（实测就是 `{"code":0,"message":"success"}`），真实数值只在
`status` 端点的 `credits` 字段里。所以领取成功后脚本会补查一次状态，如实报告所得与连签天数。

### 续期策略

- 距过期**不足 3 天**才续期（轮换型 RT 每次刷新都作废旧值，不能无脑刷）
- **不知道剩余时间就不刷新**，否则可能把自己锁死
- 续期成功后新 token 写入状态文件（`data/*_credentials.json`），下次优先读文件

### 退出码

| 码 | 含义 |
| :---: | :--- |
| `0` | 全部成功 |
| `1` | 有账号失败 / 凭据失效 / 脚本异常 |

配合面板或 cron 的 `success_exit_codes` 使用，失败即告警。

### 推送策略

默认 `fail`——只在出问题时推送。需要每次都推（调试）时设 `always`，完全静音设 `0`。

## 目录结构

```
.
├── README.md
├── trae/
│   └── trae_checkin.py       主脚本（含设备身份派生）
└── qoder/
    └── qoder_checkin.py      主脚本
```

新增一个 Agent 适配时，建议：

1. 建同名子目录（如 `windsurf/`）
2. 脚本遵循同样的约定：环境变量前缀、`*_NOTIFY` 推送策略、退出码 0/1
3. 状态文件统一放 `data/`，并加进 `.gitignore`

## 免责声明

本项目仅供学习研究。使用产生的任何后果（包括但不限于账号风险、服务条款变更）由使用者自行承担。请遵守各服务提供方的用户协议。代码按「现状」提供，不附带任何明示或暗示的担保。
