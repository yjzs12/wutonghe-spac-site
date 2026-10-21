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

★★ 2026-10-21 口径变更（B 方案）：水稻初始墒情由 SAT（饱和）改为 FC（田间持水率）
--------------------------------------------------------------------------
变更前：水稻按「移栽前已泡田」取 SAT(0.579/0.509/0.506)，相对 FC 多蓄
  (0.579−0.367)×300 + (0.509−0.320)×300 + (0.506−0.310)×400 ≈ 199 mm
  的全剖面「启动水库」，且这部分水不进入灌溉量统计。

为什么必须改（三重不自洽）：
  1) 泡田水在本模型中根本进不来 —— 模拟期 04-01 起算，5/15 移栽日之前内核
     `growing_season=False` 强制灌溉量 Irr=0（见 `modules/paddy_control.py` 顶部说明），
     泡田定额从未计入水量平衡；
  2) 但「泡田效果」又被 SAT 初始墒情偷偷算了一遍 ⇒ 同一份泡田水被重复计入，
     且计入的那一份（199 mm）既不计灌溉量、也不受任何灌溉方案控制；
  3) 后果是灌溉梯度塌缩：3 年实测 L1→L5 增产 −1.5%/−1.5%/+0.9%，
     L1「雨养型」甚至略高于 CK，与「灌溉具显著年型差异」的既有结论互相矛盾。

改为 FC 后：初始墒情只代表春整地后的自然状态（不预设饱和），移栽日的建立水层
由水层控制器在 5/15 首次灌水给出（实测首日即灌 30 mm），物理上自洽、口径唯一。
⚠ 玉米本来就是 FC，不受影响（保持原样）。
⚠ 如需复现旧的 SAT 口径结果：`build_initial_water_content(crop, "SAT")`，
  或在 SimulationConfig 里显式传 `initial_moisture="SAT"`；旧结果已归档到
  `outputs/_归档_初始SAT_2026-10-21/`。
"""

from __future__ import annotations

from modules.engine_core import InitialWaterContent

# 各作物默认初始墒情状态（2026-10-21 B 方案：水稻 SAT → FC）
DEFAULT_MOISTURE = {
    "rice": "FC",
    "corn": "FC",
}
# 旧口径（仅作回退/对照）：水稻 "SAT"
LEGACY_MOISTURE = {
    "rice": "SAT",
    "corn": "FC",
}
# 中文描述
_MOISTURE_CN = {
    "FC": "FC(田间持水率)",
    "SAT": "SAT(饱和)",
    "WP": "WP(凋萎含水率)",
}


def _crop_key(crop: str) -> str:
    c = (crop or "").lower()
    if c in ("rice", "paddy", "水稻"):
        return "rice"
    if c in ("corn", "maize", "玉米"):
        return "corn"
    return c


def build_initial_water_content(crop: str, moisture: str = None,
                                n_layers: int = 3) -> InitialWaterContent:
    """构建初始土壤水分条件。

    Parameters
    ----------
    crop     : 'rice' / 'corn'
    moisture : 初始水分状态标识
               'FC'  = 田间持水率（**默认，两作物一致**）
               'SAT' = 饱和（水稻泡田后；旧口径，仅作对照回退）
               'WP'  = 凋萎（极度干旱）
               或具体 vol 比例数值，如 0.30
    n_layers : 土壤层数（须与 Soil.nLayer 一致）
    """
    if moisture is None or moisture == "":
        moisture = DEFAULT_MOISTURE.get(_crop_key(crop), "FC")

    return InitialWaterContent(
        wc_type="Prop",
        method="Layer",
        depth_layer=list(range(1, n_layers + 1)),
        value=[moisture] * n_layers,
    )


def describe_initial_condition(crop: str, moisture: str = None) -> str:
    """返回可打印的初始条件描述（须与实际传入值一致）。"""
    key = _crop_key(crop)
    name = {"rice": "水稻", "corn": "玉米"}.get(key, crop)
    if moisture is None or moisture == "":
        moisture = DEFAULT_MOISTURE.get(key, "FC")
    desc = _MOISTURE_CN.get(str(moisture), str(moisture))
    return f"初始条件: {name} 各层土壤水分 = {desc}"


if __name__ == "__main__":
    for c in ("rice", "corn"):
        print(describe_initial_condition(c))
        iwc = build_initial_water_content(c)
        print(f"  -> IWC: wc_type={iwc.wc_type} method={iwc.method} value={iwc.value}")
