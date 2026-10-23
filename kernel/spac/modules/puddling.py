"""Separate, auditable pre-transplant net water balance (lumped wetting profile).

Production calls use the simulated soil state immediately before preparation.
FC is only a fallback/reference for standalone sensitivity calculations. A five
day representative preparation period ends the day before transplanting. Daily
irrigation establishes the selected profile/pond storage, replaces evaporation
and effective percolation, and credits actual rainfall. This is a water-budget
model, not a claim to resolve transient infiltration through a plough pan.
No weather file or in-season solver flux is altered.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from modules.initial_condition import puddling_requirement,initial_layer_water,PADDY_INIT_WATER_LAYER
from config.soil_params import get_soil_config

PUDDLING_DAYS = 5
OPEN_WATER_EVAP_FACTOR = 1.05


def puddling_balance(weather, planting_date, moisture=None, days=PUDDLING_DAYS,
                     percolation_mm_day=None,initial_soil_water=None):
    if int(days)!=days or days<1:
        raise ValueError("泡田历时必须为正整数天")
    date=pd.Timestamp(planting_date)
    dates=pd.date_range(date-pd.Timedelta(days=days),date-pd.Timedelta(days=1))
    frame=weather.set_index(pd.to_datetime(weather["Date"]))
    if not dates.isin(frame.index).all():
        raise ValueError("气象数据未覆盖完整泡田期，不能以缺失值补零")
    needed=frame.loc[dates,["Precipitation","ReferenceET"]].astype(float)
    if not np.isfinite(needed.to_numpy()).all() or (needed<0).any().any():
        raise ValueError("泡田期降雨和参考蒸散必须完整、非负且有限")
    layers=get_soil_config("rice")["layers"]
    target_profile=initial_layer_water("rice",moisture)
    initial=[lay["FC"] for lay in layers] if initial_soil_water is None else list(initial_soil_water)
    if len(initial)!=len(layers) or not np.isfinite(initial).all():
        raise ValueError("泡田前分层含水率缺失或层数不匹配")
    soil_before=sum(v*lay["depth"]*1000 for v,lay in zip(initial,layers))
    soil_target=sum(v*lay["depth"]*1000 for v,lay in zip(target_profile,layers))
    target=soil_target-soil_before+PADDY_INIT_WATER_LAYER
    if target<0:
        raise ValueError("泡田前储水高于目标，需显式排水情景，不能以负灌溉处理")
    # Effective leakage is uncertain, not an independent observation of Ksat.
    leak=float(layers[min(1, len(layers)-1)]["Ks"] if percolation_mm_day is None else percolation_mm_day)
    if not np.isfinite(leak) or leak<0:
        raise ValueError("泡田渗漏强度无效")
    storage=0.
    records=[]
    for i,(day,row) in enumerate(needed.iterrows()):
        before=storage
        rain=float(row.Precipitation)
        evap=OPEN_WATER_EVAP_FACTOR*float(row.ReferenceET)
        # Prescribed gradual wetting; the final state equals the crop initial state.
        planned=target*(i+1)/days
        irrigation=max(0.,planned-before+evap+leak-rain)
        available=before+rain+irrigation-evap-leak
        overflow=max(0.,available-target)
        storage=available-overflow
        residual=before+rain+irrigation-evap-leak-overflow-storage
        records.append(dict(Date=day,rain_mm=rain,evaporation_mm=evap,
                            percolation_mm=leak,irrigation_net_mm=irrigation,
                            overflow_mm=overflow,storage_before_mm=before,
                            storage_after_mm=storage,storage_change_mm=storage-before,
                            balance_residual_mm=residual,soil_before_period_mm=soil_before,
                            soil_target_mm=soil_target,reference_fc_storage_increment_mm=puddling_requirement("rice",moisture),
                            **{f"initial_theta_{j}":v for j,v in enumerate(initial,1)}))
    result=pd.DataFrame(records)
    if abs(storage-target)>1e-8 or result.balance_residual_mm.abs().max()>1e-8:
        raise RuntimeError("泡田水量平衡未闭合")
    return result
