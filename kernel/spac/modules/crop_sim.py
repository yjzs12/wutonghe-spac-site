# -*- coding: utf-8 -*-
"""
作物模拟模块
============
对应大纲 2.4 作物模拟模块，以播种后天数(DAP)划分生育阶段，围绕"水分-产量响应"
模拟冠层覆盖度、蒸腾蒸发、地上干物质、籽粒产量与水分胁迫：

  (1) 冠层覆盖度 CC：CC0/CCx/CGC/CDC 驱动的 Gompertz 型增长与退化
  (2) 蒸腾 Tr = Ks_stress * Kcb * CC * ET0 ；土壤蒸发 E = Kr * Ke * (1-CC) * ET0
  (3) 地上干物质 B = WP * ∑(Tr/ET0) （水分生产力）
  (4) 籽粒产量 Y = B * f_HI （收获指数调整）
  (5) 水分胁迫：根区耗水比例触发 KSexp/KSCCx/KSCC/KSsto 响应

本模块封装 SPAC 内核 的 Crop 实体构建，对接 config.crop_params 参数。
支持作物：水稻 / 玉米
"""

from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.engine_core import Crop
from config.crop_params import get_crop_preset, get_crop_params, RICE_PARAMS, CORN_PARAMS
from config.wutonghe_config import (
    RICE_PLANTING_DATE, RICE_HARVEST_DATE,
    CORN_PLANTING_DATE, CORN_HARVEST_DATE,
)


def build_crop(crop: str) -> Crop:
    """构建 SPAC 内核 Crop 对象（含梧桐河灌区参数率定）。

    Parameters
    ----------
    crop : 'rice' / 'corn'/'maize'
    """
    c = crop.lower()
    if c in ("rice", "paddy", "水稻"):
        preset = get_crop_preset("rice")
        params = get_crop_params("rice")
        planting, harvest = RICE_PLANTING_DATE, RICE_HARVEST_DATE
    elif c in ("corn", "maize", "玉米"):
        preset = get_crop_preset("corn")
        params = get_crop_params("corn")
        planting, harvest = CORN_PLANTING_DATE, CORN_HARVEST_DATE
    else:
        raise ValueError(f"未知作物: {crop}")

    crop_obj = Crop(
        c_name=preset,
        planting_date=planting,
        harvest_date=harvest,
        **params,
    )
    return crop_obj


def crop_summary(crop: str) -> str:
    """返回作物关键参数摘要。"""
    c = crop.lower()
    if c in ("rice", "paddy", "水稻"):
        name, p = "水稻", RICE_PARAMS
    elif c in ("corn", "maize", "玉米"):
        name, p = "玉米", CORN_PARAMS
    else:
        raise ValueError(f"未知作物: {crop}")
    lines = [f"作物: {name}"]
    for k in ("CC0", "CCx", "CGC", "CDC", "Kcb", "WP", "HI0",
              "Zmax", "p_up1", "p_up2", "p_up3", "Tbase", "Tupp", "PlantPop"):
        lines.append(f"  {k:10s} = {p[k]}")
    return "\n".join(lines)


if __name__ == "__main__":
    for c in ("rice", "corn"):
        print("\n" + crop_summary(c))
        cr = build_crop(c)
        print("  -> Crop 对象构建成功, Name =", cr.Name,
              "planting =", cr.planting_date, "harvest =", cr.harvest_date)
