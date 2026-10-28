"""Read-only fixed-regime result queries; never initialize or execute a model."""
from __future__ import annotations
import csv
from copy import deepcopy
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3

from services.spac import spatial
from services.spac.fixed_compute import SCHEMES, unpack

DATA = spatial.DATA / "fixed"


@lru_cache(maxsize=16)
def snapshot(snapshot_id, name):
    return read(dataset(snapshot_id) / "inputs" / name)


def dataset(snapshot_id):
    if snapshot_id == "local":
        return DATA
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot_id):
        raise ValueError("无效数据版本")
    return DATA / "batches" / snapshot_id


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def manifest():
    pointer = DATA / "current.json"
    sid = read(pointer)["snapshot_id"] if pointer.exists() else "local"
    path = dataset(sid) / "manifest.json"
    if not path.exists():
        raise ValueError("固定制度结果尚未就绪")
    meta = read(path)
    meta["snapshot_id"] = sid
    return meta


def summary(year):
    meta = manifest()
    if year not in meta["years"]:
        raise ValueError("不支持该年份")
    data = read(dataset(meta["snapshot_id"]) / f"summary_{year}.json")
    if data["batch_id"] != meta["batch_id"]:
        raise ValueError("结果批次不一致")
    return dict(ok=True, snapshot_id=meta["snapshot_id"], **data)


def years():
    try:
        meta = manifest()
    except ValueError:
        return dict(ok=True, years=[])
    return dict(ok=True, years=sorted(meta["years"], reverse=True),
        ready_years=[y for y in sorted(meta["years"], reverse=True)
            if read(dataset(meta["snapshot_id"]) / f"summary_{y}.json")["success"] > 0],
        batch_id=meta["batch_id"], snapshot_id=meta["snapshot_id"], schemes=SCHEMES)


def connection(year, point, snapshot_id):
    path = dataset(snapshot_id) / f"{year}_{point}.sqlite"
    if not path.exists():
        return None
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def grid(grid_id, year):
    meta = manifest()
    if year not in meta["years"] or not re.fullmatch(r"wth250_\d+_\d+", grid_id):
        raise ValueError("无效年份或格网")
    mapping = snapshot(meta["snapshot_id"], "grid_to_point.json")
    if grid_id not in mapping:
        raise ValueError("未知格网")
    point = mapping[grid_id]
    cls = snapshot(meta["snapshot_id"], f"crop_{year}_index.json").get(grid_id, {})
    wx = snapshot(meta["snapshot_id"], "weather_index.json")
    results, inputs = {}, {}
    con = connection(year, point, meta["snapshot_id"])
    if con:
        try:
            for crop, raw, blob, expected in con.execute(
                    "SELECT crop,metrics,detail,output_hash FROM results WHERE grid_id=? AND status='ok'", (grid_id,)):
                if hashlib.sha256(blob).hexdigest() != expected:
                    raise ValueError("结果完整性检查失败")
                record = json.loads(raw)
                results[crop] = record
                inputs[crop] = unpack(blob)
        finally:
            con.close()
    return dict(ok=True, batch_id=meta["batch_id"], snapshot_id=meta["snapshot_id"], year=year, grid_id=grid_id,
        classification=cls, point=point, weather=wx["yearly"][str(year)][point],
        results=results, inputs=inputs)


def daily(grid_id, year, crop, scheme):
    meta = manifest()
    if year not in meta["years"] or crop not in SCHEMES or scheme not in SCHEMES[crop]:
        raise ValueError("无效年份、作物或制度")
    mapping = snapshot(meta["snapshot_id"], "grid_to_point.json")
    if grid_id not in mapping:
        raise ValueError("未知格网")
    con = connection(year, mapping[grid_id], meta["snapshot_id"])
    if not con:
        raise ValueError("该格网尚无计算结果")
    try:
        row = con.execute("SELECT detail,output_hash FROM results WHERE grid_id=? AND crop=? AND status='ok'",
                          (grid_id, crop)).fetchone()
        if not row:
            raise ValueError("该格网该作物尚无计算结果")
        if hashlib.sha256(row[0]).hexdigest() != row[1]:
            raise ValueError("结果完整性检查失败")
        table = unpack(row[0], scheme)
    finally:
        con.close()
    return dict(ok=True, batch_id=meta["batch_id"], snapshot_id=meta["snapshot_id"], year=year, grid_id=grid_id,
                crop=crop, scheme=scheme, table=table)


def csv_text(table):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(table["columns"])
    writer.writerows(table["rows"])
    return "\ufeff" + stream.getvalue()


def install(app):
    from fastapi import HTTPException
    from fastapi.responses import PlainTextResponse
    base = app.state.base_path

    def guarded(fn, *args):
        try:
            return fn(*args)
        except (ValueError, KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get(f"{base}/api/distributed/fixed/years")
    def get_years():
        return years()

    @app.get(f"{base}/api/distributed/fixed/summary")
    def get_summary(year: int):
        return guarded(summary, year)

    @app.get(f"{base}/api/distributed/fixed/map")
    def get_map(year: int):
        data = guarded(summary, year)
        geo = deepcopy(snapshot(data["snapshot_id"], "grid.geojson"))
        classes = snapshot(data["snapshot_id"], f"crop_{year}_index.json")
        for feature in geo["features"]:
            feature["properties"].update(classes.get(feature["id"], {}))
        return dict(ok=True, batch_id=data["batch_id"], snapshot_id=data["snapshot_id"], geo=geo, results=data["grids"])

    @app.get(f"{base}/api/distributed/fixed/grid/{{grid_id}}")
    def get_grid(grid_id: str, year: int):
        return guarded(grid, grid_id, year)

    @app.get(f"{base}/api/distributed/fixed/inputs/{{grid_id}}")
    def download_inputs(grid_id: str, year: int):
        data = guarded(grid, grid_id, year)
        return PlainTextResponse(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{year}_{grid_id}_inputs.json"'})

    @app.get(f"{base}/api/distributed/fixed/weather/{{grid_id}}")
    def download_weather(grid_id: str, year: int):
        data = guarded(grid, grid_id, year)
        path = dataset(data["snapshot_id"]) / "inputs/weather" / data["weather"]["file"]
        return PlainTextResponse(path.read_text(encoding="utf-8-sig"), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    @app.get(f"{base}/api/distributed/fixed/daily/{{grid_id}}")
    def get_daily(grid_id: str, year: int, crop: str, scheme: str):
        return guarded(daily, grid_id, year, crop, scheme)

    @app.get(f"{base}/api/distributed/fixed/download/{{grid_id}}")
    def download(grid_id: str, year: int, crop: str, scheme: str):
        data = guarded(daily, grid_id, year, crop, scheme)
        return PlainTextResponse(csv_text(data["table"]), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{year}_{grid_id}_{crop}_{scheme}.csv"'})

    @app.get(f"{base}/api/distributed/fixed/summary.csv")
    def download_summary(year: int):
        data = guarded(summary, year)
        columns = ["统计范围", "crop", "scheme", "label", "n_grids", "area_ha", "weighted_irrigation_mm",
                   "net_water_m3", "production_t", "weighted_yield_t_ha", "可计算面积覆盖率(%)", "种植面积覆盖率(%)", "缺输入面积(ha)", "snapshot_id"]
        for r in data["schemes"]:
            c = data["coverage"][r["crop"]]
            r.update({"统计范围": "全部可计算区域" if data["complete"] else "仅已完成部分",
                "可计算面积覆盖率(%)": 100 * r["area_ha"] / c["eligible_area_ha"] if c["eligible_area_ha"] else None,
                "种植面积覆盖率(%)": 100 * r["area_ha"] / c["expected_area_ha"] if c["expected_area_ha"] else None,
                "缺输入面积(ha)": c["expected_area_ha"] - c["eligible_area_ha"], "snapshot_id": data["snapshot_id"]})
        return PlainTextResponse(csv_text(dict(columns=columns, rows=[
            [r.get(c) for c in columns] for r in data["schemes"]])), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="district_{year}_fixed.csv"'})
