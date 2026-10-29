# -*- coding: utf-8 -*-
"""
SPAC 作物生长模型 —— 灌溉制度优化运行器
==========================================
对应大纲 2.7 模型运行及接口设置 + 灌溉制度优化：

设计:
  水稻为CF、SWI、AWD、CI四制度，CF兼作常规对照。
  玉米为墒情控制六档，共 6 个方案：
    四阶段全100、全75、全50、全25、雨养、AquaCrop动态最优。
  动态最优搜索四个生育阶段的整数阈值，直接采用最高产、同产量最少净灌水的组合。
  水稻从四制度中选择最高产、同产量净灌水最少的方案。

用法：
  python run_scenarios.py                          # 默认两年(2024, 2025)
  python run_scenarios.py --year 2024              # 仅 2024 年
  python run_scenarios.py --year 2025 --crop corn
"""

from __future__ import annotations

import os
import sys
import argparse
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 中文字体
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
matplotlib.rcParams["axes.unicode_minus"] = False

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config.wutonghe_config import (
    OUTPUT_DIR, FIG_DIR, ensure_dirs, SIM_YEAR,
    get_sim_bounds, get_weather_csv_path,
)
from spac_model import run_simulation, SimulationResult
from modules.irrigation import (
    get_smt_schemes, NORMAL_LABEL, SCHEME_LABELS, SCHEME_IDS, scheme_id,
)
from modules.paddy_control import describe_bands

CROPS = ["rice", "corn"]
CROP_CN = {"rice": "水稻", "corn": "玉米"}


def run_crop_optimization(crop: str, sim_year: int, application_efficiency_pct: float=90., rice_management=None) -> List[SimulationResult]:
    """对单个作物在指定年份运行全部灌溉方案。

    水稻: CF、SWI、AWD、CI四制度
    玉米: 4 档固定墒情阈值 + 雨养 + 动态最优
    """
    schemes = get_smt_schemes(crop)
    weather_csv = get_weather_csv_path(sim_year)
    print(f"\n[{CROP_CN[crop]} {sim_year}年] 共 {len(schemes)} 方案,气象数据: {weather_csv}")

    results = []
    for label, smt, mode, depths, scheme_type in schemes:
        # 打印方案信息
        if mode == "optimize":
            info = "AquaCrop 四阶段阈值搜索"
        elif mode == "rainfed":
            info = "雨养（关闭灌溉）"
        elif mode == "rice_regime":
            from modules.rice_regimes import describe_regime
            info = describe_regime(label)
        elif mode == "normal":
            info = "(固定日历)"
        elif mode == "wlayer":
            # 水稻 CK 自方案 A 起也走 wlayer, 加 [对照] 前缀与优化档区分
            prefix = "[对照] " if scheme_type == "正常灌溉对照" else ""
            info = prefix + "水层上下限: " + describe_bands(smt)
        elif mode == "paddy":
            info = f"定额={depths} 合计={sum(depths)}mm"
        else:  # smt
            info = f"SMT={smt}"
        print(f"\n  → {label} [{scheme_type}] {info}")
        try:
            if mode == "optimize":
                from modules.corn_optimization import optimize_corn
                res = optimize_corn(run_simulation, sim_year, results,
                                    application_efficiency_pct)
                label = res.label
            else:
                res = run_simulation(crop, label, smt, mode=mode, depths=depths,
                                     scheme_type=scheme_type, sim_year=sim_year,
                                     application_efficiency_pct=application_efficiency_pct,
                                     rice_management=rice_management if crop == "rice" else None)
            y = res.summary.get("最终籽粒产量(t/ha)", 0)
            irr = res.summary.get("累计灌溉量(mm)", 0)
            wue = res.summary.get("水分利用效率(kg/m³)", 0)
            print(f"    产量={y:.2f} t/ha  灌溉量={irr:.1f} mm  WUE={wue:.2f} kg/m³")
            # 文件名用稳定英文档位 ID(L1–L5 / 对照 CK), 与中文显示名解耦
            tag = f"{crop}_{scheme_id(label)}_{sim_year}"
            # 先加入结果列表, 再逐个保存 —— 文件被 Excel 锁定时
            # 不影响该方案计入汇总表
            results.append(res)
            try:
                res.save_csv(os.path.join(OUTPUT_DIR, f"daily_{tag}.csv"))
            except PermissionError as e:
                print(f"    ⚠ CSV 保存失败(文件被 Excel 占用, 关闭后可重跑): {e}")
            # 模型原始输出 Excel(4个工作表: crop_growth/water_flux/
            # water_storage/final_stats, 未做任何整理)
            try:
                res.save_original_excel(
                    os.path.join(OUTPUT_DIR, f"original_{tag}.xlsx"))
            except PermissionError as e:
                print(f"    ⚠ 原始Excel保存失败(文件被 Excel 占用, 关闭后可重跑): {e}")
        except Exception as e:
            print(f"    ✗ 失败: {e}")
    return results


def _corn_build_optimization_table(results: List[SimulationResult], sim_year: int) -> pd.DataFrame:
    """构建优化汇总表。"""
    rows = []
    for r in results:
        irr = r.summary.get("累计灌溉量(mm)", 0)
        yld = r.summary.get("最终籽粒产量(t/ha)", 0)
        # 灌溉参数列: SMT 方案显示阈值; 水层调控显示阶段上下限;
        #   正常灌溉对照按作物区分 —— 水稻自方案 A(2026-10-21) 起也走水层调控,
        #   玉米仍是固定日历 200mm。
        if r.mode == "rice_regime":
            from modules.rice_regimes import describe_regime
            param = describe_regime(r.label)
        elif r.mode=="rainfed":
            param="雨养（关闭灌溉）" + ("；四阶段阈值=(0,0,0,0)，不触发自动补水" if r.label.startswith("最优（") else "")
        elif r.scheme_type == "墒情控制":
            param = f"SMT={r.smt}"
        elif r.scheme_type == "水层调控":
            param = describe_bands(r.smt)
        elif r.scheme_type == "水层管理":
            param = f"定额={r.depths} 合计={sum(r.depths)}mm"
        elif r.scheme_type == "正常灌溉对照" and r.smt:
            param = "对照·" + describe_bands(r.smt)
        else:  # 正常灌溉对照(玉米) —— 固定日历
            param = "固定日历"
        if r.crop=="rice" and r.summary.get("关键期保护方案"):
            from config.crop_calendar import active_reference_date
            from datetime import timedelta
            day = lambda md: active_reference_date("rice", md, sim_year)
            param += (f"；关键期覆盖：{day('05-15'):%m/%d}—{day('05-25')-timedelta(days=1):%m/%d}水层20→30mm，"
                      f"{day('07-10'):%m/%d}—{day('08-25')-timedelta(days=1):%m/%d}水层30→50mm")
        rows.append({
            "年份": sim_year,
            "作物": CROP_CN.get(r.crop, r.crop),
            "方案类型": r.scheme_type,
            "灌溉方案": r.label,
            "灌溉参数": param,
            # 泡田净补水含储水、蒸发、渗漏并扣实际降雨；毛量另列。
            "泡田定额(mm)": r.summary.get("泡田定额(mm)", 0.0),
            **{col:r.summary.get(col,0.) for col in ("泡田储水增量(mm)","泡田蒸发量(mm)","泡田渗漏量(mm)","泡田降雨量(mm)","泡田溢流量(mm)")},
            "补充灌溉量(mm)": r.summary.get("补充灌溉量(mm)", irr),
            "累计灌溉量(mm)": irr,
            "补充毛灌溉量(mm)": r.summary.get("补充毛灌溉量(mm)", 0.0),
            "累计毛灌溉量估算(mm)": r.summary.get("累计毛灌溉量估算(mm)", 0.0),
            "田间灌水效率(%)": r.summary.get("田间灌水效率(%)", 0.0),
            "灌水口径": "田间净量（含泡田储水、蒸发、渗漏，扣泡田降雨；不含渠系损失）" if r.crop=="rice" else "田间净量（不含渠系损失）",
            **{key: r.summary[key] for key in ("制度说明", "主动排水量(mm)", "扩展胁迫计算天数", "衰老胁迫计算天数", "气孔胁迫计算天数", "最大水量平衡残差(mm)") if key in r.summary},
            "方案用途": "四制度水分过程试算（未率定）" if r.crop=="rice" else "AquaCrop优化" if "墒情优化" in r.summary else "墒情对比",
            "关键期无明水天数": r.summary.get("关键期无明水天数",0),
            "推荐候选": r.label.startswith("最优（") if r.crop == "corn" else False,
            "零灌溉对照": r.mode=="rainfed",
            "搜索峰值产量(t/ha)": r.summary.get("墒情优化", {}).get("peak_yield_t_ha"),
            "最终籽粒产量(t/ha)": yld,
            "最终干籽粒产量(t/ha)": r.summary.get("最终干籽粒产量(t/ha)", yld),
            "产量含水率(%)": r.summary.get("产量含水率(%)", 0.0),
            "产量口径": "14.5%含水率稻谷（暂定）" if r.crop == "rice" else "干籽粒",
            "全季蒸腾蒸发量(mm)": r.summary.get("全季蒸腾蒸发量(mm)", 0),
            "最终干物质量(t/ha)": r.summary.get("最终干物质量(t/ha)", 0),
            "水分利用效率(kg/m³)": r.summary.get("水分利用效率(kg/m³)", 0),
            "灌溉水分利用效率(kg/m³)": (
                yld * 100 / irr if irr > 0 else None  # 零灌溉时该比值无定义
            ),
        })
    return pd.DataFrame(rows)


def _rice_build_optimization_table(results: List[SimulationResult], sim_year: int) -> pd.DataFrame:
    """构建优化汇总表。"""
    rows = []
    for r in results:
        irr = r.summary.get("累计灌溉量(mm)", 0)
        yld = r.summary.get("最终籽粒产量(t/ha)", 0)
        # 灌溉参数列: SMT 方案显示阈值; 水层调控显示阶段上下限;
        #   正常灌溉对照按作物区分 —— 水稻自方案 A(2026-10-21) 起也走水层调控,
        #   玉米仍是固定日历 200mm。
        if r.mode=="rainfed":
            param="零灌溉，不设置自动补水"
        elif r.scheme_type == "SMT调控":
            param = f"SMT={r.smt}"
        elif r.scheme_type == "水层调控":
            param = describe_bands(r.smt)
        elif r.scheme_type == "水层管理":
            param = f"定额={r.depths} 合计={sum(r.depths)}mm"
        elif r.scheme_type == "正常灌溉对照" and r.smt:
            param = "对照·" + describe_bands(r.smt)
        else:  # 正常灌溉对照(玉米) —— 固定日历
            param = "固定日历"
        if r.crop=="rice" and r.summary.get("关键期保护方案"):
            param += "；候选V6：返青10→30mm；开花保护30→50mm，按作物HIstartCD/FloweringCD及已发生延迟对齐；R为控制层实际含水量/饱和含水量"
        rows.append({
            **{col:r.summary.get(col,0.) for col in ("犁底层底累计通量(mm)","生育期深层渗漏(mm)","管理排水量(mm)","补水后目标未达天数","暂态积水天数","控制期有明水天数")},
            **{key: r.summary.get(key) for key in ("全模拟时段蒸腾蒸发量(mm)", "生育期蒸腾蒸发量(mm)", "模型冠层衰老终止", "模型生理成熟终止", "模拟生育天数", "配置成熟日数", "现场验证通过")},
            "方案ID": scheme_id(r.label),
            "参数版本": "wutonghe-rice-v6-20261023" if r.crop=="rice" else "corn-unchanged",
            "控制层深度(m)": r.summary.get("控制层深度(m)"),
            "年份": sim_year,
            "作物": CROP_CN.get(r.crop, r.crop),
            "方案类型": r.scheme_type,
            "灌溉方案": r.label,
            "灌溉参数": param,
            # 泡田净补水含储水、蒸发、渗漏并扣实际降雨；毛量另列。
            "泡田定额(mm)": r.summary.get("泡田定额(mm)", 0.0),
            **{col:r.summary.get(col,0.) for col in ("泡田储水增量(mm)","泡田蒸发量(mm)","泡田渗漏量(mm)","泡田降雨量(mm)","泡田溢流量(mm)")},
            "补充灌溉量(mm)": r.summary.get("补充灌溉量(mm)", irr),
            "累计灌溉量(mm)": irr,
            "补充毛灌溉量(mm)": r.summary.get("补充毛灌溉量(mm)", 0.0),
            "累计毛灌溉量估算(mm)": r.summary.get("累计毛灌溉量估算(mm)", 0.0),
            "田间灌水效率(%)": r.summary.get("田间灌水效率(%)", 0.0),
            "灌水口径": "田间净量（含泡田储水、蒸发、渗漏，扣泡田降雨；不含渠系损失）" if r.crop=="rice" else "田间净量（不含渠系损失）",
            "方案用途": "保水约束候选" if r.crop=="rice" and r.summary.get("关键期保护方案") else "缺水对比试验" if r.crop=="rice" else "SMT对比",
            "关键期无明水天数": r.summary.get("关键期无明水天数",0),
            "推荐候选": bool(r.summary.get("推荐候选",r.crop!="rice")),
            "零灌溉对照": r.mode=="rainfed",
            "最终籽粒产量(t/ha)": yld,
            "最终干籽粒产量(t/ha)": r.summary.get("最终干籽粒产量(t/ha)", yld),
            "产量含水率(%)": r.summary.get("产量含水率(%)", 0.0),
            "产量口径": "14.5%含水率稻谷（暂定）" if r.crop == "rice" else "干籽粒",
            "全季蒸腾蒸发量(mm)": r.summary.get("全季蒸腾蒸发量(mm)", 0),
            "最终干物质量(t/ha)": r.summary.get("最终干物质量(t/ha)", 0),
            "水分利用效率(kg/m³)": r.summary.get("水分利用效率(kg/m³)", 0),
            "灌溉水分利用效率(kg/m³)": (
                yld * 100 / irr if irr > 0 else None  # 零灌溉时该比值无定义
            ),
        })
    return pd.DataFrame(rows)


def build_optimization_table(results, sim_year):
    parts = []
    for crop, builder in (("rice", _rice_build_optimization_table), ("corn", _corn_build_optimization_table)):
        group = [r for r in results if r.crop == crop]
        if group:
            parts.append(builder(group, sim_year))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _corn_find_optimal(summary: pd.DataFrame) -> pd.DataFrame:
    """直接采用玉米模型算出的最优档位；水稻四制度待田间率定。"""
    rows = []
    for crop_cn, sub in summary.groupby("作物"):
        if crop_cn == "水稻":
            continue
        dynamic = sub[sub["灌溉方案"].str.startswith("最优（")]
        if dynamic.empty:
            raise ValueError("玉米缺少模型最优档位，不能从固定档位代选")
        if len(dynamic) != 1:
            raise ValueError("玉米最优档位不唯一")
        optimal = dynamic.iloc[0]
        rows.append({
            "作物": crop_cn,
            "方案类型": optimal["方案类型"],
            "最优方案": optimal["灌溉方案"],
            "判定性质": "AquaCrop搜索最高产、同等产量最少净灌水的SMT组合",
            "灌溉参数": optimal["灌溉参数"],
            "累计灌溉量(mm)": optimal["累计灌溉量(mm)"],
            "累计毛灌溉量估算(mm)": optimal["累计毛灌溉量估算(mm)"],
            "灌水口径": optimal["灌水口径"],
            "产量(t/ha)": optimal["最终籽粒产量(t/ha)"],
            "最大产量(t/ha)": optimal["搜索峰值产量(t/ha)"],
            "灌溉WUE(kg/m³)": None if pd.isna(optimal["灌溉水分利用效率(kg/m³)"]) else float(optimal["灌溉水分利用效率(kg/m³)"]),
            **{col: optimal.get(col) for col in ("利用系数类型", "灌溉水利用系数", "水源取水量估算(mm)", "取水量估算(m³/亩)") if col in optimal.index},
        })
    return pd.DataFrame(rows)


def _rice_optimal_row(sub):
    from modules.irrigation import RICE_LABELS
    pool = sub[sub["灌溉方案"].isin(RICE_LABELS)].copy()
    if len(pool) != len(RICE_LABELS) or pool["灌溉方案"].nunique() != len(RICE_LABELS):
        raise ValueError("水稻四制度结果不完整，无法选取最优制度")
    if not np.isfinite(pool[["最终籽粒产量(t/ha)", "累计灌溉量(mm)"]].to_numpy(dtype=float)).all():
        raise ValueError("水稻产量或灌水量无效")
    return pool.sort_values(["最终籽粒产量(t/ha)", "累计灌溉量(mm)"],
                            ascending=[False, True], kind="stable").iloc[0]


def _rice_find_optimal(summary: pd.DataFrame) -> pd.DataFrame:
    """Choose among the four supplied regimes by yield, then net irrigation."""
    from modules.irrigation import RICE_LABELS
    optimal_rows = []
    for crop_cn, sub in summary.groupby("作物"):
        optimal = _rice_optimal_row(sub)
        y_max = float(optimal["最终籽粒产量(t/ha)"])
        optimal_rows.append({
            "作物": crop_cn,
            "方案类型": optimal["方案类型"],
            "最优方案": optimal["灌溉方案"],
            "判定性质": "四制度最高产、同产量净灌水最少",
            "较峰值减产(%)": (1-(optimal["最终籽粒产量(t/ha)"]/y_max if y_max else 1))*100,
            "灌溉参数": optimal["灌溉参数"],
            "累计灌溉量(mm)": optimal["累计灌溉量(mm)"],
            "累计毛灌溉量估算(mm)": optimal["累计毛灌溉量估算(mm)"],
            "灌水口径": optimal["灌水口径"],
            "产量(t/ha)": optimal["最终籽粒产量(t/ha)"],
            "最大产量(t/ha)": y_max,
            "产量达最大百分比(%)": (optimal["最终籽粒产量(t/ha)"] / y_max if y_max else 1) * 100,
            "灌溉WUE(kg/m³)": None if pd.isna(optimal["灌溉水分利用效率(kg/m³)"]) else float(optimal["灌溉水分利用效率(kg/m³)"]),
            **{col:optimal.get(col) for col in ("利用系数类型","灌溉水利用系数","水源取水量估算(mm)","取水量估算(m³/亩)") if col in optimal.index},
        })
    return pd.DataFrame(optimal_rows)

def find_optimal(summary):
    parts = []
    for crop, finder in (("水稻", _rice_find_optimal), ("玉米", _corn_find_optimal)):
        group = summary[summary["作物"].eq(crop)]
        if not group.empty:
            result = finder(group)
            if not result.empty:
                parts.append(result)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# --------------------------------------------------------------------------- #
#  可视化
# --------------------------------------------------------------------------- #
def plot_yield_irrigation_curve(results: List[SimulationResult], crop: str,
                                  sim_year: int, savepath: str):
    """绘制产量-灌水量响应曲线(灌溉制度优化核心图)。

    水稻: 四制度（含CF常规对照）
    玉米: 四档固定阈值、雨养和动态最优共六档墒情控制(不显示正常灌溉对照)
    点标注: 玉米标SMT阈值, 水稻标定额合计
    """
    if not results:
        return
    fig, ax1 = plt.subplots(figsize=(12, 7))

    # 仅保留优化方案(水层调控/水层管理/墒情控制), 不显示正常灌溉对照
    opt_res = sorted(
        [r for r in results if crop == "rice" or r.scheme_type in ("水层调控", "水层管理", "墒情控制")],
        key=lambda r: r.summary.get("累计灌溉量(mm)", 0)
    )

    if not opt_res:
        print(f"  无优化方案数据,跳过绘图: {savepath}")
        plt.close()
        return

    # 优化方案曲线
    irr_opt = [r.summary.get("累计灌溉量(mm)", 0) for r in opt_res]
    yld_opt = [r.summary.get("最终籽粒产量(t/ha)", 0) for r in opt_res]
    wue_opt = [r.summary.get("水分利用效率(kg/m³)", 0) for r in opt_res]

    # 点标注: 玉米用SMT阈值, 水稻标该档实际累计灌水量
    def _point_label(r: SimulationResult) -> str:
        # 横轴就是累计灌水量，标注统一用「实际灌水量」；
        # SMT 阈值 / 水层上下限等设计参数见汇总表"灌溉参数"列，图上不再重复。
        role="候选" if r.summary.get("推荐候选") else "试验"
        return f"{scheme_id(r.label)} {role}\n{r.summary.get('累计灌溉量(mm)', 0):.0f}" if crop=="rice" else f"{r.label}\n{r.summary.get('累计灌溉量(mm)', 0):.0f}mm"

    labels_opt = [_point_label(r) for r in opt_res]

    # 曲线标签按作物类型选择
    curve_label = ("水层调控方案产量" if crop == "rice" else "墒情控制方案产量")
    ax1.plot(irr_opt, yld_opt, "o-", color="seagreen", linewidth=2.2, markersize=10,
             label=curve_label, zorder=3)
    ax1.set_xlabel("累计净灌溉量（mm）", fontsize=12)
    ax1.set_ylabel("稻谷产量（14.5%含水率，t/ha）" if crop == "rice" else "干籽粒产量 (t/ha)", color="seagreen", fontsize=12)
    ax1.tick_params(axis="y", labelcolor="seagreen")
    ax1.grid(True, alpha=0.3)

    # 标注每个点的实际灌水量（相邻点距离过近时上下交替，避免文字重叠）
    for i, (x, y, lab) in enumerate(zip(irr_opt, yld_opt, labels_opt)):
        ax1.annotate(lab, (x, y), textcoords="offset points",
                     xytext=((-55, -35) if i % 2 == 0 else (15, 30)), fontsize=8,
                     arrowprops=dict(arrowstyle="-", color="#888", lw=.6, shrinkB=6))

    # 水稻纵轴从零开始，不能把湿年微小波动放大成显著增产。
    if crop == "rice":
        x_min = min(irr_opt) - 50
        x_max = max(irr_opt) + 50
        ax1.set_xlim(x_min, x_max)
        ax1.set_ylim(0, max(8.5, max(yld_opt)*1.10))
    else:
        # Four-stage labels need headroom above the high-yield plateau.
        ax1.margins(x=.08, y=.15)

    # 次轴: WUE
    ax2 = ax1.twinx()
    ax2.plot(irr_opt, wue_opt, "s--", color="coral", linewidth=1.5, markersize=7,
             label="WUE", alpha=0.7)
    ax2.set_ylabel("水分利用效率 (kg/m³)", color="coral", fontsize=12)
    ax2.tick_params(axis="y", labelcolor="coral")

    ax1.legend(loc="upper left", fontsize=10)
    ax2.legend(loc="upper right", fontsize=10)
    scheme_desc = "水层调控" if crop == "rice" else "墒情控制"
    plt.title(f"{CROP_CN[crop]} 模拟产量-灌水量响应曲线（{sim_year} 年, {scheme_desc}）",
              fontsize=14, fontweight="bold")
    fig.tight_layout()
    plt.savefig(savepath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  已保存: {savepath}")


def plot_multi_crop_comparison(all_summary: pd.DataFrame, sim_year: int, savepath: str):
    """两作物产量-灌水量对比图。

    水稻用"水层调控"方案, 玉米用"墒情控制"方案, 均通过方案类型列识别。
    """
    fig, ax = plt.subplots(figsize=(12, 7))
    colors = {"水稻": "seagreen", "玉米": "orange"}
    markers = {"水稻": "o", "玉米": "s"}

    for crop_cn, sub in all_summary.groupby("作物"):
        # 优化方案(水层管理 / 墒情控制), 排除正常灌溉对照
        opt_sub = (sub if crop_cn == "水稻" else sub[sub["方案类型"].isin(["水层调控", "水层管理", "墒情控制"])]).sort_values("累计灌溉量(mm)")
        if opt_sub.empty:
            continue
        ax.plot(opt_sub["累计灌溉量(mm)"], opt_sub["最终籽粒产量(t/ha)"],
                markers.get(crop_cn, "o") + "-",
                color=colors.get(crop_cn, "blue"),
                linewidth=2, markersize=9, label=crop_cn + ("（14.5%水分）" if crop_cn == "水稻" else "（干重）"), alpha=0.85)

    ax.set_xlabel("累计净灌溉量 (mm)", fontsize=12)
    ax.set_ylabel("籽粒产量 (t/ha)", fontsize=12)
    ax.set_title(f"两作物灌溉制度优化对比（{sim_year} 年）",
                 fontsize=14, fontweight="bold")
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(savepath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  已保存: {savepath}")


def select_water_result(results, crop):
    """Use the same winner as the summary: highest yield, then least net water."""
    rows = [r for r in results if r.crop == crop]
    if crop == "rice":
        table = pd.DataFrame([{"灌溉方案": r.label, **{key: r.summary[key] for key in
                              ("最终籽粒产量(t/ha)", "累计灌溉量(mm)")}} for r in rows],
                             columns=["灌溉方案", "最终籽粒产量(t/ha)", "累计灌溉量(mm)"])
        name = _rice_optimal_row(table)["灌溉方案"]
        return next(r for r in rows if r.label == name)
    return next((r for r in rows if "墒情优化" in r.summary), None)


def plot_water_dynamics(results: List[SimulationResult], crop: str,
                          sim_year: int, savepath: str):
    """绘制本次模拟最优灌溉方案的水分动态。"""
    target = select_water_result(results, crop)
    if target is None:
        return

    d = target.daily
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    ax1, ax2 = axes
    if crop == "rice":
        ax1.plot(d["模拟日期"], d["田间水层深度(mm)"], color="steelblue",
                 label="田间水层", linewidth=1.2)
        ax1.set_ylabel("田间水层深度 (mm)")
    else:
        ax1.plot(d["模拟日期"], d["第1层土壤含水率"], color="saddlebrown",
                 label="第1层土壤含水率", linewidth=1.2)
        ax1.set_ylabel("第1层土壤含水率 (vol)")
    ax1.set_title(f"{CROP_CN[crop]} 最优方案：{target.label} 水分动态（{sim_year} 年）")
    ax1.legend(loc="upper right")
    ax1.grid(True, alpha=0.3)
    ax2.plot(d["模拟日期"], d["蒸腾蒸发量(mm)"], color="seagreen",
             label="蒸腾蒸发量 ET", linewidth=1.2)
    ax2.fill_between(d["模拟日期"], 0, d["蒸腾蒸发量(mm)"], alpha=0.3, color="seagreen")
    ax2.set_ylabel("蒸腾蒸发量 (mm)")
    ax2.set_xlabel("日期")
    ax2.legend(loc="upper right")
    ax2.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(savepath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  已保存: {savepath}")


def plot_irrigation_schedule(results: List[SimulationResult], crop: str,
                               sim_year: int, savepath: str):
    """绘制灌溉制度时序图: 对比各灌溉方案的每日灌溉量与累积灌溉量。

    上子图: 每日灌溉量(柱状), 不同颜色代表不同方案
    下子图: 累积灌溉量(折线), 直观展示各方案灌水进程差异
    仅保留优化方案(水层管理/墒情控制), 不显示正常灌溉对照。
    """
    opt_res = sorted(
        [r for r in results if crop == "rice" or r.scheme_type in ("水层调控", "水层管理", "墒情控制")],
        key=lambda r: r.summary.get("累计灌溉量(mm)", 0)
    )
    if not opt_res:
        return

    fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True)
    ax_bar, ax_cum = axes
    # 配色: 胁迫重→浅色, 灌溉足→深色
    colors = plt.cm.viridis(np.linspace(0.15, 0.85, len(opt_res)))

    bar_width = 1.0  # 每日柱宽
    n_schemes = len(opt_res)
    # 每日柱在x方向轻微错开, 避免重叠
    offsets = np.linspace(-0.4, 0.4, n_schemes) if n_schemes > 1 else [0]

    for i, r in enumerate(opt_res):
        d = r.daily
        # 点标签: 统一用「实际累计灌水量」—— 水层调控方案没有 depths,
        #   用 sum(r.depths) 会一律显示 0 mm(旧 bug, 2026-10-21 修)
        if crop == "rice":
            total = r.summary.get("累计灌溉量(mm)", 0)
            lab = f"{r.label}({total:.0f}mm)"
        else:
            lab = r.label
        # Display total irrigation without a separate preparation-water series.
        ax_bar.bar(d["模拟日期"], d["每日总净灌溉量(mm)"], width=bar_width,
                    color=colors[i], alpha=0.75, label=lab)
        # 下子图: 累积灌溉量折线（含泡田定额，与汇总表"累计灌溉量"一致）
        cum_col = d["每日总净灌溉量(mm)"]
        ax_cum.plot(d["模拟日期"], cum_col.cumsum(), "-",
                     color=colors[i], linewidth=1.8, label=lab, alpha=0.9)

    ax_bar.set_ylabel("每日净灌溉量 (mm)", fontsize=11)
    scheme_desc = "水层调控" if crop == "rice" else "墒情控制"
    ax_bar.set_title(
        f"{CROP_CN[crop]} 灌溉制度时序对比（{sim_year} 年, {scheme_desc}）",
        fontsize=13, fontweight="bold")
    ax_bar.legend(loc="upper right", fontsize=8, ncol=2)
    ax_bar.grid(True, alpha=0.3, axis="y")

    ax_cum.set_ylabel("累计净灌溉量 (mm)", fontsize=11)
    ax_cum.set_xlabel("日期", fontsize=11)
    ax_cum.legend(loc="upper left", fontsize=8, ncol=2)
    ax_cum.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(savepath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  已保存: {savepath}")


# --------------------------------------------------------------------------- #
#  主入口
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="SPAC 模型墒情与水层灌溉制度优化")
    parser.add_argument("--crop", choices=["rice", "corn", "all"],
                        default="all", help="作物选择")
    parser.add_argument("--year", type=int, default=None,
                        help="单年份运行(如 2024)")
    parser.add_argument("--years", default=None,
                        help="多年份运行(如 2024,2025)")
    args = parser.parse_args()

    ensure_dirs()

    if args.years:
        years = [int(y.strip()) for y in args.years.split(",")]
    elif args.year:
        years = [args.year]
    else:
        years = [2024, 2025]

    crops = CROPS if args.crop == "all" else [args.crop]

    print("=" * 70)
    print("SPAC 作物生长模型 —— 灌溉制度优化")
    print(f"作物: {', '.join(CROP_CN[c] for c in crops)}")
    print(f"年份: {years}")
    print("方案设计:")
    print("  水稻: 常规淹水CF、浅湿灌溉SWI、干湿交替AWD、控制灌溉CI —— 四制度过程比较")
    print("  玉米: 对照(固定净量144mm) + 四档墒情阈值(100/75/50/25) + 雨养 + AquaCrop动态最优")
    print("  水稻四制度(不预设水量或产量排序): "
          + " → ".join(f"{sid} {lab}"
                       for sid, lab in zip(SCHEME_IDS, SCHEME_LABELS)))
    print("最优判据: 玉米直接采用AquaCrop最优档位（最高产、同产量最少净灌水）；水稻四制度尚未田间率定，不输出农艺最优推荐")
    print("=" * 70)

    all_summaries = []

    for year in years:
        print(f"\n{'#'*70}\n#  {year} 年运行\n{'#'*70}")
        year_results = []
        for crop in crops:
            results = run_crop_optimization(crop, year)
            year_results.extend(results)
            plot_yield_irrigation_curve(
                results, crop, year,
                os.path.join(FIG_DIR, f"yield_irr_curve_{crop}_{year}.png"))
            plot_water_dynamics(
                results, crop, year,
                os.path.join(FIG_DIR, f"water_dynamics_{crop}_normal_{year}.png"))
            plot_irrigation_schedule(
                results, crop, year,
                os.path.join(FIG_DIR, f"irrigation_schedule_{crop}_{year}.png"))

        summary = build_optimization_table(year_results, year)
        all_summaries.append(summary)

        year_csv = os.path.join(OUTPUT_DIR, f"irrigation_optimization_{year}.csv")
        summary.to_csv(year_csv, index=False, encoding="utf-8-sig")
        print(f"\n{year} 年汇总已保存: {year_csv}")
        print(summary.to_string(index=False))

        optimal = find_optimal(summary)
        if not optimal.empty:
            opt_csv = os.path.join(OUTPUT_DIR, f"optimal_irrigation_{year}.csv")
            optimal.to_csv(opt_csv, index=False, encoding="utf-8-sig")
            print(f"\n★ {year} 年最佳灌溉方式（AquaCrop最优档位）:")
            print(optimal.to_string(index=False))

        plot_multi_crop_comparison(
            summary, year,
            os.path.join(FIG_DIR, f"multi_crop_comparison_{year}.png"))

    if len(all_summaries) > 1:
        all_summary = pd.concat(all_summaries, ignore_index=True)
        all_csv = os.path.join(OUTPUT_DIR, "irrigation_optimization_all_years.csv")
        all_summary.to_csv(all_csv, index=False, encoding="utf-8-sig")
        print(f"\n多年汇总已保存: {all_csv}")

    print(f"\n全部完成。输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
