# -*- coding: utf-8 -*-
"""
SPAC 典型作物生长模型 —— 主整合
===================================
对应大纲 2.7 模型运行及接口设置：
  将大气交互、土壤水分、作物模拟、灌溉管理、初始条件等模块整合，
  实现全过程动态模拟，并提供输入/输出 API 接口。

输入接口（对应大纲表2）：
  气象、土壤性质、作物参数、灌溉制度、初始条件
输出接口（对应大纲表3）：
  逐日 各层土壤含水率、田间水层深度、冠层覆盖度、蒸腾蒸发量、
  干物质量、籽粒产量、水分利用效率

底层引擎：自建的土壤-作物-大气连续体系（SPAC）求解内核。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Optional, Dict

import numpy as np
import pandas as pd

# 确保可导入同级 config / modules
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from modules.engine_core import SimEngine

from config.wutonghe_config import (
    LAT_RAD, ALTITUDE_M, SIM_START, SIM_END, SIM_YEAR,
    RICE_PLANTING_DATE, RICE_HARVEST_DATE,
    CORN_PLANTING_DATE, CORN_HARVEST_DATE,
    OUTPUT_DIR, REGION_NAME,
    get_sim_bounds, get_weather_csv_path,
)
from modules.atmosphere import build_weather_df
from modules.soil_water import build_soil, describe_soil, get_n_layers
from modules.crop_sim import build_crop, crop_summary
from modules.irrigation import build_irrigation, build_field_management, scenario_summary, get_smt_schemes
from modules.initial_condition import build_initial_water_content, describe_initial_condition

# 修复 SPAC 连续体系引擎紧实层根系生长 bug(详见 modules/engine_fixes.py)
from modules.engine_fixes import apply_patches
apply_patches()


@dataclass
class SimulationConfig:
    """单次模拟配置。"""
    crop: str                          # 'rice' / 'corn'
    label: str                         # 方案标签,如 '正常灌溉对照' / '适宜水量'
    smt: Optional[list] = None          # SMT 阈值列表 [s0,s1,s2,s3]；仅 mode='smt' 使用
    mode: str = "smt"                  # 'smt' / 'normal' / 'paddy'
    depths: Optional[list] = None      # 水稻 8 次灌水定额列表；仅 mode='paddy' 使用
    scheme_type: str = ""              # 方案类型: '正常灌溉对照' / '水层管理' / 'SMT调控'
    sim_year: Optional[int] = None     # 模拟年份；None 时用 SIM_YEAR
    sim_start: str = SIM_START
    sim_end: str = SIM_END
    weather_csv: Optional[str] = None   # 实测气象 CSV 路径（可选）
    initial_moisture: Optional[str] = None

    def __post_init__(self):
        # 若指定了 sim_year，则按年份覆盖 sim_start/sim_end
        if self.sim_year is not None:
            self.sim_start, self.sim_end = get_sim_bounds(self.sim_year)


@dataclass
class SimulationResult:
    """单次模拟结果容器。"""
    crop: str
    label: str                      # 方案标签(如 正常灌溉对照 / 适宜水量)
    smt: Optional[list]             # SMT 阈值(仅 SMT 方案; 其他为 None)
    mode: str                        # 'smt' / 'normal' / 'paddy'
    depths: Optional[list]          # 水稻灌水定额(仅水层管理方案; 其他为 None)
    scheme_type: str                # '正常灌溉对照' / '水层管理' / 'SMT调控'
    sim_year: int                   # 模拟年份
    daily: pd.DataFrame             # 逐日输出
    summary: Dict[str, float] = field(default_factory=dict)
    # 模型原始输出(未整理, 引擎直接产生的 4 个 DataFrame)
    raw_crop_growth: Optional[pd.DataFrame] = None
    raw_water_flux: Optional[pd.DataFrame] = None
    raw_water_storage: Optional[pd.DataFrame] = None
    raw_final_stats: Optional[pd.DataFrame] = None

    def save_csv(self, path: str) -> None:
        self.daily.to_csv(path, index=False, encoding="utf-8-sig")

    def save_original_excel(self, path: str) -> None:
        """导出 模型原始输出为 Excel(4 个工作表, 未做任何整理)。

        工作表:
          crop_growth  : 逐日作物生长(cg_arr 15列: 冠层覆盖度/生物量/收获指数/产量等)
          water_flux   : 逐日水分通量(17列: 地表蓄水/灌溉/入渗/径流/渗漏/蒸发/蒸腾等)
          water_storage: 逐日各土壤 compartment 含水率(th1...thN)
          final_stats  : 季末汇总(收获日期/干鲜产量/产量潜力/灌溉量)
        """
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            if self.raw_crop_growth is not None:
                self.raw_crop_growth.to_excel(
                    writer, sheet_name="crop_growth", index=False)
            if self.raw_water_flux is not None:
                self.raw_water_flux.to_excel(
                    writer, sheet_name="water_flux", index=False)
            if self.raw_water_storage is not None:
                self.raw_water_storage.to_excel(
                    writer, sheet_name="water_storage", index=False)
            if self.raw_final_stats is not None:
                self.raw_final_stats.to_excel(
                    writer, sheet_name="final_stats", index=False)


class SPACCropModel:
    """梧桐河灌区基于 SPAC 的典型作物生长模型。"""

    def __init__(self, config: SimulationConfig):
        self.cfg = config
        self.crop = config.crop
        self.label = config.label
        self.smt = config.smt
        self.mode = config.mode
        self.depths = config.depths
        self.scheme_type = config.scheme_type
        self.model: Optional[SimEngine] = None
        self.controller = None          # 水层调控控制器(mode='wlayer' 时非空)
        self.weather_df: Optional[pd.DataFrame] = None

    # ---------------- 输入接口 ---------------- #
    def prepare_inputs(self):
        """构建全部输入实体（大气/土壤/作物/灌溉/初始条件）。"""
        # 0) 确定模拟年份：sim_year 优先，否则用 SIM_YEAR
        sim_year = self.cfg.sim_year if self.cfg.sim_year is not None else SIM_YEAR

        # 1) 大气交互模块
        # 若未显式指定 weather_csv，则按年份自动选择 data/weather_era5_{year}.csv
        weather_csv = self.cfg.weather_csv
        if weather_csv is None:
            weather_csv = get_weather_csv_path(sim_year)

        self.weather_df = build_weather_df(weather_csv, LAT_RAD, ALTITUDE_M)
        # 内核需 Date 为无时区 datetime
        self.weather_df["Date"] = pd.to_datetime(self.weather_df["Date"]).dt.tz_localize(None)

        # 2) 土壤水分模块
        soil = build_soil(self.crop)
        n_layers = soil.nLayer
        # 3) 作物模拟模块
        crop = build_crop(self.crop)
        # 4) 灌溉管理模块 + 田间管理（水稻田埂）
        #    mode='wlayer'  : 水层调控(按生育阶段上下限闭环反馈灌水) —— 水稻全部 6 个方案
        #    mode='normal'  : 固定日历对照 —— 仅玉米在用；水稻旧日历已停用, 保留作回退
        #    mode='paddy'   : 【旧】水稻水层管理(固定日历 + 指定定额), 保留作回退
        #    mode='smt'     : 玉米 SMT 自动调控
        irrigation = build_irrigation(self.crop, self.smt, self.mode,
                                      depths=self.depths,
                                      sim_year=self.cfg.sim_year)
        field_mngt = build_field_management(self.crop)
        # 5) 初始条件模块（层数须与土壤一致）
        iwc = build_initial_water_content(self.crop, self.cfg.initial_moisture, n_layers)

        self._soil, self._crop, self._irr, self._fm, self._iwc = (
            soil, crop, irrigation, field_mngt, iwc
        )
        return self

    # ---------------- 运行 ---------------- #
    def run(self) -> "SPACCropModel":
        if self.weather_df is None:
            self.prepare_inputs()
        sim_year = self.cfg.sim_year if self.cfg.sim_year is not None else SIM_YEAR
        print(f"\n[SPAC模型] 运行: {self.crop} {self.label} "
              f"({sim_year}年)")
        print("  " + scenario_summary(self.crop, self.label))
        print("  " + describe_initial_condition(self.crop, self.cfg.initial_moisture))

        # 内核要求 sim_start_time/sim_end_time 为 'yyyy/mm/dd' 或 'yyyy-mm-dd'
        start = pd.to_datetime(self.cfg.sim_start).strftime("%Y/%m/%d")
        end = pd.to_datetime(self.cfg.sim_end).strftime("%Y/%m/%d")

        self.model = SimEngine(
            sim_start_time=start,
            sim_end_time=end,
            weather_df=self.weather_df,
            soil=self._soil,
            crop=self._crop,
            initial_water_content=self._iwc,
            irrigation_management=self._irr,
            field_management=self._fm,
        )
        # 关键：必须 till_termination=True 才会跑完整个模拟期；
        # 默认 num_steps=1 只跑 1 天，且 outputs 仅在 model_is_finished 时
        # 才被 outputs_when_model_is_finished 转为 DataFrame。
        if self.mode == "wlayer":
            from modules.paddy_control import (
                PaddyLayerController, install, uninstall, describe_bands,
            )
            self.controller = PaddyLayerController(
                self.smt, app_eff=self._irr.AppEff, max_irr=self._irr.MaxIrr)
            print("  水层上下限: " + describe_bands(self.smt))
            install(self.controller)
        try:
            self.model.run_model(till_termination=True)
        finally:
            if self.mode == "wlayer":
                uninstall()
        print("  模拟完成。")
        return self

    # ---------------- 输出接口（对应大纲表3） ---------------- #
    def collect_outputs(self) -> SimulationResult:
        """收集大纲表3 所列输出变量。

        逐日输出（8 项，严格对应大纲表3）：
          1. 模拟日期
          2. 第1、2...j层土壤含水率
          3. 田间水层深度 (mm)
          4. 冠层覆盖度 (%)
          5. 蒸腾蒸发量 (mm)
          6. 干物质量 (t/ha)
          7. 籽粒产量 (t/ha)
          8. 水分利用效率 (kg/m³)
        """
        if self.model is None:
            raise RuntimeError("模型尚未运行，请先调用 run()。")

        n_days = len(self.weather_df)
        dates = pd.to_datetime(self.weather_df["Date"]).reset_index(drop=True)

        # 读取 内核原始输出(模型结束后已自动转为 DataFrame)
        raw_cg = self.model.get_crop_growth()
        raw_wf = self.model.get_water_flux()
        raw_ws = self.model.get_water_storage()
        raw_fs = self.model.get_simulation_results()  # final_stats(未结束时为 False)
        cg_arr = np.array(raw_cg)[:n_days]
        wf_arr = np.array(raw_wf)[:n_days]
        ws_arr = np.array(raw_ws)[:n_days]

        # ---- 按大纲表3 构建 8 列输出 ----
        daily = pd.DataFrame()
        # 1) 模拟日期
        daily["模拟日期"] = dates.values

        # 2) 第1、2...j层土壤含水率（vol）
        #    ws_arr 列: [step, growing_season, dap, th1, th2, ..., thN]（N=nComp）
        #    按实际土壤层合并：耕作层(0-0.3m)=comp1-3, 犁底层(0.3-0.6m)=comp4-6, 心土层(0.6-1.0m)=comp7-10
        n_comp = ws_arr.shape[1] - 3
        # 从 soil_params 获取各层 compartment 数（按 0.1m 切分）
        from config.soil_params import get_soil_config
        soil_cfg = get_soil_config(self.crop)
        layer_depths = [lay["depth"] for lay in soil_cfg["layers"]]
        # 每层对应的 compartment 数（每 0.1m 一个 compartment）
        comp_per_layer = [max(int(round(d / 0.1)), 1) for d in layer_depths]
        # 验证总数一致
        assert sum(comp_per_layer) == n_comp, \
            f"compartment 数不匹配: {sum(comp_per_layer)} vs {n_comp}"
        # 按层合并（取该层内各 compartment 的平均含水率）
        comp_start = 0
        for layer_idx, n_in_layer in enumerate(comp_per_layer):
            cols_in_layer = list(range(comp_start, comp_start + n_in_layer))
            layer_th = np.mean(ws_arr[:, 3 + np.array(cols_in_layer)], axis=1)
            daily[f"第{layer_idx+1}层土壤含水率"] = layer_th
            comp_start += n_in_layer

        # 3) 田间水层深度 (mm)  <- water_flux 的 surface_storage 列
        #    wf_arr 列: [step, season, dap, Wr, z_gw, surface_storage, ...]
        daily["田间水层深度(mm)"] = wf_arr[:, 5]

        # 3.5) 每日灌溉量 (mm)  <- water_flux 的 IrrDay 列(索引6)
        #      method=1(SMT自动调控): 模型根据土壤水分目标自动触发的灌水量
        #      method=3(固定日历):    按预设灌水日历的当日定额(非灌水日为0)
        #      累计求和即为方案总灌溉量, 可用于绘制灌溉制度时序图
        daily["每日灌溉量(mm)"] = wf_arr[:, 6]

        # 3.6) 泡田灌溉量 (mm) —— 移栽日一次性注入的泡田定额
        #   内核在移栽日之前 growing_season=False, 强制 Irr=0, 泡田水无法通过
        #   Schedule 灌入; 泡田效果只能由初始墒情(SAT)+插秧水层表达, 这里把
        #   对应的水量显式补记为移栽日的一笔灌溉, 使水量平衡与灌溉统计闭合。
        #   ⚠ 内核的 `每日灌溉量(mm)` 保持原值, 灌水"次数"口径不受影响。
        from modules.initial_condition import puddling_requirement
        pudd = puddling_requirement(self.crop)
        daily["泡田灌溉量(mm)"] = 0.0
        if pudd > 0:
            # water_flux 第3列 = dap: 生长季内自 1 递增, 非生长季恒为 0
            dap_col = wf_arr[:, 2]
            gs_idx = np.where(dap_col == 1)[0]
            if gs_idx.size:
                daily.loc[gs_idx[0], "泡田灌溉量(mm)"] = pudd

        # 4) 冠层覆盖度 (%)  <- crop_growth 的 canopy_cover (0-1) × 100
        #    cg_arr 列: [step, season, dap, gdd, gdd_cum, z_root, canopy_cover, ...]
        daily["冠层覆盖度(%)"] = cg_arr[:, 6] * 100.0

        # 5) 蒸腾蒸发量 (mm) = 蒸腾 Tr + 棵间蒸发 Es
        #    wf_arr: Tr=col15, Es=col13
        transp = wf_arr[:, 15]   # Tr
        soil_evap = wf_arr[:, 13]  # Es
        daily["蒸腾蒸发量(mm)"] = transp + soil_evap

        # 6) 干物质量 (t/ha)  <- crop_growth 的 biomass (g/m²) × 0.01
        daily["干物质量(t/ha)"] = cg_arr[:, 8] * 0.01

        # 7) 籽粒产量 (t/ha)  <- crop_growth 的 DryYield
        daily["籽粒产量(t/ha)"] = cg_arr[:, 12]

        # 8) 水分利用效率 (kg/m³) = 当日累积产量 / 当日累积耗水（动态变化）
        cum_yield = np.nancumsum(daily["籽粒产量(t/ha)"].values)
        cum_et = np.nancumsum(daily["蒸腾蒸发量(mm)"].values)
        wue = np.zeros_like(cum_yield)
        mask = cum_et > 1e-6
        wue[mask] = cum_yield[mask] * 1000.0 / (cum_et[mask] * 10.0)
        daily["水分利用效率(kg/m³)"] = wue

        # ---- 裁剪收获后的空数据 ----
        # 内核在作物收获后(model_is_finished=True)停止记录，
        # 收获后的天数保持为0（Output数组初始化为np.zeros）。
        # 找到最后一个有作物数据（冠层覆盖度>0 或 干物质量>0）的日期，
        # 只保留到该日期的数据。
        has_crop = (daily["冠层覆盖度(%)"] > 0) | (daily["干物质量(t/ha)"] > 0)
        if has_crop.any():
            last_valid_idx = np.where(has_crop.values)[0][-1]
            daily = daily.iloc[:last_valid_idx + 1].reset_index(drop=True)

        # 汇总
        summary = self._summarize(daily)
        sim_year = self.cfg.sim_year if self.cfg.sim_year is not None else SIM_YEAR
        return SimulationResult(
            crop=self.crop, label=self.label, smt=self.smt, mode=self.mode,
            depths=self.depths, scheme_type=self.scheme_type,
            sim_year=sim_year,
            daily=daily, summary=summary,
            raw_crop_growth=raw_cg if isinstance(raw_cg, pd.DataFrame) else None,
            raw_water_flux=raw_wf if isinstance(raw_wf, pd.DataFrame) else None,
            raw_water_storage=raw_ws if isinstance(raw_ws, pd.DataFrame) else None,
            raw_final_stats=raw_fs if isinstance(raw_fs, pd.DataFrame) else None,
        )

    def _summarize(self, daily: pd.DataFrame) -> Dict[str, float]:
        """计算大纲要求的汇总指标（与表3输出对应）。"""
        s: Dict[str, float] = {}
        s["最大冠层覆盖度(%)"] = float(np.nanmax(daily["冠层覆盖度(%)"]))
        s["全季蒸腾蒸发量(mm)"] = float(np.nansum(daily["蒸腾蒸发量(mm)"]))
        s["最终干物质量(t/ha)"] = float(np.nanmax(daily["干物质量(t/ha)"]))
        s["最终籽粒产量(t/ha)"] = float(np.nanmax(daily["籽粒产量(t/ha)"]))
        s["水分利用效率(kg/m³)"] = float(daily["水分利用效率(kg/m³)"].iloc[-1])
        # 累计灌溉量（从水量通量数据中提取）
        # water_flux 列: [step, season, dap, Wr, z_gw, surface_storage,
        #                 IrrDay(6), Infl, Runoff, DeepPerc, TileDrain, CR,
        #                 GwIn, Es(13), EsPot(14), Tr(15), TrPot(16)]
        # ★ 累计灌溉量 = 内核生育期灌溉(IrrDay) + 泡田定额(移栽日一次性)
        #   "补充灌溉量"单列内核部分, 便于与"总灌溉量"区分上报。
        try:
            wf_arr = np.array(self.model.get_water_flux())[:len(daily)]
            sup = float(np.nansum(wf_arr[:, 6]))   # IrrDay
        except Exception:
            sup = 0.0
        pudd = float(np.nansum(daily.get("泡田灌溉量(mm)", pd.Series([0.0]))))
        s["补充灌溉量(mm)"] = sup
        s["泡田定额(mm)"] = pudd
        s["累计灌溉量(mm)"] = sup + pudd
        return s


# --------------------------------------------------------------------------- #
#  便捷函数：一次运行某作物某 SMT 方案
# --------------------------------------------------------------------------- #
def run_simulation(crop: str, label: str, smt: Optional[list] = None,
                   mode: str = "smt", depths: Optional[list] = None,
                   scheme_type: str = "",
                   sim_year: Optional[int] = None,
                   weather_csv: Optional[str] = None) -> SimulationResult:
    """运行单次模拟。

    Parameters
    ----------
    crop : 'rice' / 'corn'
    label : 方案标签（如 '正常灌溉对照' / '适宜水量'）
    smt   : SMT 阈值列表 [初始, 发育, 中期, 末期] %TAW；仅 mode='smt' 使用
    mode  : 'smt' / 'normal' / 'paddy'
    depths: 水稻 8 次灌水定额列表；仅 mode='paddy' 使用
    scheme_type: '正常灌溉对照' / '水层管理' / 'SMT调控'
    sim_year : 模拟年份；None 时使用 config.SIM_YEAR。
    weather_csv : 显式指定气象 CSV 路径；指定时优先级高于 sim_year 自动选择。
    """
    cfg = SimulationConfig(crop=crop, label=label, smt=smt, mode=mode,
                           depths=depths, scheme_type=scheme_type,
                           sim_year=sim_year, weather_csv=weather_csv)
    m = SPACCropModel(cfg)
    m.prepare_inputs().run()
    return m.collect_outputs()


if __name__ == "__main__":
    # 自检：水稻正常灌溉对照（默认 SIM_YEAR）—— 2026-10-21 起走官方规程上下限
    from modules.paddy_control import bands_for_ck
    res = run_simulation("rice", "正常灌溉对照", smt=bands_for_ck(),
                        mode="wlayer", scheme_type="正常灌溉对照")
    print("\n=== 逐日输出（前5行，对应大纲表3） ===")
    print(res.daily.head())
    print("\n=== 汇总 ===")
    for k, v in res.summary.items():
        print(f"  {k}: {v:.3f}")
    out_csv = os.path.join(OUTPUT_DIR, f"test_{res.crop}_{res.label}.csv")
    res.save_csv(out_csv)
    print(f"\n已保存: {out_csv}")
