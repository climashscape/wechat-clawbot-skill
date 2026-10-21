#!/usr/bin/env python3
"""WeChat ClawBot minimal client (iLink bot API).

Self-to-self messaging channel: bind a personal WeChat via QR scan, then send
text / image / file to that same WeChat account (the "微信ClawBot" contact).

Protocol mirrors @tencent-weixin/openclaw-weixin; constants pinned to the
values proven working in fooling/wechat-clawbot-gateway (channel 2.1.10).

Subcommands:
  login            QR login, saves credentials
  daemon           long-poll loop: keeps session alive, captures context tokens
  send             send text / image / file to your own WeChat
  status           check session health and known contacts

State lives in ~/.local/state/wechat-clawbot/ (credentials 0600).
Pure stdlib: MD5 and AES-128-ECB are implemented locally (iLink CDN protocol
requires these exact primitives; both are validated against standard test
vectors at first use).
"""

import argparse
import base64
import http.client
import http.server
import json
import math
import os
import secrets
import signal
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

STATE_DIR = os.environ.get(
    "WECHAT_CLAWBOT_STATE",
    os.path.expanduser("~/.local/state/wechat-clawbot"),
)
CREDS_PATH = os.path.join(STATE_DIR, "credentials.json")
TOKENS_PATH = os.path.join(STATE_DIR, "context_tokens.json")
BUF_PATH = os.path.join(STATE_DIR, "get_updates_buf.txt")
EXPIRED_FLAG = os.path.join(STATE_DIR, "session_expired")
INBOUND_PATH = os.path.join(STATE_DIR, "last_inbound.json")
QUEUE_DIR = os.path.join(STATE_DIR, "queue")
SENDLOG_PATH = os.path.join(STATE_DIR, "send_log.json")
INBOX_DIR = os.path.join(STATE_DIR, "inbox")
KEEPALIVE_PATH = os.path.join(STATE_DIR, "keepalive.json")
BRIDGE_PATH = os.path.join(STATE_DIR, "bridge.json")

# 微信服务端限制（Tencent/openclaw-weixin#202）：用户入站消息续期 24h 推送窗口；
# 主动推送每 24h 约有条数配额，超量被静默限流。这里取保守值做看护与告警。
WINDOW_S = 24 * 3600
NUDGE_REMAIN_S = 4 * 3600
SEND_QUOTA_WARN = 8

AUTH_BASE = "https://ilinkai.weixin.qq.com"
CDN_UPLOAD = "https://novac2c.cdn.weixin.qq.com/c2c/upload"

# SSRF 边界：所有请求目标都锁定在腾讯微信域名，仅 https；重定向一律拒绝
ALLOWED_HOST_SUFFIX = ".weixin.qq.com"

BOT_TYPE = "3"
CHANNEL_VERSION = "2.1.10"
APP_CLIENT_VERSION = 131338  # (2<<16)|(1<<8)|10 mirrored from official client

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
ITEM_TEXT, ITEM_IMAGE, ITEM_FILE = 1, 2, 4
STATE_FINISH = 2
UPLOAD_IMAGE, UPLOAD_FILE = 1, 3

MAX_SEND_BYTES = 20 * 1024 * 1024   # 纯 Python AES 吞吐约 1MB/s，超限既慢又易超时
MAX_QUEUE_ATTEMPTS = 5              # 补发超过该次数移入 queue/dead/，防毒丸烧配额

SESSION_EXPIRED_ERRCODE = -14
LOGIN_DEADLINE_S = 8 * 60
MAX_QR_REFRESH = 3
POLL_TIMEOUT_S = 35
API_TIMEOUT_S = 30
HEARTBEAT_INTERVAL_S = 5 * 60
MAX_RETRIES = 5
RETRY_DELAY_S = 3


LOGFILE: str | None = None


def log(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass  # pythonw 下 stderr 为 None
    if LOGFILE:
        try:
            with open(LOGFILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except OSError:
            pass


def atomic_write(path: str, data: str, mode: int = 0o600) -> None:
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(3)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ── Primitives: MD5 + AES-128-ECB, pure stdlib ───────────────
# iLink CDN 协议要求 MD5（rawfilemd5/md5 校验字段）与 AES-128-ECB（CDN 媒体加
# 密）。两者均为协议规定原语，非安全用途；实现对照 FIPS-197 / RFC 1321 测试
# 向量在首次调用时自校验，任何不符直接抛错。

_MD5_S = ([7, 12, 17, 22] * 4 + [5, 9, 14, 20] * 4
          + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4)
_MD5_K = [int(abs(math.sin(i + 1)) * (1 << 32)) & 0xFFFFFFFF for i in range(64)]


def md5_hex(data: bytes) -> str:
    msg = bytearray(data)
    bit_len = len(data) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += bit_len.to_bytes(8, "little")
    a0, b0, c0, d0 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476
    for off in range(0, len(msg), 64):
        m = struct.unpack("<16I", bytes(msg[off:off + 64]))
        a, b, c, d = a0, b0, c0, d0
        for i in range(64):
            if i < 16:
                f, g = (b & c) | (~b & d), i
            elif i < 32:
                f, g = (d & b) | (~d & c), (5 * i + 1) % 16
            elif i < 48:
                f, g = b ^ c ^ d, (3 * i + 5) % 16
            else:
                f, g = c ^ (b | ~d), (7 * i) % 16
            f = (f + a + _MD5_K[i] + m[g]) & 0xFFFFFFFF
            a, d, c = d, c, b
            s = _MD5_S[i]
            b = (b + ((f << s) | (f >> (32 - s)))) & 0xFFFFFFFF
        a0 = (a0 + a) & 0xFFFFFFFF
        b0 = (b0 + b) & 0xFFFFFFFF
        c0 = (c0 + c) & 0xFFFFFFFF
        d0 = (d0 + d) & 0xFFFFFFFF
    return struct.pack("<4I", a0, b0, c0, d0).hex()


_AES_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
_AES_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _aes_xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _aes_expand_key(key: bytes) -> list[bytes]:
    words = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = [_AES_SBOX[t[1]], _AES_SBOX[t[2]], _AES_SBOX[t[3]], _AES_SBOX[t[0]]]
            t[0] ^= _AES_RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ t[j] for j in range(4)])
    # 11 个轮密钥，每个 16 字节（按列拼接，与 AES 状态列主序一致）
    return [bytes(b for w in words[r * 4:r * 4 + 4] for b in w) for r in range(11)]


def _aes_encrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    def ark(s, rk):
        return [x ^ y for x, y in zip(s, rk)]

    def shift_rows(s):
        for r in range(1, 4):
            row = [s[r], s[4 + r], s[8 + r], s[12 + r]]
            row = row[r:] + row[:r]
            s[r], s[4 + r], s[8 + r], s[12 + r] = row

    def mix_columns(s):
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            s[c] = _aes_xtime(a0) ^ _aes_xtime(a1) ^ a1 ^ a2 ^ a3
            s[c + 1] = a0 ^ _aes_xtime(a1) ^ _aes_xtime(a2) ^ a2 ^ a3
            s[c + 2] = a0 ^ a1 ^ _aes_xtime(a2) ^ _aes_xtime(a3) ^ a3
            s[c + 3] = _aes_xtime(a0) ^ a0 ^ a1 ^ a2 ^ _aes_xtime(a3)

    s = ark(list(block), round_keys[0])
    for rnd in range(1, 10):
        s = [_AES_SBOX[b] for b in s]
        shift_rows(s)
        mix_columns(s)
        s = ark(s, round_keys[rnd])
    s = [_AES_SBOX[b] for b in s]
    shift_rows(s)
    return bytes(ark(s, round_keys[10]))


def _aes128_ecb_encrypt_padded(key: bytes, data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    padded = data + bytes([pad]) * pad
    rk = _aes_expand_key(key)
    out = bytearray()
    for off in range(0, len(padded), 16):
        out += _aes_encrypt_block(padded[off:off + 16], rk)
    return bytes(out)


def aes128_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB + PKCS7（iLink CDN 上传要求）。key 必须 16 字节。"""
    _crypto_selftest()
    return _aes128_ecb_encrypt_padded(key, data)


_CRYPTO_OK = False


def _crypto_selftest() -> None:
    """RFC 1321 / FIPS-197 标准向量，失败即拒绝运行。"""
    global _CRYPTO_OK
    if _CRYPTO_OK:
        return
    _CRYPTO_OK = True
    if md5_hex(b"") != "d41d8cd98f00b204e9800998ecf8427e" or \
            md5_hex(b"abc") != "900150983cd24fb0d6963f7d28e17f72":
        raise RuntimeError("MD5 self-test failed")
    ct = _aes128_ecb_encrypt_padded(
        bytes(range(16)), bytes.fromhex("00112233445566778899aabbccddeeff"))
    # FIPS-197 附录 C.1：PKCS7 后为两块，首块必须等于标准密文
    if not ct.startswith(bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")):
        raise RuntimeError("AES-128 self-test failed")


# ── HTTP layer ────────────────────────────────────────────────


def validate_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (
            host == ALLOWED_HOST_SUFFIX.lstrip(".") or host.endswith(ALLOWED_HOST_SUFFIX)):
        raise RuntimeError(f"blocked non-WeChat target: {url[:120]}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    # 默认直连：urllib 会继承 http_proxy 环境变量，全局代理下会把微信 API
    # 流量发给代理导致莫名失败。需要代理时设 WECHAT_CLAWBOT_PROXY。
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    proxy = os.environ.get("WECHAT_CLAWBOT_PROXY")
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


_OPENER = _build_opener()


def _headers(token: str | None) -> dict:
    uin = base64.b64encode(str(secrets.randbits(32)).encode()).decode()
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str(APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _request(url: str, body: dict | None, token: str | None, timeout: float) -> dict:
    validate_url(url)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(token),
                                 method="POST" if data else "GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            raise RuntimeError(f"blocked redirect from {url[:120]}") from None
        raise RuntimeError(f"HTTP {e.code} from {url[:120]}: "
                           f"{e.read().decode(errors='replace')[:300]}") from None


class ApiTimeout(RuntimeError):
    """请求超时。getupdates 长轮询靠它区分「空闲周期」与「真错误」。"""


def api_post(base_url: str, endpoint: str, body: dict, token: str | None = None,
             timeout: float = API_TIMEOUT_S) -> dict:
    if endpoint.startswith("ilink/bot/") and "base_info" not in body:
        body = {**body, "base_info": {"channel_version": CHANNEL_VERSION}}
    url = base_url.rstrip("/") + "/" + endpoint
    validate_url(url)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _request(url, body, token, timeout)
        except socket.timeout:
            raise ApiTimeout(f"{endpoint} timeout after {timeout}s") from None
        except (urllib.error.URLError, ConnectionError, OSError,
                http.client.HTTPException, json.JSONDecodeError) as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                log(f"[net] {endpoint} network error, retry {attempt}/{MAX_RETRIES}: {e}")
                time.sleep(RETRY_DELAY_S)
    raise RuntimeError(f"{endpoint} failed after {MAX_RETRIES} retries: {last_exc}") from last_exc


# ── Login ─────────────────────────────────────────────────────


def show_qr(content: str) -> None:
    print("\n用手机微信扫码（或在浏览器打开下面的链接后扫码）：\n")
    try:
        import qrcode  # type: ignore[import-not-found]

        qr = qrcode.QRCode(border=1)
        qr.add_data(content)
        qr.print_ascii(invert=True)
    except ImportError:
        log("[hint] 未安装 qrcode 库（pip install --user qrcode 可显示终端二维码）")
    print(f"\n链接（浏览器打开即显示二维码）: {content}\n")
    log(f"[login] 二维码链接: {content}")


def cmd_login(_args: argparse.Namespace) -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    if load_json(CREDS_PATH, None):
        log(f"[login] 已有凭据（{CREDS_PATH}）。重新绑定请先删除该文件。")
        return

    deadline = time.monotonic() + LOGIN_DEADLINE_S
    for refresh in range(MAX_QR_REFRESH):
        qr = None
        for attempt in range(1, 4):
            try:
                qr = _request(f"{AUTH_BASE}/ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
                              None, None, API_TIMEOUT_S)
                break
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                log(f"[login] 获取二维码失败（{attempt}/3）：{e}")
                time.sleep(RETRY_DELAY_S)
        if qr is None:
            sys.exit("[login] 无法连接微信服务（ilinkai.weixin.qq.com），请检查网络或代理设置")
        show_qr(qr["qrcode_img_content"])
        scanned = False
        while time.monotonic() < deadline:
            try:
                st = _request(
                    f"{AUTH_BASE}/ilink/bot/get_qrcode_status"
                    f"?qrcode={urllib.parse.quote(qr['qrcode'], safe='')}",
                    None, None, POLL_TIMEOUT_S,
                )
            except socket.timeout:
                continue
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                log(f"[login] 查询扫码状态失败：{e}，稍后重试")
                time.sleep(2)
                continue
            status = st.get("status")
            if status == "scaned" and not scanned:
                scanned = True
                log("[login] 已扫码，请在手机上确认登录…")
            elif status == "confirmed":
                if not st.get("bot_token") or not st.get("ilink_bot_id"):
                    sys.exit("[login] 服务器确认了登录但未返回 token/bot_id，请重试")
                creds = {
                    "token": st["bot_token"],
                    "baseUrl": st.get("baseurl") or AUTH_BASE,
                    "accountId": st["ilink_bot_id"],
                    "userId": st.get("ilink_user_id") or "",
                    "loginTime": int(time.time()),
                }
                validate_url(creds["baseUrl"])
                atomic_write(CREDS_PATH, json.dumps(creds, indent=2))
                log(f"[login] 绑定成功 accountId={creds['accountId']} userId={creds['userId']}")
                return
            elif status == "expired":
                log(f"[login] 二维码已过期，刷新… ({refresh + 1}/{MAX_QR_REFRESH})")
                break
            time.sleep(1)
        else:
            break
    sys.exit("[login] 超时未完成扫码，请重新运行 login")


# ── Credentials / tokens ─────────────────────────────────────


def load_creds() -> dict:
    creds = load_json(CREDS_PATH, None)
    if not isinstance(creds, dict) or not creds.get("token") or not creds.get("baseUrl"):
        sys.exit(f"[err] 凭据缺失或损坏：删除 {CREDS_PATH} 后重新运行 login 扫码")
    validate_url(creds["baseUrl"])
    return creds


def load_tokens() -> dict:
    return load_json(TOKENS_PATH, {})


def save_tokens(tokens: dict) -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    atomic_write(TOKENS_PATH, json.dumps(tokens, indent=2))


# ── 窗口看护 / 失败队列 / 额度计数 ────────────────────────────


def load_inbound() -> dict:
    return load_json(INBOUND_PATH, {})


def mark_inbound(uid: str, ts: float | None = None) -> None:
    """任何用户入站消息都会重置 24h 推送窗口。"""
    data = load_inbound()
    data[uid] = int(ts or time.time())
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    atomic_write(INBOUND_PATH, json.dumps(data))


def window_remaining(uid: str) -> float | None:
    """窗口剩余秒数；None = 无入站记录（窗口状态未知）。可为负（已过期）。"""
    ts = load_inbound().get(uid)
    return None if ts is None else ts + WINDOW_S - time.time()


def enqueue_failed(to: str, items: list[dict]) -> str:
    """入队失败消息；同目标同内容去重（agent 重试不会造成重复送达）。"""
    os.makedirs(QUEUE_DIR, mode=0o700, exist_ok=True)
    sig = md5_hex(json.dumps({"to": to, "items": items},
                             sort_keys=True, ensure_ascii=False).encode())
    for existing in os.listdir(QUEUE_DIR):
        if not existing.endswith(".json"):
            continue
        entry = load_json(os.path.join(QUEUE_DIR, existing), None)
        if isinstance(entry, dict) and entry.get("sig") == sig:
            return existing
    name = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}.json"
    path = os.path.join(QUEUE_DIR, name)
    atomic_write(path, json.dumps({"to": to, "items": items, "sig": sig,
                                   "ts": int(time.time()), "attempts": 0}))
    return name


def replay_queue(creds: dict, uid: str) -> None:
    """窗口恢复（收到该用户入站消息）后自动补发排队内容。"""
    if not os.path.isdir(QUEUE_DIR):
        return
    for name in sorted(os.listdir(QUEUE_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(QUEUE_DIR, name)
        entry = load_json(path, None)
        if not isinstance(entry, dict) or entry.get("to") != uid:
            continue
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry["attempts"] > MAX_QUEUE_ATTEMPTS:
            dead = os.path.join(QUEUE_DIR, "dead")
            os.makedirs(dead, mode=0o700, exist_ok=True)
            os.replace(path, os.path.join(dead, name))
            log(f"[daemon] 补发超过 {MAX_QUEUE_ATTEMPTS} 次仍失败，移入 queue/dead/：{name}")
            continue
        resp = send_items(creds, uid, entry.get("items") or [])
        if resp.get("ret", 0) == 0:
            os.unlink(path)
            log(f"[daemon] 补发成功，移除队列 {name}")
        else:
            atomic_write(path, json.dumps(entry))
            log(f"[daemon] 补发仍失败（第 {entry['attempts']} 次），保留队列 {name}")
            break


def acquire_singleton_lock():
    """daemon 单实例锁：posix 用 flock；无 fcntl 的平台退化为 pidfile 尽力而为。"""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    lock_path = os.path.join(STATE_DIR, "daemon.lock")
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


_SEND_LOCK = threading.Lock()


def note_send(ts: float) -> None:
    """记录主动推送时间，用于 24h 配额告警（进程内加锁，计数为告警用途）。"""
    with _SEND_LOCK:
        log_entries = [t for t in load_json(SENDLOG_PATH, [])
                       if time.time() - t < WINDOW_S] + [ts]
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        atomic_write(SENDLOG_PATH, json.dumps(log_entries))
    recent = len([t for t in log_entries if time.time() - t < WINDOW_S])
    if recent >= SEND_QUOTA_WARN:
        log(f"[quota] 24h 内主动推送已达 {recent} 条（平台配额约 10 条/24h，超量会被限流）")


def find_target(creds: dict, to: str | None) -> str | None:
    """解析收件人；只允许绑定的本人或已知联系人（收到过消息的会话）。"""
    allowed = set(load_tokens())
    if creds.get("userId"):
        allowed.add(creds["userId"])
        candidate = to or creds["userId"]
    elif to:
        candidate = to
    elif len(allowed) == 1:
        candidate = next(iter(allowed))
    else:
        return None
    return candidate if candidate in allowed else None


def resolve_target(creds: dict, to: str | None) -> str:
    target = find_target(creds, to)
    if not target:
        sys.exit("[err] 收件目标无效：本技能只发给绑定的本人或已知联系人。请先给微信ClawBot"
                 "发一条消息（daemon 会记录你的 userId），或用 --to <已知 userId> 指定")
    return target


# ── Media (CDN upload) ────────────────────────────────────────


def upload_media(creds: dict, raw: bytes, media_type: int, to_user: str) -> tuple[dict, int]:
    key = secrets.token_bytes(16)
    key_hex = key.hex()
    filekey = secrets.token_bytes(16).hex()
    encrypted = aes128_ecb_encrypt(key, raw)
    # rawfilemd5 是 iLink 协议要求的服务端校验字段（官方客户端同样传 MD5），
    # 协议规定值，非安全用途
    resp = api_post(creds["baseUrl"], "ilink/bot/getuploadurl", {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user,
        "rawsize": len(raw),
        "filesize": len(encrypted),
        "rawfilemd5": md5_hex(raw),
        "aeskey": key_hex,
        "no_need_thumb": True,
    }, creds["token"])
    if resp.get("upload_param"):
        cdn_url = (f"{CDN_UPLOAD}?encrypted_query_param="
                   f"{urllib.parse.quote(resp['upload_param'], safe='')}"
                   f"&filekey={urllib.parse.quote(filekey, safe='')}")
    else:
        cdn_url = resp.get("upload_full_url")
    if cdn_url:
        validate_url(cdn_url)
    if not cdn_url:
        raise RuntimeError(f"getuploadurl failed: {json.dumps(resp)[:300]}")

    req = urllib.request.Request(cdn_url, data=encrypted,
                                 headers={"Content-Type": "application/octet-stream"},
                                 method="POST")
    try:
        with _OPENER.open(req, timeout=120) as cdn:
            cdn.read()
            param = cdn.headers.get("x-encrypted-param") or cdn.headers.get("x-encrypted-query-param")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"CDN upload failed: {e.code} {e.headers.get('x-error-message', '')}") from None
    if not param:
        param = resp.get("upload_param") or ""
    if not param:
        raise RuntimeError("CDN 上传成功但未返回下载参数")
    cdn_media = {
        "encrypt_query_param": param,
        "aes_key": base64.b64encode(key_hex.encode()).decode(),
        "encrypt_type": 1,
    }
    return cdn_media, len(encrypted)


# ── Send ──────────────────────────────────────────────────────


def send_items(creds: dict, to: str, items: list[dict]) -> dict:
    tokens = load_tokens()
    context_token = tokens.get(to)
    if not context_token:
        log(f"[warn] 没有 {to} 的 context_token（24 小时内你没给微信ClawBot发过消息），"
            f"发送大概率被丢弃")
    resp = api_post(creds["baseUrl"], "ilink/bot/sendmessage", {
        "msg": {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": f"bot-{int(time.time() * 1000)}-{secrets.token_hex(3)}",
            "message_type": MSG_TYPE_BOT,
            "message_state": STATE_FINISH,
            "item_list": items,
            "context_token": context_token,
        },
    }, creds["token"])
    ret = resp.get("ret", 0)
    if ret != 0:
        reason = ("窗口失效（context_token 过期）" if ret == -2
                  else f"服务端拒绝 ret={ret}")
        log(f"[send] 失败：{reason} errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}")
    else:
        note_send(time.time())
    return resp


def do_send(creds: dict, to: str, text: str | None = None, file: str | None = None,
            image: str | None = None, as_name: str | None = None) -> tuple[str, dict]:
    """构建消息并发送；业务失败自动入队。返回 ("ok"|"queued", resp)。"""
    items: list[dict] = []
    if text:
        items.append({"type": ITEM_TEXT, "text_item": {"text": text}})
    for path, media_type, item_type, key in (
        (file, UPLOAD_FILE, ITEM_FILE, "file_item"),
        (image, UPLOAD_IMAGE, ITEM_IMAGE, "image_item"),
    ):
        if not path:
            continue
        path = os.path.realpath(os.path.expanduser(path))
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) > MAX_SEND_BYTES:
            raise ValueError(f"文件 {len(raw) // 1024 // 1024}MB 超过 {MAX_SEND_BYTES // 1024 // 1024}MB "
                             f"上限（纯 Python 加密慢且易超时），请压缩或改用网盘链接")
        cdn_media, enc_size = upload_media(creds, raw, media_type, to)
        item: dict = {"type": item_type, key: {"media": cdn_media}}
        if item_type == ITEM_FILE:
            item[key].update({
                "file_name": as_name or os.path.basename(path),
                "len": str(len(raw)),
                "md5": md5_hex(raw),
            })
        else:
            item[key]["mid_size"] = enc_size
        items.append(item)
    if not items:
        raise ValueError("empty message")
    resp = send_items(creds, to, items)
    if resp.get("ret", 0) == 0:
        return "ok", resp
    enqueue_failed(to, items)
    return "queued", resp


def cmd_send(args: argparse.Namespace) -> None:
    creds = load_creds()
    to = resolve_target(creds, args.to)
    if not (args.text or args.file or args.image):
        sys.exit("[err] 没有要发送的内容：用 --text / --file / --image 至少给一个")
    try:
        result, _resp = do_send(creds, to, text=args.text, file=args.file,
                                image=args.image, as_name=args.as_name)
    except FileNotFoundError as e:
        sys.exit(f"[err] 文件不存在: {e}")
    if result == "ok":
        print(f"OK -> {to}")
    else:
        # QUEUED = 已受理，退出码 0：调用方**不要重试**（重试会被去重，但无意义），
        # 用户下一条微信消息到达后 daemon 自动补发
        print(f"QUEUED -> {to}：已入队待补发（勿重试）")


def status_summary(creds: dict) -> str:
    """单行状态（微信内 /status 指令回复用）。"""
    try:
        resp = api_post(creds["baseUrl"], "ilink/bot/getconfig", {}, creds["token"])
        expired = (resp.get("errcode") == SESSION_EXPIRED_ERRCODE
                   or resp.get("ret") == SESSION_EXPIRED_ERRCODE)
        sess = "EXPIRED" if expired else "ok"
    except RuntimeError:
        sess = "unknown"
    lines = [f"session: {sess}"]
    for uid in load_tokens():
        rem = window_remaining(uid)
        if rem is None:
            lines.append("window: 未知（无入站记录）")
        elif rem <= 0:
            lines.append("window: 已过期（本条消息已续期）")
        else:
            lines.append(f"window: 剩余 {rem / 3600:.1f} 小时")
    if os.path.isdir(QUEUE_DIR):
        pending = len([f for f in os.listdir(QUEUE_DIR) if f.endswith(".json")])
        if pending:
            lines.append(f"queue: {pending} 条待补发")
    return "\n".join(lines)


def cmd_status(_args: argparse.Namespace) -> None:
    creds = load_creds()
    try:
        resp = api_post(creds["baseUrl"], "ilink/bot/getconfig", {}, creds["token"])
    except RuntimeError as e:
        sys.exit(f"[status] 请求失败: {e}")
    expired = (resp.get("errcode") == SESSION_EXPIRED_ERRCODE
               or resp.get("ret") == SESSION_EXPIRED_ERRCODE)
    age_days = (time.time() - creds.get("loginTime", 0)) / 86400
    print(f"session: {'EXPIRED(需重新 login 扫码)' if expired else 'ok'}")
    print(f"account: {creds.get('accountId')}  bound {age_days:.1f} 天前")
    tokens = load_tokens()
    print(f"contacts: {', '.join(tokens) or '(无，先给微信ClawBot发条消息)'}")
    for uid in tokens:
        rem = window_remaining(uid)
        if rem is None:
            print(f"window({uid[:8]}…): 未知（无入站记录，先发一条消息）")
        elif rem <= 0:
            print(f"window({uid[:8]}…): 已过期 {-rem / 3600:.1f} 小时，发消息即续期")
        else:
            print(f"window({uid[:8]}…): 剩余 {rem / 3600:.1f} 小时")
    if os.path.isdir(QUEUE_DIR):
        pending = len([f for f in os.listdir(QUEUE_DIR) if f.endswith(".json")])
        if pending:
            print(f"queue: {pending} 条待补发（窗口恢复后自动送出）")


# ── Daemon ────────────────────────────────────────────────────


def cmd_daemon(args: argparse.Namespace) -> None:
    global LOGFILE
    LOGFILE = getattr(args, "logfile", None)
    creds = load_creds()
    base_url, token = creds["baseUrl"], creds["token"]
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    lock_fd = acquire_singleton_lock()
    if lock_fd is None:
        sys.exit("[daemon] 已有实例在运行（daemon.lock 被占用），本次退出")
    if os.path.exists(EXPIRED_FLAG):
        os.unlink(EXPIRED_FLAG)
    buf = ""
    if os.path.isfile(BUF_PATH):
        with open(BUF_PATH, encoding="utf-8") as f:
            buf = f.read().rstrip("\n")  # 游标是服务端 opaque 串，只去掉落盘换行

    stop = {"flag": False}

    def on_term(_sig, _frm):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    try:
        resp = api_post(base_url, "ilink/bot/msg/notifystart", {}, token)
        if resp.get("ret", 0) != 0:
            log(f"[daemon] notifystart ret={resp.get('ret')} errmsg={resp.get('errmsg')}")
    except RuntimeError as e:
        log(f"[daemon] notifystart error: {e}")

    log(f"[daemon] running, account={creds.get('accountId')}")
    tokens = load_tokens()
    failures = 0
    last_heartbeat = time.monotonic()
    last_window_check = 0.0
    nudged: dict[str, int] = {}
    last_receipt: dict[str, float] = {}

    def keepalive_config(uid: str) -> dict:
        """保活配置：keepalive.json 可全局配置，也可 users.<uid> 按用户覆盖。
        字段：enabled / lead_minutes / text（静态文案）/ file（路径，读取其当前
        内容作正文，超 1500 字符截断——适合配合 cron 生成天气、日报、日程）。"""
        cfg = load_json(KEEPALIVE_PATH, {})
        if not isinstance(cfg, dict):
            log("[daemon] keepalive.json 根节点必须是对象，已忽略")
            return {}
        users = cfg.get("users")
        entry = users.get(uid) if isinstance(users, dict) else None
        if isinstance(entry, dict):
            cfg = {**cfg, **entry}
        cfg.setdefault("enabled", True)
        cfg.setdefault("lead_minutes", NUDGE_REMAIN_S // 60)
        try:
            cfg["lead_minutes"] = int(cfg["lead_minutes"])
        except (TypeError, ValueError):
            cfg["lead_minutes"] = NUDGE_REMAIN_S // 60
        return cfg

    def keepalive_nudge(uid: str) -> None:
        """窗口临近过期时推送个性化内容 + 续期提示（协议限制下唯一可行的自动化）。"""
        remaining = window_remaining(uid)
        if remaining is None:
            return
        window_id = int(load_inbound().get(uid, 0))
        cfg = keepalive_config(uid)
        lead_s = cfg["lead_minutes"] * 60
        if not cfg.get("enabled") or not (0 < remaining <= lead_s) \
                or nudged.get(uid) == window_id:
            return
        content = ""
        src = cfg.get("file")
        if src:
            try:
                with open(os.path.expanduser(str(src)), encoding="utf-8") as f:
                    content = f.read().strip()[:1500]
            except OSError as e:
                log(f"[daemon] keepalive file error: {e}")
        elif cfg.get("text"):
            content = str(cfg["text"])
        tail = (f"\n\n——\n[comm-wechat] 推送窗口约 {max(1, int(remaining / 60))} 分钟后过期，"
                f"回复任意内容即可续期 24 小时")
        resp = send_items(creds, uid, [{
            "type": ITEM_TEXT,
            "text_item": {"text": (content + tail) if content else
                          f"[comm-wechat] 推送窗口约 {max(1, int(remaining / 60))} 分钟后过期，"
                          f"回复任意内容即可续期 24 小时（此提醒由 daemon 自动发出）"},
        }])
        nudged[uid] = window_id
        if resp.get("ret", 0) == 0:
            log(f"[daemon] 保活提醒已发送给 {uid}")
        else:
            log(f"[daemon] 保活提醒发送失败 ret={resp.get('ret')}（本窗口内不再重试）")

    def spool_inbound(uid: str, text: str) -> None:
        os.makedirs(INBOX_DIR, mode=0o700, exist_ok=True)
        name = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}.json"
        atomic_write(os.path.join(INBOX_DIR, name),
                     json.dumps({"uid": uid, "text": text, "ts": int(time.time())}))

    def handle_inbound(uid: str, msg: dict) -> None:
        """入站闭环：落盘供 agent 读取；内置指令；通用回执（60s 防抖，省配额）。"""
        text = ""
        for item in msg.get("item_list") or []:
            if item.get("type") == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                break
        if not text:
            text = "[媒体消息]"
        spool_inbound(uid, text)
        reply: str | None = None
        if text.strip() == "/status":
            reply = status_summary(creds)
        elif text.strip() == "/help":
            reply = ("comm-wechat 指令：\n/status 通道状态\n/help 本帮助\n"
                     "其他任意消息：续期推送窗口并转存 inbox/ 供 agent 读取")
        elif time.time() - last_receipt.get(uid, 0) >= 60:
            reply = "✅ 已收到，推送窗口已续期 24 小时"
        if reply:
            resp = send_items(creds, uid, [{"type": ITEM_TEXT, "text_item": {"text": reply}}])
            if resp.get("ret", 0) == 0:
                last_receipt[uid] = time.time()

    def start_bridge():
        """本地 HTTP 桥（默认关）：外部系统 POST /send 推送微信。配置见 bridge.json。"""
        cfg = load_json(BRIDGE_PATH, None)
        if cfg is None:
            atomic_write(BRIDGE_PATH, json.dumps({
                "enabled": False, "port": 8787,
                "token": secrets.token_hex(16)}, indent=2))
            log(f"[bridge] 已生成默认配置 {BRIDGE_PATH}（enabled: false，需要时打开）")
            return None
        if not cfg.get("enabled"):
            return None
        bridge_token = str(cfg.get("token") or "")
        port = int(cfg.get("port", 8787))

        class Handler(http.server.BaseHTTPRequestHandler):
            def _json(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                return bool(bridge_token) and \
                    self.headers.get("Authorization", "") == f"Bearer {bridge_token}"

            def do_GET(self):
                if not self._authorized():
                    return self._json(401, {"error": "unauthorized"})
                self._json(200, {"ok": True})

            def do_POST(self):
                if self.path != "/send":
                    return self._json(404, {"error": "not found"})
                if not self._authorized():
                    return self._json(401, {"error": "unauthorized"})
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 1_000_000))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    to = find_target(creds, payload.get("to"))
                    if not to:
                        return self._json(400, {"error": "target not allowed"})
                    result, resp = do_send(creds, to, text=payload.get("text"),
                                           file=payload.get("file"),
                                           image=payload.get("image"),
                                           as_name=payload.get("as_name"))
                    self._json(200, {"result": result, "ret": resp.get("ret", 0)})
                except FileNotFoundError as e:
                    self._json(400, {"error": f"file not found: {e}"})
                except (ValueError, json.JSONDecodeError) as e:
                    self._json(400, {"error": str(e)})
                except RuntimeError as e:
                    self._json(500, {"error": str(e)})
                except Exception as e:  # 兜底：任何异常都不让 handler 线程死掉
                    self._json(500, {"error": str(e)[:200]})

            def log_message(self, *args):  # noqa: ANN002, ANN003
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log(f"[bridge] listening on 127.0.0.1:{port} (POST /send, Bearer token)")
        return server

    try:
        start_bridge()
    except OSError as e:
        log(f"[daemon] bridge start failed: {e}")

    def session_expired() -> bool:
        log("[daemon] 会话已过期（errcode -14）：重新运行 login 扫码")
        atomic_write(EXPIRED_FLAG, "")
        return False

    while not stop["flag"]:
        if time.monotonic() - last_window_check >= 60:
            last_window_check = time.monotonic()
            for uid in load_tokens():
                try:
                    keepalive_nudge(uid)
                except RuntimeError as e:
                    log(f"[daemon] keepalive nudge error: {e}")

        if time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL_S:
            last_heartbeat = time.monotonic()
            try:
                hb = api_post(base_url, "ilink/bot/getconfig", {}, token)
                if hb.get("errcode") == SESSION_EXPIRED_ERRCODE and not session_expired():
                    break
            except RuntimeError as e:
                log(f"[daemon] heartbeat error: {e}")

        try:
            resp = api_post(base_url, "ilink/bot/getupdates", {"get_updates_buf": buf},
                            token, timeout=POLL_TIMEOUT_S)
        except ApiTimeout:
            continue
        except RuntimeError as e:
            failures += 1
            log(f"[daemon] getupdates error: {e}")
            time.sleep(RETRY_DELAY_S if failures < 5 else 30)
            continue
        failures = 0

        if resp.get("ret", 0) != 0:
            if resp.get("errcode") == SESSION_EXPIRED_ERRCODE and not session_expired():
                break
            log(f"[daemon] getupdates ret={resp.get('ret')} errcode={resp.get('errcode')} "
                f"errmsg={resp.get('errmsg')}")
            time.sleep(2)
            continue

        if resp.get("get_updates_buf"):
            buf = resp["get_updates_buf"]
            atomic_write(BUF_PATH, buf)

        for msg in resp.get("msgs") or []:
            uid = msg.get("from_user_id")
            ct = msg.get("context_token")
            if not uid or uid == creds.get("accountId"):
                continue  # 忽略自身 bot 回显
            is_user = msg.get("message_type") == MSG_TYPE_USER
            if is_user:
                mark_inbound(uid)
            if ct:
                fresh = load_tokens()   # 现读现写，避免陈旧内存快照覆盖磁盘上的手工修正
                if fresh.get(uid) != ct:
                    fresh[uid] = ct
                    save_tokens(fresh)
                    log(f"[daemon] captured context_token for {uid}")
            if is_user:
                if uid != creds.get("userId"):
                    log(f"[daemon] 忽略非绑定用户 {uid} 的消息（本技能只服务绑定本人）")
                    continue
                try:
                    handle_inbound(uid, msg)
                    replay_queue(creds, uid)
                except RuntimeError as e:
                    log(f"[daemon] inbound/replay error: {e}")

    try:
        api_post(base_url, "ilink/bot/msg/notifystop", {}, token, timeout=10)
    except Exception:
        pass
    log("[daemon] stopped")


# ── CLI ───────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description="WeChat ClawBot (iLink) minimal client")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="扫码绑定微信（保存凭据）")
    d = sub.add_parser("daemon", help="常驻轮询：保活并捕获 context_token")
    d.add_argument("--logfile", help="日志同时追加写入该文件（Windows 服务化推荐）")
    sub.add_parser("status", help="查看会话状态与已知联系人")

    s = sub.add_parser("send", help="发送文本/图片/文件到自己的微信")
    s.add_argument("--text", help="文本内容")
    s.add_argument("--file", help="文件路径（以文件消息发送）")
    s.add_argument("--image", help="图片路径（以图片消息发送）")
    s.add_argument("--as-name", help="文件消息显示的文件名（默认用原文件名）")
    s.add_argument("--to", help="目标 userId（默认发给绑定的自己）")

    args = p.parse_args()
    {"login": cmd_login, "daemon": cmd_daemon,
     "send": cmd_send, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
