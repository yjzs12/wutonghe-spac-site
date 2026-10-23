# -*- coding: utf-8 -*-
"""
灌溉管理模块 —— 灌溉制度优化
==================================
对应大纲 2.5 灌溉管理模块：
  通过设定不同生育期水层上下限 / SMT 阈值,模拟从"雨养型(重胁迫)"到"较高水量(过量灌溉)"
  的对比梯度,得出不同灌水量时的模拟产量响应。

方案设计(按作物类型分别采用不同优化策略):
  (1) 水稻 —— 水层上下限闭环调控(6 个方案, 逐日反馈改 Schedule, 内核零改动)
      淹水水稻需维持田面水层, SMT(土壤水分目标)不适用;
      各方案给「各生育阶段水层下限/上限」(见 modules/paddy_control.py),
      降到下限即灌到上限, 形成"雨养型→适宜水量→较高水量"的水量梯度。
      正常灌溉对照(CK)自 2026-10-21 起同样走闭环: 上下限取省级规程的
      「返青/分蘖 0→30、拔节孕穗/抽穗 0→50、灌浆 0→30」, 原「固定日期+
      固定定额 891 mm」的日历仅保留作回退(mode='normal')。
      ⇒ 全工程 6 个方案共用同一套灌溉机制, 差异只体现在上下限表。

  (2) 玉米 —— 墒情控制六档 + 正常灌溉对照
      旱作无水层, 通过 SMT 设定不同生育期土壤水分目标下限
      固定阈值为四阶段全100、全75、全50、全25；雨养关闭灌溉。
      最优四阶段阈值由 AquaCrop 搜索，优先最高产，同产量下尽量减少灌水。
      正常灌溉对照(固定日历)作为基准。

  玉米最优档位直接采用模型搜索结果；水稻等待四个新方案后再确定最优。
  未输入水价、粮价及灌溉成本，不据此宣称经济最优或实测最优。

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
from modules.paddy_control import bands_for, bands_for_ck, describe_bands, CK_BANDS


# =========================================================================== #
#  方案标签与类型常量
# =========================================================================== #

# 水稻 5 档方案中文标签；玉米另用四阶段阈值标签。
# 命名口径("水量档位型"): 直接按灌水量高低命名, 不混用"胁迫/灌溉"两套词;
#   低水量侧 = 水分胁迫重, 高水量侧 = 过量灌溉。
# 2026-10-21 由 7 档压到 5 档: 水稻 7 档实测 L4–L7 产量完全相同(多灌 319mm 换 0 产量),
#   只有 4 个有效信息点; 玉米 7 档高端同样平台化。5 档让相邻档灌水量与产量都错开,
#   避免把重复处理当独立样本做统计。
SCHEME_LABELS = [
    "雨养型", "低水量", "中低水量", "适宜水量", "较高水量",
]

# 与 SCHEME_LABELS 一一对应的稳定英文档位 ID
# 仅用于输出文件名与内部标识, 与中文显示名解耦 ——
# 以后再调整显示名, 不会影响已归档的输出文件名。
# 注: 历史上的 L6/L7 已停用(其输出文件仍保留在归档目录, 名称不受影响)。
SCHEME_IDS = ["L1", "L2", "L3", "L4", "L5"]

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
    if label.startswith("最优（"):
        return "OPT"
    if label == "雨养":
        return "RAIN"
    for level in (100, 75, 50, 25):
        if label == corn_smt_label([level] * 4):
            return f"SMT{level}"
    if label in SCHEME_LABELS:
        return SCHEME_IDS[SCHEME_LABELS.index(label)]
    return str(label)

# 方案类型
TYPE_NORMAL = "正常灌溉对照"   # 基准情景：水稻=水层反馈，玉米=固定日历净144mm，均非实测处理记录
TYPE_PADDY = "水层管理"        # 水稻固定水层管理(旧: 固定日期+固定定额, 已停用但保留)
TYPE_WLAYER = "水层调控"       # 水稻按阶段上下限闭环调控(现行)
TYPE_SMT = "墒情控制"           # 玉米根区土壤水分阈值控制


def corn_smt_label(smt, optimal=False):
    """四个生育阶段的实际阈值，不将显示名称当作计算参数。"""
    return ("最优" if optimal else "") + "（" + "，".join(str(int(v)) for v in smt) + "）"


# =========================================================================== #
#  水稻【旧】固定水层管理方案 —— method=3 + 7 档灌水定额梯度（已停用，仅存档）
#
#  ⚠ 2026-10-21 起水稻 5 档改走 mode='wlayer'（阶段水层上下限闭环），
#     下表 RICE_PADDY_LEVELS 与 RICE_NORMAL_DEPTHS 仅作固定日历对照的存档依据。
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

# 水稻 7 档灌水定额(30 个灌水日期的定额列表, 单位 mm) —— 旧固定日历, 已停用
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

# 水稻【旧】正常灌溉对照的固定日历定额（合计 891 mm）—— 已停用, 仅作回退/存档
#
#  ⚠ 2026-10-21 方案 A 起, CK 改走「官方规程上下限」闭环(paddy_control.CK_BANDS),
#     本表不再参与主流程; 若需回退到旧口径, 把 get_smt_schemes() 里 CK 那行改回
#     (NORMAL_LABEL, None, "normal", None, TYPE_NORMAL) 即可, build_irrigation 的
#     mode='normal' 分支仍完好。
#  实测量级(供对照): 名义 891 mm, 因 5/10 泡田 50 mm 与 5/14 的 25 mm 落在移栽日
#     之前被内核强制归零, 实际入田 816 mm / 27 次(30 个日期 − 晒田 1 个 − 移栽前 2 个)。
#  设计依据: 寒地淹水水稻, 配合Ks=8mm/day的低渗漏土壤
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
    from config.crop_calendar import active_reference_date
    dates = [active_reference_date("rice", f"{m:02d}-{d:02d}", sim_year) for m, d in RICE_IRR_DATES_MD]
    return pd.DataFrame({
        "Date": pd.to_datetime(dates),
        "Depth": depths,
    })


# =========================================================================== #
#  玉米墒情控制 —— 四档固定阈值，另有雨养和动态优化
#
#  设计原理:
#  - 低阈值(雨养型/低水量): 允许土壤水大量消耗 → 产量低 → 优化过程前段
#  - 中阈值: 一组供比较的补灌触发条件，不预设最优档位
#  - 高阈值(较高水量): 几乎无胁迫 → 产量趋于平稳/略降 → 灌水浪费
#
#  SMT 数值语义: 当日 Dr/TAW > 1 - SMT/100 时灌溉(补到田间持水)。
#  ⇒ **SMT 越大 = 允许耗竭越少 = 灌得越多**。
#
#  固定对比档按用户指定的100/75/50/25设置，不预设某档农学最优。
# =========================================================================== #

SMT_LEVELS: Dict[str, List[List[int]]] = {
    "corn": [[level] * 4 for level in (100, 75, 50, 25)],
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
        # 玉米正常灌溉: 4 次灌水, 毛量合计 160 mm → 田间净量 144 mm
        # ★ 2026-10-22 改：原 200 mm 毛（净 180）比灌溉定额高 25–60%。
        #   现按 75% 水文年通用值 96 m³/亩 = 144 mm（净）设定，
        #   毛量 = 144 / AppEff(90%) = 160 mm，按原比例缩放（×0.8）。
        #   依据 data/产量 生育期.xlsx：玉米 50% 75 m³/亩 = 112.5 mm、75% 96 = 144 mm。
        # 关键期(拔节孕穗/抽雄吐丝/灌浆)3次, 苗期1次
        dates = [
            f"{sim_year}/05/15",   # 苗期补水
            f"{sim_year}/06/15",   # 来源表6月中旬拔节的代表日期
            f"{sim_year}/07/25",   # 来源表7月下旬吐丝的代表日期
            f"{sim_year}/08/15",   # 来源表8月中旬灌浆的代表日期
        ]
        depths = [32, 40, 48, 40]   # 毛量合计 160 mm（净 144 mm）
        from config.crop_calendar import active_reference_date
        dates = [active_reference_date("corn", day[5:], sim_year) for day in dates]
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
    """返回作物的全部灌溉方案列表。

    水稻: 正常灌溉对照(官方规程上下限) + 5 档水层调控方案(阶段水层上下限)
    玉米: 固定净定额144mm对照 + 四档固定阈值 + 雨养 + 动态最优

    Returns
    -------
    List of (label, smt, mode, depths, scheme_type)
    """
    c = crop.lower()
    if c in ("rice", "paddy", "水稻"):
        # 水稻: 正常灌溉对照 + 5 档水层调控, **全部 6 个方案走同一套闭环机制**
        #   各档不再给"定额", 而给"各生育阶段水层上下限"(bands),
        #   借 SchemeTuple 的 smt 槽位承载 —— 语义与玉米 SMT 阈值同理:
        #   "括号里写明每个阶段的目标, 由模型自己算灌水量"。
        #   2026-10-21 方案 A: CK 也由「固定日历 891mm」改为「官方规程上下限」
        #   (见 modules/paddy_control.py::CK_BANDS), 但 scheme_type 仍标
        #   TYPE_NORMAL —— 这样 find_optimal() 会正确地把对照排除在优化档之外。
        schemes: List[SchemeTuple] = [
            (NORMAL_LABEL, bands_for_ck(), "wlayer", None, TYPE_NORMAL),
        ]
        for label in SCHEME_LABELS:
            schemes.append((label, bands_for(label), "wlayer", None, TYPE_WLAYER))
        return schemes

    if c in ("corn", "maize", "玉米"):
        key = "corn"
    else:
        raise ValueError(f"未知作物: {crop}")

    # 玉米: 对照单列；四个固定阈值、真正雨养、AquaCrop 动态搜索最优，共六档。
    schemes = [(NORMAL_LABEL, None, "normal", None, TYPE_NORMAL)]
    for smt in SMT_LEVELS[key]:
        schemes.append((corn_smt_label(smt), list(smt), "smt", None, TYPE_SMT))
    schemes.append(("雨养", None, "rainfed", None, TYPE_SMT))
    schemes.append(("最优（运行后计算）", None, "optimize", None, TYPE_SMT))
    return schemes


# =========================================================================== #
#  IrrigationManagement 构建
# =========================================================================== #

def build_irrigation(crop: str, smt: Optional[List[int]] = None,
                     mode: str = "smt",
                     depths: Optional[List[int]] = None,
                     sim_year: Optional[int] = None,
                     application_efficiency_pct: float = 90.) -> IrrigationManagement:
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
    if mode == "optimize":
        raise ValueError("动态最优须先经 AquaCrop 搜索，不能直接作为灌溉管理参数")
    import math
    efficiency=float(application_efficiency_pct)
    if not math.isfinite(efficiency) or not 0<efficiency<=100:
        raise ValueError("田间施加效率须在(0,100]%内")
    if mode=="rainfed":
        if is_rice:
            raise ValueError("水稻缺水试验仍含泡田，不能当作真正零灌溉处理")
        return IrrigationManagement(irrigation_method=0,AppEff=efficiency)
    if sim_year is None:
        from config.wutonghe_config import SIM_YEAR
        sim_year = SIM_YEAR

    # ---------- 水稻 ---------- #
    if is_rice:
        if mode == "normal":
            # 【已由方案 A 取代, 仅保留作回退】水稻旧固定日历对照(891 mm)
            #   现行 CK 走 mode='wlayer' + paddy_control.CK_BANDS
            schedule = _build_rice_schedule(sim_year, RICE_NORMAL_DEPTHS)
            return IrrigationManagement(
                irrigation_method=3, Schedule=schedule,
                MaxIrr=100, WetSurf=100, AppEff=efficiency,
            )
        if mode == "wlayer":
            # 水层闭环调控: 日历全 0, 每天由 PaddyLayerController 回填当日定额
            #   MaxIrr 放到 300 —— 上限 190mm 需毛量 190/0.9≈211mm, 不能被截断
            from config.wutonghe_config import get_sim_bounds
            start, end = get_sim_bounds(sim_year)
            dates = pd.date_range(pd.to_datetime(start), pd.to_datetime(end), freq="D")
            schedule = pd.DataFrame({"Date": dates, "Depth": 0.0})
            return IrrigationManagement(
                irrigation_method=3, Schedule=schedule,
                MaxIrr=300, WetSurf=100, AppEff=efficiency,
            )
        if mode == "paddy":
            # 【旧方案, 保留作回退】水层管理(固定日期 + 指定定额梯度)
            if depths is None:
                raise ValueError("水稻 mode='paddy' 必须提供 depths 参数")
            schedule = _build_rice_schedule(sim_year, depths)
            return IrrigationManagement(
                irrigation_method=3, Schedule=schedule,
                MaxIrr=100, WetSurf=100, AppEff=efficiency,
            )
        # mode='smt' 用于水稻时退化为正常灌溉(SMT 不适合淹水水稻)
        schedule = _build_rice_schedule(sim_year, RICE_NORMAL_DEPTHS)
        return IrrigationManagement(
            irrigation_method=3, Schedule=schedule,
            MaxIrr=100, WetSurf=100, AppEff=efficiency,
        )

    # ---------- 玉米(旱作) ---------- #
    if mode == "normal":
        schedule = _build_dryland_normal_schedule(crop, sim_year)
        # CK is a fixed NET reference scenario (144 mm), not annual observation.
        schedule["Depth"] *= 90./efficiency
        return IrrigationManagement(
            irrigation_method=3, Schedule=schedule,
            MaxIrr=float(schedule["Depth"].max()), WetSurf=100, AppEff=efficiency,
        )

    # SMT 自动调控
    if smt is None:
        # 雨养(无灌溉)——保留接口,主流程不再使用
        return IrrigationManagement(irrigation_method=0,AppEff=efficiency)

    return IrrigationManagement(
        irrigation_method=1, SMT=list(smt),
        MaxIrr=54./(efficiency/100.), WetSurf=100, AppEff=efficiency,
    )


def build_field_management(crop: str) -> FieldMngt:
    """田间管理：水稻设置田埂(bund)蓄水；旱田(玉米)默认。"""
    if crop.lower() in ("rice", "paddy", "水稻"):
        # z_bund=0.20m(200mm田埂) —— 2026-10-21 由 40mm 上调, 用于按「阶段目标
        #   水层」反算灌水定额: 40mm 田埂下维持 20-40mm 水层时, 3天以上的灌水
        #   间隔必然「单次定额大→溢流 / 定额小→落干」两头不可兼得
        #   (实测 2025: 拔节期 45mm/3d 当天溢流 25.9mm)。
        #   200mm 可容纳关键期深水(寒地分蘖期 50-70mm 护苗亦满足), 溢流不再先于
        #   水分亏缺发生, 水层深度重新成为有效的过程变量。
        # 历史: 曾用 0.25m(250mm) 致 2024 抽穗开花期水层最大 135mm; 后降至 40mm。
        # bund_water = 插秧时的田面水层(默认 30mm, 浅水插秧 1-3cm)。
        #   内核在移栽日 `reset_initial_conditions` 中写入
        #   `surface_storage = min(bund_water, z_bund)`，因此该值就是"泡田后插秧"
        #   的初始水层；对应的 150.3mm 泡田定额（0–60cm 饱和 + 水层）在
        #   initial_condition 里计量。2026-10-22 起心土层（60–100cm）不再计入
        #   泡田台账（其重力水 8d 内排空，已计入生育期渗漏）。
        #   历史: 20mm 时移栽日田面过浅, 且剖面取 FC, 相当于"未泡田直接插秧"。
        from modules.initial_condition import PADDY_INIT_WATER_LAYER
        return FieldMngt(bunds=True, z_bund=0.20,
                         bund_water=PADDY_INIT_WATER_LAYER,
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
            if depths:
                extra = f" depths={depths}"
            elif mode == "wlayer":
                extra = f" bands={describe_bands(smt)}"
            elif smt:
                extra = f" SMT={smt}"
            else:
                extra = " (固定日历)"
            print(f"  {label}: mode={mode} type={stype}{extra}")
