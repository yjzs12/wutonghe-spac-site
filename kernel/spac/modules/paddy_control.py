# -*- coding: utf-8 -*-
"""梧桐河候选参数 V6：日尺度田面水层/控制层饱和度反馈。
文献综合与工程保守取值，未经梧桐河田间验证；不是标准原表。
补水经原引擎入渗，排水单列并计入总地表出流，不直接修改土壤含水率。
"""
from __future__ import annotations
from copy import deepcopy
import math
import numpy as np
import pandas as pd
from modules.engine_core import core_module as _core

PARAMETER_VERSION = "wutonghe-rice-v6-20261023"
Z_BUND_MM = 200.0
RICE_LABELS = ["常规淹水", "浅湿灌溉", "干湿交替（关键期保水型）", "控制灌溉（关键期保水型）"]
RICE_IDS = ["CF", "SWI", "AWD", "CI"]
STAGE_NAMES = ["移栽返青", "分蘖前期", "分蘖盛期", "分蘖末晒田", "拔节孕穗", "抽穗开花保护", "灌浆乳熟", "成熟前停灌"]

def pond(lo, hi):
    return dict(trigger="pond_mm", lower=float(lo), target_pond_mm=float(hi))

def moisture(lo, hi=0.):
    return dict(trigger="saturation_ratio", lower=float(lo), target_pond_mm=float(hi))

DRY = dict(trigger="drying", lower=.70, target_pond_mm=0.)
OFF = dict(trigger="off", lower=0., target_pond_mm=0.)
RICE_WATER_BANDS = {
    RICE_LABELS[0]: [pond(10,30),pond(10,40),pond(10,40),DRY,pond(30,50),pond(30,50),pond(10,40),OFF],
    RICE_LABELS[1]: [pond(10,30),pond(0,30),pond(0,20),DRY,pond(20,40),pond(30,50),pond(0,20),OFF],
    RICE_LABELS[2]: [pond(10,30),moisture(.80,30),moisture(.80,30),DRY,moisture(.85,30),pond(30,50),moisture(.80,20),OFF],
    RICE_LABELS[3]: [pond(10,30),moisture(.80),moisture(.80),DRY,moisture(.90),pond(30,50),moisture(.80),OFF],
}
CK_BANDS = deepcopy(RICE_WATER_BANDS[RICE_LABELS[0]])

def bands_for(label):
    return deepcopy(RICE_WATER_BANDS[label])

def bands_for_ck():
    return deepcopy(CK_BANDS)

def protected_scheme(label):
    return label in RICE_LABELS

def validate_bands(bands):
    if len(bands) != len(STAGE_NAMES):
        raise ValueError("水稻管理阶段数量不一致")
    for rule in bands:
        kind,lo,hi = rule["trigger"],rule["lower"],rule["target_pond_mm"]
        if not all(math.isfinite(v) for v in (lo,hi)) or not 0<=hi<=Z_BUND_MM:
            raise ValueError("水稻灌溉阈值无效")
        if kind == "pond_mm":
            valid = 0<=lo<hi
        elif kind in ("saturation_ratio","drying"):
            valid = 0<lo<1
        else:
            valid = kind=="off" and lo==hi==0
        if not valid:
            raise ValueError("水稻阈值类型或单位无效")

def describe_bands(bands):
    validate_bands(bands)
    parts=[]
    for name,r in zip(STAGE_NAMES,bands):
        kind,lo,hi=r["trigger"],r["lower"],r["target_pond_mm"]
        if kind=="off": text="停灌自然落干"
        elif kind=="drying": text="排明水晒田；R≤70%救援复水、暂缓强排"
        elif kind=="pond_mm": text=f"H {lo:g}→{hi:g}mm"
        else: text=f"R {lo:.0%}→"+(f"H {hi:g}mm" if hi else "控制层饱和、目标无明水")
        parts.append(name+":"+text)
    return " | ".join(parts)

def control_layer_state(init_cond,param_struct,depth=.20):
    prof=param_struct.Soil.profile
    dz=np.asarray(prof.dz,dtype=float)
    weights=np.clip(depth-(np.cumsum(dz)-dz),0.,dz)
    sat=np.asarray(prof.th_s,dtype=float)
    th=np.asarray(init_cond.th,dtype=float)
    capacity=float(np.sum(sat*weights))
    if depth<=0 or depth>float(dz.sum())+1e-9 or capacity<=0 or not np.isfinite(th).all():
        raise ValueError("控制层深度或土壤含水率无效")
    return float(np.sum(th*weights)/capacity),float(np.sum(np.maximum(0.,sat-th)*weights)*1000.)

class PaddyLayerController:
    def __init__(self,bands,app_eff=90.,max_irr=300.,protect_critical=True,
                 planting_date=None,flowering_start_dap=80,flowering_days=19,
                 maturity_dap=133,control_depth_m=.20,stop_days=7,
                 regreen_days=17,tillering_peak_dap=33,drying_start_dap=48,
                 drying_days=7,rain_max_mm=50.,rain_retention_days=5,cold_events=None,
                 ci_natural_drydown=True,ci_split_refill=True,
                 ci_ponding_limit_days=None,ci_ponding_limit_depth_mm=None,
                 ci_duration_drain_target_mm=None,awd_natural_drydown=True,
                 awd_ponding_limit_days=None,awd_ponding_limit_depth_mm=None,
                 awd_duration_drain_target_mm=None,pre_drying_days=3,
                 drying_handoff=True,drying_min_days=5,recovery_budget_days=2,
                 rescue_split_refill=True,natural_drydown_all=False):
        validate_bands(bands)
        if not 0<app_eff<=100 or not 0<max_irr or not .05<=control_depth_m<=1.:
            raise ValueError("灌水效率、限额或控制层深度无效")
        if not (1<=regreen_days<tillering_peak_dap<drying_start_dap
                and 5<=drying_days<=7 and drying_start_dap+drying_days<flowering_start_dap
                and flowering_days>0 and flowering_start_dap+flowering_days<maturity_dap-stop_days
                and stop_days in (7,15) and 0<rain_max_mm<=Z_BUND_MM and rain_retention_days>=1):
            raise ValueError("水稻生育阶段或蓄雨配置无效")
        if not isinstance(ci_natural_drydown,bool) or not isinstance(ci_split_refill,bool):
            raise ValueError("CI管理开关必须为布尔值")
        if not isinstance(awd_natural_drydown,bool):
            raise ValueError("AWD管理开关必须为布尔值")
        for mode,days,depth,target in (
                ("CI",ci_ponding_limit_days,ci_ponding_limit_depth_mm,ci_duration_drain_target_mm),
                ("AWD",awd_ponding_limit_days,awd_ponding_limit_depth_mm,awd_duration_drain_target_mm)):
            limits=(days,depth,target)
            if any(v is not None for v in limits):
                if any(v is None for v in limits) or not all(math.isfinite(v) for v in limits):
                    raise ValueError(f"{mode}耐淹规则须明确水深、持续天数和排水目标")
                if (isinstance(days,bool) or int(days)!=days or days<1
                        or not 0<=target<depth<=rain_max_mm):
                    raise ValueError(f"{mode}独立耐淹规则无效")
        for name,value in (("pre_drying_days",pre_drying_days),("drying_min_days",drying_min_days),("recovery_budget_days",recovery_budget_days)):
            if isinstance(value,bool) or not isinstance(value,(int,np.integer)):
                raise ValueError(f"{name}须为整数")
        if (not 0<=pre_drying_days<=7 or drying_start_dap-pre_drying_days<tillering_peak_dap
                or not 3<=drying_min_days<=drying_days or not 1<=recovery_budget_days<=7
                or not isinstance(drying_handoff,bool)):
            raise ValueError("晒田过渡与复水衔接配置无效")
        if not all(isinstance(v,bool) for v in (protect_critical,rescue_split_refill,natural_drydown_all)):
            raise ValueError("保护、救援及排水开关必须为布尔值")
        self.protect_critical=protect_critical
        self.rescue_split_refill=rescue_split_refill
        self.natural_drydown_all=natural_drydown_all
        self._drain_previous_stage=None
        self.pre_drying_days=pre_drying_days
        self.drying_handoff=drying_handoff
        self.drying_min_days=drying_min_days
        self.recovery_budget_days=recovery_budget_days
        self._drying_closed=False
        self._recovery_start_dap=None
        self.is_ci=any(b["trigger"]=="saturation_ratio" and b["target_pond_mm"]==0 for b in bands)
        self.is_awd=not self.is_ci and any(b["trigger"]=="saturation_ratio" and b["target_pond_mm"]>0 for b in bands)
        self.awd_natural_drydown=awd_natural_drydown
        self.awd_ponding_limit_days=awd_ponding_limit_days
        self.awd_ponding_limit_depth_mm=awd_ponding_limit_depth_mm
        self.awd_duration_drain_target_mm=awd_duration_drain_target_mm
        self.ci_natural_drydown=ci_natural_drydown
        self.ci_split_refill=ci_split_refill
        self.ci_ponding_limit_days=ci_ponding_limit_days
        self.ci_ponding_limit_depth_mm=ci_ponding_limit_depth_mm
        self.ci_duration_drain_target_mm=ci_duration_drain_target_mm
        self._independent_wet_days=0
        self._ci_refilling=False
        self._previous_stage=None
        self.bands=deepcopy(bands)
        self.app_eff,self.max_irr=float(app_eff),float(max_irr)
        self.planting_date=pd.Timestamp(planting_date) if planting_date is not None else None
        self.flowering_start_dap,self.flowering_days=int(flowering_start_dap),int(flowering_days)
        self.maturity_dap,self.stop_days=int(maturity_dap),int(stop_days)
        self.control_depth_m=float(control_depth_m)
        self.regreen_days,self.tillering_peak_dap=regreen_days,tillering_peak_dap
        self.drying_start_dap,self.drying_days=drying_start_dap,drying_days
        self.rain_max_mm,self.rain_retention_days=rain_max_mm,rain_retention_days
        self.cold_events=[]
        for event in cold_events or []:
            start,end=pd.Timestamp(event['start']),pd.Timestamp(event['end'])
            lo,hi=float(event.get('lower_mm',170)),float(event.get('target_mm',200))
            if end<start or not 170<=lo<hi<=200:
                raise ValueError("人工防冷事件日期或水层无效")
            self.cold_events.append((start,end,lo,hi))
        self.records=[]
        self._excess_days=0
        self._rescue_recovery=False

    def dap(self,date):
        date=pd.Timestamp(date)
        plant=self.planting_date if self.planting_date is not None else pd.Timestamp(year=date.year,month=5,day=15)
        return (date-plant).days+1

    def stage_index(self,date,adjusted_dap=None):
        dap=self.dap(date)
        if dap<1 or dap>self.maturity_dap: return -1
        if dap>=self.maturity_dap-self.stop_days: return 7
        adj=dap if adjusted_dap is None else adjusted_dap
        if self.flowering_start_dap<=adj<self.flowering_start_dap+self.flowering_days: return 5
        if adj>=self.flowering_start_dap+self.flowering_days: return 6
        if dap<=self.regreen_days: return 0
        if dap<self.tillering_peak_dap: return 1
        if dap<self.drying_start_dap: return 2
        if dap<self.drying_start_dap+self.drying_days: return 3
        return 4

    def step(self,date,storage,et0=0.,saturation_ratio=None,deficit_mm=None,adjusted_dap=None,
             infiltration_capacity_mm=None,rain_mm=0.):
        idx=self.stage_index(date,adjusted_dap)
        r=deepcopy(self.bands[idx] if idx>=0 else OFF)
        name=STAGE_NAMES[idx] if idx>=0 else "非生育期"
        cold=False
        if 0<=idx<7:
            for start,end,lo,hi in self.cold_events:
                if start<=pd.Timestamp(date)<=end:
                    r=pond(lo,hi);name="人工防冷保水";cold=True;break
        storage=max(0.,float(storage))
        dap=self.dap(date)
        pre=bool(not cold and idx==2 and self.pre_drying_days>0
                 and self.drying_start_dap-self.pre_drying_days<=dap<self.drying_start_dap)
        ended_today=False
        if (self.drying_handoff and idx==3 and not cold and not self._drying_closed
                and dap-self.drying_start_dap>=self.drying_min_days
                and ((storage<=.5 and saturation_ratio is not None and saturation_ratio<=DRY["lower"])
                     or self._rescue_recovery)):
            self._drying_closed=True
            self._recovery_start_dap=dap
            ended_today=True
        if (self.drying_handoff and idx==4 and not cold and self._recovery_start_dap is None):
            self._recovery_start_dap=dap
        recovering=bool(not cold and self.drying_handoff and idx in (3,4)
                        and self._recovery_start_dap is not None
                        and 0<=dap-self._recovery_start_dap<self.recovery_budget_days)
        if pre:
            r=deepcopy(DRY)  # Keep the existing 70%SAT safety floor; do not rebuild a pond.
        elif idx==3 and self._drying_closed and not cold:
            r=deepcopy(self.bands[4])  # Management changes, not crop phenology/calendar.
        effective_drying=bool(idx==3 and not self._drying_closed and not cold)
        kind,lo,hi=r["trigger"],r["lower"],r["target_pond_mm"]
        if kind!="off" and (saturation_ratio is None or deficit_mm is None
                               or not np.isfinite([saturation_ratio,deficit_mm]).all()):
            raise ValueError("缺少控制层实测模型状态，不得以ET0或TAW替代")
        # Conservative daily guard: ET0 and surface intake bound; no credit for future rain.
        critical=bool(idx in (0,5) or cold)
        effective_lower=lo
        if self.protect_critical and critical and kind=="pond_mm":
            if infiltration_capacity_mm is None or not np.isfinite([et0,infiltration_capacity_mm]).all() or et0<0 or infiltration_capacity_mm<=0:
                raise ValueError("关键期日内保水需要有效ET0和入渗上限")
            effective_lower=min(hi,max(lo,1.2*et0+infiltration_capacity_mm+0.5))
        trigger=(storage<=effective_lower+1e-9 if kind=="pond_mm" else
                 storage<=.5 and saturation_ratio<=lo if kind in ("saturation_ratio","drying") else False)
        split=self.is_ci and self.ci_split_refill and kind=="saturation_ratio" and hi==0
        if idx!=self._previous_stage or not split:
            self._ci_refilling=False
        self._previous_stage=idx
        limited=split or pre or recovering or (effective_drying and self.rescue_split_refill)
        if limited:
            if (infiltration_capacity_mm is None
                    or not np.isfinite([infiltration_capacity_mm,rain_mm]).all()
                    or infiltration_capacity_mm<=0 or rain_mm<0):
                raise ValueError("限量补水需要正的表层入渗能力和非负当日降雨")
        if split:
            if saturation_ratio>=.99:
                self._ci_refilling=False
            if trigger:
                self._ci_refilling=True
            trigger=self._ci_refilling and storage<=.5
        net=max(0.,float(deficit_mm or 0.)+hi-storage) if trigger else 0.
        # Daily retrospective rain input occupies inlet capacity before irrigation.
        # This is a conservative application budget, not a claim that all water
        # reaches the control layer or that the entire profile can accept Ksat.
        inlet=max(0.,float(infiltration_capacity_mm)+hi-storage-rain_mm) if limited else None
        applied=min(net,inlet) if limited else net
        irr=min(self.max_irr,applied/(self.app_eff/100.))
        # CI/AWD drainage is independent of irrigation targets; CF/SWI retain V2.
        # Drying removes standing water daily. Maturity starts with natural drydown.
        if not effective_drying:
            self._rescue_recovery=False
        elif irr>0:
            self._rescue_recovery=True
        elif storage<=.5:
            self._rescue_recovery=False
        cap=Z_BUND_MM
        if idx>=0:
            if cold: cap=Z_BUND_MM
            elif effective_drying: cap=self.rain_max_mm if self._rescue_recovery else 0.
            elif self.dap(date)>=self.maturity_dap: cap=0.
            else: cap=max(hi,self.rain_max_mm)
        rec=dict(日期=pd.Timestamp(date).strftime("%Y-%m-%d"),阶段=name,阶段序号=idx,
                 参数版本=PARAMETER_VERSION,控制层深度_m=self.control_depth_m,
                 田面水层_前=storage,控制层饱和度=saturation_ratio,
                 补至饱和缺水量_mm=deficit_mm,阈值类型=kind,下限=lo,上限=hi,有效触发下限=effective_lower,
                 晒田历日=max(0,dap-self.drying_start_dap) if idx==3 else 0,
                 晒田无明水晨间=bool(effective_drying and storage<=.5),
                 晒田救援分次=bool(effective_drying and self.rescue_split_refill and trigger),
                 灌水定额=irr,目标净补水_mm=net,限额截断=self.max_irr*self.app_eff/100.+1e-8<applied,
                 CI分次复水=bool(split and self._ci_refilling),当日补水入渗预算_mm=inlet,
                 分次延后净水量_mm=max(0.,net-applied),当日降雨_mm=float(rain_mm),
                 管理排水原因="",独立耐淹连续天数=0,
                 晒田前过渡=pre,实际晒田管理=effective_drying,提前结束晒田=ended_today,
                 晒田已结束=self._drying_closed,复水衔接=recovering,
                 衔接状态=("晒前过渡" if pre else "晒后复水" if recovering else
                           "提前复水后常规管理" if idx==3 and self._drying_closed else
                           "晒田救援" if effective_drying and self._rescue_recovery else
                           "明确晒田" if effective_drying else "常规管理"),
                 关键期=critical,保护规则启用=bool(critical and self.protect_critical),
                 管理排水_mm=0.,排水上限_mm=cap,灌溉触发=bool(trigger),
                 救援复水保留=self._rescue_recovery,犁底层底通量_mm=0.,
                 剖面底深层渗漏_mm=0.,犁底层饱和度_后=0.)
        self.records.append(rec)
        return float(irr)

    def finish_day(self,storage,ratio):
        rec=self.records[-1]
        idx=rec['阶段序号'];target=rec['上限'];cap=rec['排水上限_mm']
        if idx!=self._drain_previous_stage:
            self._excess_days=0
            self._independent_wet_days=0
        self._drain_previous_stage=idx
        independent=(idx==7 or self.natural_drydown_all or (self.is_ci and self.ci_natural_drydown) or (self.is_awd and self.awd_natural_drydown)
                     or rec["晒田前过渡"] or rec["复水衔接"] or (idx==3 and rec["晒田已结束"]))
        days=self.ci_ponding_limit_days if self.is_ci else self.awd_ponding_limit_days
        depth=self.ci_ponding_limit_depth_mm if self.is_ci else self.awd_ponding_limit_depth_mm
        drain_target=self.ci_duration_drain_target_mm if self.is_ci else self.awd_duration_drain_target_mm
        if independent:
            self._excess_days=0
            eligible=idx in (1,2,4,6,7) and rec['阶段']!='人工防冷保水' and not rec['晒田前过渡']
            if (eligible and days is not None
                    and storage>depth):
                self._independent_wet_days+=1
                if self._independent_wet_days>=days:
                    cap=min(cap,drain_target)
                    rec['管理排水原因']="独立耐淹时限"
            else:
                self._independent_wet_days=0
            rec['独立耐淹连续天数']=self._independent_wet_days
        elif idx>=0 and storage>target+.5 and rec['阶段']!='人工防冷保水' and not rec['救援复水保留']:
            self._excess_days+=1
            if self._excess_days>self.rain_retention_days: cap=min(cap,target)
        else: self._excess_days=0
        drain=max(0.,storage-cap) if idx>=0 else 0.
        if drain>0 and not rec['管理排水原因']:
            if rec['实际晒田管理'] and not rec['救援复水保留']: reason="明确晒田"
            elif cap==0 and rec['阈值类型']=="off": reason="成熟排水"
            elif not independent and self._excess_days>self.rain_retention_days: reason="原方案超目标时限"
            else: reason="超过允许蓄水深"
            rec['管理排水原因']=reason
        if drain<=0:
            rec['管理排水原因']=""
        if rec['CI分次复水'] and ratio>=.99:
            self._ci_refilling=False
        rec['最终排水上限_mm']=cap
        after=max(0.,storage-drain)
        rec.update(管理排水_mm=drain,田面水层_后=after,控制层饱和度_后=ratio,
                   连续超目标天数=self._excess_days,
                   补水后目标未达=bool(rec['灌水定额']>0 and (after+1.<target or ratio<.99)),
                   暂态积水=bool(rec['灌水定额']>0 and rec['阈值类型']=='saturation_ratio' and target==0 and after>.5),
                   控制期有明水=bool(rec['阈值类型']=='saturation_ratio' and target==0 and after>.5))
        return drain

    def to_frame(self):
        return pd.DataFrame(self.records)

_ORIGINAL_STEP=None
_ORIGINAL_INFILTRATION=None
_ACTIVE=None


def _recording_infiltration(*args, **kwargs):
    result=_ORIGINAL_INFILTRATION(*args, **kwargs)
    if _ACTIVE is not None and _ACTIVE.records:
        from config.soil_params import get_soil_config
        prof=args[0]
        bottom=get_soil_config("rice")["pan_bottom_m"]
        indices=np.flatnonzero(np.isclose(np.cumsum(prof.dz),bottom,atol=1e-8))
        if len(indices)!=1:
            raise RuntimeError("犁底层底边界未与计算单元对齐")
        _ACTIVE.records[-1]["犁底层底通量_mm"]=float(result[5][indices[0]])
    return result

def _hooked_solution_single_time_step(init_cond,param_struct,clock_struct,weather_step,outputs):
    ctrl=_ACTIVE
    if ctrl is None:
        return _ORIGINAL_STEP(init_cond,param_struct,clock_struct,weather_step,outputs)
    t=int(clock_struct.time_step_counter)
    ratio,deficit=control_layer_state(init_cond,param_struct,ctrl.control_depth_m)
    adjusted=ctrl.dap(clock_struct.step_start_time)-float(getattr(init_cond,'delayed_cds',0.))
    sched=param_struct.IrrMngt.Schedule
    if not 0<=t<len(sched): raise RuntimeError("水稻灌溉日历与模拟步长不一致")
    sched[t]=ctrl.step(clock_struct.step_start_time,init_cond.surface_storage,
                       float(weather_step[3]),ratio,deficit,adjusted,
                       infiltration_capacity_mm=float(param_struct.Soil.Profile.Ksat[0]),
                       rain_mm=float(weather_step[2]))
    result=_ORIGINAL_STEP(init_cond,param_struct,clock_struct,weather_step,outputs)
    new_cond=result[0]
    from config.soil_params import get_soil_config
    cfg=get_soil_config("rice")
    prof=param_struct.Soil.Profile
    top=cfg["pan_bottom_m"]-cfg["layers"][1]["depth"]
    weights=np.clip(cfg["pan_bottom_m"]-(np.cumsum(prof.dz)-prof.dz),0.,prof.dz)-np.clip(top-(np.cumsum(prof.dz)-prof.dz),0.,prof.dz)
    ctrl.records[-1]["犁底层饱和度_后"]=float(np.sum(new_cond.th*weights)/np.sum(prof.th_s*weights))
    ctrl.records[-1]["剖面底深层渗漏_mm"]=float(outputs.water_flux[t,9])
    ratio_after,_=control_layer_state(new_cond,param_struct,ctrl.control_depth_m)
    drain=ctrl.finish_day(float(new_cond.surface_storage),ratio_after)
    if drain>0:
        new_cond.surface_storage-=drain
        # Native water_flux: col5 surface storage, col8 surface runoff.
        # Management drainage is explicitly part of total surface outflow.
        outputs.water_flux[t,5]=new_cond.surface_storage
        outputs.water_flux[t,8]+=drain
    return result

def install(ctrl):
    global _ORIGINAL_STEP,_ORIGINAL_INFILTRATION,_ACTIVE
    if _ACTIVE is not None: raise RuntimeError("水稻逐日钩子禁止并发运行")
    import aquacrop.timestep.run_single_timestep as timestep
    _ORIGINAL_INFILTRATION=timestep.infiltration
    timestep.infiltration=_recording_infiltration
    _ORIGINAL_STEP=_core.solution_single_time_step
    _core.solution_single_time_step=_hooked_solution_single_time_step
    _ACTIVE=ctrl

def uninstall():
    global _ORIGINAL_STEP,_ORIGINAL_INFILTRATION,_ACTIVE
    _ACTIVE=None
    if _ORIGINAL_INFILTRATION is not None:
        import aquacrop.timestep.run_single_timestep as timestep
        timestep.infiltration=_ORIGINAL_INFILTRATION
        _ORIGINAL_INFILTRATION=None
    if _ORIGINAL_STEP is not None:
        _core.solution_single_time_step=_ORIGINAL_STEP
        _ORIGINAL_STEP=None
