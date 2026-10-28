#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Qoder 每日积分领取 —— 青龙 / 呆呆 / 任意 Python3（纯标准库，无第三方依赖）

================================================================================
一、它做什么
================================================================================
Qoder（国际版 `qoder` / 中国版 `qodercn`）没有固定的"签到端点"。每日 100 积分
是通过服务端下发的「活动列表」驱动的：

    GET  {openApiBase}/sash/api/v1/me/campaigns
    POST {openApiBase}/sash/api/v1/me/campaigns/{campaignId}/claim   ← 请求体是空串

所以本脚本先拉活动列表，筛出 `actionType=CLAIM_BENEFIT` 且
`claimStatus=CLAIMABLE` 的条目，再逐个领取。**没有可领目标就一个 POST 都不发。**

================================================================================
二、最容易踩的坑：北京时间 10:00 刷新，不是自然日 0 点
================================================================================
活动在**每日 10:00（UTC+8）**刷新。也就是说 0:00 ~ 9:59 之间看到的那一轮，
在 Qoder 眼里**仍然属于昨天**：

    has_qoder_campaign_refreshed_today(now) = (UTC+8 的小时数 >= 10)

判定**只看本地时钟**，不看活动里的任何日期字段。这条不能省：

  · 刷新前看到 `CLAIMED` → 报"尚未刷新"，**不能**说"今天已领取"
    （说了用户就以为今天不必再领，而 IDE 里其实还没刷新出来）
  · 刷新前领取成功 → 如实报"领取成功"，但要标记 `未计入今日`
    （否则调度器会认为今天已处理，10:00 之后的新额度整天没人领）

这正是上一版 wb-checkin 出问题的地方：它把"未刷新"当成"今天已有结论"，
写进 `settled_today()`，导致 10:00 之后永远不会再自动跑。

================================================================================
三、鉴权
================================================================================
    Accept:          application/json
    Authorization:   Bearer <security_oauth_token 优先，其次 access_token>
    Cosy-ClientType: "10"        ← 桌面 app 身份；写错会让活动列表为空
    User-Agent:      "Qoder"     ← 恒为 "Qoder"，不带版本号

`Cosy-MachineToken` / `Cosy-MachineType`：某些资料称"必须带上才会下发可领取的
活动"。**实测（2026-10-23，qodercn 真实账号）不需要** —— 不带这两个头同样能拿到
`CLAIM_BENEFIT / CLAIMABLE / 100`。本脚本因此只在凭证里带这两个字段时才发，
不主动扫描本机 Qoder 客户端目录、不伪造设备指纹。

如果哪天服务端改成必须带，从 qoder 客户端的
`%APPDATA%\Qoder\SharedClientCache\cache\machine_token.json` 里取
`token` → `Cosy-MachineToken`、`type` → `Cosy-MachineType`，写进凭证 JSON 即可。

================================================================================
四、幂等
================================================================================
**靠预检**：第一步就是拉活动列表，`today_checked_in` 为真时一个 POST 都不发。
领取响应里 `replayed: true` 是第二道保险 —— 重复领取同样返回 HTTP 200，
但 `replayed` 为 true、**不含 `benefit`**、`claimedAt` 是上一次的旧时间。
只看状态码会把"今天已领"误报成"领取成功 +100"。

================================================================================
五、环境变量
================================================================================
  QODER_TOKEN            必填。多账号用换行分隔，每行：`token#备注`
                         （备注可省略；uid 不是签到必需字段，可写可不写）
                         也接受直接粘贴完整凭证 JSON（含 access_token 字段）

  ⚠️ 站点已写死为中国版 https://openapi.qoder.com.cn，不再有切换变量。
     国际版免费账号没有每日领取权益（实测 userQuota.remaining=0、
     活动列表无 CLAIM_BENEFIT），留着开关只会误导。要改见 main() 里那一行。

  QODER_MACHINE_TOKEN    可选。Cosy-MachineToken（见上文第三节）
  QODER_MACHINE_TYPE     可选。Cosy-MachineType，必须与上面成对出现

  SERVERCHAN_SENDKEY     可选。Server酱 SendKey
                         sctp 开头走方糖通道 https://{uid}.push.ft07.com/send/{key}.send
                         其余走 Turbo    https://sctapi.ftqq.com/{key}.send

  QODER_NOTIFY           推送策略，默认 fail（只在失败时推）
                           fail   仅失败推送 ← 默认，省额度（Server酱免费版 5 条/天）
                           always 每次都推（调试用）
                           0      完全关闭
  QODER_DRY_RUN          默认 0。设 1 只查询不领取（调试用）
  QODER_FORCE            默认 0。设 1 忽略 10:00 刷新判断（一般不要用）

================================================================================
六、退出码
================================================================================
  0  今天确实领到了，或今天早已领过
  1  有账号领取失败 / 凭据失效 / 尚未刷新 / 没有可领活动
     ——「尚未刷新」也返回 1 是**故意的**：它是"今天还没办成"的真实状态，
     调度器应当在 10:00 之后再跑一次。若你的调度只在 10:00 后执行，
     这个分支不会触发。

================================================================================
七、协议来源
================================================================================
  wb-checkin/wb/channels/qoder.py（逐行对照 business.mjs 的实现）
  dsh-providers-hub-master/src/qoder-credits.ts + qoder-machine.ts（TS 实现与实测记录）

两处结论冲突时以**真机实测**为准，冲突点已在第三节注明。
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# Windows 控制台默认 GBK，打印中文会抛 UnicodeEncodeError（Linux 容器是 UTF-8，无此问题）。
# 这里尽力切到 UTF-8；切不动就退化成问号替换，绝不因为「日志打不出来」而让任务失败。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 常量

SITES = {
    "cn": {
        "name": "Qoder（中国版）",
        "open_api_base": "https://openapi.qoder.com.cn",
    },
    "intl": {
        "name": "Qoder",
        "open_api_base": "https://openapi.qoder.sh",
    },
}

CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
USAGE_PATH = "/sash/api/v2/me/usage"

ACTION_CLAIM_BENEFIT = "CLAIM_BENEFIT"
STATUS_CLAIMABLE = "CLAIMABLE"
STATUS_CLAIMED = "CLAIMED"

REFRESH_HOUR_UTC8 = 10
BILLING_UTC_OFFSET_HOURS = 8
TIMEOUT_SECS = 20

NOT_ACTIVATED_HINT = (
    "该账号尚未在 Qoder 侧开通每日领取（每日 100 Credits）。"
    "请先用 Qoder 官方客户端登录一次该账号，开通后再回来领取。"
)
NOT_REFRESHED_YET_HINT = (
    "今天的每日活动尚未刷新（每日 %d:00（UTC+8）刷新），"
    "当前看到的是昨天那一轮，请稍后再来领取。" % REFRESH_HOUR_UTC8
)
MSG_ALREADY = "今天已领取"
MSG_NO_CAMPAIGN = "当前没有可领取的活动"

_SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------- 时钟


def utc8_now(ts=None):
    """当前 UTC+8 时间。"""
    moment = ts if ts is not None else time.time()
    return datetime.fromtimestamp(moment, tz=timezone.utc) + timedelta(
        hours=BILLING_UTC_OFFSET_HOURS
    )


def mask_remark(remark):
    """输出里对账号备注做脱敏：11 位手机号 → 18*******62；其它原样返回。

    与 workbuddy / trae 的日志风格保持一致，避免把完整账号写在日志里。
    """
    text = str(remark or "").strip()
    if len(text) == 11 and text.isdigit():
        return "%s*******%s" % (text[:2], text[-2:])
    return text


def campaign_refreshed_today(ts=None):
    """活动是否已刷新到「今天这一轮」：UTC+8 小时数 >= 10。

    只看本地时钟，不看活动里的任何日期字段。
    """
    return utc8_now(ts).hour >= REFRESH_HOUR_UTC8


# ---------------------------------------------------------------- HTTP


def http_json(method, url, headers, body=None, timeout=TIMEOUT_SECS):
    """发请求并尽量解析 JSON。

    返回 (ok, status, parsed_or_None, raw_text)。
    `body` 传空字符串 "" 表示「有请求体但内容为空」（claim 端点要求如此）。
    """
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:                     # 网络层错误
        return False, 0, None, "%s: %s" % (type(exc).__name__, exc)

    parsed = None
    if raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    ok = 200 <= status < 300 and isinstance(parsed, dict)
    return ok, status, parsed, raw


def describe_non_json(status, text):
    """非 JSON 响应的可读原因（凭据失效时网关会回 HTML）。"""
    if status in (401, 403):
        return "凭据已失效（HTTP %d），请重新登录该账号" % status
    snippet = " ".join(str(text).split())[:80]
    return "服务端返回了非 JSON 响应（HTTP %d）：%s" % (status, snippet)


# ---------------------------------------------------------------- 凭证解析


def parse_accounts(raw):
    """解析 QODER_TOKEN。

    每行形如：
        <token>#<备注>              ← 推荐
        <token>                     ← 无备注
        {"access_token": "..."}     ← 整段 JSON（单账号）

    返回 [{"token": str, "remark": str}]。
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    # 整段 JSON：单账号的完整凭证
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            return []
        tok = obj.get("security_oauth_token") or obj.get("access_token") or ""
        if not tok:
            return []
        remark = str(obj.get("remark") or obj.get("uid") or "账号1")[:24]
        return [{"token": tok.strip(), "remark": remark}]

    accounts = []
    for idx, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("#")
        token = parts[0].strip()
        remark = "#".join(parts[1:]).strip() or "账号%d" % idx
        if token:
            accounts.append({"token": token, "remark": remark[:24]})
    return accounts


# ---------------------------------------------------------------- 请求头


def build_headers(token, machine_token="", machine_type=""):
    """构造 /sash/ 端点的公共请求头。

    ⚠️ `Cosy-ClientType` 必须是 "10"（桌面 app 身份）。用 "5"（CLI）时
    服务端返回的活动列表恒为空 —— 会被误判成「没有可领取的活动」。
    """
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "Cosy-ClientType": "10",
        "User-Agent": "Qoder",
    }
    # 成对才发（实测非必需，但服务端若某天要求时必须成对）
    if machine_token and machine_type:
        headers["Cosy-MachineToken"] = machine_token
        headers["Cosy-MachineType"] = machine_type
    return headers


# ---------------------------------------------------------------- 解析


def parse_campaigns(body):
    """把活动列表响应解析成统一结构。

    只取领取决策真正用到的字段；`campaignId` 放宽到数字（接口若改成数字型，
    严格只认字符串会让活动被**静默丢弃**，表现为「没有可领取的活动」）。
    """
    campaigns = []
    raw = body.get("campaigns")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            cid = item.get("campaignId")
            if isinstance(cid, (int, float)) and not isinstance(cid, bool):
                cid = str(int(cid))
            if not isinstance(cid, str) or not cid.strip():
                continue
            benefit = item.get("benefit")
            amount = None
            if isinstance(benefit, dict):
                amt = benefit.get("amount")
                if isinstance(amt, (int, float)) and not isinstance(amt, bool):
                    amount = float(amt)
                elif isinstance(amt, str):
                    try:
                        amount = float(amt)
                    except ValueError:
                        amount = None
            campaigns.append(
                {
                    "campaignId": cid.strip(),
                    "campaignKey": item.get("campaignKey") or "",
                    "actionType": item.get("actionType") or "",
                    "claimStatus": item.get("claimStatus") or "",
                    "amount": amount,
                }
            )
    return {
        "show_campaign": body.get("showCampaign") is True,
        "claimable": body.get("claimable") is True,
        "campaigns": campaigns,
    }


def benefit_campaigns(parsed):
    return [c for c in parsed["campaigns"] if c["actionType"] == ACTION_CLAIM_BENEFIT]


def claimable_campaigns(parsed):
    return [
        c
        for c in parsed["campaigns"]
        if c["actionType"] == ACTION_CLAIM_BENEFIT
        and c["claimStatus"] == STATUS_CLAIMABLE
    ]


def claimed_before(parsed):
    return any(c["claimStatus"] == STATUS_CLAIMED for c in benefit_campaigns(parsed))


def is_not_activated(parsed, usage):
    """判「账号没在 Qoder 侧开通过每日领取」。判据顺序不能换：

      1. 活动里**有任何** CLAIM_BENEFIT → 不是未开通
      2. usage 不是对象 → false（查不到就不能断言未开通）
      3. usage.qoderUsage 不是对象 → false
      4. **`addOnQuota` 键不存在** → true

    ⚠️ 第 4 步看的是「键是否存在」，不是数值大小，也不是 userQuota。
    ⚠️ `qoderUsage` 是空对象 {} 时第 3 步仍成立（只看类型），
       所以判空只能用 isinstance，不能用 `if not usage`（空 dict 是 falsy）。
    """
    if any(c["actionType"] == ACTION_CLAIM_BENEFIT for c in parsed["campaigns"]):
        return False
    if not isinstance(usage, dict):
        return False
    qoder_usage = usage.get("qoderUsage")
    if not isinstance(qoder_usage, dict):
        return False
    return "addOnQuota" not in qoder_usage


# ---------------------------------------------------------------- 余额（仅供展示）


def quota_remaining(quota):
    if not isinstance(quota, dict):
        return None
    total = quota.get("total")
    used = quota.get("used")
    remaining = quota.get("remaining")
    nums = [
        v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        for v in (total, used, remaining)
    ]
    if all(v is None for v in nums):
        return None
    total, used, remaining = nums
    if remaining is not None:
        return max(0.0, float(remaining))
    return max(0.0, (total or 0.0) - (used or 0.0))


def fetch_balance(headers, site):
    """查总剩余积分。任何异常都返回 None —— 余额查不到不影响领取。"""
    ok, status, body, raw = http_json(
        "GET", site["open_api_base"] + USAGE_PATH, headers
    )
    if not ok or not isinstance(body, dict):
        return None
    if body.get("displayMode") == "enterprise":
        return None                      # 企业版没有这套配额口径
    usage = body.get("qoderUsage")
    if not isinstance(usage, dict):
        return None
    parts = []
    for key in ("userQuota", "addOnQuota"):
        v = quota_remaining(usage.get(key))
        if v is not None:
            parts.append(v)
    dedicated = usage.get("dedicatedResourcePackages")
    if isinstance(dedicated, list):
        for item in dedicated:
            v = quota_remaining(item)
            if v is not None:
                parts.append(v)
    if not parts:
        return None
    return round(sum(parts), 2)


# ---------------------------------------------------------------- 领取单个


def claim_one(headers, site, campaign_id):
    """领取一个活动。返回 (kind, credit, message)，kind ∈ claimed/already/failed。"""
    url = "%s%s/%s/claim" % (
        site["open_api_base"],
        CAMPAIGNS_PATH,
        urllib.parse.quote(campaign_id, safe=""),
    )
    req_headers = dict(headers)
    req_headers["Content-Type"] = "application/json"
    # ⚠️ 请求体是**空字符串**，不是 "{}"。抓包实测 content-length: 0。
    ok, status, body, raw = http_json("POST", url, req_headers, body="")

    if not ok:
        if status == 0:
            return "failed", 0.0, "领取：网络错误 %s" % raw[:80]
        return "failed", 0.0, "领取：" + describe_non_json(status, raw)

    # replayed 优先于 status（服务端回放上次结果）
    if body.get("replayed") is True:
        return "already", 0.0, MSG_ALREADY

    st = body.get("status")
    if isinstance(st, str) and st and st != STATUS_CLAIMED:
        return "failed", 0.0, "领取未成功（status=%s）" % st

    benefit = body.get("benefit")
    amount = 0.0
    if isinstance(benefit, dict):
        amt = benefit.get("amount")
        if isinstance(amt, (int, float)) and not isinstance(amt, bool):
            amount = float(amt)
        elif isinstance(amt, str):
            try:
                amount = float(amt)
            except ValueError:
                amount = 0.0
    return "claimed", amount, ""


# ---------------------------------------------------------------- 单账号流程


def run_account(acc, site, mach_token, mach_type, dry_run, force):
    """跑一个账号。返回 dict：
        status  claimed / already / inactive / failed / skipped
        credit  本次领取到的积分
        message 给用户看的一句话
        detail  多行明细
    """
    token = acc["token"]
    headers = build_headers(token, mach_token, mach_type)
    refreshed = campaign_refreshed_today() or force

    # ① 预检：拉活动列表
    ok, status, body, raw = http_json(
        "GET", site["open_api_base"] + CAMPAIGNS_PATH, headers
    )
    if not ok:
        if status == 0:
            return {"status": "failed", "credit": 0.0,
                    "message": "活动列表查询失败：网络错误", "detail": raw[:120]}
        return {"status": "failed", "credit": 0.0,
                "message": "活动列表查询失败：" + describe_non_json(status, raw),
                "detail": ""}

    parsed = parse_campaigns(body)

    if dry_run:
        targets = claimable_campaigns(parsed)
        return {
            "status": "skipped",
            "credit": 0.0,
            "message": "演练模式：发现 %d 个可领取活动，未实际领取" % len(targets),
            "detail": "",
        }

    targets = claimable_campaigns(parsed)

    # ② 没有可领目标 → 三种情况分开说（顺序不能换）
    if not targets:
        if claimed_before(parsed):
            if not refreshed:
                # 刷新前看到的 CLAIMED 属于昨天。说成「今天已领取」是谎报。
                return {"status": "inactive", "credit": 0.0,
                        "message": "尚未刷新", "detail": NOT_REFRESHED_YET_HINT}
            return {"status": "already", "credit": 0.0,
                    "message": MSG_ALREADY, "detail": ""}

        # 从未领过：用 usage 判断「账号没开通过」
        ok_u, _, ubody, _ = http_json(
            "GET", site["open_api_base"] + USAGE_PATH, headers
        )
        usage = ubody if ok_u else None
        if is_not_activated(parsed, usage):
            return {"status": "inactive", "credit": 0.0,
                    "message": "未开通每日领取", "detail": NOT_ACTIVATED_HINT}
        return {"status": "inactive", "credit": 0.0,
                "message": MSG_NO_CAMPAIGN, "detail": ""}

    # ③ 逐个领取
    total = 0.0
    ok_count = 0
    first_error = ""
    for target in targets:
        kind, credit, msg = claim_one(headers, site, target["campaignId"])
        if kind == "claimed":
            total += credit
            ok_count += 1
        elif kind == "failed" and not first_error:
            first_error = msg

    # ⚠️ 判据是 total > 0，不是「有成功响应」：
    #    amount 缺失的 claimed 会被降级，避免把 0 积分报成领取成功。
    if total > 0:
        msg = "领取成功 +%g 积分（%d 个活动）" % (total, ok_count)
        if not refreshed:
            msg += "，但今天的轮次尚未刷新（不计入今日）"
        return {"status": "claimed", "credit": total, "message": msg, "detail": ""}

    if first_error:
        return {"status": "failed", "credit": 0.0, "message": first_error, "detail": ""}

    # 全部 replayed / 成功但 0 积分
    return {"status": "already", "credit": 0.0, "message": MSG_ALREADY, "detail": ""}


# ---------------------------------------------------------------- 状态持久化
#
# 凭据续期后必须落盘 —— 容器里改不了面板环境变量。
#
# 默认写到**脚本同级的 data/ 目录**（呆呆里是 /app/Dumb-Panel/data/，在数据卷内），
# 不存在就退回脚本目录（青龙单文件部署、本地调试都能跑）。
# 可用 QODER_STATE_DIR 显式指定。
#
# ⚠️ 首次运行从环境变量 QODER_TOKEN 读种子；之后优先读状态文件，
#    因为里面是**续期后**的最新值，比环境变量新。

def state_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.environ.get("QODER_STATE_DIR", "").strip(),
        os.path.join(os.path.dirname(here), "data"),   # ../data
        here,
    ):
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".w")
            with open(probe, "w") as fh:
                fh.write("")
            os.remove(probe)
            return cand
        except Exception:
            continue
    return here


STATE_FILE = os.path.join(state_dir(), "qoder_credentials.json")

#: 距过期不足这么久就续期（秒）。取 3 天：既留足余量，又不会频繁轮换 RT。
REFRESH_LEAD_SECS = 3 * 86400


def load_state():
    """读状态文件。返回 {"accounts": {备注: {token, refresh_token, uid, machine_id, expire_time}}}"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
            return data
    except Exception:
        pass
    return {"accounts": {}}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        try:
            os.chmod(STATE_FILE, 0o600)     # 含明文凭据，只给属主读
        except Exception:
            pass
        return True
    except Exception as exc:
        print("⚠️ 状态写入失败（%s）：本次结果未持久化，下次仍用环境变量里的旧值" % exc)
        return False


def parse_iso_ms(value):
    """把 ISO8601 或毫秒/秒时间戳统一成 unix 秒。失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000.0 if value > 1e12 else float(value)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        n = float(s)
        return n / 1000.0 if n > 1e12 else n
    # 2026-11-22T03:43:56Z
    try:
        txt = s.replace("Z", "+00:00")
        return datetime.fromisoformat(txt).timestamp()
    except Exception:
        return None


def needs_refresh(account, now=None):
    """距过期不足 REFRESH_LEAD_SECS 才续期。

    ⚠️ 取不到过期时间时返回 **False**：宁可等 401 再刷，也不要在不知道
    剩多久的情况下把 refresh_token 轮换掉。
    """
    exp = parse_iso_ms(account.get("expire_time"))
    if exp is None:
        return False
    moment = now if now is not None else time.time()
    return (exp - moment) < REFRESH_LEAD_SECS


def refresh_credential(account, site):
    """调 qoder 续期端点，返回更新后的 account dict；失败抛异常。

    实测（2026-10-23，真实 qodercn 账号）：
        POST {openApiBase}/api/v1/deviceToken/refresh
        body {"refresh_token": ..., "machine_id": ...}   ← **蛇形命名**
        200 {"device_token": "dt-...", "refresh_token": "drt-...",
             "token_type": "Bearer",
             "expires_at": "2026-11-22T03:43:56Z",        ← **ISO 字符串，不是毫秒**
             "refresh_token_expires_at": "2027-10-18T03:43:56Z"}

    ⚠️ 三个容易写错的点（都有实测背书）：
      1. body 必须是 `refresh_token`（蛇形）。发驼峰 `refreshToken` 会被拒：
         HTTP 400 {"errorCode":"DeviceRefreshTokenRequired"}
      2. `machine_id` 必填（登录时绑定的设备指纹）
      3. 响应里 access token 的字段名是 **`device_token`**，不是 accessToken；
         过期时间是 **ISO 字符串**，不是毫秒时间戳
    """
    url = site["open_api_base"] + "/api/v1/deviceToken/refresh"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "qoder/1.0.0",
    }
    payload = {
        "refresh_token": account.get("refresh_token", ""),
        "machine_id": account.get("machine_id", ""),
    }
    ok, status, body, raw = http_json("POST", url, headers, body=json.dumps(payload))
    if not ok or not isinstance(body, dict):
        if status in (401, 403):
            raise RuntimeError("续期被拒（HTTP %s），需重新登录该账号" % status)
        raise RuntimeError("续期失败：HTTP %s %s" % (status, str(raw)[:100]))

    data = body.get("data") if isinstance(body.get("data"), dict) else body
    new_tok = data.get("device_token") or data.get("access_token") or data.get("token")
    if not new_tok:
        raise RuntimeError("续期响应缺少 device_token")

    updated = dict(account)
    updated["token"] = new_tok
    updated["security_oauth_token"] = new_tok      # qoderBearerToken 优先取它
    new_rt = data.get("refresh_token") or data.get("refreshToken")
    if new_rt:
        updated["refresh_token"] = new_rt
    exp = parse_iso_ms(data.get("expires_at") or data.get("expireAt"))
    if exp:
        updated["expire_time"] = exp
    rt_exp = parse_iso_ms(data.get("refresh_token_expires_at"))
    if rt_exp:
        updated["refresh_token_expire_time"] = rt_exp
    return updated


def ensure_fresh(account, site):
    """需要就续期，返回 (account, 状态描述)。续期失败不抛 —— 用旧 token 继续试。"""
    if not needs_refresh(account):
        exp = parse_iso_ms(account.get("expire_time"))
        if exp:
            return account, "not_needed（剩 %.1f 天）" % ((exp - time.time()) / 86400)
        return account, "not_needed"
    if not account.get("refresh_token"):
        return account, "no_refresh_token（无 RT，无法续期）"
    try:
        updated = refresh_credential(account, site)
        exp = parse_iso_ms(updated.get("expire_time"))
        return updated, "ok（新到期 %s）" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "未知"
        )
    except Exception as exc:
        return account, "failed（%s）" % str(exc)[:80]


# ---------------------------------------------------------------- 通知


def push_serverchan(sendkey, title, content):
    """Server酱推送。sctp 开头走方糖通道，其余走 Turbo。"""
    if not sendkey:
        return False
    key = sendkey.strip()
    if key.lower().startswith("sctp"):
        # sctp{uid}t{...} → https://{uid}.push.ft07.com/send/{key}.send
        digits = ""
        for ch in key[4:]:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            return False
        url = "https://%s.push.ft07.com/send/%s.send" % (digits, key)
    else:
        url = "https://sctapi.ftqq.com/%s.send" % key

    payload = urllib.parse.urlencode({"title": title[:100], "desp": content[:30000]})
    req = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        return body.get("code") == 0 or body.get("errno") == 0
    except Exception as exc:
        print("⚠️ 推送失败：%s" % exc)
        return False


# ---------------------------------------------------------------- 主流程


def main():
    # 站点写死为中国版。国际版（openapi.qoder.sh）的免费账号没有每日领取权益
    # （实测：userQuota.remaining=0，活动列表里没有 CLAIM_BENEFIT），
    # 留着切换开关只会让人误以为配错了。要跑国际版就改这一行。
    site = SITES["cn"]

    accounts = parse_accounts(os.environ.get("QODER_TOKEN", ""))
    if not accounts:
        print("❌ 未找到可用账号。请设置环境变量 QODER_TOKEN")
        print("   格式（每行一个）：token#备注")
        print("   token 从 Qoder 客户端的 access_token 取，或直接用 security_oauth_token")
        return 1

    dry_run = (os.environ.get("QODER_DRY_RUN") or "0").strip() == "1"
    force = (os.environ.get("QODER_FORCE") or "0").strip() == "1"
    mach_token = (os.environ.get("QODER_MACHINE_TOKEN") or "").strip()
    mach_type = (os.environ.get("QODER_MACHINE_TYPE") or "").strip()

    # ---- 与状态文件合并：状态里的值更新（它是上次续期的产物）----
    state = load_state()
    saved = state.get("accounts") or {}
    for acc in accounts:
        ent = saved.get(acc["remark"])
        if isinstance(ent, dict) and ent.get("token"):
            acc.setdefault("machine_id", ent.get("machine_id", ""))
            acc["token"] = ent["token"]
            acc["refresh_token"] = ent.get("refresh_token", "")
            acc["expire_time"] = ent.get("expire_time")
            acc["refresh_token_expire_time"] = ent.get("refresh_token_expire_time")
    # machine_id 也可能从环境变量补
    for acc in accounts:
        acc.setdefault("machine_id", (os.environ.get("QODER_MACHINE_ID") or "").strip())

    now = utc8_now()
    refreshed = campaign_refreshed_today()
    print("╔════════════════════════════════════════╗")
    print("║ 🎯 Qoder 每日积分领取                  ║")
    print("╚════════════════════════════════════════╝")
    print("🌐 站点: %s" % site["name"])
    print("🕐 时间: %s（UTC+8）" % now.strftime("%Y-%m-%d %H:%M:%S"))
    print("🔄 轮次: %s" % ("今日已刷新" if refreshed else "尚未刷新（10:00 后才算今天）"))
    print("👥 账号数: %d%s" % (len(accounts), "（演练模式）" if dry_run else ""))
    print("📁 状态文件: %s" % STATE_FILE)

    results = []
    state_dirty = False
    for idx, acc in enumerate(accounts, 1):
        print("")
        print("╭─ 👤 账号%d  %s" % (idx, mask_remark(acc["remark"])))

        # ---- 续期（需要才刷，失败不阻塞）----
        acc, refresh_note = ensure_fresh(acc, site)
        if refresh_note.startswith("ok"):
            print("   🔑 凭据: 已续期")
            state_dirty = True
        elif refresh_note.startswith(("failed", "no_refresh_token")):
            print("   ⚠️ 凭据: 续期未完成（%s），仍用旧 token 尝试" % refresh_note)
        elif refresh_note.startswith("not_needed"):
            print("   🔑 凭据: 有效，无需续期")

        # 续期成功后把最新值写回状态（即便本次领取失败也保留，避免重复轮换）
        if acc.get("refresh_token"):
            state["accounts"][acc["remark"]] = {
                "token": acc.get("token", ""),
                "refresh_token": acc.get("refresh_token", ""),
                "machine_id": acc.get("machine_id", ""),
                "expire_time": acc.get("expire_time"),
                "refresh_token_expire_time": acc.get("refresh_token_expire_time"),
            }
            state_dirty = True

        try:
            r = run_account(acc, site, mach_token, mach_type, dry_run, force)
        except Exception as exc:
            r = {"status": "failed", "credit": 0.0,
                 "message": "异常：%s: %s" % (type(exc).__name__, exc), "detail": ""}
        r["remark"] = acc["remark"]
        results.append(r)

        icon = {
            "claimed": "✅", "already": "✅", "inactive": "ℹ️",
            "failed": "❌", "skipped": "🔍",
        }.get(r["status"], "•")
        print("   %s %s" % (icon, r["message"]))
        if r.get("detail"):
            print("   %s" % r["detail"])

        # 顺手展示余额（失败不影响结论）
        try:
            bal = fetch_balance(build_headers(acc["token"], mach_token, mach_type), site)
            if bal is not None:
                print("   💰 当前剩余积分：%g" % bal)
        except Exception:
            pass

    # ---- 汇总 ----
    if state_dirty:
        save_state(state)

    print("")
    claimed = [r for r in results if r["status"] == "claimed"]
    already = [r for r in results if r["status"] == "already"]
    inactive = [r for r in results if r["status"] == "inactive"]
    failed = [r for r in results if r["status"] == "failed"]
    total_credit = sum(r["credit"] for r in claimed)

    parts = []
    if claimed:
        parts.append("%d 个领取成功 +%g 积分" % (len(claimed), total_credit))
    if already:
        parts.append("%d 个今天已领" % len(already))
    if inactive:
        parts.append("%d 个未开启" % len(inactive))
    if failed:
        parts.append("%d 个失败" % len(failed))
    summary = "，".join(parts) if parts else "没有需要处理的账号"
    print("🏁 %s" % summary)

    # ---- 推送：只在失败时发 ----
    # ⚠️ Server酱 免费版每天只有 5 条额度，成功必须静默。
    # 「尚未刷新」是正常状态（10:00 前跑），也不推。
    # 需要每次都推（调试用）时设 QODER_NOTIFY=always。
    notify_mode = (os.environ.get("QODER_NOTIFY") or "fail").strip().lower()
    if notify_mode != "0" and not dry_run:
        sendkey = (os.environ.get("SERVERCHAN_SENDKEY") or "").strip()
        need_push = bool(failed) or notify_mode == "always"
        if sendkey and need_push:
            lines = ["**%s** · %s" % (site["name"], now.strftime("%Y-%m-%d %H:%M"))]
            for r in results:
                icon = {"claimed": "✅", "already": "✅", "inactive": "ℹ️",
                        "failed": "❌"}.get(r["status"], "•")
                lines.append("- %s `%s` %s" % (icon, r["remark"], r["message"]))
                if r.get("detail"):
                    lines.append("  %s" % r["detail"])
            lines.append("")
            lines.append("汇总：%s" % summary)
            ok = push_serverchan(sendkey, "❌ Qoder 领取失败", "\n".join(lines))
            print("🔔 失败推送%s" % ("成功" if ok else "失败"))
        elif sendkey and failed:
            print("⚠️ 推送失败但未配置 SendKey")
        elif not need_push:
            print("ℹ️ 全部正常，静默不推送（节省 Server酱 额度）")

    # ---- 退出码 ----
    # 失败与「尚未刷新」都算未办成 → 1，让调度器知道今天还没落实。
    # 已领取 / 今天已领 / 未开通 都算正常终态 → 0。
    if dry_run:
        return 0
    if failed or any(r["status"] == "inactive" and r["message"] == "尚未刷新"
                     for r in results):
        if failed:
            print("❌ 有账号领取失败，退出码 1")
        else:
            print("⏳ 尚未到今日刷新时间，退出码 1（10:00 后重跑即可）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
