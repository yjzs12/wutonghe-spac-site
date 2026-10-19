# -*- coding: utf-8 -*-
"""账号、会话、图形验证码 —— 两个独立网站共用的鉴权内核。

设计取舍（为什么是这个形态）
----------------------------
1. **不引入任何第三方依赖**。两个站点分别部署在 Render 免费层与本平台云容器，
   装包越少越不容易在部署侧翻车。密码用标准库 `hashlib.pbkdf2_hmac`，会话用
   `hmac` 签名的 Cookie，验证码用纯 Python 手写 PNG 编码器 + 自绘点阵字体。
2. **会话无状态**。Cookie 里只放「用户名 + 角色 + 过期时间 + 随机数」，服务端
   用 HMAC-SHA256 签名。进程重启 / 容器重新发布都不会让用户掉线，也不需要把
   会话写盘（Render 免费层没有持久盘）。
3. **账号只有内置账号，没有注册**。账号表在 `账号.json`，密码只存
   PBKDF2-SHA256（20 万轮）的盐+摘要，不存明文。加人/改密码 → 改文件、重启。
4. **验证码状态在内存**。一次性、3 分钟过期、取用即删。服务重启后失效属正常，
   用户刷新一下验证码图片即可。

对外接口
--------
    auth.login(username, password)          -> dict | None
    auth.make_session(user_row)             -> str（签名 token）
    auth.read_session(token)                -> dict | None（已校验签名与有效期）
    auth.new_captcha()                      -> (cid, png_bytes)
    auth.check_captcha(cid, text)           -> (ok: bool, message: str)
    auth.install(app, key)                  -> 给 FastAPI 挂中间件 + /api/auth/* 路由
    auth.current(request, key)              -> dict | None
    auth.current_from_cookie(cookie, key)   -> dict | None（网关代理进程用）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any

# ⚠ 必须在**模块级**拿到 Request：
#   本文件有 `from __future__ import annotations`，注解决议被延迟成字符串，
#   FastAPI 用 typing.get_type_hints() 在**模块全局**里找 `Request`。若只在
#   install() 里局部导入，就找不到 → 把 request 当成必填 query 参数 → 全部 422。
#   try/except 是为了让不装 fastapi 的脚本（工具/生成账号.py）也能导入本模块。
try:
    from fastapi import Request
except Exception:                                          # noqa: BLE001
    Request = None                                         # type: ignore

# --------------------------------------------------------------------------- #
#  常量
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent

ACCOUNTS_FILE = Path(os.environ.get("WTH_ACCOUNTS") or (ROOT / "账号.json"))
SESSION_TTL = int(os.environ.get("WTH_SESSION_TTL") or 12 * 3600)   # 12 小时
PBKDF2_ROUNDS = 200_000
CAPTCHA_TTL = 180                                                   # 3 分钟
CAPTCHA_LEN = 4

# 登录失败限流：同 IP 连续失败 5 次，锁 5 分钟
MAX_FAILS = 5
LOCK_SECONDS = 300

ROLE_ADMIN = "admin"
ROLE_USER = "user"

# 验证码字符集：去掉 0/O、1/I/L 这些肉眼难分的
CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_lock = threading.Lock()
_captchas: dict[str, tuple[str, float]] = {}
_fails: dict[str, list[float]] = {}


# --------------------------------------------------------------------------- #
#  账号文件
# --------------------------------------------------------------------------- #
_accounts_cache: tuple[float, list[dict]] | None = None


def _accounts() -> list[dict]:
    """读账号表（带 mtime 缓存：改完文件不用重启也能生效）。"""
    global _accounts_cache
    try:
        mtime = ACCOUNTS_FILE.stat().st_mtime
    except OSError:
        return []
    if _accounts_cache and _accounts_cache[0] == mtime:
        return _accounts_cache[1]
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = [r for r in (raw.get("accounts") or []) if r.get("user")]
    _accounts_cache = (mtime, rows)
    return rows


def account_exists(user: str) -> bool:
    return any(r["user"] == user for r in _accounts())


def _find(user: str) -> dict | None:
    for r in _accounts():
        if r["user"] == user:
            return r
    return None


def hash_password(password: str, salt: str | None = None,
                  rounds: int = PBKDF2_ROUNDS) -> tuple[str, str, int]:
    """返回 (salt_hex, hash_hex, rounds)。"""
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            bytes.fromhex(salt), rounds)
    return salt, h.hex(), rounds


def login(username: str, password: str) -> dict | None:
    """校验账号密码。成功返回 {'user','name','role'}，失败返回 None。"""
    row = _find((username or "").strip())
    if not row:
        # 仍然做一次哈希，避免用响应时间区分「用户不存在」与「密码错」
        hash_password(password or "", "00" * 16, 20_000)
        return None
    try:
        salt = str(row.get("salt") or "")
        rounds = int(row.get("iter") or PBKDF2_ROUNDS)
        expect = str(row.get("hash") or "")
    except (TypeError, ValueError):
        return None
    if not salt or not expect:
        return None
    got = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                              bytes.fromhex(salt), rounds).hex()
    if not hmac.compare_digest(got, expect):
        return None
    return {"user": row["user"], "name": row.get("name") or row["user"],
            "role": row.get("role") or ROLE_USER}


# --------------------------------------------------------------------------- #
#  会话：HMAC 签名 Cookie（无状态）
# --------------------------------------------------------------------------- #
def _secret() -> bytes:
    """会话签名密钥。

    落在 work/.auth_secret（同一台机器上的网关与子服务共用同一个 work 目录，
    因此签名互认）。文件不可写时退化为按安装路径派生 —— 能跑，但换机器会掉线。
    """
    p = _secret_path()
    try:
        if p.exists():
            v = p.read_text(encoding="utf-8").strip()
            if v:
                return bytes.fromhex(v)
        v = secrets.token_hex(32)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(v, encoding="utf-8")
        return bytes.fromhex(v)
    except (OSError, ValueError):
        return hashlib.sha256(("wth-fallback:" + str(ROOT)).encode()).digest()


def _secret_path() -> Path:
    from core.config import CONFIG
    return Path(CONFIG.work_dir) / ".auth_secret"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def make_session(user_row: dict) -> str:
    payload = {
        "u": user_row["user"],
        "r": user_row.get("role") or ROLE_USER,
        "e": int(time.time()) + SESSION_TTL,
        "n": secrets.token_hex(6),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read_session(token: str) -> dict | None:
    """校验签名 + 过期 + 账号仍然存在；返回 {'user','name','role'}。"""
    try:
        body, sig = (token or "").split(".", 1)
        want = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, want):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("e") or 0) < time.time():
            return None
        row = _find(payload.get("u") or "")
        if row is None:                       # 账号被删 → 旧 cookie 立即失效
            return None
        role = row.get("role") or ROLE_USER
        if role != payload.get("r"):          # 角色被改 → 旧 cookie 立即失效
            return None
        return {"user": row["user"], "name": row.get("name") or row["user"],
                "role": role}
    except (ValueError, KeyError, json.JSONDecodeError, TypeError):
        return None


def cookie_name(key: str) -> str:
    return f"wth_sess_{key or 'site'}"


def cookie_kwargs() -> dict:
    return {"path": "/", "httponly": True, "samesite": "lax", "max_age": SESSION_TTL}


# --------------------------------------------------------------------------- #
#  登录失败限流
# --------------------------------------------------------------------------- #
def locked_for(ip: str) -> int:
    """返回还需等待的秒数（0 = 未锁）。"""
    now = time.time()
    with _lock:
        hits = [t for t in _fails.get(ip, []) if now - t < LOCK_SECONDS]
        _fails[ip] = hits
        if len(hits) < MAX_FAILS:
            return 0
        return max(0, int(LOCK_SECONDS - (now - hits[0])))


def note_fail(ip: str) -> None:
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip: str) -> None:
    with _lock:
        _fails.pop(ip, None)


# --------------------------------------------------------------------------- #
#  图形验证码：纯 Python 手写 PNG + 5x7 点阵字体（无 PIL / 无字体文件）
# --------------------------------------------------------------------------- #
_FONT5x7: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}

_DARK = ((28, 58, 110), (18, 86, 74), (118, 38, 38), (58, 46, 104), (16, 68, 96))


def _png(width: int, height: int, rows: list[bytearray]) -> bytes:
    """最小 PNG 编码（8-bit 真彩色，无滤波）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _draw_captcha(code: str) -> bytes:
    w, h, scale, gap, pad = 128, 48, 4, 7, 11
    rnd = random.Random()
    rows = [bytearray([250, 251, 252] * w) for _ in range(h)]

    def put(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < w and 0 <= y < h:
            i = x * 3
            rows[y][i:i + 3] = bytes(color)

    # 背景噪点
    for _ in range(260):
        put(rnd.randrange(w), rnd.randrange(h), (rnd.randint(226, 244),) * 3)

    # 干扰线（浅灰，不压暗字符）
    for _ in range(3):
        x0, y0 = rnd.randrange(w), rnd.randrange(h)
        x1, y1 = rnd.randrange(w), rnd.randrange(h)
        steps = max(abs(x1 - x0), abs(y1 - y0)) or 1
        for s in range(steps + 1):
            x = round(x0 + (x1 - x0) * s / steps)
            y = round(y0 + (y1 - y0) * s / steps)
            put(x, y, (204, 209, 217))

    # 逐字符绘制
    for idx, ch in enumerate(code.upper()):
        glyph = _FONT5x7.get(ch)
        if not glyph:
            continue
        ox = pad + idx * (5 * scale + gap) + rnd.randint(-1, 1)
        oy = pad + rnd.randint(-3, 3)
        color = _DARK[rnd.randrange(len(_DARK))]
        for ry, line in enumerate(glyph):
            for rx, bit in enumerate(line):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        put(ox + rx * scale + dx, oy + ry * scale + dy, color)
    return _png(w, h, rows)


def new_captcha() -> tuple[str, bytes]:
    """生成一个验证码：返回 (captcha_id, PNG 字节)。"""
    code = "".join(secrets.choice(CAPTCHA_CHARS) for _ in range(CAPTCHA_LEN))
    cid = secrets.token_hex(8)
    now = time.time()
    with _lock:
        for k, (_, exp) in list(_captchas.items()):
            if exp < now:
                _captchas.pop(k, None)
        _captchas[cid] = (code, now + CAPTCHA_TTL)
    return cid, _draw_captcha(code)


def check_captcha(cid: str, text: str) -> tuple[bool, str]:
    """一次性校验；无论对错都立刻作废（防同一张图反复试）。"""
    with _lock:
        item = _captchas.pop((cid or "").strip(), None)
    if not item:
        return False, "验证码已失效，请点图片换一张"
    code, exp = item
    if exp < time.time():
        return False, "验证码已过期，请点图片换一张"
    if (text or "").strip().upper() != code:
        return False, "验证码不正确"
    return True, ""


# --------------------------------------------------------------------------- #
#  FastAPI 装配
# --------------------------------------------------------------------------- #
PUBLIC_PATHS = ("/api/auth/", "/api/health")


def _rel_path(request, base: str) -> str:
    path = request.url.path
    if base and path.startswith(base):
        path = path[len(base):] or "/"
    return path


def current(request, key: str) -> dict | None:
    row = read_session(request.cookies.get(cookie_name(key), ""))
    if row is None:
        row = read_session(request.cookies.get(cookie_name(""), ""))
    return row


def current_from_cookie(cookie_header: str, key: str) -> dict | None:
    """给网关（纯代理进程）用：从原始 Cookie 头里挑出本站的会话。"""
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if name in (cookie_name(key), cookie_name("")):
            row = read_session(value)
            if row:
                return row
    return None


def install(app, key: str) -> None:
    """给微服务挂上：① 登录/登出/验证码/当前用户 ② 全站鉴权中间件。

    只拦 `/api/*` 与 `/files/*`：页面 HTML 本身不含任何业务数据，让它先渲染出来、
    由前端 auth.js 判断登录态并盖遮罩，比服务端 302 跳转少一层路径前缀的坑
    （同一份页面要同时兼容 `/` 与 `/crop/` 两种部署形态）。
    """
    from fastapi.responses import JSONResponse

    base = str(app.state.base_path or "")

    @app.get(f"{base}/api/auth/captcha")
    def auth_captcha():
        cid, png = new_captcha()
        return {"ok": True, "id": cid,
                "image": "data:image/png;base64," + base64.b64encode(png).decode()}

    @app.post(f"{base}/api/auth/login")
    async def auth_login(request: Request):
        try:
            body = await request.json()
        except Exception:                                   # noqa: BLE001
            body = {}
        ip = (request.client.host if request.client else "") or "?"

        wait = locked_for(ip)
        if wait:
            return JSONResponse({"ok": False, "error": f"尝试次数过多，请 {wait} 秒后再试"},
                                status_code=429)

        ok, msg = check_captcha(str(body.get("captcha_id") or ""),
                                str(body.get("captcha") or ""))
        if not ok:
            note_fail(ip)
            return JSONResponse({"ok": False, "error": msg, "captcha": False},
                                status_code=400)

        row = login(str(body.get("username") or ""), str(body.get("password") or ""))
        if row is None:
            note_fail(ip)
            return JSONResponse({"ok": False, "error": "账号或密码不正确"},
                                status_code=401)

        clear_fails(ip)
        resp = JSONResponse({"ok": True, "user": row})
        resp.set_cookie(cookie_name(key), make_session(row), **cookie_kwargs())
        return resp

    @app.post(f"{base}/api/auth/logout")
    def auth_logout():
        resp = JSONResponse({"ok": True})
        # ★ 只发 1 条 Set-Cookie：多删一个从未签发过的历史名，会被代理层合并掉真正的那条删除。
        resp.delete_cookie(cookie_name(key), path="/")
        return resp

    @app.get(f"{base}/api/auth/me")
    def auth_me(request: Request):
        row = current(request, key)
        if row is None:
            return JSONResponse({"ok": False, "need_login": True}, status_code=401)
        return {"ok": True, "user": row}

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        path = _rel_path(request, base)
        guarded = path.startswith("/api/") or path.startswith("/files/")
        if guarded and not any(path.startswith(p) for p in PUBLIC_PATHS):
            row = current(request, key)
            if row is None:
                return JSONResponse(
                    {"ok": False, "error": "未登录或登录已过期，请重新登录",
                     "need_login": True}, status_code=401)
            request.state.user = row
        return await call_next(request)


def require_admin(request) -> bool:
    row = getattr(request.state, "user", None) or {}
    return row.get("role") == ROLE_ADMIN


def current_any(request) -> dict | None:
    """不看路径前缀，从任意本站会话 Cookie 里认出用户。

    为什么需要这条兜底：中间件在「子应用被 Mount 进网关进程」这种挂载形态下
    不会执行（见 gateway/main.py::_auth_guard 的说明），于是 `request.state.user`
    永远是空的 —— 而 owner 过滤全靠它，空 viewer 会让**登录用户也看不到自己的任务**。
    这里直接读 Cookie，签名与有效期照样校验，安全强度不变。
    """
    try:
        items = request.cookies.items()
    except Exception:                                       # noqa: BLE001
        return None
    for name, value in items:
        if name.startswith("wth_sess_"):
            row = read_session(value)
            if row:
                return row
    return None


def viewer(request) -> dict:
    """当前请求者：优先中间件塞的 state，其次直接解 Cookie。"""
    row = getattr(request.state, "user", None) or current_any(request)
    return row or {"user": "", "role": ""}


def can_see(owner: str, viewer_row: dict) -> bool:
    """owner 为空/legacy 的资源只给管理员看（历史数据）。"""
    if not viewer_row:
        return False
    if viewer_row.get("role") == ROLE_ADMIN:
        return True
    return bool(owner) and owner == viewer_row.get("user")
