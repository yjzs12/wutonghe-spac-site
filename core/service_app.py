# -*- coding: utf-8 -*-
"""微服务公共装配：页面渲染、任务路由、产物文件服务。

两个前缀概念必须分清楚（这是代理式微服务最容易踩的坑）：

* **base_path（内部路由前缀）** —— 本进程自己按什么路径对外提供服务。
  挂在网关后面时网关会把 `/spac/xxx` 剥成 `/xxx`，所以 base_path 通常是 ""；
  单独启动某个服务做调试时才需要设成 "/spac"。
* **public_prefix（对外前缀）** —— 浏览器看到的路径前缀，只用于拼页面里的
  资源与接口 URL。挂在网关后面时是 "/spac"，单独跑时是 ""。

页面 HTML 与前端 JS 统一写 `__BASE__` 占位符，由 render_page() 用 public_prefix
替换 —— 同一份前端既能挂在网关下，也能独立跑。
"""
from __future__ import annotations

import html
import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

mimetypes.add_type("image/tiff", ".tif")
mimetypes.add_type("image/tiff", ".tiff")
mimetypes.add_type("application/geo+json", ".geojson")
mimetypes.add_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")


def static_version(static_dir: Path | None) -> str:
    """静态资源版本号 = 目录内最新 mtime。

    页面里 css/js 写成 `?v=__V__`，渲染时替换成本值 —— 只要改过前端资源，
    版本号自动变，浏览器不会再用旧缓存。手工维护版本号迟早会忘。
    """
    if not static_dir or not static_dir.exists():
        return "0"
    newest = 0
    for p in static_dir.rglob("*"):
        try:
            if p.is_file():
                newest = max(newest, int(p.stat().st_mtime))
        except OSError:
            continue
    return str(newest)


def render_page(path: Path, public_prefix: str, static_dir: Path | None = None) -> str:
    html = path.read_text(encoding="utf-8").replace("__BASE__", public_prefix.rstrip("/"))
    return html.replace("__V__", static_version(static_dir))


def make_service(title: str, version: str, base_path: str = "",
                 public_prefix: str = "", description: str = "") -> FastAPI:
    """建一个微服务应用。base_path 为空表示"根路径对外"。"""
    base = base_path.rstrip("/")
    prefix = public_prefix.rstrip("/")
    app = FastAPI(title=title, version=version, description=description,
                  docs_url=f"{base}/docs",
                  openapi_url=f"{base}/openapi.json")
    app.state.base_path = base
    app.state.public_prefix = prefix
    app.state.title = title
    app.state.version = version
    return app


def mount_pages(app: FastAPI, web_dir: Path, pages: dict[str, str],
                static_dir: Path | None = None) -> None:
    """挂载页面与静态资源。

    pages: {路由后缀: HTML 文件名}。"" 是首页；其余是模块内的子页面。
    """
    base = app.state.base_path
    prefix = app.state.public_prefix

    def _serve(file: Path):
        if not file.exists():
            raise HTTPException(404, f"页面不存在: {file.name}")
        # HTML 绝不缓存：页面里带着 ?v=<静态资源 mtime>，一旦 HTML 被缓存成旧版，
        # 里面的旧版本号会继续命中浏览器里的旧 css/js —— 改了前端却看不到效果。
        return HTMLResponse(render_page(file, prefix, static_dir),
                            headers={"Cache-Control": "no-store, must-revalidate"})

    home = web_dir / (pages.get("") or next(iter(pages.values())))
    routes = [f"{base}/"] if base else ["/"]
    if base:
        routes.append(base)
    for r in routes:
        app.get(r, include_in_schema=False)(lambda f=home: _serve(f))

    if static_dir is not None and static_dir.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount(f"{base}/static", StaticFiles(directory=str(static_dir)), name="static")

    for route, name in pages.items():
        if not route:
            continue
        file = web_dir / name
        app.get(f"{base}/{route}", include_in_schema=False)(lambda f=file: _serve(f))


# ==========================================================================
# 表格类产物预览 —— xlsx / csv 在服务端转成 HTML 表格
#
# 为什么需要：浏览器没有内置 Office 渲染器，xlsx 的 MIME
# `application/vnd.openxmlformats-…spreadsheetml.sheet` 会被一律交给下载器，
# 于是「预览」按钮对 Excel 结果文件等于「下载」。这里把表读出来渲染成 HTML，
# 浏览器就能直接看；原始文件仍走 `?download=1` 下载。
#
# 约束：
# * 只读、只展示前 N 行 M 列，不回写、不改动产物；
# * 单元格内容交给 pandas.to_html(escape=True) 转义，不拼裸字符串；
# * 超大文件 / 解析失败都返回**提示页**而不是 500，页面上给下载入口 ——
#   用户至少能拿到文件，不会只看到一串报错。
# ==========================================================================
PREVIEW_TABLE_EXT = {".xlsx", ".xlsm", ".csv", ".tsv"}
PREVIEW_MAX_BYTES = 60 * 1024 * 1024      # 超过就只给下载入口，避免读进内存打爆进程
PREVIEW_MAX_ROWS = 300
PREVIEW_MAX_COLS = 60

_PREVIEW_CSS = """
:root{--brand:#0e7c66;--brand-soft:#e6f5f1;--bg:#f5f7f8;--card:#fff;
--border:#dfe4e8;--text:#16211f;--muted:#5d6b68;--sunken:#eef1f3;--err:#c0392b;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root{--brand:#2bbd99;--brand-soft:#12312a;--bg:#141a19;
--card:#1b2322;--border:#2d3735;--text:#e6eeec;--muted:#9aa8a5;--sunken:#212a29;--err:#f0796a;--warn:#e0b155}}
*{box-sizing:border-box}
body{margin:0;padding:0;background:var(--bg);color:var(--text);
font:14px/1.6 "PingFang SC","Microsoft YaHei UI","Microsoft YaHei","Noto Sans SC",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--border);
padding:14px 22px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{margin:0;font-size:16px;font-weight:600;word-break:break-all}
.meta{color:var(--muted);font-size:12.5px}
.spacer{flex:1}
.btn{display:inline-block;padding:6px 14px;border-radius:8px;background:var(--brand);color:#fff;
text-decoration:none;font-size:13px;white-space:nowrap}
.btn:hover{filter:brightness(1.08)}
main{padding:18px 22px 40px}
.sheet{margin:0 0 26px}
.sheet h2{margin:0 0 8px;font-size:14px;color:var(--brand);display:flex;gap:10px;align-items:baseline}
.sheet h2 .n{color:var(--muted);font-size:12px;font-weight:400}
.tags{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}
.tags a{padding:4px 10px;border:1px solid var(--border);border-radius:999px;background:var(--card);
color:var(--text);text-decoration:none;font-size:12.5px}
.tags a:hover{border-color:var(--brand);color:var(--brand)}
.note{margin:0 0 12px;padding:9px 13px;border-left:3px solid var(--warn);background:var(--card);
color:var(--muted);font-size:12.5px;border-radius:0 8px 8px 0}
.err{padding:14px 16px;border-left:3px solid var(--err);background:var(--card);border-radius:0 8px 8px 0}
.tbl-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:auto;
max-height:72vh}
table.tbl{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:12.5px}
table.tbl thead th{position:sticky;top:0;z-index:2;background:var(--sunken);text-align:left;
padding:8px 11px;border-bottom:1px solid var(--border);font-weight:600;white-space:nowrap}
table.tbl tbody td{padding:6px 11px;border-bottom:1px solid var(--border);white-space:nowrap}
table.tbl tbody tr:nth-child(even){background:color-mix(in srgb,var(--sunken) 45%,transparent)}
table.tbl tbody tr:hover{background:var(--brand-soft)}
.empty{color:var(--muted);padding:16px 4px}
"""


def _preview_doc(title: str, dl_href: str, body: str, sub: str = "") -> str:
    """套上页面骨架。title 与 sub 都要转义 —— 文件名来自用户上传/任务产物。"""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)} · 结果预览</title><style>{_PREVIEW_CSS}</style></head><body>"
        f"<header><h1>{html.escape(title)}</h1>{sub}"
        f"<span class=\"spacer\"></span>"
        f"<a class=\"btn\" href=\"{html.escape(dl_href, quote=True)}\">下载原始文件</a></header>"
        f"<main>{body}</main></body></html>"
    )


def _df_table(df, more_row: bool, more_col: bool) -> str:
    """DataFrame → HTML 表格（含截断提示）。单元格一律转义。"""
    import pandas as pd  # noqa: F401  仅用其 to_html

    if df.shape[0] == 0:
        return '<div class="empty">（空表）</div>'
    df = df.iloc[:, :PREVIEW_MAX_COLS]
    # 列名统一成字符串：openpyxl 可能给出 None / 数字 / 重复列名
    df = df.rename(columns=lambda c: "" if c is None else str(c))
    tbl = df.to_html(index=False, border=0, classes="tbl", na_rep="—", escape=True)
    tips = []
    if more_row:
        tips.append(f"仅显示前 {PREVIEW_MAX_ROWS} 行")
    if more_col:
        tips.append(f"仅显示前 {PREVIEW_MAX_COLS} 列")
    if tips:
        tbl = f'<p class="note">{"，".join(tips)}；完整数据请下载原始文件。</p>' + tbl
    return f'<div class="tbl-wrap">{tbl}</div>'


def _read_csv_any(p: Path, sep: str):
    """CSV 编码兜底：中文 Windows 导出的表格常见 GBK。"""
    import pandas as pd
    last = None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(p, sep=sep, nrows=PREVIEW_MAX_ROWS + 1, encoding=enc)
        except UnicodeDecodeError as e:      # 换下一种编码
            last = e
        except Exception:
            raise
    raise last if last else ValueError("无法解码 CSV")


def table_preview_html(target: Path, dl_href: str) -> str:
    """把 xlsx / csv 产物渲染成一页 HTML 表格。任何异常都转成提示页。"""
    import pandas as pd

    name = target.name
    size = target.stat().st_size
    sub = f'<span class="meta">{size / 1024:.0f} KB</span>'

    if size > PREVIEW_MAX_BYTES:
        return _preview_doc(
            name, dl_href,
            f'<p class="err">文件 {size / 1048576:.1f} MB，超过 {PREVIEW_MAX_BYTES // 1048576} MB '
            '的在线预览上限，请下载后用 Excel 打开。</p>', sub)

    ext = target.suffix.lower()
    blocks: list[tuple[str, object]] = []
    try:
        if ext in (".csv", ".tsv"):
            df = _read_csv_any(target, "\t" if ext == ".tsv" else ",")
            blocks.append(("", df))
        else:
            sheets = pd.read_excel(target, sheet_name=None,
                                   nrows=PREVIEW_MAX_ROWS + 1, engine="openpyxl")
            blocks = [(str(k), v) for k, v in sheets.items()]
    except Exception as e:                    # noqa: BLE001  产物格式千奇百怪，不细分
        return _preview_doc(
            name, dl_href,
            f'<p class="err">无法解析为表格：{html.escape(type(e).__name__)}: '
            f'{html.escape(str(e))}</p>'
            '<p class="note">该文件可能不是标准表格，或已被密码保护。请下载后用 Excel 打开。</p>',
            sub)

    if not blocks:
        return _preview_doc(name, dl_href, '<div class="empty">（无工作表）</div>', sub)

    parts = []
    if len(blocks) > 1:
        nav = "".join(
            f'<a href="#s{i}">{html.escape(n or f"Sheet{i + 1}")}</a>'
            for i, (n, _) in enumerate(blocks))
        parts.append(f'<div class="tags">{nav}</div>')
    for i, (n, df) in enumerate(blocks):
        more_row = len(df) > PREVIEW_MAX_ROWS
        more_col = df.shape[1] > PREVIEW_MAX_COLS
        label = html.escape(n) if n else "数据"
        count = f'<span class="n">≥{PREVIEW_MAX_ROWS} 行 × {df.shape[1]} 列</span>' if more_row \
            else f'<span class="n">{df.shape[0]} 行 × {df.shape[1]} 列</span>'
        parts.append(
            f'<section class="sheet"><h2 id="s{i}">{label}{count}</h2>'
            f'{_df_table(df, more_row, more_col)}</section>')
    return _preview_doc(name, dl_href, "".join(parts), sub)


def add_common_routes(app: FastAPI, manager, env_fn=None) -> None:
    """任务状态 / 日志 / 产物下载 / 历史任务 / 健康检查。

    ⚠ 除 `/api/health` 外，所有路由都必须带 `Request` 并交 viewer 做归属校验：
    任务、日志、产物是按用户名隔离的，漏一个就等于没隔离（拿到 job_id 就能读）。
    `/api/health` 只返回版本号与队列长度，供网关探活，因此不查归属。
    """
    base = app.state.base_path

    def _viewer(request: Request) -> dict:
        from core import auth
        return auth.viewer(request)

    @app.get(f"{base}/api/job/{{job_id}}")
    def job_status(job_id: str, request: Request, offset: int = 0):
        job = manager.get(job_id, _viewer(request))
        if not job:
            raise HTTPException(404, "任务不存在")
        return job.snapshot(offset)

    @app.get(f"{base}/api/jobs")
    def jobs_list(request: Request, limit: int = 20):
        return {"jobs": manager.recent(limit, _viewer(request)),
                "queued": manager.queue_len()}

    @app.get(f"{base}/api/job/{{job_id}}/log.txt")
    def job_log(job_id: str, request: Request):
        job = manager.get(job_id, _viewer(request))
        if not job:
            raise HTTPException(404, "任务不存在")
        return PlainTextResponse(
            job.full_log(), media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="log_{job_id}.txt"'})

    @app.get(f"{base}/files/{{job_id}}/{{relpath:path}}")
    def job_file(job_id: str, relpath: str, request: Request,
                 download: int = 0, preview: int = 0):
        job = manager.get(job_id, _viewer(request))
        if not job:
            raise HTTPException(404, "任务不存在")
        root = Path(job.out_dir).resolve()
        target = (root / relpath).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(403, "非法路径")
        if not target.is_file():
            raise HTTPException(404, f"文件不存在: {relpath}")
        dl_href = f"{app.state.public_prefix}/files/{quote(job_id)}/" \
                  + "/".join(quote(s) for s in Path(relpath).parts) + "?download=1"
        # 表格类产物：浏览器不能内联打开 xlsx，服务端转 HTML 再给（详见 table_preview_html）
        if preview and not download and target.suffix.lower() in PREVIEW_TABLE_EXT:
            return HTMLResponse(
                table_preview_html(target, dl_href),
                headers={"Cache-Control": "private, max-age=600"})
        headers = {"Cache-Control": "private, max-age=600"}
        if download:
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(target.name)
        return FileResponse(str(target), headers=headers)

    @app.get(f"{base}/api/env")
    def env_check():
        if env_fn is None:
            return {"ok": True, "items": []}
        return env_fn()

    @app.get(f"{base}/api/health")
    def health():
        jobs = manager.recent(1000)
        return {
            "ok": True,
            "service": app.state.title,
            "version": app.state.version,
            "base_path": base or "/",
            "public_prefix": app.state.public_prefix or "/",
            "queued": manager.queue_len(),
            "jobs_total": len(jobs),
        }
