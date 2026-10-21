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

★★★ 2026-10-21 22:xx 最终口径：水稻 = SAT（泡田态）+ 泡田定额显式计量
--------------------------------------------------------------------------
历史两版都只对了一半，本版把「物理」和「台账」同时改对：

  版本1（≤21:00）  thini=SAT，但不计量
      → 物理对（插秧前必须泡田、剖面饱和），台账错（凭空多 199 mm 免费水，
        既不计灌溉量、也不受方案控制），导致灌溉梯度塌缩。

  版本2（21:00 方案 B）thini=FC，不计量
      → 台账干净了，但物理错：插秧时田面只有 20 mm、剖面只到 FC，
        而内核在移栽日之前 `growing_season=False` 强制 Irr=0，
        这 199 mm 永远补不回来（首灌单次仅 33/56 mm，Ksat 也渗不下去）。
        实测灌水量因此系统性偏低：CK 577 mm，远低于省级水稻灌溉定额。

  版本3（本版，现行）
      ① 物理：thini=SAT（全剖面），移栽日田面水层 30 mm（浅水插秧 1–3 cm）
         —— 与 FAO AquaCrop PaddyRice 预设的初始条件一致；
      ② 台账：新增「泡田定额(mm)」= Σ(SAT−FC)×1000×dz + 田面水层
         = 63.6 + 56.7 + 78.4 + 30 ≈ 228.7 mm，
         **单列为一笔灌溉**，计入「累计灌溉量」；
      ③ 逐日输出新增 `泡田灌溉量(mm)` 列（仅移栽日取该值），
         内核的 `每日灌溉量(mm)` 仍保持内核原值（灌水频次统计口径不变）。

  ⚠ 为什么不能靠 Schedule 在移栽前灌：内核 `run_single_timestep` 在
    `growing_season=False` 时把 Irr 硬置 0、且改用 FallowIrrMngt，
    Schedule 完全不读；一天灌 229 mm 又会先撞 z_bund=200 mm 溢流。
    因此移栽前的泡田水只能通过初始条件注入，再在台账上补记。
  ⚠ 本定额是「净泡田水量」，不含泡田期（约 10–15 d）的蒸发与渗漏损失；
    实测黑龙江稻田泡田定额 1500–2500 m³/ha（150–250 mm），228.7 mm 落在区间内。
  ⚠ 玉米不受影响（初始仍 FC，泡田定额 0）。
  ⚠ 如需复现版本1/2 的口径：`build_initial_water_content(crop, "FC")`，
    旧结果分别归档于 `outputs/_归档_初始SAT_2026-10-21/`（v1）与
    `outputs/_归档_Ksat5_胁迫1.0_2026-10-21/`（v2 的口径可由当前输出复现）。
"""

from __future__ import annotations

from modules.engine_core import InitialWaterContent

# 各作物默认初始墒情状态
#   水稻 SAT = 移栽（插秧）时已泡田，剖面饱和——作物学事实，不是"免费赠水"，
#              配套 `puddling_requirement()` 计量后才成立。
DEFAULT_MOISTURE = {
    "rice": "SAT",
    "corn": "FC",
}
# 备选口径（仅作回退/对照）：水稻 "FC"（v2 口径，未计泡田）
LEGACY_MOISTURE = {
    "rice": "FC",
    "corn": "FC",
}

# 插秧时的田面水层（mm）：浅水插秧/返青期 1–3 cm，取中值 30 mm。
# 该值同时作为 `build_field_management()` 的 bund_water，
# 内核在移栽日重置时写入 `InitCond.surface_storage = min(bund_water, z_bund)`。
PADDY_INIT_WATER_LAYER = 30.0

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


def puddling_requirement(crop: str) -> float:
    """泡田定额（mm）——把剖面从 FC 提到 SAT，再建立插秧水层所需的水量。

    只对水稻返回非零值；玉米（旱田）返回 0。

    计算口径：Σ (SAT_i − FC_i) × 1000 × dz_i  + PADDY_INIT_WATER_LAYER
    其中 dz 为各层厚度（m），结果单位 mm。
    """
    from config.soil_params import get_soil_config

    if _crop_key(crop) != "rice":
        return 0.0
    cfg = get_soil_config(crop)
    vol = 0.0
    for lay in cfg["layers"]:
        vol += (lay["SAT"] - lay["FC"]) * 1000.0 * lay["depth"]
    return vol + PADDY_INIT_WATER_LAYER


def build_initial_water_content(crop: str, moisture: str = None,
                                n_layers: int = 3) -> InitialWaterContent:
    """构建初始土壤水分条件。

    Parameters
    ----------
    crop     : 'rice' / 'corn'
    moisture : 初始水分状态标识
               'SAT' = 饱和（**水稻默认**：移栽前已泡田，配套泡田定额计量）
               'FC'  = 田间持水率（**玉米默认**；水稻为 v2 回退口径）
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
    txt = f"初始条件: {name} 各层土壤水分 = {desc}"
    pudd = puddling_requirement(crop)
    if pudd > 0:
        txt += (f"（泡田态，插秧水层 {PADDY_INIT_WATER_LAYER:.0f} mm；"
                f"泡田定额 {pudd:.1f} mm 计入灌溉量）")
    return txt


if __name__ == "__main__":
    for c in ("rice", "corn"):
        print(describe_initial_condition(c))
        iwc = build_initial_water_content(c)
        print(f"  -> IWC: wc_type={iwc.wc_type} method={iwc.method} value={iwc.value}")
        print(f"  -> 泡田定额: {puddling_requirement(c):.1f} mm")
