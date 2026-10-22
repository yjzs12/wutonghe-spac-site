# -*- coding: utf-8 -*-
"""
SPAC 作物生长模型 —— SMT 灌溉制度优化运行器
==========================================
对应大纲 2.7 模型运行及接口设置 + 灌溉制度优化：

设计:
  2 种作物(水稻/玉米) × 3 年份 × 6 方案(正常灌溉 + 5 档) = 36 次模拟
  5 档方案按灌水量由低到高排列(与 modules.irrigation.SCHEME_LABELS 一致):
    L1 雨养型 → L2 低水量 → L3 中低水量 → L4 适宜水量 → L5 较高水量
  体现灌溉制度优化过程(由计算结果决定,不预设最优档位)。
  通过设定不同生育阶段的水层上下限 / SMT 阈值(从雨养型到较高水量),
  得出不同灌水量时的产量,绘制产量-灌水量响应曲线,
  寻找边际产量拐点(产量达 95% 最大值时的最小灌水量)即为95%峰值节水候选灌溉制度。

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


def run_crop_optimization(crop: str, sim_year: int, application_efficiency_pct: float=90.) -> List[SimulationResult]:
    """对单个作物在指定年份运行全部灌溉方案。

    水稻: 正常灌溉对照 + 5 档水层调控方案
    玉米: 正常灌溉对照 + 5 档 SMT 调控方案
    """
    schemes = get_smt_schemes(crop)
    weather_csv = get_weather_csv_path(sim_year)
    print(f"\n[{CROP_CN[crop]} {sim_year}年] 共 {len(schemes)} 方案,气象数据: {weather_csv}")

    results = []
    for label, smt, mode, depths, scheme_type in schemes:
        # 打印方案信息
        if mode == "normal":
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
            res = run_simulation(crop, label, smt, mode=mode, depths=depths,
                                  scheme_type=scheme_type, sim_year=sim_year,
                                  application_efficiency_pct=application_efficiency_pct)
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


def build_optimization_table(results: List[SimulationResult], sim_year: int) -> pd.DataFrame:
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
            param += "；关键期覆盖：05/15—24水层20→30mm，07/10—08/24水层30→50mm"
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


def find_optimal(summary: pd.DataFrame, yield_target: float=0.95) -> pd.DataFrame:
    """寻找达到所选峰值比例时净灌水最少的候选。

    比例默认为95%，可在90%—100%之间设置；这是保产节水约束，
    不能据此证明产量拐点、经济最优或当地实测最优。

    正常灌溉对照也参与比较，避免100%门槛时隐去实际最高产的对照。
    水稻仍需满足关键期保水资格；不按方案名称预设候选。
    """
    import math
    if not math.isfinite(yield_target) or not .90<=yield_target<=1.:
        raise ValueError("保产比例须在90%—100%之间")
    optimal_rows = []
    for crop_cn, sub in summary.groupby("作物"):
        opt_only = sub[sub["方案类型"].isin(["水层调控", "水层管理", "SMT调控", "正常灌溉对照"])].copy()
        if opt_only.empty:
            continue
        opt_only = opt_only.sort_values(["累计灌溉量(mm)","最终籽粒产量(t/ha)"],ascending=[True,False],kind="stable")
        # Benchmark and candidate pool both include the reference treatment.
        y_max = float(sub["最终籽粒产量(t/ha)"].max())
        if not math.isfinite(y_max) or y_max<=0:
            raise ValueError("峰值产量无效，无法筛选节水候选")
        threshold = yield_target * y_max
        if "推荐候选" in opt_only:
            opt_only=opt_only[opt_only["推荐候选"].eq(True)]
        if opt_only.empty:
            continue
        # 找到第一个达到 95% 最大产量的点(按灌水量升序)
        reached = opt_only[opt_only["最终籽粒产量(t/ha)"] >= threshold-1e-9]
        if reached.empty:
            continue
        optimal = reached.iloc[0]
        optimal_rows.append({
            "作物": crop_cn,
            "方案类型": optimal["方案类型"],
            "最优方案": optimal["灌溉方案"],
            "候选方案": optimal["灌溉方案"],
            "保产比例(%)": yield_target*100,
            "较峰值减产(%)": (1-optimal["最终籽粒产量(t/ha)"]/y_max)*100,
            "判定性质": "给定气象与参数下的节水候选，不是实测或经济最优",
            "灌溉参数": optimal["灌溉参数"],
            "累计灌溉量(mm)": optimal["累计灌溉量(mm)"],
            "累计毛灌溉量估算(mm)": optimal["累计毛灌溉量估算(mm)"],
            "灌水口径": optimal["灌水口径"],
            "产量(t/ha)": optimal["最终籽粒产量(t/ha)"],
            "最大产量(t/ha)": y_max,
            "产量达最大百分比(%)": optimal["最终籽粒产量(t/ha)"] / y_max * 100,
            "灌溉WUE(kg/m³)": None if pd.isna(optimal["灌溉水分利用效率(kg/m³)"]) else float(optimal["灌溉水分利用效率(kg/m³)"]),
            **{col:optimal.get(col) for col in ("利用系数类型","灌溉水利用系数","水源取水量估算(mm)","取水量估算(m³/亩)") if col in optimal.index},
        })
    return pd.DataFrame(optimal_rows)


# --------------------------------------------------------------------------- #
#  可视化
# --------------------------------------------------------------------------- #
def plot_yield_irrigation_curve(results: List[SimulationResult], crop: str,
                                  sim_year: int, savepath: str):
    """绘制产量-灌水量响应曲线(灌溉制度优化核心图)。

    水稻: 5 档水层调控方案(不显示正常灌溉对照)
    玉米: 5 档 SMT 调控方案(不显示正常灌溉对照)
    点标注: 玉米标SMT阈值, 水稻标定额合计
    """
    if not results:
        return
    fig, ax1 = plt.subplots(figsize=(12, 7))

    # 仅保留优化方案(水层调控/水层管理/SMT调控), 不显示正常灌溉对照
    opt_res = sorted(
        [r for r in results if r.scheme_type in ("水层调控", "水层管理", "SMT调控")],
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
        return f"{scheme_id(r.label)} {role}\n{r.summary.get('累计灌溉量(mm)', 0):.0f}" if crop=="rice" else f"{r.summary.get('累计灌溉量(mm)', 0):.0f}"

    labels_opt = [_point_label(r) for r in opt_res]

    # 曲线标签按作物类型选择
    curve_label = ("水层调控方案产量" if crop == "rice" else "SMT方案产量")
    ax1.plot(irr_opt, yld_opt, "o-", color="seagreen", linewidth=2.2, markersize=10,
             label=curve_label, zorder=3)
    ax1.set_xlabel("累计净灌溉量（mm）", fontsize=12)
    ax1.set_ylabel("稻谷产量（14.5%含水率，t/ha）" if crop == "rice" else "干籽粒产量 (t/ha)", color="seagreen", fontsize=12)
    ax1.tick_params(axis="y", labelcolor="seagreen")
    ax1.grid(True, alpha=0.3)

    # 标注每个点的实际灌水量（相邻点距离过近时上下交替，避免文字重叠）
    for i, (x, y, lab) in enumerate(zip(irr_opt, yld_opt, labels_opt)):
        too_close = i > 0 and abs(x - irr_opt[i - 1]) < 40
        ax1.annotate(lab, (x, y), textcoords="offset points",
                     xytext=(8, -18 if too_close else 8), fontsize=8)

    # 水稻纵轴从零开始，不能把湿年微小波动放大成显著增产。
    if crop == "rice":
        x_min = min(irr_opt) - 50
        x_max = max(irr_opt) + 50
        ax1.set_xlim(x_min, x_max)
        ax1.set_ylim(0, max(8.5, max(yld_opt)*1.10))

    # 次轴: WUE
    ax2 = ax1.twinx()
    ax2.plot(irr_opt, wue_opt, "s--", color="coral", linewidth=1.5, markersize=7,
             label="WUE", alpha=0.7)
    ax2.set_ylabel("水分利用效率 (kg/m³)", color="coral", fontsize=12)
    ax2.tick_params(axis="y", labelcolor="coral")

    ax1.legend(loc="upper left", fontsize=10)
    scheme_desc = "水层调控" if crop == "rice" else "SMT调控"
    plt.title(f"{CROP_CN[crop]} 模拟产量-灌水量响应曲线（{sim_year} 年, {scheme_desc}）",
              fontsize=14, fontweight="bold")
    fig.tight_layout()
    plt.savefig(savepath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  已保存: {savepath}")


def plot_multi_crop_comparison(all_summary: pd.DataFrame, sim_year: int, savepath: str):
    """两作物产量-灌水量对比图。

    水稻用"水层调控"方案, 玉米用"SMT调控"方案, 均通过方案类型列识别。
    """
    fig, ax = plt.subplots(figsize=(12, 7))
    colors = {"水稻": "seagreen", "玉米": "orange"}
    markers = {"水稻": "o", "玉米": "s"}

    for crop_cn, sub in all_summary.groupby("作物"):
        # 优化方案(水层管理 / SMT调控), 排除正常灌溉对照
        opt_sub = sub[sub["方案类型"].isin(["水层调控", "水层管理", "SMT调控"])].sort_values("累计灌溉量(mm)")
        if opt_sub.empty:
            continue
        ax.plot(opt_sub["累计灌溉量(mm)"], opt_sub["最终籽粒产量(t/ha)"],
                f"{markers.get(crop_cn, 'o')}-",
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


def plot_water_dynamics(results: List[SimulationResult], crop: str,
                          sim_year: int, savepath: str):
    """绘制水分动态图(正常灌溉对照方案)。"""
    # 使用正常灌溉对照方案(与冠层/干物质曲线数据来源一致)
    target = next(
        (r for r in results if r.scheme_type == "正常灌溉对照"), None)
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
    ax1.set_title(f"{CROP_CN[crop]} 正常灌溉 水分动态（{sim_year} 年）")
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
    """绘制灌溉制度时序图: 对比 5 档优化方案的每日灌溉量与累积灌溉量。

    上子图: 每日灌溉量(柱状), 不同颜色代表不同方案
    下子图: 累积灌溉量(折线), 直观展示各方案灌水进程差异
    仅保留优化方案(水层管理/SMT调控), 不显示正常灌溉对照。
    """
    opt_res = sorted(
        [r for r in results if r.scheme_type in ("水层调控", "水层管理", "SMT调控")],
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
            lab = f"{r.label}(SMT={r.smt})" if r.smt else r.label
        # Display total irrigation without a separate preparation-water series.
        ax_bar.bar(d["模拟日期"], d["每日总净灌溉量(mm)"], width=bar_width,
                    color=colors[i], alpha=0.75, label=lab)
        # 下子图: 累积灌溉量折线（含泡田定额，与汇总表"累计灌溉量"一致）
        cum_col = d["每日总净灌溉量(mm)"]
        ax_cum.plot(d["模拟日期"], cum_col.cumsum(), "-",
                     color=colors[i], linewidth=1.8, label=lab, alpha=0.9)

    ax_bar.set_ylabel("每日净灌溉量 (mm)", fontsize=11)
    scheme_desc = "水层调控" if crop == "rice" else "SMT调控"
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
    parser = argparse.ArgumentParser(description="SPAC 模型 SMT 灌溉制度优化")
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
    print("  水稻: 对照(官方规程上下限) + 5 档水层调控 —— 6 个方案同一套闭环机制")
    print("  玉米: 对照(固定日历 200mm) + 5 档 SMT调控(土壤水分目标阈值梯度)")
    print("  5 档梯度(灌水量由低到高): "
          + " → ".join(f"{sid} {lab}"
                       for sid, lab in zip(SCHEME_IDS, SCHEME_LABELS)))
    print("最优判据: 产量达 95% 最大值时的最小灌水量(边际产量拐点)")
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
            print(f"\n★ {year} 年95%峰值节水候选灌溉方案(产量达 95% 最大值):")
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
