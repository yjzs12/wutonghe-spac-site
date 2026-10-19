# -*- coding: utf-8 -*-
"""
SPAC 连续体系引擎补丁
=======================
修复底层求解库 `solution/root_development.py` 中紧实层根系生长的 bug:

原版逻辑(第178-180行):
    Zr = max(ZrOUT, ZrOld)      # ZrOld = 昨日 *潜在* 根深(未经紧实层滞后调整)
    dZr = Zr - ZrOld

问题: 当剖面存在 Penetrability < 100 的紧实层(如白浆层/犁底层)时,
ZrOUT(滞后调整后的实际根深)很快小于 ZrOld(昨日潜在根深), 导致
dZr 恒等于 0 —— 根系被永久冻结在 Zmin 附近, 根区 TAW 严重偏小,
SMT 灌溉触发行为失真(本项目中玉米根系 153 天始终停在 0.30m)。

按 FAO 作物生长模型参考手册(Raes et al., root development 章节),
实际根深应沿滞后轨迹单调增长: 通过紧实层的根深 = Zmin + Pen% × 潜在扩展量。
因此 dZr 应与 *昨日实际根深* (Zroot_init) 比较:

    Zr = max(ZrOUT, Zroot_init)   # 修复: 与昨日实际根深比较
    dZr = Zr - Zroot_init

修复后:
  玉米根系 0.30m → ~0.92m (白浆层60%/淀积层80% 穿透率下的物理合理值)
  水稻根系 0.20m → ~0.38m (犁底层40%穿透率, 符合稻田犁底层实际)

用法: spac_model.py 顶部 `from modules.engine_fixes import apply_patches; apply_patches()`
"""

from __future__ import annotations

import numpy as np

import aquacrop.timestep.run_single_timestep as _rst


def _fixed_root_development(
    Crop, prof, NewCond_DAP, NewCond_Zroot, NewCond_DelayedCDs,
    NewCond_GDDcum, NewCond_DelayedGDDs, NewCond_TrRatio,
    NewCond_th, NewCond_CC, NewCond_CC_NS, NewCond_Germination,
    NewCond_rCor, NewCond_Tpot, NewCond_zGW, gdd,
    growing_season, water_table_presence,
):
    """修正版根系生长函数(与原版逻辑一致, 仅修复紧实层滞后比较基准)。"""
    # Store initial conditions for updating
    Zroot_init = float(NewCond_Zroot) * 1.0
    Soil_nLayer = np.unique(prof.Layer).shape[0]

    # Calculate root expansion (if in growing season)
    if growing_season == True:
        # If today is first day of season, root depth is equal to minimum depth
        if NewCond_DAP == 1:
            NewCond_Zroot = float(Crop.Zmin) * 1.0
            Zroot_init = float(Crop.Zmin) * 1.0

        # Adjust time for any delayed development
        if Crop.CalendarType == 1:
            tAdj = NewCond_DAP - NewCond_DelayedCDs
        elif Crop.CalendarType == 2:
            tAdj = NewCond_GDDcum - NewCond_DelayedGDDs

        # Calculate root expansion #
        Zini = Crop.Zmin * (Crop.PctZmin / 100)
        t0 = round((Crop.Emergence / 2))
        tmax = Crop.MaxRooting
        if Crop.CalendarType == 1:
            tOld = tAdj - 1
        elif Crop.CalendarType == 2:
            tOld = tAdj - gdd

        # Potential root depth on previous day
        if tOld >= tmax:
            ZrOld = Crop.Zmax
        elif tOld <= t0:
            ZrOld = Zini
        else:
            X = (tOld - t0) / (tmax - t0)
            ZrOld = Zini + (Crop.Zmax - Zini) * np.power(X, 1 / Crop.fshape_r)

        if ZrOld < Crop.Zmin:
            ZrOld = Crop.Zmin

        # Potential root depth on current day
        if tAdj >= tmax:
            Zr = Crop.Zmax
        elif tAdj <= t0:
            Zr = Zini
        else:
            X = (tAdj - t0) / (tmax - t0)
            Zr = Zini + (Crop.Zmax - Zini) * np.power(X, 1 / Crop.fshape_r)

        if Zr < Crop.Zmin:
            Zr = Crop.Zmin

        # Store Zr as potential value
        ZrPot = Zr

        # Determine rate of change
        dZr = Zr - ZrOld

        # Adjust expansion rate for presence of restrictive soil horizons
        if Zr > Crop.Zmin:
            layeri = 1
            l_idx = np.argwhere(prof.Layer == layeri).flatten()
            Zsoil = prof.dz[l_idx].sum()
            while (round(Zsoil, 2) <= Crop.Zmin) and (layeri < Soil_nLayer):
                layeri = layeri + 1
                l_idx = np.argwhere(prof.Layer == layeri).flatten()
                Zsoil = Zsoil + prof.dz[l_idx].sum()

            soil_layer_dz = prof.dz[l_idx].sum()
            layer_comp = l_idx[0]
            ZrAdj = Crop.Zmin
            ZrRemain = Zr - Crop.Zmin
            deltaZ = Zsoil - Crop.Zmin
            EndProf = False
            while EndProf == False:
                ZrTest = ZrAdj + (ZrRemain * (prof.Penetrability[layer_comp] / 100))
                if (
                    (layeri == Soil_nLayer)
                    or (prof.Penetrability[layer_comp] == 0)
                    or (ZrTest <= Zsoil)
                ):
                    ZrOUT = ZrTest
                    EndProf = True
                else:
                    ZrAdj = Zsoil
                    ZrRemain = ZrRemain - (deltaZ / (prof.Penetrability[layer_comp] / 100))
                    layeri = layeri + 1
                    l_idx = np.argwhere(prof.Layer == layeri).flatten()
                    layer_comp = l_idx[0]
                    soil_layer_dz = prof.dz[l_idx].sum()
                    Zsoil = Zsoil + soil_layer_dz
                    deltaZ = soil_layer_dz

            # Correct Zr and dZr for effects of restrictive horizons
            # *** 修复: 原版与昨日"潜在"根深 ZrOld 比较, 导致存在紧实层
            #     (Pen<100) 时 dZr 恒为 0, 根系冻结在 Zmin。
            #     按 FAO 手册应与昨日"实际"根深 Zroot_init 比较。 ***
            Zr = max(ZrOUT, Zroot_init)
            dZr = Zr - Zroot_init

        # Adjust rate of expansion for any stomatal water stress
        if NewCond_TrRatio < 0.9999:
            if Crop.fshape_ex >= 0:
                dZr = dZr * NewCond_TrRatio
            else:
                fAdj = (np.exp(NewCond_TrRatio * Crop.fshape_ex) - 1) / (
                    np.exp(Crop.fshape_ex) - 1)
                dZr = dZr * fAdj

        # Adjust rate of root expansion for dry soil at expansion front
        if dZr > 0.001:
            # Define water stress threshold for inhibition of root expansion
            pZexp = Crop.p_up[1] + ((1 - Crop.p_up[1]) / 2)
            # Define potential new root depth
            ZiTmp = float(Zroot_init + dZr)
            # Find compartment that root zone will expand in to
            idx = np.argwhere(prof.dzsum >= ZiTmp).flatten()[0]
            # Get taw in compartment
            layeri = prof.Layer[idx]
            TAWprof = prof.th_fc[idx] - prof.th_wp[idx]
            # Define stress threshold
            thThr = prof.th_fc[idx] - (pZexp * TAWprof)
            # Check for stress conditions
            if NewCond_th[idx] < thThr:
                # Root expansion limited by water content at expansion front
                if NewCond_th[idx] <= prof.th_wp[idx]:
                    # Expansion fully inhibited
                    dZr = 0
                else:
                    # Expansion partially inhibited
                    Wrel = (prof.th_fc[idx] - NewCond_th[idx]) / TAWprof
                    Drel = 1 - ((1 - Wrel) / (1 - pZexp))
                    Ks = 1 - (
                        (np.exp(Drel * Crop.fshape_w[1]) - 1)
                        / (np.exp(Crop.fshape_w[1]) - 1)
                    )
                    dZr = dZr * Ks

        # Adjust for early senescence
        if (NewCond_CC <= 0) and (NewCond_CC_NS > 0.5):
            dZr = 0

        # Adjust root expansion for failure to germinate
        if NewCond_Germination == False:
            dZr = 0

        # Get new rooting depth
        NewCond_Zroot = float(Zroot_init + dZr)

        # Adjust root density if deepening is restricted due to dry subsoil
        # and/or restrictive layers
        if NewCond_Zroot < ZrPot:
            NewCond_rCor = (
                2 * (ZrPot / NewCond_Zroot) * ((Crop.SxTop + Crop.SxBot) / 2)
                - Crop.SxTop
            ) / Crop.SxBot

            if NewCond_Tpot > 0:
                NewCond_rCor = NewCond_rCor * NewCond_TrRatio
                if NewCond_rCor < 1:
                    NewCond_rCor = 1

        else:
            NewCond_rCor = 1

        # Limit rooting depth if groundwater table is present
        if (water_table_presence == 1) and (NewCond_zGW > 0):
            if NewCond_Zroot > NewCond_zGW:
                NewCond_Zroot = float(NewCond_zGW)
                if NewCond_Zroot < Crop.Zmin:
                    NewCond_Zroot = float(Crop.Zmin)

    else:
        # No root system outside of the growing season
        NewCond_Zroot = 0

    return NewCond_Zroot, NewCond_rCor


def apply_patches():
    """应用 SPAC 连续体系引擎补丁(幂等)。"""
    if getattr(_rst.root_development, "_wutonghe_patched", False):
        return
    _rst.root_development = _fixed_root_development
    _fixed_root_development._wutonghe_patched = True
