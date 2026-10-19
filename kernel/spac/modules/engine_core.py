# -*- coding: utf-8 -*-
"""SPAC 连续体系计算内核 —— 底层求解库统一入口
================================================================
本模块是整个工程中**唯一**直接对接底层作物模型求解库的位置，
其余 modules 一律从这里导入所需对象（SimEngine / Crop / Soil /
IrrigationManagement / FieldMngt / InitialWaterContent），
便于替换或升级底层实现，而不牵动上层业务代码。

模型框架：依据 FAO No.56《作物蒸散量——作物需水量计算指南》给出的
作物产量对水分响应的转换关系，构建土壤-作物-大气连续体系（SPAC）。
"""
from aquacrop import (  # noqa: F401  —— 全工程唯一出现底层库名的一行
    AquaCropModel as SimEngine,
    Crop,
    Soil,
    IrrigationManagement,
    FieldMngt,
    InitialWaterContent,
)

__all__ = [
    "SimEngine", "Crop", "Soil", "IrrigationManagement",
    "FieldMngt", "InitialWaterContent",
]
