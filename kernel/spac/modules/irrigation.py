# -*- coding: utf-8 -*-
"""
灌溉管理模块 —— 灌溉制度优化
==================================
对应大纲 2.5 灌溉管理模块：
  通过设定不同生育期灌溉下限/灌水定额,模拟从"极低水量(重胁迫)"到"超高水量(过量灌溉)"
  的完整梯度,得出不同灌水量时的产量响应曲线,寻找边际产量拐点
  (经济最优灌溉制度)。

方案设计(按作物类型分别采用不同优化策略):
  (1) 水稻 —— 固定水层管理方案(method=3, 7档灌水定额梯度)
      淹水水稻需维持田面水层, SMT(土壤水分目标)不适用;
      通过在同一组灌水日期上设置 7 档不同定额(185→650 mm),
      形成"极低水量→适宜水量→超高水量"的水层管理梯度。
      正常灌溉对照(定额 460 mm)作为基准。

  (2) 玉米 —— SMT 自动调控方案(method=1, 7档阈值梯度)
      旱作无水层, 通过 SMT 设定不同生育期土壤水分目标下限
      (阈值越小胁迫越重), 形成完整优化梯度。
      正常灌溉对照(固定日历)作为基准。

  经济最优点出现在中间档"适宜水量"附近(产量达 95% 最大值时的最小灌水量),
  体现灌溉制度优化过程(非首档即为最优)。

SMT 四阶段阈值含义(%TAW,根区有效水下限,仅用于玉米):
  SMT[0] = 初始期 ( emergence - 10% CC )
  SMT[1] = 发育期 ( 10% CC - 80% CC )
  SMT[2] = 中期   ( 80% CC - 开始衰老 )   ← 需水关键期
  SMT[3] = 末期   ( 开始衰老 - 成熟 )
  值越小 → 允许消耗越多 → 胁迫越重 → 灌水量越少
  值越大 → 越早补水 → 灌水量越多
"""

from __future__ import annotations

from typing import Dict, List, Tuple, Optional, Union

import pandas as pd
from modules.engine_core import IrrigationManagement, FieldMngt


# =========================================================================== #
#  方案标签与类型常量
# =========================================================================== #

# 7 档方案中文标签(灌水量由低到高, 正常灌溉量落在中间基准档)
# 命名口径("水量档位型"): 直接按灌水量高低命名, 不混用"胁迫/灌溉"两套词;
#   低水量侧 = 水分胁迫重, 高水量侧 = 过量灌溉。
# 设计依据: 水稻定额从 ~400mm(极低水量)到 ~1557mm(超高水量),
#           向更小方向延伸以观察产量变化拐点
SCHEME_LABELS = [
    "极低水量", "低水量", "中低水量",
    "适宜水量", "较高水量", "高水量", "超高水量",
]

# 与 SCHEME_LABELS 一一对应的稳定英文档位 ID
# 仅用于输出文件名与内部标识, 与中文显示名解耦 ——
# 以后再调整显示名, 不会影响已归档的输出文件名。
SCHEME_IDS = ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]

# 正常灌溉对照标签
NORMAL_LABEL = "正常灌溉对照"

# 对照档的文件名 ID(农学惯例: CK = check, 对照)
NORMAL_ID = "CK"


def scheme_id(label: str) -> str:
    """方案中文标签 → 稳定英文档位 ID(输出文件名用)。

    未登记的标签原样返回, 保证向后兼容。
    """
    if label == NORMAL_LABEL:
        return NORMAL_ID
    if label in SCHEME_LABELS:
        return SCHEME_IDS[SCHEME_LABELS.index(label)]
    return str(label)

# 方案类型
TYPE_NORMAL = "正常灌溉对照"   # 固定日历基准
TYPE_PADDY = "水层管理"        # 水稻固定水层管理
TYPE_SMT = "SMT调控"           # 玉米 SMT 调控


# =========================================================================== #
#  水稻固定水层管理方案 —— method=3 + 7 档灌水定额梯度
#
#  设计原理:
#  - 30 个关键灌水日期(泡田→返青→分蘖→晒田→拔节孕穗→抽穗开花→灌浆)
#    与正常灌溉对照保持一致, 仅改变每次灌水定额, 形成从胁迫到过量的梯度
#  - 关键期(拔节孕穗/抽穗开花)3天一灌, 维持20-50mm水层
#  - 返青/分蘖4-5天一灌, 灌浆5天一灌
#  - 低定额(~454 mm): 水层薄且难以持续, 关键期出现水分胁迫
#  - 中定额(~747 mm): 维持 20-30 mm 水层, 产量接近最大值 → 经济最优区
#  - 高定额(~1345 mm): 长期深水层, 产量增长趋缓甚至下降 → 过量灌溉
# =========================================================================== #

# 水稻 7 档灌水定额(30 个灌水日期的定额列表, 单位 mm)
# 设计依据: 在正常灌溉对照(891mm)基础上, 按比例缩放形成水量梯度
# 第13个位置(7/5晒田期,索引12)恒为0, 所有档位一致
RICE_PADDY_LEVELS: List[List[int]] = [
    # L1 极低水量  合计 ~400 mm (正常定额的45%)  ← 起始档(向更小方向延伸)
    [22, 11, 11, 11, 11, 11, 8, 8, 8, 8, 8, 8, 0, 32, 20, 20, 20, 20, 20, 20, 20, 15, 15, 15, 15, 15, 5, 5, 5, 5],
    # L2 低水量    合计 ~550 mm (正常定额的62%)
    [30, 15, 15, 15, 15, 15, 11, 11, 11, 11, 11, 11, 0, 43, 28, 28, 28, 28, 28, 28, 28, 21, 21, 21, 21, 21, 7, 7, 7, 7],
    # L3 中低水量  合计 ~700 mm (正常定额的78%)
    [38, 19, 19, 19, 19, 19, 14, 14, 14, 14, 14, 14, 0, 55, 35, 35, 35, 35, 35, 35, 35, 27, 27, 27, 27, 27, 9, 9, 9, 9],
    # L4 适宜水量  合计 ~891 mm (正常定额的100%) ← 正常灌溉基准
    [50, 25, 25, 25, 25, 25, 18, 18, 18, 18, 18, 18, 0, 70, 45, 45, 45, 45, 45, 45, 45, 35, 35, 35, 35, 35, 12, 12, 12, 12],
    # L5 较高水量  合计 ~1130 mm (正常定额的127%)
    [64, 32, 32, 32, 32, 32, 23, 23, 23, 23, 23, 23, 0, 89, 57, 57, 57, 57, 57, 57, 57, 44, 44, 44, 44, 44, 15, 15, 15, 15],
    # L6 高水量    合计 ~1345 mm (正常定额的151%)
    [75, 38, 38, 38, 38, 38, 27, 27, 27, 27, 27, 27, 0, 105, 68, 68, 68, 68, 68, 68, 68, 53, 53, 53, 53, 53, 18, 18, 18, 18],
    # L7 超高水量  合计 ~1557 mm (正常定额的175%)
    [88, 44, 44, 44, 44, 44, 32, 32, 32, 32, 32, 32, 0, 122, 78, 78, 78, 78, 78, 78, 78, 61, 61, 61, 61, 61, 21, 21, 21, 21],
]

# 水稻正常灌溉对照定额(关键期3天一灌, 维持20-50mm水层, 合计 ~891 mm)
# 设计依据: 寒地淹水水稻, 配合Ks=8mm/day的低渗漏土壤
#   返青期4天一灌, 25mm/次 → 维持薄水层促进返青
#   关键期(拔节孕穗/抽穗开花)3天一灌, 45/35mm/次 → 维持20-50mm水层(2024/2025均可满足)
#   复水期(7/10)70mm较大定额 → 晒田后快速建立水层
#   灌浆5天一灌, 12mm/次, 后期减少定额
RICE_NORMAL_DEPTHS = [
    50,   # 5/10  泡田(移栽前, 建立水层)
    25,   # 5/14  返青(移栽后3天)
    25,   # 5/18  返青
    25,   # 5/22  返青
    25,   # 5/26  返青
    25,   # 5/30  返青末
    18,   # 6/4   分蘖初
    18,   # 6/9   分蘖
    18,   # 6/14  分蘖
    18,   # 6/19  分蘖
    18,   # 6/24  分蘖盛
    18,   # 6/29  分蘖末
    0,    # 7/5   晒田期(不灌水)
    70,   # 7/10  晒田复水(拔节孕穗, 关键期, 较大定额快速建立水层)
    45,   # 7/13  拔节孕穗
    45,   # 7/16  拔节孕穗
    45,   # 7/19  拔节孕穗
    45,   # 7/22  孕穗
    45,   # 7/25  孕穗
    45,   # 7/28  孕穗
    45,   # 7/31  孕穗末
    35,   # 8/3   抽穗开花(关键期)
    35,   # 8/6   抽穗
    35,   # 8/10  开花
    35,   # 8/14  开花末
    35,   # 8/18  灌浆初
    12,   # 8/23  灌浆
    12,   # 8/28  灌浆
    12,   # 9/2   灌浆末
    12,   # 9/7   乳熟-蜡熟(断水前最后补一次)
]

# 水稻灌水日期(月/日, 年份由 sim_year 注入)
# 共30个日期, 覆盖5/10-9/7
#   返青期(5/10-5/30): 4天一灌, 6次
#   分蘖期(6/4-6/29): 5天一灌, 6次
#   晒田期(7/5): 不灌水(depth=0)
#   拔节孕穗(7/10-7/31): 3天一灌, 8次(关键期)
#   抽穗开花(8/3-8/18): 3-4天一灌, 5次(关键期)
#   灌浆成熟(8/23-9/7): 5天一灌, 4次
RICE_IRR_DATES_MD = [
    (5, 10),   # 泡田(移栽前, 建立水层)
    (5, 14),   # 返青(移栽后3天)
    (5, 18),   # 返青
    (5, 22),   # 返青
    (5, 26),   # 返青
    (5, 30),   # 返青末
    (6, 4),    # 分蘖初
    (6, 9),    # 分蘖
    (6, 14),   # 分蘖
    (6, 19),   # 分蘖
    (6, 24),   # 分蘖盛
    (6, 29),   # 分蘖末
    (7, 5),    # 晒田期(不灌水, depth=0, 促进根系下扎)
    (7, 10),   # 晒田复水(拔节孕穗, 关键期)
    (7, 13),   # 拔节孕穗
    (7, 16),   # 拔节孕穗
    (7, 19),   # 拔节孕穗
    (7, 22),   # 孕穗
    (7, 25),   # 孕穗
    (7, 28),   # 孕穗
    (7, 31),   # 孕穗末
    (8, 3),    # 抽穗开花(关键期)
    (8, 6),    # 抽穗
    (8, 10),   # 开花
    (8, 14),   # 开花末
    (8, 18),   # 灌浆初
    (8, 23),   # 灌浆
    (8, 28),   # 灌浆
    (9, 2),    # 灌浆末
    (9, 7),    # 乳熟-蜡熟(断水前最后补一次)
]


def _build_rice_schedule(sim_year: int, depths: List[int]) -> pd.DataFrame:
    """构建水稻固定灌水日历(method=3 所需的 DataFrame)。

    注: 晒田期(7/5)depth=0 表示当日不灌水, 仅作为日历节点存在。
    内核 method=3 允许 depth=0 的灌水事件(实际不灌水)。
    """
    if len(depths) != len(RICE_IRR_DATES_MD):
        raise ValueError(f"水稻定额长度({len(depths)})与灌水日期数({len(RICE_IRR_DATES_MD)})不匹配")
    dates = [f"{sim_year}/{m:02d}/{d:02d}" for m, d in RICE_IRR_DATES_MD]
    return pd.DataFrame({
        "Date": pd.to_datetime(dates),
        "Depth": depths,
    })


# =========================================================================== #
#  玉米 SMT 调控方案 —— method=1 + 7 档阈值梯度
#
#  设计原理:
#  - 低阈值(L1–L3 极低/低/中低水量): 允许土壤水大量消耗 → 产量低 → 优化过程前段
#  - 中阈值(L4 适宜水量): 产量接近最大值, 灌水量适中 → 经济最优区
#  - 高阈值(L5–L7 较高/高/超高水量): 几乎无胁迫 → 产量趋于平稳/略降 → 灌水浪费
# =========================================================================== #

SMT_LEVELS: Dict[str, List[List[int]]] = {
    "corn": [
        [1,  1,  1,  1],    # L1 极低水量  ≈50mm (几乎不灌溉)
        [5,  8,  8,  5],    # L2 低水量    ≈100mm
        [15, 20, 20, 10],   # L3 中低水量  ≈150mm
        [30, 40, 35, 25],   # L4 适宜水量  ≈200mm ← 正常灌溉基准
        [50, 60, 55, 35],   # L5 较高水量  ≈260mm
        [70, 80, 75, 50],   # L6 高水量    ≈330mm
        [85, 95, 90, 65],   # L7 超高水量  ≈420mm
    ],
}


# =========================================================================== #
#  正常灌溉对照 —— 玉米固定灌水日历
# =========================================================================== #

def _build_dryland_normal_schedule(crop: str, sim_year: int) -> pd.DataFrame:
    """构建玉米正常灌溉的固定灌水日历。

    灌溉定额基于三江平原实际灌溉实践(ET0修复后约654-714mm,补充灌溉)。
    水分平衡: ETc(约500-600mm) - 有效降水(约410mm) + 土壤水利用 ≈ 150-220mm。
    """
    c = crop.lower()
    if c in ("corn", "maize", "玉米"):
        # 玉米正常灌溉: 4次灌水, 合计200mm
        # 关键期(拔节孕穗/抽雄吐丝/灌浆)3次, 苗期1次
        dates = [
            f"{sim_year}/05/15",   # 苗期补水
            f"{sim_year}/07/05",   # 拔节孕穗(关键期)
            f"{sim_year}/07/20",   # 抽雄吐丝(关键期)
            f"{sim_year}/08/05",   # 灌浆初(关键期)
        ]
        depths = [40, 50, 60, 50]   # 合计 200 mm
    else:
        raise ValueError(f"未知旱作作物: {crop}")
    return pd.DataFrame({
        "Date": pd.to_datetime(dates),
        "Depth": depths,
    })


# =========================================================================== #
#  方案清单生成
# =========================================================================== #

# 方案元组: (label, smt, mode, depths, scheme_type)
#   label       : 方案标签
#   smt         : SMT 阈值列表(仅 SMT 方案); 其他为 None
#   mode        : 'normal' / 'paddy' / 'smt'
#   depths      : 灌水定额列表(仅水稻水层管理方案); 其他为 None
#   scheme_type : '正常灌溉对照' / '水层管理' / 'SMT调控'
SchemeTuple = Tuple[str, Optional[List[int]], str, Optional[List[int]], str]


def get_smt_schemes(crop: str) -> List[SchemeTuple]:
    """返回作物的全部灌溉方案列表(正常灌溉对照 + 7档优化方案)。

    水稻: 正常灌溉对照 + 7档水层管理方案(固定日历 + 不同定额)
    玉米: 正常灌溉对照 + 7档 SMT 调控方案

    Returns
    -------
    List of (label, smt, mode, depths, scheme_type)
    """
    c = crop.lower()
    if c in ("rice", "paddy", "水稻"):
        # 水稻: 正常灌溉对照 + 7档水层管理
        schemes: List[SchemeTuple] = [(NORMAL_LABEL, None, "normal", None, TYPE_NORMAL)]
        for label, depths in zip(SCHEME_LABELS, RICE_PADDY_LEVELS):
            schemes.append((label, None, "paddy", list(depths), TYPE_PADDY))
        return schemes

    if c in ("corn", "maize", "玉米"):
        key = "corn"
    else:
        raise ValueError(f"未知作物: {crop}")

    # 玉米: 正常灌溉对照 + 7档 SMT
    schemes = [(NORMAL_LABEL, None, "normal", None, TYPE_NORMAL)]
    for label, smt in zip(SCHEME_LABELS, SMT_LEVELS[key]):
        schemes.append((label, list(smt), "smt", None, TYPE_SMT))
    return schemes


# =========================================================================== #
#  IrrigationManagement 构建
# =========================================================================== #

def build_irrigation(crop: str, smt: Optional[List[int]] = None,
                     mode: str = "smt",
                     depths: Optional[List[int]] = None,
                     sim_year: Optional[int] = None) -> IrrigationManagement:
    """根据作物与方案构建灌溉管理对象。

    Parameters
    ----------
    crop     : 'rice' / 'corn'
    smt      : 4 阈值列表(仅 mode='smt' 使用)
    mode     : 'normal' / 'paddy' / 'smt'
    depths   : 水稻 8 次灌水定额列表(仅 mode='paddy' 使用);
               mode='normal' 且 crop=rice 时用 RICE_NORMAL_DEPTHS
    sim_year : 年份(用于生成灌水日历日期)
    """
    is_rice = crop.lower() in ("rice", "paddy", "水稻")
    if sim_year is None:
        from config.wutonghe_config import SIM_YEAR
        sim_year = SIM_YEAR

    # ---------- 水稻 ---------- #
    if is_rice:
        if mode == "normal":
            # 水稻正常灌溉对照(固定定额 460 mm)
            schedule = _build_rice_schedule(sim_year, RICE_NORMAL_DEPTHS)
            return IrrigationManagement(
                irrigation_method=3, Schedule=schedule,
                MaxIrr=100, WetSurf=100, AppEff=90,
            )
        if mode == "paddy":
            # 水稻水层管理方案(指定定额梯度)
            if depths is None:
                raise ValueError("水稻 mode='paddy' 必须提供 depths 参数")
            schedule = _build_rice_schedule(sim_year, depths)
            return IrrigationManagement(
                irrigation_method=3, Schedule=schedule,
                MaxIrr=100, WetSurf=100, AppEff=90,
            )
        # mode='smt' 用于水稻时退化为正常灌溉(SMT 不适合淹水水稻)
        schedule = _build_rice_schedule(sim_year, RICE_NORMAL_DEPTHS)
        return IrrigationManagement(
            irrigation_method=3, Schedule=schedule,
            MaxIrr=100, WetSurf=100, AppEff=90,
        )

    # ---------- 玉米(旱作) ---------- #
    if mode == "normal":
        schedule = _build_dryland_normal_schedule(crop, sim_year)
        return IrrigationManagement(
            irrigation_method=3, Schedule=schedule,
            MaxIrr=60, WetSurf=100, AppEff=90,
        )

    # SMT 自动调控
    if smt is None:
        # 雨养(无灌溉)——保留接口,主流程不再使用
        return IrrigationManagement(irrigation_method=0)

    return IrrigationManagement(
        irrigation_method=1, SMT=list(smt),
        MaxIrr=60, WetSurf=100, AppEff=90,
    )


def build_field_management(crop: str) -> FieldMngt:
    """田间管理：水稻设置田埂(bund)蓄水；旱田(玉米)默认。"""
    if crop.lower() in ("rice", "paddy", "水稻"):
        # z_bund=0.04m(40mm田埂) 限制关键期(抽穗开花)水层≤40mm
        # 此前 z_bund=0.25m(250mm) 致 2024 抽穗开花期水层最大 135mm,
        # 易触发淹水胁迫影响蒸腾/产量; 降至 40mm 后水层稳定在 20-40mm
        # bund_water=20mm 初始田面有薄水层(返青期起始状态)
        return FieldMngt(bunds=True, z_bund=0.04, bund_water=20.0,
                         mulches=False, sr_inhb=False)
    return FieldMngt()  # 旱田默认


def scenario_summary(crop: str, label: str) -> str:
    """返回方案描述。"""
    c = crop.lower()
    name = ("水稻" if c in ("rice", "paddy", "水稻")
            else "玉米" if c in ("corn", "maize", "玉米")
            else crop)
    return f"{name} {label}"


if __name__ == "__main__":
    for c in ("rice", "corn"):
        print(f"\n=== {c} 灌溉方案 ===")
        for label, smt, mode, depths, stype in get_smt_schemes(c):
            extra = (f" depths={depths}" if depths
                     else f" SMT={smt}" if smt else " (固定日历)")
            print(f"  {label}: mode={mode} type={stype}{extra}")
