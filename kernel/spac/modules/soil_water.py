# -*- coding: utf-8 -*-
"""
土壤水分模块
============
对应大纲 2.3 土壤水分模块：
  将作物根区/田间水层考虑为一个水分储存层，跟踪降雨、灌溉、径流、蒸发、
  蒸腾、渗漏等通量，依据土壤水分平衡计算根区储水量与耗水量。
  土壤剖面分层，给定 SAT/FC/PWP/Ks/渗透系数，以 1 天为步长模拟水分运移。

本模块封装 SPAC 内核 的 Soil 实体构建，对接 config.soil_params 的分层配置。
采用 'custom' 土壤 + add_layer() 显式构建多层剖面，确保层数与水力参数可控。
"""

from __future__ import annotations

from modules.engine_core import Soil

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.soil_params import (
        PADDY_SOIL, DRYLAND_SOIL, get_evap_params, get_soil_config,
    )


def build_soil(crop: str) -> Soil:
    """根据作物构建 SPAC 内核 Soil 对象（custom 多层剖面）。

    Parameters
    ----------
    crop : 'rice' / 'corn'
    Returns
    -------
    Soil 对象，分层与水力参数已按梧桐河灌区配置；soil.nLayer 为实际层数。
    """
    cfg = get_soil_config(crop)
    layers = cfg["layers"]
    ep = get_evap_params(crop)   # 按作物获取蒸发深度与 CN

    # 构造 compartment 厚度 dz：以 0.1 m 为基本单元，覆盖全部土层总深
    total_depth = sum(lay["depth"] for lay in layers)
    n_comp = max(int(round(total_depth / 0.1)), 4)
    dz = [0.1] * n_comp  # 等厚 0.1m 的 compartment

    # 'custom' 仅创建 profile 骨架，再逐层 add_layer
    soil = Soil(
        soil_type="custom",
        dz=dz,
        cn=cfg.get("cn", ep["cn"]),
        calc_cn=0,
        adj_cn=1,          # 按土壤水分自动调整 CN
        adj_rew=1,         # 自动计算 readily evaporable water
        evap_z_surf=ep["evap_z_surf"],
        evap_z_min=ep["evap_z_min"],
        evap_z_max=ep["evap_z_max"],
        kex=1.1,
        f_evap=4,
        f_wrel_exp=0.4,
        fwcc=50,
        z_cn=0.3,
        z_germ=0.3,
        fshape_cr=16,
        z_top=0.1,
    )

    # 逐层添加：add_layer(thickness, thWP, thFC, thS, Ksat, penetrability)
    # layer1 的 thickness 即为该层底深；其后为各层自身厚度（累加到上一层底深）
    for i, lay in enumerate(layers):
        soil.add_layer(
            lay["depth"],
            lay["PWP"], lay["FC"], lay["SAT"],
            lay["Ks"], lay["penetrability"],
        )

    soil.Name = cfg["name"]
    return soil


def get_n_layers(crop: str) -> int:
    """返回配置的土壤层数。"""
    return len(get_soil_config(crop)["layers"])


def describe_soil(crop: str) -> str:
    """返回人类可读的土壤配置描述。"""
    cfg = get_soil_config(crop)
    lines = [f"土壤: {cfg['name']} (custom, {len(cfg['layers'])}层)"]
    for i, lay in enumerate(cfg["layers"], 1):
        lines.append(
            f"  层{i}: 深{lay['depth']:.2f}m  SAT={lay['SAT']:.2f} FC={lay['FC']:.2f} "
            f"PWP={lay['PWP']:.2f} Ks={lay['Ks']:.0f}mm/d"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    for c in ("rice", "corn"):
        print("\n" + describe_soil(c))
        s = build_soil(c)
        print(f"  -> Soil 构建成功: Name={s.Name} nComp={s.nComp} nLayer={s.nLayer}")
