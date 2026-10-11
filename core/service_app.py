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

import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
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


def add_common_routes(app: FastAPI, manager, env_fn=None) -> None:
    """任务状态 / 日志 / 产物下载 / 历史任务 / 健康检查。"""
    base = app.state.base_path

    @app.get(f"{base}/api/job/{{job_id}}")
    def job_status(job_id: str, offset: int = 0):
        job = manager.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return job.snapshot(offset)

    @app.get(f"{base}/api/jobs")
    def jobs_list(limit: int = 20):
        return {"jobs": manager.recent(limit), "queued": manager.queue_len()}

    @app.get(f"{base}/api/job/{{job_id}}/log.txt")
    def job_log(job_id: str):
        job = manager.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return PlainTextResponse(
            job.full_log(), media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="log_{job_id}.txt"'})

    @app.get(f"{base}/files/{{job_id}}/{{relpath:path}}")
    def job_file(job_id: str, relpath: str, download: int = 0):
        job = manager.get(job_id)
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
        headers = {"Cache-Control": "public, max-age=3600"}
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
