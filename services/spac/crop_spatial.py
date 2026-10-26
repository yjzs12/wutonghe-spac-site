"""Exact crop-area intersections, including boundary pixels and CRS transforms."""
from __future__ import annotations

from collections import defaultdict
import math

import numpy as np
import rasterio
from rasterio.features import shapes
from rasterio.windows import from_bounds
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

from services.spac.spatial import GRID_CRS

# Confirmed from the extraction model's rf_model_2024_meta.json.
DEFAULT_CLASS_MAP = {0: "other", 1: "rice", 2: "corn"}


def inspect_raster(path):
    with rasterio.open(path) as src:
        if src.driver != "GTiff" or src.count != 1 or not src.crs:
            raise ValueError("分类结果须为带 CRS 的单波段 GeoTIFF")
        if src.width * src.height > 100_000_000:
            raise ValueError("分类影像超过一亿像元，请先裁剪到灌区")
        if src.transform.b or src.transform.d:
            raise ValueError("暂不支持旋转影像，请重投影为北向上的分类栅格")
        return dict(crs=src.crs.to_string(), width=src.width, height=src.height,
                    nodata=src.nodata)


def crop_areas(src, cell, class_map=None, transforms=None):
    """cell is UTM52N geometry. Unknown class codes remain other, never a crop."""
    codes = DEFAULT_CLASS_MAP if class_map is None else class_map
    to_src, to_metric = transforms or (
        Transformer.from_crs(GRID_CRS, src.crs, always_xy=True).transform,
        Transformer.from_crs(src.crs, GRID_CRS, always_xy=True).transform)
    footprint = transform(to_src, cell)
    raw = from_bounds(*footprint.bounds, transform=src.transform)
    left, top = max(0, math.floor(raw.col_off)), max(0, math.floor(raw.row_off))
    right = min(src.width, math.ceil(raw.col_off+raw.width))
    bottom = min(src.height, math.ceil(raw.row_off+raw.height))
    area = defaultdict(float, rice=0., corn=0., other=0.)
    if right > left and bottom > top:
        window = rasterio.windows.Window(left, top, right-left, bottom-top)
        data = src.read(1, window=window, masked=True)
        values = np.asarray(data.data)
        mask = ~np.ma.getmaskarray(data) & np.isfinite(values)
        if mask.any() and not np.all(values[mask] == np.floor(values[mask])):
            raise ValueError("分类影像包含小数类别，请上传离散分类编码")
        for geometry, value in shapes(values.astype("float32"), mask=mask,
                                      transform=src.window_transform(window)):
            polygon = transform(to_metric, shape(geometry))
            clipped = cell.intersection(polygon)
            key = codes.get(int(value), "other")
            area[key] += clipped.area/10000
    total = cell.area/10000
    classified = sum(area.values())
    # Reprojection curved-edge roundoff may produce tiny excesses only.
    if classified > total and classified - total > max(1e-5, total*1e-5):
        raise ValueError("分类相交面积超过格网面积，请检查 CRS")
    if classified > total:
        factor = total/classified
        area = {key: val*factor for key, val in area.items()}
        classified = total
    return dict(areas_ha=dict(area), unclassified_ha=max(0., total-classified),
                coverage_fraction=classified/total if total else 0.,
                dominant_crop=max(area, key=area.get) if classified else "missing")


def overlay(path, cells, progress=None, class_map=None):
    inspect_raster(path)
    result = {}
    with rasterio.open(path) as src:
        transforms = (Transformer.from_crs(GRID_CRS, src.crs, always_xy=True).transform,
                      Transformer.from_crs(src.crs, GRID_CRS, always_xy=True).transform)
        for i, (key, cell) in enumerate(cells.items()):
            result[key] = crop_areas(src, cell, class_map, transforms)
            if progress and i % 100 == 0:
                progress(f"种植结构空间匹配 {i+1}/{len(cells)} 格")
    return result
