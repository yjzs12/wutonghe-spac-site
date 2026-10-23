# -*- coding: utf-8 -*-
"""Initial water and net puddling storage share the same profile boundary.

Rice candidate V2: puddled 0--0.2 m SAT; deeper layers FC, plus 30 mm ponding.
This is an explicit unmeasured scenario, not proof of the local saturation depth.
This module supplies the storage increment from an FC reference. The complete
preparation water budget is evaluated separately by modules.puddling, including
preparation ET/percolation/rainfall. Storage input and drainage output
are separate water-balance terms; drainage is never deducted from this input.
"""
from __future__ import annotations
from modules.engine_core import InitialWaterContent

DEFAULT_MOISTURE = {"rice": "PROFILE", "corn": "FC"}
LEGACY_MOISTURE = {"rice": "FC", "corn": "FC"}
PADDY_INIT_WATER_LAYER = 30.0
PUDDLING_SAT_DEPTH = 0.20


def _crop_key(crop: str) -> str:
    c = (crop or "").lower()
    return "rice" if c in ("rice", "paddy", "水稻") else "corn" if c in ("corn", "maize", "玉米") else c


def initial_layer_water(crop: str, moisture=None):
    """Return explicit volumetric water fractions for the actual soil layers."""
    from config.soil_params import get_soil_config
    key = _crop_key(crop)
    mode = DEFAULT_MOISTURE.get(key, "FC") if moisture is None or moisture == "" else moisture
    values, top = [], 0.0
    for lay in get_soil_config(crop)["layers"]:
        if mode == "PROFILE" and key == "rice":
            overlap = max(0., min(top + lay["depth"], get_soil_config(crop).get("puddling_sat_depth_m", PUDDLING_SAT_DEPTH)) - top)
            value = lay["FC"] + (lay["SAT"] - lay["FC"]) * overlap / lay["depth"]
        elif mode in ("SAT", "FC", "WP"):
            value = lay["PWP"] if mode == "WP" else lay[mode]
        else:
            value = float(mode)
        if not 0 <= value <= lay["SAT"]:
            raise ValueError("初始体积含水率超出土层可容纳范围")
        values.append(float(value))
        top += lay["depth"]
    return values


def puddling_requirement(crop: str, moisture=None) -> float:
    """FC-to-profile storage increment, NOT the complete preparation irrigation."""
    from config.soil_params import get_soil_config
    if _crop_key(crop) != "rice":
        return 0.0
    layers = get_soil_config(crop)["layers"]
    values = initial_layer_water(crop, moisture)
    return sum(max(value-lay["FC"], 0.)*1000*lay["depth"] for value,lay in zip(values,layers)) + PADDY_INIT_WATER_LAYER


def build_initial_water_content(crop: str, moisture=None, n_layers: int=3):
    values = initial_layer_water(crop, moisture)
    if len(values) != n_layers:
        raise ValueError("初始水分层数与土壤剖面不一致")
    return InitialWaterContent(wc_type="Num", method="Layer",
                              depth_layer=list(range(1,n_layers+1)), value=values)


def describe_initial_condition(crop: str, moisture=None) -> str:
    key = _crop_key(crop)
    profile = initial_layer_water(crop, moisture)
    text = f"初始条件: {key} 分层体积含水率=" + ",".join(f"{v:.3f}" for v in profile)
    if key == "rice":
        text += f"；插秧水层{PADDY_INIT_WATER_LAYER:.0f}mm；FC参考净泡田储水估算{puddling_requirement(crop,moisture):.1f}mm"
    return text
