# -*- coding: utf-8 -*-
"""
初始条件模块
============
对应大纲 2.6 初始条件设置：
  参考灌区历史资料确定生育期日期作为模拟起始时间，以 1 天为步长完成步长划分；
  抓取模拟开始阶段墒情站点土壤水位/含水率，对各层土壤含水率赋值作为初始条件。

内核通过 InitialWaterContent 设定初始土壤水分：
  wc_type : 'Prop' 按比例 / 'Num' 按数值
  method  : 'Layer' 按层 / 'Depth' 按深度
  depth_layer : 层序号列表
  value   : 各层初始水分值，可取 'FC'(田间持水率)/'SAT'/... 或数值(vol%)
"""

from __future__ import annotations

from modules.engine_core import InitialWaterContent


def build_initial_water_content(crop: str, moisture: str = "FC",
                                n_layers: int = 3) -> InitialWaterContent:
    """构建初始土壤水分条件。

    Parameters
    ----------
    crop     : 'rice' / 'corn'
    moisture : 初始水分状态标识
               'FC'  = 田间持水率（默认，春播/插秧前典型）
               'SAT' = 饱和（水稻泡田后）
               'WP'  = 凋萎（极度干旱）
               或具体 vol 比例数值，如 0.30
    n_layers : 土壤层数（须与 Soil.nLayer 一致）
    """
    # 水稻移栽前已泡田，初始偏饱和；玉米播种时为田间持水率
    if moisture is None or moisture == "":
        moisture = "SAT" if crop.lower() in ("rice", "paddy", "水稻") else "FC"

    return InitialWaterContent(
        wc_type="Prop",
        method="Layer",
        depth_layer=list(range(1, n_layers + 1)),
        value=[moisture] * n_layers,
    )


def describe_initial_condition(crop: str) -> str:
    c = crop.lower()
    if c in ("rice", "paddy", "水稻"):
        name, moisture = "水稻", "SAT(泡田饱和)"
    elif c in ("corn", "maize", "玉米"):
        name, moisture = "玉米", "FC(田间持水率)"
    else:
        name, moisture = crop, "FC(田间持水率)"
    return f"初始条件: {name} 各层土壤水分 = {moisture}"


if __name__ == "__main__":
    for c in ("rice", "corn"):
        print(describe_initial_condition(c))
        iwc = build_initial_water_content(c)
        print(f"  -> IWC: wc_type={iwc.wc_type} method={iwc.method} value={iwc.value}")
