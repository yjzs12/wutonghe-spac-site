"""Spatial inputs for SPAC. Geometry and soil provenance stay explicit."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform, unary_union

DATA = Path(__file__).parent / "data" / "distributed"
GRID_CRS = "EPSG:32652"
GRID_SIZE = 250
TO_METERS = Transformer.from_crs(4326, GRID_CRS, always_xy=True).transform
TO_WGS84 = Transformer.from_crs(GRID_CRS, 4326, always_xy=True).transform

# Preserve the current model's inherited hydraulic-depth mapping while keeping
# the NEW thin-pan geometry. These source depths are proxies, not observations
# of the 20--28 cm pan. Never reinterpret six nodal depths as six layer widths.
SOURCE_DEPTHS = {"rice": [[0, 10, 30], [60], [60], [100]],
                 "corn": [[0, 10, 30], [60], [100]]}
MODEL_DEPTHS = {"rice": [.20, .08, .32, .40], "corn": [.30, .30, .40]}


def layer_mapping(crop, layers):
    if [round(layer["depth"], 6) for layer in layers] != MODEL_DEPTHS[crop]:
        raise ValueError("当前内核土层格式已变化，请更新格网转换规则后重新准备数据")
    top = 0.
    result = []
    for layer, depths in zip(layers, SOURCE_DEPTHS[crop]):
        bottom = round(top+layer["depth"], 6)
        result.append(dict(top_m=top, bottom_m=bottom, thickness_m=layer["depth"],
                           source_depths_cm=depths, aggregation="arithmetic_mean",
                           note="继承当前模型参数映射；原始节点深度不等同于新土层观测"))
        top = bottom
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False,
                               separators=(",", ":")), encoding="utf-8")


def make_grid(boundary, size=GRID_SIZE):
    """Clip metric cells to polygonal irrigation boundary; retain exact areas."""
    polygons = [shape(f["geometry"]) for f in boundary["features"]
                if f.get("geometry", {}).get("type") in ("Polygon", "MultiPolygon")]
    region = transform(TO_METERS, unary_union(polygons))
    if not region.is_valid:
        region = region.buffer(0)
    xmin, ymin, xmax, ymax = region.bounds
    features = []
    for y in range(math.floor(ymin / size), math.ceil(ymax / size)):
        for x in range(math.floor(xmin / size), math.ceil(xmax / size)):
            cell = region.intersection(box(x * size, y * size, (x+1)*size, (y+1)*size))
            if cell.is_empty or cell.area < .01:
                continue
            point = transform(TO_WGS84, cell.representative_point())
            features.append(dict(type="Feature", id=f"wth{size}_{x}_{y}",
                geometry=mapping(transform(TO_WGS84, cell)),
                properties=dict(grid_id=f"wth{size}_{x}_{y}", area_ha=cell.area/10000,
                                longitude=point.x, latitude=point.y)))
    return dict(type="FeatureCollection", grid_version=f"wth-utm52n-{size}-v1",
                grid_size_m=size, area_crs=GRID_CRS, features=features)


@lru_cache(maxsize=1)
def grid():
    return read_json(DATA / "grid.geojson")


@lru_cache(maxsize=1)
def grid_index():
    return {f["id"]: f for f in grid()["features"]}


@lru_cache(maxsize=1)
def projected_cells():
    return {key: transform(TO_METERS, shape(f["geometry"])) for key, f in grid_index().items()}


def soil_for(soils, grid_id):
    return deepcopy(soils.get("cells", {}).get(grid_id, dict(status="missing",
        source="该格无已导入土壤数据", crops={})))


def validate_soils(data, validate_agronomy):
    """Uploaded profiles must specify units, provenance and known cell IDs."""
    if data.get("grid_version") != grid()["grid_version"]:
        raise ValueError("土壤文件的 grid_version 与当前格网不一致，请下载模板")
    cells = data.get("cells")
    if not isinstance(cells, dict) or not cells or len(cells) > len(grid_index()):
        raise ValueError("土壤文件须含非空 cells 对象")
    clean = dict(grid_version=grid()["grid_version"], cells={})
    for key, item in cells.items():
        if key not in grid_index():
            raise ValueError(f"未知格网：{key}")
        if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not item["source"].strip():
            raise ValueError(f"{key} 缺少土壤来源 source")
        if item.get("status") not in ("grid_profile", "grid_sample", "point_only"):
            raise ValueError("土壤 status 须为 grid_profile / grid_sample / point_only")
        crops = item.get("crops", {})
        if not crops or set(crops) - {"rice", "corn"}:
            raise ValueError("土壤 crops 仅支持 rice 和 corn")
        for crop, profile in crops.items():
            if not isinstance(profile, dict) or not profile.get("soil_layers"):
                raise ValueError("必须提供完整 soil_layers，不能用预设替代缺测土壤")
            validate_agronomy([crop], 2024, {crop: profile})
        clean["cells"][key] = deepcopy(item)
    # Round trip rejects NaN/Infinity, including arbitrary raw-property metadata.
    json.dumps(clean, allow_nan=False)
    return clean


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
