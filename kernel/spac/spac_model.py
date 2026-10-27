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
    application_efficiency_pct: float = 90.
    rice_management: Optional[dict] = None

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
    puddling_daily: Optional[pd.DataFrame] = None
    management_daily: Optional[pd.DataFrame] = None

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
            if self.management_daily is not None:
                self.management_daily.to_excel(writer, sheet_name="rice_management", index=False)
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
            if self.puddling_daily is not None and not self.puddling_daily.empty:
                self.puddling_daily.to_excel(writer, sheet_name="puddling_balance", index=False)


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
                                      sim_year=self.cfg.sim_year,
                                      application_efficiency_pct=self.cfg.application_efficiency_pct)
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
            # Preserve the simulated April soil-water state at corn sowing.
            # The default off_season=False resets the profile to thini on 4/25.
            off_season=self.crop=="corn",
        )
        # 关键：必须 till_termination=True 才会跑完整个模拟期；
        # 默认 num_steps=1 只跑 1 天，且 outputs 仅在 model_is_finished 时
        # 才被 outputs_when_model_is_finished 转为 DataFrame。
        if self.mode == "wlayer":
            from modules.paddy_control import (
                PaddyLayerController, install, uninstall, describe_bands,
            )
            self.controller = PaddyLayerController(
                self.smt, app_eff=self._irr.AppEff, max_irr=self._irr.MaxIrr,
                planting_date=f"{sim_year}/{self._crop.planting_date}",
                flowering_start_dap=int(self._crop.HIstartCD)+1,
                flowering_days=int(self._crop.FloweringCD),
                maturity_dap=int(self._crop.MaturityCD),
                **(self.cfg.rice_management or {}))
            print("  水层上下限: " + describe_bands(self.smt))
            install(self.controller)
        try:
            self.model._initialize()
            # AquaCrop 3.1 updates dzsum during runoff partitioning. Pandas
            # Copy-on-Write exposes it as a read-only view; give this run its
            # own writable buffer without changing the soil depth values.
            profile = self.model._param_struct.Soil.Profile
            profile.dzsum = profile.dzsum.copy()
            if self.crop=="rice" and pd.Timestamp(start)<pd.Timestamp(f"{sim_year}/{self._crop.planting_date}"):
                # Natural pre-season FC is an explicit unmeasured April-1 prior.
                # Preserve the selected transplant profile in thini, but do not
                # give the fallow field a second uncounted saturated profile.
                self.model._init_cond.thini=self.model._init_cond.thini.copy()
                self.model._init_cond.th=np.asarray(self.model._param_struct.Soil.profile.th_fc,dtype=float).copy()
                self.model._init_cond.surface_storage=0.
            self.model.run_model(till_termination=True,initialize_model=False)
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
        daily["降雨量(mm)"] = self.weather_df["Precipitation"].to_numpy()[:n_days]
        daily["参考蒸散ET0(mm)"] = self.weather_df["ReferenceET"].to_numpy()[:n_days]

        # 2) 第1、2...j层土壤含水率（vol）
        #    ws_arr 列: [step, growing_season, dap, th1, th2, ..., thN]（N=nComp）
        #    按实际土壤层合并：耕作层(0-0.3m)=comp1-3, 犁底层(0.3-0.6m)=comp4-6, 心土层(0.6-1.0m)=comp7-10
        n_comp = ws_arr.shape[1] - 3
        # 从 soil_params 获取各层 compartment 数（按 0.1m 切分）
        from config.soil_params import get_soil_config
        soil_cfg = get_soil_config(self.crop)
        layer_depths = [lay["depth"] for lay in soil_cfg["layers"]]
        actual_dz=np.asarray(self.model._param_struct.Soil.Profile.dz,dtype=float)
        if abs(actual_dz.sum()-sum(layer_depths))>1e-8:
            raise ValueError("求解器改变了土壤剖面深度，无法按原层界报告储水与土壤含水率")
        # Map actual solver layer IDs; no assumption of 10cm cells.
        actual_layers=np.asarray(self.model._param_struct.Soil.Profile.Layer,dtype=int)
        for layer_idx,depth in enumerate(layer_depths,1):
            indices=np.flatnonzero(actual_layers==layer_idx)
            if not len(indices) or abs(actual_dz[indices].sum()-depth)>1e-8:
                raise ValueError("求解器土层与输出层界不一致")
            daily[f"第{layer_idx}层土壤含水率"]=np.average(
                ws_arr[:,3+indices],axis=1,weights=actual_dz[indices])

        # 3) 田间水层深度 (mm)  <- water_flux 的 surface_storage 列
        #    wf_arr 列: [step, season, dap, Wr, z_gw, surface_storage, ...]
        daily["田间水层深度(mm)"] = wf_arr[:, 5]

        # 3.5) 每日灌溉量 (mm)  <- water_flux 的 IrrDay 列(索引6)
        #      method=1(SMT自动调控): 模型根据土壤水分目标自动触发的灌水量
        #      method=3(固定日历):    按预设灌水日历的当日定额(非灌水日为0)
        #      原始毛量另列；每日灌溉量为生育期补充净量，不包含泡田。
        # IrrDay施加毛量在入渗时才乘AppEff，主显示统一净量，毛量另列。
        # 方法4的IrrDay已是净量，不能再乘一次效率。
        efficiency = float(self._irr.AppEff) / 100.0
        if not 0 < efficiency <= 1:
            raise ValueError("田间灌水效率必须在(0,100]%范围内")
        raw_irrigation = wf_arr[:, 6]
        if int(self._irr.irrigation_method) == 4:
            daily["每日灌溉量(mm)"] = raw_irrigation
            daily["每日毛灌溉量(mm)"] = raw_irrigation / efficiency
        else:
            daily["每日毛灌溉量(mm)"] = raw_irrigation
            daily["每日灌溉量(mm)"] = raw_irrigation * efficiency

        # Separate pre-transplant balance, joined to daily reporting without altering
        # observed rainfall or the crop solver. Native fallow fluxes are retained in
        # raw sheets; puddling_balance is the authoritative preparation-stage ledger.
        from modules.initial_condition import initial_layer_water, PADDY_INIT_WATER_LAYER
        from modules.puddling import puddling_balance
        prep=None
        prep_columns={"irrigation_net_mm":"泡田灌溉量(mm)",
                      "storage_change_mm":"泡田储水增量(mm)",
                      "evaporation_mm":"泡田蒸发量(mm)",
                      "percolation_mm":"泡田渗漏量(mm)",
                      "rain_mm":"泡田降雨量(mm)","overflow_mm":"泡田溢流量(mm)"}
        for column in prep_columns.values():
            daily[column]=0.0
        if self.crop == "rice":
            year=self.cfg.sim_year if self.cfg.sim_year is not None else SIM_YEAR
            plant=pd.Timestamp(f"{year}/{self._crop.planting_date}")
            from modules.puddling import PUDDLING_DAYS
            prep_start=plant-pd.Timedelta(days=PUDDLING_DAYS)
            positions=np.flatnonzero(dates.eq(prep_start))
            if not len(positions) or positions[0]<1:
                raise ValueError("模拟期须覆盖泡田开始前至少一天，以衔接泡田前土壤储水")
            initial_profile=[float(daily.iloc[positions[0]-1][f"第{j}层土壤含水率"]) for j in range(1,len(layer_depths)+1)]
            prep=puddling_balance(self.weather_df,plant,self.cfg.initial_moisture,initial_soil_water=initial_profile)
            lookup=prep.set_index("Date")
            for source,column in prep_columns.items():
                daily[column]=dates.map(lookup[source]).fillna(0.).to_numpy()
            from config.soil_params import get_soil_config
            layers=get_soil_config(self.crop)["layers"]
            target=initial_layer_water(self.crop,self.cfg.initial_moisture)
            soil_store=sum((v-old)*lay["depth"]*1000 for v,old,lay in zip(target,initial_profile,layers))
            for rec in prep.itertuples():
                mask=dates.eq(rec.Date)
                fraction=min(1.,rec.storage_after_mm/soil_store) if soil_store>0 else 0.
                for j,(v,old) in enumerate(zip(target,initial_profile),1):
                    daily.loc[mask,f"第{j}层土壤含水率"]=old+fraction*(v-old)
                daily.loc[mask,"田间水层深度(mm)"]=min(PADDY_INIT_WATER_LAYER,max(0.,rec.storage_after_mm-soil_store))

        daily["每日总净灌溉量(mm)"] = daily["每日灌溉量(mm)"] + daily["泡田灌溉量(mm)"]
        # 泡田毛量仅按相同田间效率估算，不等于渠首取水量。
        daily["每日总毛灌溉量估算(mm)"] = (
            daily["每日毛灌溉量(mm)"] + daily["泡田灌溉量(mm)"] / efficiency)

        # 4) 冠层覆盖度 (%)  <- crop_growth 的 canopy_cover (0-1) × 100
        #    cg_arr 列: [step, season, dap, gdd, gdd_cum, z_root, canopy_cover, ...]
        daily["冠层覆盖度(%)"] = cg_arr[:, 6] * 100.0

        # 5) 蒸腾蒸发量 (mm) = 蒸腾 Tr + 棵间蒸发 Es
        #    wf_arr: Tr=col15, Es=col13
        transp = wf_arr[:, 15]   # Tr
        soil_evap = wf_arr[:, 13]  # Es
        daily["蒸腾蒸发量(mm)"] = transp + soil_evap
        if prep is not None:
            prep_mask=dates.isin(prep.Date)
            # Replace, do not add to, the native fallow evaporation for these days.
            daily.loc[prep_mask,"蒸腾蒸发量(mm)"]=dates[prep_mask].map(prep.set_index("Date").evaporation_mm).to_numpy()

        # 6) 干物质量 (t/ha)  <- crop_growth 的 biomass (g/m²) × 0.01
        daily["干物质量(t/ha)"] = cg_arr[:, 8] * 0.01

        # 水稻主报告折算至14.5%含水率，保留干籽粒；玉米保持原有干重口径。
        from config.crop_params import yield_moisture
        grain_moisture = yield_moisture(self.crop)
        daily["干籽粒产量(t/ha)"] = cg_arr[:, 12]
        daily["籽粒产量(t/ha)"] = cg_arr[:, 12] / (1.0-grain_moisture)
        daily["产量含水率(%)"] = grain_moisture * 100.0

        # 8) 水分利用效率 (kg/m³) = 当日累积产量 / 当日累积耗水（动态变化）
        # DryYield已是截至当日的累计产量，不能再次累加。
        cum_yield = daily["籽粒产量(t/ha)"].to_numpy()
        et_for_wue = daily["蒸腾蒸发量(mm)"].values.copy()
        if self.crop=="rice":
            et_for_wue = np.where(wf_arr[:,2]>0,et_for_wue,0.)
        cum_et = np.nancumsum(et_for_wue)
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

        if self.controller is not None:
            management=self.controller.to_frame().set_index("日期")
            keys=daily["模拟日期"].dt.strftime("%Y-%m-%d")
            for col in ("阶段","阈值类型","下限","上限","控制层饱和度","控制层饱和度_后",
                        "犁底层底通量_mm","剖面底深层渗漏_mm","犁底层饱和度_后","救援复水保留",
                        "管理排水_mm","补水后目标未达","暂态积水","控制期有明水","限额截断",
                        "CI分次复水","当日补水入渗预算_mm","分次延后净水量_mm","管理排水原因",
                        "独立耐淹连续天数","最终排水上限_mm","晒田前过渡","实际晒田管理",
                        "提前结束晒田","晒田已结束","复水衔接","衔接状态",
                        "有效触发下限","晒田历日","晒田无明水晨间","晒田救援分次"):
                if col not in management.columns:
                    continue
                daily[col]=keys.map(management[col])
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
            puddling_daily=prep,
            management_daily=self.controller.to_frame() if self.controller else None,
        )

    def _summarize(self, daily: pd.DataFrame) -> Dict[str, float]:
        """计算大纲要求的汇总指标（与表3输出对应）。"""
        s: Dict[str, float] = {}
        s["最大冠层覆盖度(%)"] = float(np.nanmax(daily["冠层覆盖度(%)"]))
        s["全季蒸腾蒸发量(mm)"] = float(np.nansum(daily["蒸腾蒸发量(mm)"]))
        s["最终干物质量(t/ha)"] = float(np.nanmax(daily["干物质量(t/ha)"]))
        s["最终籽粒产量(t/ha)"] = float(np.nanmax(daily["籽粒产量(t/ha)"]))
        s["最终干籽粒产量(t/ha)"] = float(np.nanmax(daily["干籽粒产量(t/ha)"]))
        s["产量含水率(%)"] = float(daily["产量含水率(%)"].iloc[-1])
        s["水分利用效率(kg/m³)"] = float(daily["水分利用效率(kg/m³)"].iloc[-1])
        # 累计净量包含完整简化泡田水量平衡的净补水，储水与各项通量另列。
        sup = float(np.nansum(daily["每日灌溉量(mm)"]))
        pudd = float(np.nansum(daily.get("泡田灌溉量(mm)", pd.Series([0.0]))))
        s["补充灌溉量(mm)"] = sup
        s["泡田定额(mm)"] = pudd
        for col in ("泡田储水增量(mm)","泡田蒸发量(mm)","泡田渗漏量(mm)","泡田降雨量(mm)","泡田溢流量(mm)"):
            s[col]=float(daily[col].sum())
        s["累计灌溉量(mm)"] = sup + pudd
        s["补充毛灌溉量(mm)"] = float(np.nansum(daily["每日毛灌溉量(mm)"]))
        s["累计毛灌溉量估算(mm)"] = float(np.nansum(daily["每日总毛灌溉量估算(mm)"]))
        s["田间灌水效率(%)"] = float(self._irr.AppEff)
        if self.crop=="rice" and self.controller is not None:
            management=self.controller.to_frame()
            critical=management["关键期"]
            failures=int((critical & management["田面水层_后"].le(.5)).sum())
            dates=daily["模拟日期"].dt.strftime("%Y-%m-%d")
            active_dates=set(management.loc[management["阶段序号"].ge(0),"日期"])
            active=dates.isin(active_dates)
            s["全模拟时段蒸腾蒸发量(mm)"]=s["全季蒸腾蒸发量(mm)"]
            s["生育期蒸腾蒸发量(mm)"]=float(daily.loc[active,"蒸腾蒸发量(mm)"].sum())
            s["全季蒸腾蒸发量(mm)"]=s["生育期蒸腾蒸发量(mm)"]
            s["模型冠层衰老终止"]=int(self.model._init_cond.crop_dead)
            s["模型生理成熟终止"]=int(self.model._init_cond.crop_mature)
            s["模拟生育天数"]=int(active.sum())
            s["配置成熟日数"]=self.controller.maturity_dap
            s["现场验证通过"]=0
            s["关键期无明水天数"]=failures
            s["关键期保护方案"]=int(getattr(self.controller,"protect_critical",True))
            s["推荐候选"]=int(failures==0)
            s["控制层深度(m)"]=self.controller.control_depth_m
            s["犁底层底累计通量(mm)"]=float(management.loc[management["阶段序号"].ge(0),"犁底层底通量_mm"].sum())
            s["生育期深层渗漏(mm)"]=float(management.loc[management["阶段序号"].ge(0),"剖面底深层渗漏_mm"].sum())
            s["管理排水量(mm)"]=float(management["管理排水_mm"].sum())
            s["补水后目标未达天数"]=int(management["补水后目标未达"].sum())
            s["CI分次复水天数"]=int(management["CI分次复水"].sum())
            s["暂态积水天数"]=int(management["暂态积水"].sum())
            s["控制期有明水天数"]=int(management["控制期有明水"].sum())
        else:
            s["关键期无明水天数"]=0
            s["关键期保护方案"]=0
            s["推荐候选"]=1
        return s


# --------------------------------------------------------------------------- #
#  便捷函数：一次运行某作物某 SMT 方案
# --------------------------------------------------------------------------- #
def run_simulation(crop: str, label: str, smt: Optional[list] = None,
                   mode: str = "smt", depths: Optional[list] = None,
                   scheme_type: str = "",
                   sim_year: Optional[int] = None,
                   weather_csv: Optional[str] = None,
                   application_efficiency_pct: float = 90.,
                   rice_management: Optional[dict] = None) -> SimulationResult:
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
                           sim_year=sim_year, weather_csv=weather_csv,
                           application_efficiency_pct=application_efficiency_pct,
                            rice_management=rice_management)
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
