# -*- coding: utf-8 -*-
"""作物生长模型微服务 —— HTTP 接口层。

独立启动：
    python -m services.spac.main --port 8801 --standalone
挂网关启动（默认）：网关把 /spac/* 剥掉前缀后转发到本服务的 /*，所以内部路由前缀是 ""。

接口
----
    GET  /api/years                     已有气象数据的年份
    GET  /api/weather_template          气象 CSV 模板（7 列）
    POST /api/weather                   上传并导入气象 CSV
    GET  /api/weather/preview?year=     读回某年气象数据前若干行 + 统计
    POST /api/run                       启动所选作物方案模拟，返回 job_id
    GET  /api/env                       依赖与内核自检
    GET  /api/job/{id}?offset=          任务状态 + 增量日志
    GET  /api/jobs                      最近任务
    GET  /files/{id}/{rel}              产物预览 / 下载
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os                                                   # noqa: E402

from fastapi import File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402
from pydantic import BaseModel, Field                        # noqa: E402

from core import auth                                          # noqa: E402
from core import service_app as sa                            # noqa: E402
from core.config import CONFIG                                # noqa: E402
from core.jobs import JobManager                              # noqa: E402
from services.spac import engine                              # noqa: E402

TITLE = "基于 SPAC 的典型作物生长模型"
VERSION = engine.VERSION
BASE_PATH = os.environ.get("SPAC_BASE_PATH", "")              # 内部路由前缀
PUBLIC_PREFIX = os.environ.get("SPAC_PUBLIC_PREFIX", "/spac")  # 浏览器看到的前缀
WEB_DIR = Path(__file__).resolve().parent / "web"

app = sa.make_service(TITLE, VERSION, BASE_PATH, PUBLIC_PREFIX,
                      description=f"土壤-作物-大气连续体耦合模拟 · "
                                  f"水稻 {engine.n_schemes('rice')} 方案；玉米墒情控制六档及对照")
manager = JobManager(CONFIG.work_dir / "spac", "spac")

# 登录 / 验证码 / 全站鉴权（中间件只拦 /api/* 与 /files/*）
auth.install(app, "spac")


# --------------------------------------------------------------------------- #
#  气象数据
# --------------------------------------------------------------------------- #
@app.get(f"{BASE_PATH}/api/years")
def years(request: Request):
    who = auth.viewer(request)
    try:
        ys = engine.available_years(who.get("user") or "")
    except Exception as e:                                    # noqa: BLE001
        return JSONResponse({"ok": False, "years": [],
                             "error": f"{type(e).__name__}: {e}"})
    start_md, end_md = engine.weather_period()
    return {"ok": True, "years": ys,
            "period": f"{start_md.replace('/', '-')} ~ {end_md.replace('/', '-')}",
            "data_dir": str(engine.data_dir(who.get("user") or ""))}


@app.get(f"{BASE_PATH}/api/weather_template")
def weather_template():
    """示例气象 CSV（7 列，含表头与 2 天样例）。"""
    text = (
        "日期,最低气温,最高气温,降雨量,风速,相对湿度,太阳辐射\n"
        "2026-04-01,5.2,18.6,0.0,3.0,65,18.9\n"
        "2026-04-02,6.1,20.3,1.2,2.4,61,20.1\n"
    )
    return PlainTextResponse(
        "\ufeff" + text, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="weather_template.csv"'})


@app.post(f"{BASE_PATH}/api/weather")
async def upload_weather(request: Request, file: UploadFile = File(...),
                         override_era5: bool = Form(False)):
    name = Path(file.filename or "weather.csv").name
    dst = CONFIG.upload_dir / f"{int(time.time() * 1000)}_{name}"
    dst.write_bytes(await file.read())
    try:
        return engine.import_weather(dst, override_era5=override_era5,
                                     owner=auth.viewer(request).get("user") or "")
    except FileExistsError as e:
        return JSONResponse({"ok": False, "need_confirm": True, "error": str(e)},
                            status_code=409)
    except engine.WeatherImportError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:                                    # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"导入失败：{type(e).__name__}: {e}"},
                            status_code=500)


@app.get(f"{BASE_PATH}/api/weather/preview")
def weather_preview(request: Request, year: int, limit: int = 12):
    try:
        return engine.weather_preview(year, limit,
                                      auth.viewer(request).get("user") or "")
    except Exception as e:                                    # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                            status_code=500)


# --------------------------------------------------------------------------- #
#  运行
# --------------------------------------------------------------------------- #
CROP_CN = {"rice": "水稻", "corn": "玉米"}


class SoilLayerReq(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}
    depth: float
    SAT: float
    FC: float
    PWP: float
    Ks: float
    penetrability: float


class AgronomyReq(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}
    planting_date: str | None = None
    soil_type: str
    soil_layers: list[SoilLayerReq] | None = None
    soil_cn: float | None = None


class RunReq(BaseModel):
    model_config = {"extra": "forbid"}
    year: int = Field(..., description="模拟年份")
    crop: str = Field("both", description="rice | corn | both")
    rice_profile: str = "rice_surface"
    corn_profile: str = "corn_canal"
    rice_coefficient: float | None = None
    corn_coefficient: float | None = None
    rice_application_efficiency_pct: float = Field(90.,gt=0.,le=100.)
    corn_application_efficiency_pct: float = Field(90.,gt=0.,le=100.)
    rice_agronomy: AgronomyReq | None = None
    corn_agronomy: AgronomyReq | None = None


@app.post(f"{BASE_PATH}/api/run")
def run(req: RunReq, request: Request):
    crops = {"rice": ["rice"], "corn": ["corn"], "both": ["rice", "corn"]}.get(req.crop)
    if not crops:
        raise HTTPException(400, "crop 只能是 rice / corn / both")
    options={c:{"profile":getattr(req,c+"_profile"),"coefficient":getattr(req,c+"_coefficient"),
                "application_efficiency_pct":getattr(req,c+"_application_efficiency_pct")} for c in crops}
    agronomy = {c: value.model_dump(exclude_none=True) for c in crops
                if (value := getattr(req, c + "_agronomy")) is not None}
    try:
        engine.validate_run_settings(crops,options)
        engine.validate_agronomy_settings(crops, req.year, agronomy)
    except ValueError as e:
        raise HTTPException(400,str(e)) from e

    try:
        years_avail = engine.available_years(auth.viewer(request).get("user") or "")
    except Exception as e:                                    # noqa: BLE001
        raise HTTPException(500, f"内核不可用：{type(e).__name__}: {e}")
    if req.year not in years_avail:
        raise HTTPException(400, f"{req.year} 年无气象数据。可用年份："
                                 f"{'、'.join(map(str, years_avail)) or '无'}")

    who = auth.viewer(request).get("user") or ""
    n_scn = sum(engine.n_schemes(c) for c in crops)
    title = f"{req.year} 年 · {'+'.join(CROP_CN[c] for c in crops)} · {n_scn} 方案"

    def task(job):
        job.progress = f"{req.year} 年模拟中"
        return engine.run(crops, req.year, job.out_dir, owner=who,
                          progress=lambda msg: setattr(job, "progress", msg),
                          irrigation_options=options, agronomy_options=agronomy)

    job = manager.submit(title, task, params=req.model_dump(),
                         owner=who)
    return {"ok": True, "job_id": job.id, "title": title,
            "n_scenarios": n_scn}


# --------------------------------------------------------------------------- #
#  环境自检 / 页面 / 任务路由
# --------------------------------------------------------------------------- #
@app.get(f"{BASE_PATH}/api/capabilities")
def capabilities():
    engine.bootstrap()
    from config.irrigation_params import UTILIZATION_PROFILES
    return {
        "ok": True,
        "service": "spac",
        "crops": [{"value": "rice", "label": "水稻"},
                  {"value": "corn", "label": "玉米"},
                  {"value": "both", "label": "两种都运行"}],
        "n_scenarios_per_crop": {crop: engine.n_schemes(crop) for crop in ("rice", "corn")},
        "corn_moisture_control": {
            "label": "墒情控制",
            "fixed_smt": [[value] * 4 for value in (100, 75, 50, 25)],
            "rainfed": True,
            "optimal": "AquaCrop四阶段阈值搜索；直接采用最高产、同产量最少净灌水的组合",
            "reference_separate": True,
        },
        "figures_per_crop": 6,
        "accepts": ["weather_csv"],
        "utilization_profiles":UTILIZATION_PROFILES,
        "agronomy":engine.agronomy_capabilities(),
        "optimization_rule":"max_yield_then_min_irrigation",
        "rice_optimization_status":"pending_four_schemes",
    }


sa.add_common_routes(app, manager, env_fn=engine.probe_env)
sa.mount_pages(app, WEB_DIR, {"": "spac.html"}, WEB_DIR / "static")


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #
def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="作物生长模型微服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=CONFIG.spac_port)
    ap.add_argument("--standalone", action="store_true",
                    help="独立对外（内部路由前缀也设成 /spac），用于脱离网关调试")
    args = ap.parse_args()

    if args.standalone and not BASE_PATH:
        app.state.base_path = PUBLIC_PREFIX
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
