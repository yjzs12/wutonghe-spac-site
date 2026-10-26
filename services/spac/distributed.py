"""Distributed SPAC: immutable inputs, per-crop area accounting and partial reports."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import uuid

import pandas as pd

from core import auth
from core.config import CONFIG
from core.tables import df_records, file_entries
from services.spac import crop_spatial, engine, spatial

MAX_CELLS = 64
CN = {"rice": "水稻", "corn": "玉米"}


def private_dir(owner):
    key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]
    root = CONFIG.work_dir / "distributed_inputs" / key
    root.mkdir(parents=True, exist_ok=True)
    return root


def source_path(owner, source_id, kind):
    if not re.fullmatch(r"[a-f0-9]{16}", source_id):
        raise ValueError("无效数据源编号")
    path = private_dir(owner) / source_id
    if not (path / "meta.json").is_file():
        raise ValueError("数据源不存在或无权访问")
    meta = spatial.read_json(path / "meta.json")
    if meta.get("kind") != kind or meta.get("status") != "ready":
        raise ValueError("数据源类型不匹配或尚未处理完成")
    return path, meta


def new_source(owner, kind, year=None, name=""):
    source_id = uuid.uuid4().hex[:16]
    path = private_dir(owner) / source_id
    path.mkdir()
    meta = dict(id=source_id, kind=kind, year=year, name=name[:160], status="processing")
    spatial.write_json(path / "meta.json", meta)
    return path, meta


def load_soils(owner, source_id="builtin"):
    if source_id == "builtin":
        return spatial.read_json(spatial.DATA / "soils.json")
    if source_id == "gee_grid" and (spatial.DATA / "gee_soils.json").exists():
        return spatial.read_json(spatial.DATA / "gee_soils.json")
    path, _ = source_path(owner, source_id, "soil")
    return spatial.read_json(path / "soils.json")


def load_crop(owner, source_id):
    if not source_id:
        return {}, None
    if source_id == "builtin2025" and (spatial.DATA / "crop_2025_index.json").exists():
        return spatial.read_json(spatial.DATA / "crop_2025_index.json"), spatial.read_json(spatial.DATA / "crop_2025_meta.json")
    path, meta = source_path(owner, source_id, "crop")
    return spatial.read_json(path / "index.json"), meta


def accessible_crop_job(job_id, viewer):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise ValueError("无效种植结构任务编号")
    path = CONFIG.work_dir / "crop" / job_id
    meta_file = path / "job.json"
    if not meta_file.is_file():
        raise ValueError("种植结构任务不存在或无权访问")
    meta = spatial.read_json(meta_file)
    if not auth.can_see(meta.get("owner") or "__legacy__", viewer) or meta.get("status") != "done":
        raise ValueError("种植结构任务不存在或无权访问")
    files = sorted(path.glob("crop_classification_*.tif"))
    if not files:
        raise ValueError("该任务没有分类 GeoTIFF；请从提取网站下载后导入")
    year = meta.get("params", {}).get("year") or meta.get("result", {}).get("year")
    if not year:
        match = re.search(r"(20\d{2})", files[0].name)
        year = int(match.group(1)) if match else None
    if not year:
        raise ValueError("无法确定分类年份，请下载该成果并按实际年份导入")
    return files[0], int(year), meta


def sources(viewer):
    owner = viewer.get("user") or ""
    datasets = []
    if (spatial.DATA / "crop_2025_index.json").exists():
        datasets.append(spatial.read_json(spatial.DATA / "crop_2025_meta.json"))
    for path in private_dir(owner).glob("*/meta.json"):
        item = spatial.read_json(path)
        # Do not return absolute paths or another user's results.
        datasets.append({k: item.get(k) for k in ("id", "kind", "year", "name", "status", "error")})
    jobs = []
    for path in sorted((CONFIG.work_dir / "crop").glob("*/job.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
        meta = spatial.read_json(path)
        if auth.can_see(meta.get("owner") or "__legacy__", viewer) and meta.get("status") == "done":
            jobs.append(dict(id=path.parent.name, title=meta.get("title", path.parent.name)))
    soil_sources = [dict(id="builtin", name="GEE 已下载代表点（3点，非全区覆盖）")]
    if (spatial.DATA / "gee_soils.json").exists():
        soil_sources.insert(0, dict(id="gee_grid", name="GEE 格网采样（250m，含模型假设参数）"))
    soil_sources.extend(dict(id=x["id"], name=x["name"]) for x in datasets
                        if x["kind"] == "soil" and x["status"] == "ready")
    return dict(ok=True, datasets=datasets, soil_sources=soil_sources, crop_jobs=jobs,
                years=engine.available_years(owner), max_cells=MAX_CELLS,
                grid_version=spatial.grid()["grid_version"], class_map=crop_spatial.DEFAULT_CLASS_MAP,
                weather_basis="所选年份的同一套逐日气象用于所有选中格网；尚未进行气象空间插值")


def import_crop(path, meta, progress=None, class_map=None):
    try:
        meta["raster"] = crop_spatial.inspect_raster(path / "classification.tif")
        meta["sha256"] = spatial.digest_file(path / "classification.tif")
        meta["class_map"] = class_map or crop_spatial.DEFAULT_CLASS_MAP
        index = crop_spatial.overlay(path / "classification.tif", spatial.projected_cells(), progress, class_map)
        spatial.write_json(path / "index.json", index)
        meta["status"] = "ready"
        spatial.write_json(path / "meta.json", meta)
        return dict(type="distributed_import", source_id=meta["id"], year=meta["year"],
                    n_grids=len(index), message="分类结果已按格网实际相交面积匹配")
    except Exception as exc:
        meta.update(status="failed", error=str(exc)[:400])
        spatial.write_json(path / "meta.json", meta)
        raise


def grid_view(owner, soil_source="builtin", crop_source=""):
    soils = load_soils(owner, soil_source)
    classes, _ = load_crop(owner, crop_source)
    result = deepcopy(spatial.grid())
    counts = {}
    for feature in result["features"]:
        key = feature["id"]
        soil = spatial.soil_for(soils, key)
        status = soil["status"]
        feature["properties"].update(soil_status=status,
            soil_crops=list(soil.get("crops", {})), **classes.get(key, {}))
        counts[status] = counts.get(status, 0)+1
    result["soil_counts"] = counts
    return result


def detail(owner, key, soil_source="builtin", crop_source=""):
    if key not in spatial.grid_index():
        raise ValueError("未知格网")
    soils = load_soils(owner, soil_source)
    classes, meta = load_crop(owner, crop_source)
    return dict(ok=True, feature=spatial.grid_index()[key], soil=spatial.soil_for(soils, key),
                classification=classes.get(key), classification_year=meta["year"] if meta else None)


def prepare(req, owner):
    """Freeze all model inputs before queueing; later uploads cannot mutate a job."""
    ids = req["grid_ids"]
    if not ids or len(ids) > MAX_CELLS or len(set(ids)) != len(ids):
        raise ValueError(f"每次请选择1至{MAX_CELLS}个不重复格网")
    if any(key not in spatial.grid_index() for key in ids):
        raise ValueError("请求包含未知格网")
    soils = load_soils(owner, req["soil_source"])
    classes, crop_meta = load_crop(owner, req["crop_source"])
    if not crop_meta:
        raise ValueError("请先导入或选择种植结构提取结果")
    if crop_meta["year"] != req["year"] and not req["allow_cross_year"]:
        raise ValueError("分类年份与气象模拟年份不一致；请切换年份或明确允许跨年情景")
    weather_path = engine.weather_csv_path(req["year"], owner)
    if weather_path is None:
        raise ValueError("该年无气象数据，请先导入")
    weather = weather_path.read_bytes()
    # Match the management defaults resolved by engine._run_configured exactly.
    management = dict(ci_natural_drydown=True, ci_split_refill=True,
        ci_ponding_limit_days=None, ci_ponding_limit_depth_mm=None,
        ci_duration_drain_target_mm=None, awd_natural_drydown=True,
        awd_ponding_limit_days=None, awd_ponding_limit_depth_mm=None,
        awd_duration_drain_target_mm=None, pre_drying_days=3,
        drying_handoff=True, drying_min_days=5, recovery_budget_days=2) | req["rice_management"]
    settings = engine.validate_run_settings(["rice", "corn"], None, management)
    items, skipped = [], []
    for key in ids:
        soil = spatial.soil_for(soils, key)
        cls = classes.get(key, {})
        for crop in ("rice", "corn"):
            area = cls.get("areas_ha", {}).get(crop, 0.)
            if area <= 1e-8:
                continue
            reason = None
            if soil["status"] == "missing" or crop not in soil.get("crops", {}):
                reason = "缺少该作物的完整格网土壤剖面"
            elif soil["status"] in ("point_only", "grid_sample") and not req["allow_representative_soil"]:
                reason = "须明确同意采用代表采样和模型假设土壤参数"
            if reason:
                skipped.append(dict(grid_id=key, crop=crop, crop_area_ha=area, status="skipped", reason=reason))
                continue
            agronomy = deepcopy(soil["crops"][crop])
            if req.get(crop+"_planting_date"):
                agronomy["planting_date"] = req[crop+"_planting_date"]
            if req.get(crop+"_harvest_date"):
                agronomy["harvest_date"] = req[crop+"_harvest_date"]
            engine.validate_agronomy_settings([crop], req["year"], {crop: agronomy})
            items.append(dict(grid_id=key, crop=crop, crop_area_ha=area,
                soil_source=soil["source"], soil_status=soil["status"], agronomy=agronomy))
        if not cls or cls.get("coverage_fraction", 0) == 0:
            skipped.append(dict(grid_id=key, crop="unknown", crop_area_ha=spatial.grid_index()[key]["properties"]["area_ha"],
                                status="skipped", reason="没有有效种植分类"))
    if not items:
        reasons = sorted({row["reason"] for row in skipped})
        raise ValueError("没有可计算的格网作物：" + "；".join(reasons or ["所选格网没有水稻或玉米面积"]))
    return dict(items=items, skipped=skipped, classes={key: classes.get(key, {}) for key in ids},
                weather=weather, weather_sha256=hashlib.sha256(weather).hexdigest(),
                weather_name=weather_path.name, crop_meta=deepcopy(crop_meta),
                soils=soils, irrigation=settings, rice_management=management)


def simulate(crop, year, agronomy, out_dir, management):
    """Use the same scenario runner and optimum rule as the existing website."""
    engine.bootstrap()
    rs, wc = engine._ensure()
    from config.simulation_settings import use_settings
    validated = engine.validate_agronomy_settings([crop], year, {crop: agronomy})
    previous_out = rs.OUTPUT_DIR
    rs.OUTPUT_DIR = str(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with use_settings(validated):
            results = rs.run_crop_optimization(crop, year, 90., rice_management=management)
            if len(results) != engine.n_schemes(crop):
                raise ValueError("部分灌溉方案失败，不能从不完整候选中选优")
            table = rs.build_optimization_table(results, year)
            optimal = rs.find_optimal(table)
            label = optimal.iloc[0]["最优方案"]
            chosen = next(r for r in results if r.label == label)
            metric = dict(scheme=label,
                irrigation_mm=float(chosen.summary["累计灌溉量(mm)"]),
                yield_t_ha=float(chosen.summary["最终籽粒产量(t/ha)"]),
                et_mm=float(chosen.summary["全季蒸腾蒸发量(mm)"]))
            if any(not math.isfinite(v) or v < 0 for v in metric.values() if isinstance(v, float)):
                raise ValueError("模拟产生无效水量或产量")
            return metric, results, chosen, df_records(table), validated
    finally:
        rs.OUTPUT_DIR = previous_out


def aggregate(rows):
    summaries = []
    for crop in ("rice", "corn", "all"):
        selected = [r for r in rows if r["status"] == "done" and (crop == "all" or r["crop"] == crop)]
        area = sum(r["crop_area_ha"] for r in selected)
        water = sum(r["net_water_m3"] for r in selected)
        # Rice and corn have different grain moisture bases: never add their tonnages.
        summaries.append(dict(crop=crop, simulated_area_ha=area, net_water_m3=water,
            source_water_m3=sum(r["source_water_m3"] for r in selected),
            production_t=sum(r["production_t"] for r in selected) if crop != "all" else None,
            weighted_irrigation_mm=water/(10*area) if area else None,
            weighted_yield_t_ha=sum(r["production_t"] for r in selected)/area if area and crop != "all" else None))
    return summaries


def run(req, prepared, out_dir, progress=None):
    """Runs on the SAME SPAC JobManager worker as single-region simulations."""
    engine.bootstrap()
    rs, wc = engine._ensure()
    out_dir = Path(out_dir)
    weather_dir = out_dir / "weather"
    weather_dir.mkdir(parents=True, exist_ok=True)
    weather_path = weather_dir / f"weather_{req['year']}.csv"
    weather_path.write_bytes(prepared["weather"])
    _, checked_year, days, expected, _ = engine.validate_weather_csv(weather_path)
    if checked_year != req["year"] or days != expected:
        raise ValueError("气象数据没有完整覆盖模拟期，请补齐逐日数据")
    old_data = wc.DATA_DIR
    wc.DATA_DIR = weather_dir
    rows, groups, time_series = list(prepared["skipped"]), {}, {}
    files = []
    snapshot = dict(req, weather_sha256=prepared["weather_sha256"],
                    weather_name=prepared["weather_name"], classification=prepared["crop_meta"],
                    grid_version=spatial.grid()["grid_version"], inputs=prepared["items"],
                    weather_basis="所有格网共用一套逐日气象；未做空间插值",
                    optimization_rule="max_yield_then_min_irrigation", max_water_budget=None,
                    resolved_rice_management=prepared["rice_management"])
    spatial.write_json(out_dir / "run_inputs.json", snapshot)
    try:
        for i, item in enumerate(prepared["items"]):
            key, crop = item["grid_id"], item["crop"]
            if progress:
                progress(f"{i+1}/{len(prepared['items'])} · {key} · {CN[crop]}")
            row = {k: item[k] for k in ("grid_id", "crop", "crop_area_ha", "soil_source", "soil_status")}
            group_key = hashlib.sha256(json.dumps([crop, item["agronomy"]], sort_keys=True).encode()).hexdigest()[:20]
            try:
                if group_key not in groups:
                    folder = out_dir / "simulations" / group_key
                    metric, results, chosen, table, validated = simulate(crop, req["year"], item["agronomy"], folder, prepared["rice_management"])
                    groups[group_key] = metric
                    chosen.save_csv(str(folder / "optimal_daily.csv"))
                    spatial.write_json(folder / "scenarios.json", table)
                    spatial.write_json(folder / "settings.json", validated)
                    files.extend([f"simulations/{group_key}/optimal_daily.csv", f"simulations/{group_key}/scenarios.json", f"simulations/{group_key}/settings.json"])
                    chart_columns = [c for c in ("模拟日期", "冠层覆盖度(%)", "干物质量(t/ha)", "籽粒产量(t/ha)", "每日总净灌溉量(mm)") if c in chosen.daily]
                    daily = chosen.daily[chart_columns].copy()
                    if "模拟日期" in daily:
                        daily["模拟日期"] = daily["模拟日期"].astype(str)
                    time_series[group_key] = df_records(daily)
                metric = groups[group_key]
                area = item["crop_area_ha"]
                coef = prepared["irrigation"][crop]["coefficient"]
                row.update(metric, status="done", simulation_id=group_key,
                    net_water_m3=10*area*metric["irrigation_mm"],
                    source_water_m3=10*area*metric["irrigation_mm"]/coef,
                    production_t=area*metric["yield_t_ha"])
            except Exception as exc:
                row.update(status="failed", reason=str(exc)[:500])
            rows.append(row)
    finally:
        wc.DATA_DIR = old_data
    summary = aggregate(rows)
    selected_area = sum(spatial.grid_index()[key]["properties"]["area_ha"] for key in req["grid_ids"])
    supported_area = sum(sum(prepared["classes"].get(key, {}).get("areas_ha", {}).get(c, 0.) for c in ("rice", "corn")) for key in req["grid_ids"])
    coverage = dict(selected_grid_count=len(req["grid_ids"]), selected_area_ha=selected_area,
        district_area_ha=sum(f["properties"]["area_ha"] for f in spatial.grid()["features"]),
        supported_crop_area_ha=supported_area,
        simulated_crop_area_ha=summary[-1]["simulated_area_ha"],
        unmodeled_supported_area_ha=max(0., supported_area-summary[-1]["simulated_area_ha"]),
        other_area_ha=sum(c.get("areas_ha", {}).get("other", 0.) for c in prepared["classes"].values()),
        unclassified_area_ha=sum(c.get("unclassified_ha", spatial.grid_index()[key]["properties"]["area_ha"]) for key, c in prepared["classes"].items()),
        successful=sum(r["status"] == "done" for r in rows), failed=sum(r["status"] == "failed" for r in rows),
        skipped=sum(r["status"] == "skipped" for r in rows),
        basis="仅汇总所选格网中成功模拟的作物面积；其他、未分类和未模拟部分不计为零需水")
    results_geo = deepcopy(spatial.grid())
    results_geo["features"] = [f for f in results_geo["features"] if f["id"] in req["grid_ids"]]
    inputs_geo = deepcopy(results_geo)
    for feature in results_geo["features"]:
        feature["properties"]["results"] = [r for r in rows if r["grid_id"] == feature["id"]]
    for feature in inputs_geo["features"]:
        key = feature["id"]
        feature["properties"].update(soil=spatial.soil_for(prepared["soils"], key),
                                      classification=prepared["classes"].get(key))
    spatial.write_json(out_dir / "grid_results.geojson", results_geo)
    spatial.write_json(out_dir / "grid_inputs.geojson", inputs_geo)
    spatial.write_json(out_dir / "coverage_report.json", coverage)
    pd.DataFrame(rows).to_csv(out_dir / "grid_results.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(summary).to_csv(out_dir / "district_summary.csv", index=False, encoding="utf-8-sig")
    files += ["grid_results.geojson", "grid_inputs.geojson", "grid_results.csv", "district_summary.csv", "coverage_report.json", "run_inputs.json"]
    if not coverage["successful"]:
        # Failures remain inspectable in the task folder, never reported as success.
        raise ValueError("所有格网作物模拟失败：" + next((r.get("reason", "") for r in rows if r["status"] == "failed"), "无成功结果"))
    return dict(type="distributed", year=req["year"], rows=rows, summary=summary,
                coverage=coverage, time_series=time_series, files=file_entries(out_dir, files),
                generated_at=datetime.now(timezone.utc).isoformat(), input_settings=snapshot)
