"""Authenticated distributed-model routes; all sources are IDs, never user paths."""
from __future__ import annotations

import json
import shutil
from typing import Literal

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import auth
from services.spac import distributed as d, engine, spatial


class DistributedRunReq(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}
    year: int = Field(ge=1900, le=2100)
    grid_ids: list[str] = Field(min_length=1, max_length=d.MAX_CELLS)
    soil_source: str = "gee_grid"
    crop_source: str
    # Retained for compatibility with saved jobs; no acknowledgement gate.
    allow_representative_soil: bool = True
    allow_cross_year: bool = False
    rice_planting_date: str | None = None
    corn_planting_date: str | None = None
    rice_harvest_date: str | None = None
    corn_harvest_date: str | None = None
    rice_management: dict = Field(default_factory=dict)


class CropJobReq(BaseModel):
    model_config = {"extra": "forbid"}
    job_id: str


def install(app, manager, management_model):
    from services.spac import fixed_results
    fixed_results.install(app)
    base = app.state.base_path

    def owner(request):
        return auth.viewer(request).get("user") or ""

    def bad(exc):
        return HTTPException(400, str(exc))

    @app.get(f"{base}/api/distributed/sources")
    def sources(request: Request):
        return d.sources(auth.viewer(request))

    @app.get(f"{base}/api/distributed/grid")
    def grid(request: Request, soil_source: str = "builtin", crop_source: str = ""):
        try:
            return d.grid_view(owner(request), soil_source, crop_source)
        except ValueError as exc:
            raise bad(exc) from exc

    @app.get(f"{base}/api/distributed/grid/{{grid_id}}")
    def detail(grid_id: str, request: Request, soil_source: str = "builtin", crop_source: str = ""):
        try:
            return d.detail(owner(request), grid_id, soil_source, crop_source)
        except ValueError as exc:
            raise bad(exc) from exc

    @app.get(f"{base}/api/distributed/soil_template")
    def soil_template():
        data = spatial.read_json(spatial.DATA / "soils.json")
        return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="grid_soil_template.json"'})

    @app.post(f"{base}/api/distributed/soil")
    async def soil_import(request: Request, file: UploadFile = File(...)):
        raw = await file.read(10*1024*1024+1)
        if len(raw) > 10*1024*1024:
            raise HTTPException(413, "土壤 JSON 最大10MB")
        try:
            clean = spatial.validate_soils(json.loads(raw.decode("utf-8-sig")), engine.validate_agronomy_settings)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise bad(exc) from exc
        path, meta = d.new_source(owner(request), "soil", name=file.filename or "格网土壤")
        spatial.write_json(path / "soils.json", clean)
        meta.update(status="ready", n_grids=len(clean["cells"]))
        spatial.write_json(path / "meta.json", meta)
        return dict(ok=True, source_id=meta["id"], n_grids=len(clean["cells"]))

    def queue_crop(path, meta):
        job = manager.submit(f"种植结构空间匹配 · {meta['year']}",
            lambda job: d.import_crop(path, meta, lambda msg: setattr(job, "progress", msg)),
            params=dict(type="distributed_import", source_id=meta["id"]), owner=meta.pop("_owner"))
        return dict(ok=True, job_id=job.id, source_id=meta["id"])

    @app.post(f"{base}/api/distributed/classification")
    async def classification(request: Request, year: int = Form(...), file: UploadFile = File(...)):
        if not 1900 <= year <= 2100:
            raise HTTPException(400, "分类年份须在1900至2100之间")
        path, meta = d.new_source(owner(request), "crop", year, file.filename or "种植分类")
        count = 0
        try:
            with (path / "classification.tif").open("wb") as stream:
                while chunk := await file.read(1024*1024):
                    count += len(chunk)
                    if count > 128*1024*1024:
                        raise ValueError("分类影像最大128MB，请裁剪为灌区范围")
                    stream.write(chunk)
            d.crop_spatial.inspect_raster(path / "classification.tif")
        except Exception as exc:
            meta.update(status="failed", error=str(exc)[:400])
            spatial.write_json(path / "meta.json", meta)
            (path / "classification.tif").unlink(missing_ok=True)
            raise bad(exc) from exc
        meta["_owner"] = owner(request)
        return queue_crop(path, meta)

    @app.post(f"{base}/api/distributed/classification/from-job")
    def classification_from_job(req: CropJobReq, request: Request):
        try:
            src, year, job_meta = d.accessible_crop_job(req.job_id, auth.viewer(request))
        except ValueError as exc:
            raise bad(exc) from exc
        if src.stat().st_size > 128*1024*1024:
            raise HTTPException(400, "该成果超过128MB，请下载后裁剪导入")
        path, meta = d.new_source(owner(request), "crop", year, job_meta.get("title", req.job_id))
        shutil.copyfile(src, path / "classification.tif")
        meta.update(_owner=owner(request), crop_job_id=req.job_id)
        return queue_crop(path, meta)

    @app.post(f"{base}/api/distributed/run")
    def run(req: DistributedRunReq, request: Request):
        raise HTTPException(410, "分布式模块已改为固定制度结果查询，请在单区模块进行模拟")
