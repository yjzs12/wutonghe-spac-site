# -*- coding: utf-8 -*-
"""
作物参数配置 —— 作物模拟模块
==============================
依据大纲 2.4 作物模拟模块，针对梧桐河灌区水稻、玉米两类典型作物，
给出 内核所需的冠层、蒸腾蒸发、干物质、产量与水分胁迫参数。

大纲要求的关键参数：
  冠层覆盖度：CC0, CCx, CGC, CDC
  蒸腾蒸发：Kcb(基础作物系数)
  地上干物质：WP(水分生产力)
  籽粒产量：HI0(参照收获指数)
  水分胁迫：p_up/p_lo(根区耗水上下限) 对应 KSexp/KSCCx/KSCC/KSsto

说明：以 内核内置预设('PaddyRice'/'Maize')为基线，
      仅覆盖区域率定所需的关键农学/生理参数，生育期日历等由预设与
      planting_date/harvest_date 驱动，避免覆盖冲突。
      生育期依据实测资料 data/产量 生育期.xlsx。
"""

from __future__ import annotations

# =========================================================================== #
#  水稻（PaddyRice）—— 梧桐河灌区水田主体作物
# =========================================================================== #
# 生育期：5/15 移栽 → 9/25 收获 = 133 天
RICE_PARAMS = {
    # —— 种植方式 ——
    "PlantMethod": 0,         # 求解库编码：0=移栽，1=播种
    "PlantPop": 250000,       # 有效苗个体/ha；非穴数实测，冠层先验，待苗情资料验证
    "SeedSize": 6.0,          # 单苗投影面积cm²；与密度共同决定实际初始覆盖率1.5%
    "CalendarType": 1,
    "SwitchGDD": 0,

    # —— 冠层覆盖度 CC（大纲核心参数）——
    "CC0": 0.015,             # 与内核由密度×单苗面积计算的值一致；非直接覆盖参数
    "CCx": 0.95,              # 最大冠层覆盖率
    "CGC": 0.1125,            # 冠层生长系数(GDD模式)
    "CGC_CD": 0.1125,         # 冠层生长系数(日历天数模式) — 必须设置,否则被预设值覆盖
    "CDC": 0.10,              # 冠层退化系数(GDD模式)
    "CDC_CD": 0.10,           # 冠层退化系数(日历天数模式) — 必须设置,否则被预设值覆盖

    # —— 蒸腾蒸发 ——
    "Kcb": 1.10,              # 基础作物系数

    # —— 地上干物质 ——
    "WP": 15.0,               # 标准化干物质水分生产力g/m²，C3作物先验区间下沿
                              # 与HI0、冠层物候联合约束，不用逐年缩放或灌水量修正产量

    # —— 籽粒产量 ——
    "HI0": 0.40,              # 干籽粒/地上部干物质，待独立年份与生物量观测验证
    "YldWC": 85.5,            # 本库该值为籽粒干物质百分比；对应14.5%标准含水率

    # —— 根系 ——
    "Zmin": 0.20,             # 最小有效根系深度 (m)
    "Zmax": 0.50,             # 最大有效根系深度 (m)

    # —— 水分胁迫上下限（根区耗水比例 %TAW）——
    # 注意: 内核使用索引参数 p_up2/p_up3 等(覆盖预设数组), 标量 p_up/p_lo 会被忽略。
    # Preserve the deployed shared baseline. Single-zone runs explicitly override
    # p_up2 to .50; immutable fixed results and other kernel callers keep 1.0.
    # Native boundary p_up2=p_lo2=1 disables this drought-stomatal channel.
    "p_up2": 1.0,
    "p_up3": 0.55,            # 衰老胁迫起点（55% TAW）

    # 水田作物暂不考虑通气胁迫；显式关闭原生通道（PaddyRice预设）。
    "Aer": -1e10,
    "LagAer": 1e10,

    # —— 温度胁迫（三江平原早春低温）——
    "Tbase": 8.0,             # 基础温度
    "Tupp": 30.0,             # 上限生长温度
    "Tmin_up": 8.0, "Tmin_lo": 3.0, "Tmax_up": 35.0, "Tmax_lo": 40.0,

    # —— 生育期日历（CalendarType=1，按日历天数）——
    # 移栽 5/15 → 收获 9/25 = 133 天
    "EmergenceCD": 3.0,       # 移栽恢复返青期；不是重新播种出苗
    "MaxRootingCD": 60.0,     # 7月中下旬达最大有效根深的代表情景，非实测
    "SenescenceCD": 100.0,    # 8月下旬灌浆期间开始衰老；95—110d范围内条件拟合
    "HIstartCD": 80.0,        # 内核含返青延迟后8月上旬开花/形成产量，与来源表旬期对应
    "FloweringCD": 19.0,      # 开花持续天数（水稻预设，非开花日期；待本地物候验证）
    "YldFormCD": 45.0,        # 从开花累积HI至9月中下旬；不能把HI起点等同灌浆旬首
    "MaturityCD": 133.0,      # 成熟收获（5/15 + 133 = 9/25）
}

# 来源表未注明产量含水率。本轮按寒地粳稻14.5%暂定，并保留干重供追溯。
# 农业农村部《水稻机械化生产技术指导意见》给出粳稻14.5%标准水分。
RICE_YIELD_MOISTURE = 0.145


def yield_moisture(crop: str) -> float:
    """报告产量的湿基含水率；玉米保持原有干重口径，避免改变未请求的率定。"""
    return RICE_YIELD_MOISTURE if crop.lower() in ("rice", "paddy", "水稻") else 0.0

# =========================================================================== #
#  玉米（Maize）—— 梧桐河灌区主要旱田作物
# =========================================================================== #
# 生育期：4/25 播种 → 9/25 收获 = 153 天
CORN_PARAMS = {
    "PlantMethod": 1,         # 求解库编码：1=播种，0=移栽
    "PlantPop": 75000,        # 种植密度 (株/ha)

    # —— 冠层覆盖度 CC ——
    "CC0": 0.12,              # 仅供文档参考;实际CC0=PlantPop*SeedSize*1e-8
    "CCx": 0.96,
    "CGC": 0.165,             # 冠层生长系数(GDD模式)
    "CGC_CD": 0.165,          # 日历天数模式 — 必须设置,否则被预设值(0.16312)覆盖
    "CDC": 0.12,              # 冠层退化系数(GDD模式)
    "CDC_CD": 0.12,           # 日历天数模式 — 必须设置,否则被预设值(0.11691)覆盖

    # —— 蒸腾蒸发 ——
    "Kcb": 1.05,

    # —— 地上干物质 ——
    "WP": 12.9,               # 沿用历史水分生产力；本轮纠正土壤边界后未按最终产量再次缩放
                              # 需在含水率、灌水处理一致的独立观测下验证

    # —— 籽粒产量 ——
    "HI0": 0.48,              # 参照收获指数 — 寒地玉米率定值

    # —— 根系 ——
    "Zmin": 0.30,
    "Zmax": 0.90,             # 仅有1m土壤剖面；保留求解器0.1m底边界，避免自动扩成1.3m

    # —— 水分胁迫上下限（旱作，缺水敏感）——
    # 注意: 内核的 Crop 类忽略标量 p_up/p_lo, 必须用索引参数:
    #   p_up1/p_lo1: 冠层扩展胁迫 (玉米对缺水极敏感, 14%耗竭即开始受限)
    #   p_up2/p_lo2: 气孔关闭胁迫 (69%耗竭开始, 凋萎点完全关闭)
    #   p_up3/p_lo3: 冠层衰老胁迫 (69%耗竭开始加速衰老)
    #   p_up4/p_lo4: 授粉失败胁迫 (80%耗竭开始影响授粉)
    # 取 FAO 玉米标准阈值(与 Maize 预设一致, 显式声明便于追溯)
    "p_up1": 0.14, "p_lo1": 0.72,
    "p_up2": 0.69, "p_lo2": 1.00,
    "p_up3": 0.69, "p_lo3": 1.00,
    "p_up4": 0.80, "p_lo4": 1.00,

    # —— 通气（渍水）胁迫：沿用 Maize 预设 Aer=5 vol% / LagAer=3 d ——
    # 2024-2026 复核：三年表层含水率最大 0.501 / 0.471 / 0.490，均低于
    # 触发阈值 SAT-0.05 = 0.518，该通道全年 0 天触发，取值不影响结果，未改动。
    "Aer": 5.0, "LagAer": 3,

    # —— 温度胁迫 ——
    "Tbase": 8.0,
    "Tupp": 30.0,
    "Tmin_up": 12.0, "Tmax_up": 35.0,

    # —— 生育期日历（CalendarType=1，按日历天数）——
    # 播种 4/25 → 收获 9/25 = 153 天
    "EmergenceCD": 6.0,       # 出苗（播种后6天）
    "MaxRootingCD": 120.0,    # 约8月下旬最大根深的先验；原表无根深观测，不等同抽雄期
    "SenescenceCD": 130.0,    # 冠层开始衰老（蜡熟期）
    "HIstartCD": 90.0,        # 旬级物候代表值：有效HI首次为正约7/27，对应7月下旬吐丝
    "FloweringCD": 13.0,      # 开花持续天数（玉米预设，非开花日期；待本地物候验证）
    "YldFormCD": 55.0,        # 与HI起点联动的候选历时，非原表直接实测；HIend=145d
    "MaturityCD": 153.0,      # 成熟收获（4/25 + 153 = 9/25）
}


def get_crop_preset(crop: str) -> str:
    """返回 内核内置作物预设名。"""
    c = crop.lower()
    if c in ("rice", "paddy", "水稻"):
        return "PaddyRice"
    if c in ("corn", "maize", "玉米"):
        return "Maize"
    raise ValueError(f"未知作物: {crop}")


def get_crop_params(crop: str) -> dict:
    """返回作物参数覆盖字典（用于 Crop(**params)）。

    用户在前端「生长参数」面板里改的值（存于 settings['growth']）在此覆盖默认值，
    与土壤参数一样逐次校验后生效。
    """
    c = crop.lower()
    if c in ("rice", "paddy", "水稻"):
        result, key = dict(RICE_PARAMS), "rice"
    elif c in ("corn", "maize", "玉米"):
        result, key = dict(CORN_PARAMS), "corn"
    else:
        raise ValueError(f"未知作物: {crop}")

    try:
        from config.simulation_settings import get_active
        growth = (get_active(key) or {}).get("growth") or {}
    except Exception:                                  # noqa: BLE001
        growth = {}
    allowed = {item["key"] for item in GROWTH_SPEC[key]}
    for name, value in growth.items():
        if name in allowed:
            result[name] = float(value)
    return result


# --------------------------------------------------------------------------- #
#  前端可调参数规格（顺序即界面顺序，按对产量的影响强弱排列）
# --------------------------------------------------------------------------- #
GROWTH_SPEC = {
    "rice": [
        dict(key="CCx", label="最大冠层覆盖度 CCx", hint="封行后的冠层上限", min=.50, max=1.00, step=.01),
        dict(key="CGC_CD", label="冠层生长速率 (1/d)", hint="出苗到封行的扩张速度", min=.02, max=.30, step=.005),
        dict(key="CDC_CD", label="冠层衰老速率 (1/d)", hint="灌浆期冠层衰减速度", min=.02, max=.30, step=.005),
        dict(key="Kcb", label="基础作物系数 Kcb", hint="最大蒸腾相对参考蒸散", min=.60, max=1.40, step=.01),
        dict(key="WP", label="水分生产力 (g/m²)", hint="单位蒸腾的地上干物质产量", min=8., max=30., step=.5),
        dict(key="HI0", label="收获指数 HI0", hint="籽粒占地上干物质比例", min=.20, max=.60, step=.01),
        dict(key="Zmax", label="最大根深 (m)", hint="须小于土壤剖面深度减 0.1 m", min=.30, max=1.20, step=.05),
    ],
    "corn": [
        dict(key="CCx", label="最大冠层覆盖度 CCx", hint="封行后的冠层上限", min=.50, max=1.00, step=.01),
        dict(key="CGC_CD", label="冠层生长速率 (1/d)", hint="出苗到封行的扩张速度", min=.02, max=.35, step=.005),
        dict(key="CDC_CD", label="冠层衰老速率 (1/d)", hint="灌浆期冠层衰减速度", min=.02, max=.35, step=.005),
        dict(key="Kcb", label="基础作物系数 Kcb", hint="最大蒸腾相对参考蒸散", min=.60, max=1.40, step=.01),
        dict(key="WP", label="水分生产力 (g/m²)", hint="单位蒸腾的地上干物质产量", min=8., max=30., step=.5),
        dict(key="HI0", label="收获指数 HI0", hint="籽粒占地上干物质比例", min=.20, max=.60, step=.01),
        dict(key="Zmax", label="最大根深 (m)", hint="须小于土壤剖面深度减 0.1 m", min=.30, max=1.20, step=.05),
    ],
}

# Editable solver inputs, shared by UI metadata, validation, and Crop construction.
PARAMETER_GROUPS = [
    dict(key="growth", label="生长与产量参数", keys=["PlantPop", "SeedSize", "CCx", "CGC_CD", "CDC_CD", "Kcb", "WP", "HI0"]),
    dict(key="roots", label="根系参数", keys=["Zmin", "Zmax"]),
    dict(key="stress", label="水分胁迫参数", keys=["p_up1", "p_lo1", "p_up2", "p_lo2", "p_up3", "p_lo3", "p_up4", "p_lo4", "fshape_w2", "LagAer"]),
    dict(key="temperature", label="温度胁迫参数", keys=["Tbase", "Tupp", "Tmin_up", "Tmin_lo", "Tmax_up", "Tmax_lo"]),
]
_EXTRA_SPEC = [
    dict(key="LagAer", label="通气与水层胁迫参数 LagAer", hint="当前水稻默认1e10表示关闭原生通道，不代表实际耐淹天数；有限值会改变有水层时的蒸腾响应", min=1, max=1e10, step=1),
    dict(key="PlantPop", label="种植密度（株/ha）", hint="与单苗面积共同决定初始冠层", min=1000, max=1000000, step=1),
    dict(key="SeedSize", label="单苗投影面积（cm²）", hint="初始冠层=密度×单苗面积×10⁻⁸", min=.1, max=50, step=.1),
    dict(key="Zmin", label="最小根深（m）", hint="须不大于最大根深", min=.05, max=1.2, step=.01),
    dict(key="fshape_w2", label="气孔胁迫曲线形状", hint="控制起点与完全关闭之间的响应形状", min=.1, max=10, step=.1),
]
for _i, _name in enumerate(["冠层扩展", "气孔关闭", "冠层衰老", "授粉失败"], 1):
    for _edge, _label in [("up", "起点"), ("lo", "完全胁迫")]:
        _EXTRA_SPEC.append(dict(key=f"p_{_edge}{_i}", label=f"{_name}{_label}（TAW比例）", hint="根区有效水耗竭比例；起点小于完全胁迫值，均为1时关闭该缺水通道", min=0, max=1, step=.01))
for _key, _label in [("Tbase", "基础生长温度"), ("Tupp", "上限生长温度"), ("Tmin_up", "授粉低温胁迫起点"), ("Tmin_lo", "授粉低温完全胁迫"), ("Tmax_up", "授粉高温胁迫起点"), ("Tmax_lo", "授粉高温完全胁迫")]:
    _EXTRA_SPEC.append(dict(key=_key, label=f"{_label}（℃）", hint="低温完全胁迫<低温起点；高温起点<高温完全胁迫", min=-10, max=60, step=.1))
for _crop in GROWTH_SPEC:
    GROWTH_SPEC[_crop].extend(dict(s) for s in _EXTRA_SPEC)


def solver_defaults(crop):
    from modules.engine_core import Crop
    key = _canon(crop)
    base = RICE_PARAMS if key == "rice" else CORN_PARAMS
    obj = Crop(get_crop_preset(key), planting_date="05/15")
    return {s["key"]: float(base.get(s["key"], getattr(obj, s["key"], 0))) for s in GROWTH_SPEC[key]}


# 物候期：一律用「播种/移栽后天数」表达，日期由网站按所选播种日推算。
PHENOLOGY_SPEC = {
    "rice": [
        dict(key="EmergenceCD", label="返青恢复 (DAP)", min=1, max=40),
        dict(key="MaxRootingCD", label="最大根深 (DAP)", min=10, max=120),
        dict(key="HIstartCD", label="产量形成开始 (DAP)", min=20, max=160),
        dict(key="SenescenceCD", label="冠层始衰 (DAP)", min=20, max=160),
    ],
    "corn": [
        dict(key="EmergenceCD", label="出苗 (DAP)", min=1, max=40),
        dict(key="MaxRootingCD", label="最大根深 (DAP)", min=10, max=140),
        dict(key="HIstartCD", label="产量形成开始 (DAP)", min=20, max=180),
        dict(key="SenescenceCD", label="冠层始衰 (DAP)", min=20, max=180),
    ],
}


def _canon(crop: str) -> str:
    c = crop.lower()
    return "rice" if c in ("rice", "paddy", "水稻") else "corn"


def growth_defaults(crop: str) -> dict:
    """默认值（未经用户覆盖）——供界面回填。"""
    key = _canon(crop)
    base = RICE_PARAMS if key == "rice" else CORN_PARAMS
    return solver_defaults(crop)


def phenology_defaults(crop: str) -> dict:
    key = _canon(crop)
    base = RICE_PARAMS if key == "rice" else CORN_PARAMS
    return {item["key"]: base[item["key"]] for item in PHENOLOGY_SPEC[key]}


if __name__ == "__main__":
    for name, p in (("水稻", RICE_PARAMS), ("玉米", CORN_PARAMS)):
        print(f"\n=== {name} 关键参数 ===")
        for k, v in p.items():
            print(f"  {k:12s} = {v}")
