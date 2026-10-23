# -*- coding: utf-8 -*-
"""Rice feedback scenarios using measured model states, not cumulative ET0.

lo >= 0: standing-water lower limit in mm; hi is the target pond depth.
lo < 0: root-zone depletion threshold (% TAW); hi is a net application depth,
        not a groundwater level or a promise of post-infiltration pond depth.
hi == 0: no in-season supplemental irrigation (initial puddling is separate).
Deficit scenarios are experiments, not recommended safe-AWD prescriptions.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Sequence, Tuple
import numpy as np
import pandas as pd
from modules.engine_core import core_module as _core
from config.crop_calendar import active_reference_date

STAGES: List[Tuple[str,str,str]] = [
    ("返青","05-15","05-31"), ("分蘖","06-01","06-30"),
    ("晒田停灌","07-01","07-09"), ("拔节孕穗","07-10","07-31"),
    ("抽穗开花","08-01","08-10"), ("灌浆","08-11","09-10"),
]
STAGE_NAMES = [s[0] for s in STAGES]
RICE_WATER_BANDS: Dict[str,List[Tuple[float,float]]] = {
    "雨养型": [(0,0)]*6,
    "低水量": [(-70,20),(-70,20),(0,0),(-70,25),(-70,25),(-70,20)],
    "中低水量": [(-50,30),(-50,30),(0,0),(-50,35),(-50,35),(-50,25)],
    "适宜水量": [(-30,35),(-30,30),(0,0),(-30,40),(-30,40),(-30,25)],
    "较高水量": [(5,40),(0,35),(0,0),(5,50),(5,50),(0,30)],
}
CK_BANDS = [(0,30),(0,30),(0,0),(0,50),(0,50),(0,30)]
Z_BUND_MM = 200.0


def protected_scheme(label):
    """L1/L2 remain deficit experiments; L3--L5 and CK enforce critical water."""
    return label in ("正常灌溉对照", "中低水量", "适宜水量", "较高水量")


def critical_water_rule(date):
    """Representative windows, not observed exact dates; mm pond lower/target."""
    day = pd.Timestamp(date).date()
    mapped = lambda md: active_reference_date("rice", md, day.year)
    if mapped("05-15") <= day < mapped("05-25"):
        return "移栽返青保水",20.,30.
    if mapped("07-10") <= day < mapped("08-25"):
        # Includes the actual August flowering window implied by HIstart/FloweringCD.
        return "孕穗开花保水",30.,50.
    return None


def bands_for(level_label):
    return list(RICE_WATER_BANDS[level_label])


def bands_for_ck():
    return list(CK_BANDS)


def validate_bands(bands):
    if len(bands) != len(STAGES):
        raise ValueError("水稻管理阶段数量不一致")
    for lo,hi in bands:
        if hi < 0 or hi > Z_BUND_MM-5 or (hi>0 and lo>=hi) or lo < -100:
            raise ValueError("水稻灌水阈值或定额超出允许范围")


def describe_bands(bands):
    parts=[]
    for (lo,hi),name in zip(bands,STAGE_NAMES):
        rule = "不补灌" if hi<=0 else f"耗竭{-lo:.0f}%TAW→净补{hi:.0f}mm" if lo<0 else f"水层{lo:.0f}→{hi:.0f}mm"
        parts.append(f"{name}:{rule}")
    return " | ".join(parts)


def root_depletion_fraction(init_cond, param_struct):
    """Actual FC-relative root-zone deficit / TAW; accounts for wet soil."""
    prof = param_struct.Soil.profile
    dz = np.asarray(prof.dz, dtype=float)
    top = np.cumsum(dz)-dz
    root_depth = max(float(init_cond.z_root), .20)
    weights = np.clip(root_depth-top, 0., dz)
    fc,wp = np.asarray(prof.th_fc), np.asarray(prof.th_wp)
    th = np.asarray(init_cond.th)
    taw = float(np.sum((fc-wp)*weights))
    deficit = max(0., float(np.sum((fc-th)*weights)))
    return float(np.clip(deficit/taw,0.,1.)) if taw>0 else 0.


class PaddyLayerController:
    def __init__(self, bands, app_eff=90., max_irr=300., protect_critical=False):
        validate_bands(bands)
        if not 0 < app_eff <= 100 or max_irr <= 0:
            raise ValueError("灌水效率和单次限额无效")
        self.bands = list(bands)
        self.app_eff, self.max_irr = float(app_eff),float(max_irr)
        self.protect_critical=bool(protect_critical)
        self.records=[]

    @staticmethod
    def stage_index(date):
        day = pd.Timestamp(date).date()
        return next((i for i, (_, a, b) in enumerate(STAGES)
                     if active_reference_date("rice", a, day.year) <= day <
                     active_reference_date("rice", (pd.Timestamp(f"{day.year}-{b}") +
                                                   pd.Timedelta(days=1)).strftime("%m-%d"), day.year)), -1)

    def step(self,date,storage,et0=0.,depletion_fraction=None):
        i=self.stage_index(date)
        name,lo,hi = (STAGE_NAMES[i],*self.bands[i]) if i>=0 else ("断水/非生育期",0.,0.)
        rule=critical_water_rule(date)
        if self.protect_critical and rule is not None:
            name,lo,hi=rule
        storage=max(0.,float(storage))
        if lo<0 and depletion_fraction is None:
            raise ValueError("根区耗水比例缺失，不能用ET0累计量代替")
        state = storage if lo>=0 or storage>.5 else -100*float(depletion_fraction)
        irr=0.
        if hi>0 and state<=lo:
            net = hi if lo<0 else max(0.,hi-storage)
            irr=min(self.max_irr,net/(self.app_eff/100.))
        self.records.append(dict(日期=pd.Timestamp(date).strftime("%Y-%m-%d"),阶段=name,
                                 田面水层_前=storage,根区耗竭比例=depletion_fraction,
                                 状态量=state,下限=lo,上限=hi,灌水定额=irr,
                                 关键期=rule is not None,保护规则启用=self.protect_critical and rule is not None))
        return float(irr)

    def to_frame(self):
        return pd.DataFrame(self.records)


_ORIGINAL_STEP=None
_ACTIVE: Optional[PaddyLayerController]=None


def _hooked_solution_single_time_step(init_cond,param_struct,clock_struct,weather_step,outputs):
    if _ACTIVE is not None:
        t=int(clock_struct.time_step_counter)
        sched=param_struct.IrrMngt.Schedule
        if 0<=t<len(sched):
            sched[t]=_ACTIVE.step(clock_struct.step_start_time,init_cond.surface_storage,
                                 float(weather_step[3]),root_depletion_fraction(init_cond,param_struct))
    return _ORIGINAL_STEP(init_cond,param_struct,clock_struct,weather_step,outputs)


def install(ctrl):
    global _ORIGINAL_STEP,_ACTIVE
    if _ORIGINAL_STEP is None:
        _ORIGINAL_STEP=_core.solution_single_time_step
        _core.solution_single_time_step=_hooked_solution_single_time_step
    _ACTIVE=ctrl


def uninstall():
    global _ORIGINAL_STEP,_ACTIVE
    _ACTIVE=None
    if _ORIGINAL_STEP is not None:
        _core.solution_single_time_step=_ORIGINAL_STEP
        _ORIGINAL_STEP=None
