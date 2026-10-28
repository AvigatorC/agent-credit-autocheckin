#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae Work 每日签到 —— 青龙 / 呆呆 / 任意 Python 3（纯标准库，零第三方依赖）

由 trae_checkin.js 移植而来，行为、请求头、错误分类与设备身份派生完全一致。

================================================================================
推荐配置（两个变量搞定）
================================================================================
    TRAE_TOKEN = eyJ...          ← access_token（不带 Cloud-IDE-JWT 前缀也行）
    TRAE_UID   = 123456789012345 ← 账号的 user_id

三个设备身份头全部由 uid 确定性派生（算法对齐 dsh-providers-hub 的 trae.js）：

    X-Device-Id      = seededDigits(15, uid, "devid")   15 位十进制
    X-Market-User-Id = 稳定 UUID v4
    Vscode-Sessionid = seededStream(uid, "sess", 32).hex()

好处：每个账号独享一套稳定设备身份，规避服务端「每设备每天一次」的配额；
      token 刷新后不用重新抓一堆请求头。

多账号：TRAE_TOKEN_1 / TRAE_UID_1、TRAE_TOKEN_2 / TRAE_UID_2 …（最多 20 组）

================================================================================
环境变量
================================================================================
    TRAE_TOKEN / TRAE_UID        【必填】单账号凭据（不带 JWT 前缀也可）
    TRAE_TOKEN_N / TRAE_UID_N    【可选】多账号，N = 1..20
    TRAE_STATE_DIR               【可选】状态文件目录；不设则脚本同级 data/
    TRAE_ARCHIVE_DIR             【可选】归档目录（哨兵文件直接写入，不联网核对）
    TRAE_NOTIFY                  【可选】推送策略：fail（默认，仅失败推）/ always / 0
    TRAE_LOTTERY                 【可选】设为 1 时签到后顺带抽奖
    TRAE_BALANCE                 【可选】设为 0 跳过积分查询
    TRAE_TIMEOUT                 【可选】单次请求超时毫秒（默认 15000）
    TRAE_DEBUG                   【可选】设为 1 输出诊断日志
    SERVERCHAN_SENDKEY           【可选】Server酱推送（sctp 开头走方糖通道）

================================================================================
错误分类与冷却（对齐 dsh 的 trae-cooldown-table.ts）
================================================================================
    1005 / hard-plan       套餐权益不足        12 小时
    4008 / quota-exceeded  日配额已耗尽        24 小时
    4011 / soft-rate       请求频率超限        60 秒
    1001 / session-dead    认证失败（需重登）   0（冷却无用，只能重登）
    404  / not-found       端点不存在          1 小时
    5xx  / server          服务端错误          1 小时
    4xx  / client          请求错误            1 小时
    网络不可达                                  1 小时
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# 统一的 HTTPS 上下文（与 qoder 一致，避免各环境证书链差异导致偶发失败）
_SSL_CTX = ssl.create_default_context()

# ---------------------------------------------------------------- 配置常量
DEFAULT_API_HOST = "https://api.trae.cn"
STATUS_PATH = os.environ.get("TRAE_STATUS_PATH") or "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = os.environ.get("TRAE_CLAIM_PATH") or "/trae/api/v2/ug/checkin_credits/claim"
USAGE_PATH = os.environ.get("TRAE_USAGE_PATH") or "/trae/api/v2/pay/ide_user_ent_usage"
BALANCE_ENABLED = os.environ.get("TRAE_BALANCE") != "0"
TIMEOUT = int(os.environ.get("TRAE_TIMEOUT") or 15000) / 1000.0
DEBUG = os.environ.get("TRAE_DEBUG") == "1"

# 客户端身份常量（对齐 dsh-providers-hub 的 traeCheckinHeaders）
IDE_VERSION = os.environ.get("TRAE_IDE_VERSION") or "1.107.1"
TRAE_UA = "VSCode %s (TRAE SOLO CN)" % IDE_VERSION
MARKET_CLIENT_ID = "VSCode %s" % IDE_VERSION
LSCBD_AID = "787976"
LSCBD_PLATFORM = "windows"
PACKAGE_TYPE = "stable_cn"

# 1024 打卡抽奖
LOTTERY_PATH = "/trae/api/v2/ug/activity/action"
LOTTERY_ACTIVITY_ID = "checkin_1024"
LOTTERY_REQ_SOURCE = 1

# 凭据续期（对齐 JS 版 / dsh 的 trae-auth.ts）
TRAE_REFRESH_HOST = "https://api.trae.cn"
TRAE_REFRESH_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
TRAE_CLIENT_ID = "en1oxy7wnw8j9n"
TRAE_REFRESH_UA = "Trae/0.1.52"
# 距过期不足 3 天就续期。轮换型 RT 每次刷新都会作废旧值，所以不要无脑刷。
REFRESH_LEAD_SECS = 3 * 86400

# 签到「人数过多」：官方业务码 9074，建议 5 分钟后再试
TRAE_CHECKIN_BUSY_CODE = 9074

# 错误类别 → 冷却毫秒
TRAE_COOLDOWN_MS = {
    "quota-exceeded": 24 * 3600 * 1000,
    "hard-plan": 12 * 3600 * 1000,
    "soft-rate": 60 * 1000,
    "busy": 5 * 60 * 1000,          # 9074 签到人数过多
    "not-found": 3600 * 1000,
    "session-dead": 0,
    "server": 3600 * 1000,
    "client": 3600 * 1000,
    "none": 0,
}


def cooldown_for(kind):
    return TRAE_COOLDOWN_MS.get(kind, 3600 * 1000)


def mask_name(name):
    """输出里对账号名脱敏：11 位手机号 → 18*******62（与 workbuddy / qoder 对齐）。"""
    text = str(name or "").strip()
    if len(text) == 11 and text.isdigit():
        return "%s*******%s" % (text[:2], text[-2:])
    return text


# ---------------------------------------------------------------- 设备身份派生
def seeded_stream(seed, salt, nbytes):
    """SHA-256 确定性伪随机流：同输入永远同输出（对应 JS seededStream）。"""
    import hashlib
    prefix = ("%s:%s" % (salt, seed)).encode("utf-8")
    result = b""
    counter = 0
    while len(result) < nbytes:
        result += hashlib.sha256(prefix + counter.to_bytes(4, "big")).digest()
        counter += 1
    return result[:nbytes]


def seeded_digits(n, seed, salt):
    return "".join(str(b % 10) for b in seeded_stream(seed, salt, n))


def derive_device_id15(user_id):
    return seeded_digits(15, user_id, "devid")


def derive_market_user_id(user_id):
    """稳定 UUID v4 形态（脚本要求同账号每天一致，故用 seededStream 派生）。"""
    raw = bytearray(seeded_stream(user_id, "market", 16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def derive_session_id(user_id):
    return seeded_stream(user_id, "sess", 32).hex()


def build_checkin_headers(token, uid, overrides=None):
    """构造签到请求头（对齐 traeCheckinHeaders）。"""
    device_id = derive_device_id15(uid)
    market_user_id = derive_market_user_id(uid)
    session_id = derive_session_id(uid)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Cloud-IDE-JWT " + token if not token.startswith("Cloud-IDE-JWT ") else token,
        "User-Agent": TRAE_UA,
        "X-Device-Id": device_id,
        "X-Market-User-Id": market_user_id,
        "Vscode-Sessionid": session_id,
        "X-Cloudide-Aid": LSCBD_AID,
        "X-Cloudide-Platform": LSCBD_PLATFORM,
        "X-Cloudide-Version": IDE_VERSION,
        "X-Market-Client-Id": MARKET_CLIENT_ID,
        "X-Product-Package-Type": PACKAGE_TYPE,
    }
    for key, value in (overrides or {}).items():
        if value is not None:
            headers[key] = str(value)
    return headers


# ---------------------------------------------------------------- 状态文件
def resolve_state_dir():
    """状态目录：TRAE_STATE_DIR 优先，其次脚本同级 data/，最后脚本目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("TRAE_STATE_DIR", "").strip(),
                 os.path.join(here, "data"),
                 here):
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            return cand
        except Exception:
            continue
    return here


STATE_DIR = resolve_state_dir()
STATE_FILE = os.path.join(STATE_DIR, "trae_credentials.json")


def load_state():
    try:
        if os.path.isfile(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("accounts", {})
                return data
    except Exception as exc:
        print("⚠️ 状态读取失败（%s）：按空状态继续" % exc)
    return {"accounts": {}}


def save_state(state):
    try:
        temp = STATE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(temp, STATE_FILE)
    except Exception as exc:
        print("⚠️ 状态写入失败（%s）：本次结果未持久化，下次仍用环境变量里的旧值" % exc)


# ---------------------------------------------------------------- 时间工具
def to_unix_seconds(value):
    """把秒 / 毫秒 / ISO 字符串统一成 Unix 秒；失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text) / (1000.0 if float(text) > 1e11 else 1.0)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def format_expiry_date(value):
    sec = to_unix_seconds(value)
    if sec is None:
        return ""
    return datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=8))).strftime("%Y/%m/%d")


def format_credits(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return ("%d" % num) if abs(num - int(num)) < 1e-9 else ("%.2f" % num)


# ---------------------------------------------------------------- 账号解析
def parse_env_tokens():
    """从 TRAE_TOKEN / TRAE_TOKEN_N 解析多账号。"""
    accounts = []
    single = (os.environ.get("TRAE_TOKEN") or "").strip()
    single_uid = (os.environ.get("TRAE_UID") or "").strip()
    single_rt = (os.environ.get("TRAE_REFRESH_TOKEN") or "").strip()
    if single:
        accounts.append({"name": (os.environ.get("TRAE_NAME") or "账号1").strip(),
                         "token": single, "uid": single_uid,
                         "refresh_token": single_rt,
                         "expires_at": to_unix_seconds(os.environ.get("TRAE_EXPIRES_AT"))})
    for i in range(1, 21):
        token = (os.environ.get("TRAE_TOKEN_%d" % i) or "").strip()
        uid = (os.environ.get("TRAE_UID_%d" % i) or "").strip()
        if not token:
            continue
        name = (os.environ.get("TRAE_NAME_%d" % i) or "账号%d" % (len(accounts) + 1)).strip()
        accounts.append({"name": name, "token": token, "uid": uid,
                         "refresh_token": (os.environ.get("TRAE_REFRESH_TOKEN_%d" % i) or "").strip(),
                         "expires_at": to_unix_seconds(os.environ.get("TRAE_EXPIRES_AT_%d" % i))})
    return accounts


def build_accounts(state):
    """环境变量优先，状态文件补充（刷新后的新 token 以状态为准）。"""
    accounts = parse_env_tokens()
    stored = state.get("accounts") or {}
    for acc in accounts:
        saved = stored.get(acc["name"]) or {}
        if saved.get("access_token"):
            acc["token"] = saved["access_token"]
        if saved.get("refresh_token"):
            acc["refresh_token"] = saved["refresh_token"]
        acc.setdefault("expires_at", saved.get("expires_at"))
        acc.setdefault("refresh_expires_at", saved.get("refresh_expires_at"))
    return accounts


# ---------------------------------------------------------------- 凭据续期
def needs_refresh(account, now_sec=None):
    """是否需要续期。

    ⚠️ 只有「明确知道快过期」才刷：轮换型 RT 每刷新一次就作废旧值，
       不知道剩余时间时盲目刷新反而会把自己锁死。
    """
    now = now_sec if now_sec is not None else time.time()
    exp = to_unix_seconds(account.get("expires_at"))
    if exp is None:
        return False
    return (exp - now) < REFRESH_LEAD_SECS


def refresh_credential(account):
    """用 RefreshToken 换新 access_token。

    接口：POST /cloudide/api/v3/trae/oauth/ExchangeToken
          {"ClientID": "...", "RefreshToken": "...", "ClientSecret": "-", "UserID": ""}
    回执：{"Result": {"Token": "...", "RefreshToken": "...", "TokenExpireAt": <秒>}}
    """
    url = TRAE_REFRESH_HOST + TRAE_REFRESH_PATH
    payload = {
        "ClientID": TRAE_CLIENT_ID,
        "RefreshToken": account.get("refresh_token") or "",
        "ClientSecret": "-",
        "UserID": "",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": TRAE_REFRESH_UA,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:120]
        raise TraeError("续期失败：HTTP %s %s" % (exc.code, body), kind="session-dead")
    except Exception as exc:
        raise TraeError("续期失败：%s" % exc, kind="server")

    try:
        body = json.loads(text) if text.strip() else {}
    except ValueError:
        raise TraeError("续期响应不是 JSON：%s" % text[:100], kind="session-dead")

    result = body.get("Result") or body.get("result") or {}
    token = result.get("Token") or result.get("token") or result.get("accessToken")
    if not token:
        raise TraeError("续期响应缺少 Token", kind="session-dead")

    updated = dict(account)
    updated["token"] = token
    new_rt = result.get("RefreshToken") or result.get("refreshToken")
    if new_rt:
        updated["refresh_token"] = new_rt
    exp = to_unix_seconds(result.get("TokenExpireAt") or result.get("tokenExpireAt"))
    if exp:
        updated["expires_at"] = exp
    rt_exp = to_unix_seconds(result.get("RefreshExpireAt"))
    if rt_exp:
        updated["refresh_expires_at"] = rt_exp
    return updated


def ensure_fresh(account):
    """需要就续期；失败不抛，用旧 token 继续试。

    返回 (account, 说明文案)，与 JS 版 ensureFresh 对齐。
    """
    if not needs_refresh(account):
        exp = to_unix_seconds(account.get("expires_at"))
        if exp:
            days = (exp - time.time()) / 86400
            return account, "有效，无需续期（剩 %.1f 天）" % days
        return account, "有效，无需续期"
    if not account.get("refresh_token"):
        return account, "无 refresh_token，无法续期"
    try:
        updated = refresh_credential(account)
        exp = to_unix_seconds(updated.get("expires_at"))
        if exp:
            remain = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone(
                timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
            return updated, "已续期（到期 %s）" % remain
        return updated, "已续期"
    except TraeError as exc:
        return account, "续期失败（%s），仍用旧 token 尝试" % str(exc)[:80]


# ---------------------------------------------------------------- HTTP 封装
class TraeError(Exception):
    def __init__(self, message, kind="none", http_status=None, code=None):
        super().__init__(message)
        self.traeKind = kind
        self.http_status = http_status
        self.code = code
        self.cooldownMs = cooldown_for(kind)


def request_json(url, headers, body=None, method="GET"):
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise TraeError("网络不可达：%s" % exc.reason, kind="server")
    except Exception as exc:
        raise TraeError("请求异常：%s" % exc, kind="server")

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"_raw": raw[:200]}

    if status >= 400:
        code = payload.get("code")
        message = payload.get("msg") or payload.get("message")
        # 非 JSON 响应（网关 HTML）时给出更可读的原因，区分凭据失效与服务端异常
        if not message:
            if status in (401, 403):
                raise TraeError("凭据已失效（HTTP %d），需重新登录" % status,
                                kind="session-dead", http_status=status)
            snippet = " ".join(str(raw).split())[:80]
            raise TraeError("服务端返回非 JSON（HTTP %d）：%s" % (status, snippet),
                            kind=classify_trae_error(status, code, raw),
                            http_status=status, code=code)
        kind = classify_trae_error(status, code, message)
        raise TraeError("%s（HTTP %s%s）" % (
            message, status, "，code %s" % code if code is not None else ""), kind=kind,
            http_status=status, code=code)
    return payload


def classify_trae_error(http_status, code, message):
    """错误分类（对齐 trae-cooldown-table.ts）。

    ⚠️ 业务码可能是字符串（后端在部分网关以 "9074" 返回），统一按整数比较。
    """
    text = str(message or "").lower()
    try:
        code_num = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_num = None

    if code_num == TRAE_CHECKIN_BUSY_CODE or "人数过多" in str(message or "")             or "too many" in text:
        return "busy"
    if code_num == 1005 or "hard-plan" in text or "套餐权益" in text:
        return "hard-plan"
    if code_num == 4008 or "quota" in text or "配额" in text:
        return "quota-exceeded"
    if code_num == 4011 or "rate" in text or "频率" in text:
        return "soft-rate"
    if code_num == 1001 or "session" in text or "认证" in text:
        return "session-dead"
    if http_status == 404:
        return "not-found"
    if http_status and http_status >= 500:
        return "server"
    if http_status and http_status >= 400:
        return "client"
    return "none"


def api_hosts():
    hosts = []
    raw = (os.environ.get("TRAE_API_HOSTS") or "").strip()
    if raw:
        hosts.extend(h.strip().rstrip("/") for h in raw.replace(",", " ").split() if h.strip())
    default = (os.environ.get("TRAE_API_HOST") or DEFAULT_API_HOST).strip().rstrip("/")
    if default and default not in hosts:
        hosts.append(default)
    return hosts or [DEFAULT_API_HOST]


def request_status(headers):
    """查询签到状态；旧域名返回 HTML 404 时切换候选主机。"""
    last_error = None
    for host in api_hosts():
        url = host + STATUS_PATH
        if DEBUG:
            print("诊断：尝试状态接口 %s" % url)
        try:
            payload = request_json(url, headers)
            return {"host": host, "data": payload.get("data") or payload,
                    "rawCode": payload.get("code"), "raw": payload}
        except TraeError as exc:
            last_error = exc
            if exc.traeKind != "not-found":
                raise
            if DEBUG:
                print("诊断：%s 返回 404，尝试下一个 API 主机" % host)
    raise last_error or TraeError("没有可用的 Trae API 主机")


# ---------------------------------------------------------------- 积分查询
def extract_number(obj, keys):
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            try:
                return float(obj[key])
            except (TypeError, ValueError):
                continue
    return None


def request_usage(headers, host):
    """查询积分余额。

    ⚠️ 必须是 **POST + body `{"require_usage": true, "req_source": 2}`**
       （对齐 dsh-providers-hub 的 fetchTraeCreditBalance）。
       用 GET 时服务端虽可能仍返回，但拿不到完整的 usage 明细。
    """
    url = host + USAGE_PATH
    if DEBUG:
        print("诊断：查询积分 usage %s" % url)
    body = {"require_usage": True, "req_source": 2}
    try:
        payload = request_json(url, headers, body=json.dumps(body), method="POST")
    except TraeError as exc:
        if DEBUG:
            print("诊断：积分 usage 查询失败：%s" % exc)
        return None
    return payload.get("data") or payload


def summarize_usage(usage):
    """解析 usage 概览 → (总剩余, [(名称, 剩余, 到期时间)])。

    真实结构（2026-10 实测）：
        {"usage_summary": {"consumed_amount": .., "total_amount": ..},
         "user_entitlement_pack_list": [
            {"display_desc": "免费", "entitlement_base_info": {
                "entitlement_id": .., "end_time": <unix秒>,
                "quota": {"credits_limit": ..}}}, ..]}
    """
    if not isinstance(usage, dict):
        return None

    summary = usage.get("usage_summary") or {}
    total_amount = extract_number(summary, ("total_amount",))
    consumed = extract_number(summary, ("consumed_amount",))
    total_remain = None
    if total_amount is not None and consumed is not None:
        total_remain = total_amount - consumed

    rows = []
    for pack in (usage.get("user_entitlement_pack_list") or []):
        if not isinstance(pack, dict):
            continue
        info = pack.get("entitlement_base_info") or {}
        name = str(pack.get("display_desc") or pack.get("name") or "权益包").strip()
        quota = info.get("quota") or {}
        credits = None
        for key in ("credits_limit", "credits", "limit"):
            if key in quota:
                try:
                    credits = float(quota[key])
                    break
                except (TypeError, ValueError):
                    continue
        if credits is None:
            continue
        rows.append((name, credits, info.get("end_time")))

    if total_remain is None and not rows:
        return None

    # 同名权益包（如签到奖励）合并展示，避免刷屏
    merged = {}
    for name, credits, end_time in rows:
        key = name
        if key in merged:
            prev_credits, prev_end = merged[key]
            merged[key] = (prev_credits + credits, max(filter(None, [prev_end, end_time]), default=None))
        else:
            merged[key] = (credits, end_time)

    ordered = sorted(merged.items(), key=lambda kv: kv[1][0], reverse=True)
    return (total_remain, [(n, c, e) for n, (c, e) in ordered])


# ---------------------------------------------------------------- 签到
def check_one(account):
    """单账号签到，返回日志行列表。"""
    name = account.get("name") or "未命名账号"
    uid = str(account.get("uid") or "").strip()
    token = str(account.get("token") or "").strip()
    lines = []

    if not token:
        raise TraeError("缺少 TRAE_TOKEN", kind="session-dead")
    if not uid:
        raise TraeError("缺少 TRAE_UID（账号的 user_id）", kind="session-dead")

    headers = build_checkin_headers(token, uid, account.get("headers"))

    # ---- 查询状态 ----
    status = request_status(headers)
    host = status["host"]
    data = status["data"] if isinstance(status["data"], dict) else {}

    signed = bool(data.get("checkin_done") or data.get("checked_in")
                  or data.get("today_checked_in"))
    # status 端点才有真实积分数（见下方 claim 分支的说明）
    status_credit = extract_number(data, ("credits", "credit", "granted_credits"))
    streak = extract_number(data, ("streak_days", "streakDays"))

    if signed:
        tail = ""
        if status_credit:
            tail += " +%s积分" % format_credits(status_credit)
        if streak:
            tail += "（连签 %d 天）" % int(streak)
        lines.append("   ✅ 今天已经签到" + tail)
    else:
        # ---- 领取 ----
        # ⚠️ claim 响应**不含积分数**：实测完整响应就是 {"code":0,"message":"success"}。
        #    真实数值只在 status 端点的 credits 字段里，所以领取成功后必须补查一次。
        payload = request_json(host + CLAIM_PATH, dict(headers), body="{}", method="POST")
        claim_code = payload.get("code")
        # 业务码可能是字符串形态（"9074"），只认数字会误判成功
        try:
            claim_code_num = int(claim_code) if claim_code is not None else 0
        except (TypeError, ValueError):
            claim_code_num = 0

        if claim_code_num == TRAE_CHECKIN_BUSY_CODE:
            message = (payload.get("message") or payload.get("msg")
                       or "签到人数过多，请稍后再试")
            raise TraeError(message, kind="busy", code=9074)

        # 补查状态拿真实积分与连签
        earned = None
        try:
            fresh = request_status(headers)
            fresh_data = fresh["data"] if isinstance(fresh["data"], dict) else {}
            earned = extract_number(fresh_data, ("credits", "credit", "granted_credits"))
            fresh_streak = extract_number(fresh_data, ("streak_days", "streakDays"))
            if fresh_streak:
                streak = fresh_streak
        except TraeError:
            pass

        tail = ""
        if earned:
            tail += " +%s积分" % format_credits(earned)
        if streak:
            tail += "（连签 %d 天）" % int(streak)
        lines.append("   ✅ 签到成功" + tail)

    # ---- 余额 ----
    if BALANCE_ENABLED:
        usage = request_usage(headers, host)
        summary = summarize_usage(usage)
        if summary:
            total, rows = summary
            if total is not None:
                lines.append("   💰 积分: 剩余 %s" % format_credits(total))
            for row_name, remain, end_time in rows[:5]:
                expiry = format_expiry_date(end_time)
                suffix = "，到期 %s" % expiry if expiry else ""
                lines.append("      · %s: 剩余 %s%s" % (row_name, format_credits(remain), suffix))

    # ---- 抽奖（可选）----
    if os.environ.get("TRAE_LOTTERY") == "1":
        try:
            lot_url = host + LOTTERY_PATH
            lot_payload = request_json(
                lot_url, headers,
                body={"activityId": LOTTERY_ACTIVITY_ID, "reqSource": LOTTERY_REQ_SOURCE},
                method="POST")
            lot_data = lot_payload.get("data") or lot_payload
            message = lot_data.get("msg") or lot_data.get("message") or "已参与"
            lines.append("   🎰 抽奖: %s" % str(message)[:60])
        except TraeError as exc:
            lines.append("   🎰 抽奖未执行: %s" % str(exc)[:60])

    return lines


# ---------------------------------------------------------------- 推送
def serverchan_key():
    for name in ("SERVERCHAN_SENDKEY", "PUSH_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def push_serverchan(title, content):
    key = serverchan_key()
    if not key:
        return "no_key"
    if key.startswith("sctp"):
        match = key[len("sctp"):]
        digits = "".join(ch for ch in match[:5] if ch.isdigit())
        rest = match[len(digits):]
        url = "https://%s.push.ft07.com/send/%s.send" % (digits, key)
        if not rest:
            return "bad_key"
    else:
        url = "https://sctapi.ftqq.com/%s.send" % key
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        if payload.get("code") == 0:
            return "ok"
        return "fail:%s" % (payload.get("message") or payload.get("msg") or "未知")
    except Exception as exc:
        return "error:%s" % str(exc)[:80]


def send_notification(title, content):
    result = push_serverchan(title, content)
    if result == "ok":
        if DEBUG:
            print("通知：已通过 Server酱 发送")
        return
    if result == "no_key":
        if DEBUG:
            print("通知：未配置 SERVERCHAN_SENDKEY，跳过")
        return
    print("Server酱 推送结果：%s" % result)


# ---------------------------------------------------------------- 主流程
def main():
    accounts = build_accounts(load_state())
    if not accounts:
        print("❌ 未找到可用账号。请设置 TRAE_TOKEN 与 TRAE_UID")
        print("   格式（单账号）：TRAE_TOKEN=eyJ...  TRAE_UID=<user_id>")
        print("   多账号：TRAE_TOKEN_1 / TRAE_UID_1、TRAE_TOKEN_2 / TRAE_UID_2 …")
        return 1

    print("╔════════════════════════════════════════╗")
    print("║ 🛰️ Trae Work 每日签到                  ║")
    print("╚════════════════════════════════════════╝")
    print("👥 账号数: %d" % len(accounts))
    if DEBUG:
        print("📁 状态文件: %s" % STATE_FILE)
        print("诊断：API 主机候选 %s" % " , ".join(api_hosts()))

    state = load_state()
    state_dirty = False
    results = []
    failed = False

    for idx, account in enumerate(accounts, 1):
        print("")
        print("╭─ 👤 账号%d  %s" % (idx, mask_name(account.get("name"))))
        try:
            # ---- 先按需续期（失败不阻塞，仍用旧 token 试）----
            fresh, note = ensure_fresh(account)
            if fresh.get("token") != account.get("token"):
                account = fresh
                print("   🔑 凭据: %s" % note)
            elif account.get("refresh_token"):
                # 配了 RT 就show 状态：让人知道续期通道是通的
                print("   🔑 凭据: %s" % note)
            elif DEBUG:
                print("   🔑 凭据: %s" % note)

            for line in check_one(account):
                print(line)
            results.append("成功：账号%d" % idx)
        except TraeError as exc:
            failed = True
            kind_part = ""
            if exc.traeKind and exc.traeKind != "none":
                kind_part = "［%s，建议冷却 %d 分钟］" % (
                    exc.traeKind, round((exc.cooldownMs or 0) / 60000))
            message = "%s%s：%s" % (account.get("name") or "未命名账号", kind_part, exc)
            print("   ❌ %s" % message)
            # session-dead 冷却没有意义，只能重新登录 —— 给出可执行的下一步
            if exc.traeKind == "session-dead":
                print("      该账号凭据已失效，需重新获取：")
                print("      1) 打开 Trae 客户端并确认已登录")
                print("      2) 取出新的 access_token，更新面板变量 TRAE_TOKEN")
                print("      3) 若同时有 refresh_token，可选配 TRAE_REFRESH_TOKEN 免去手动更新")
            results.append("失败：%s" % message)
        except Exception as exc:
            failed = True
            message = "%s：%s" % (account.get("name") or "未命名账号", exc)
            print("   ❌ 异常：%s" % message)
            results.append("失败：%s" % message)
        finally:
            if account.get("token") and account.get("uid") and account.get("refresh_token"):
                state.setdefault("accounts", {})[account["name"]] = {
                    "access_token": account["token"],
                    "refresh_token": account["refresh_token"],
                    "expires_at": account.get("expires_at"),
                    "refresh_expires_at": account.get("refresh_expires_at"),
                }
                state_dirty = True

    if state_dirty:
        save_state(state)

    ok_count = len(accounts) - len([r for r in results if r.startswith("失败")])
    print("")
    print("🏁 完成 %d/%d 个账号" % (ok_count, len(accounts)))

    # ---- 推送：默认只在失败时发 ----
    content = "\n".join(results)
    notify_mode = (os.environ.get("TRAE_NOTIFY") or "fail").strip().lower()
    if notify_mode == "0":
        pass
    elif failed:
        send_notification("❌ TRAE 签到失败", content)
    elif notify_mode == "always":
        send_notification("TRAE 签到成功", content)
    else:
        print("ℹ️ 签到正常，静默不推送（节省 Server酱 额度）")

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("❌ 脚本执行失败：%s" % exc)
        notify_mode = (os.environ.get("TRAE_NOTIFY") or "fail").strip().lower()
        if notify_mode != "0":
            send_notification("❌ TRAE 脚本异常", "脚本执行失败：%s" % exc)
        sys.exit(1)
