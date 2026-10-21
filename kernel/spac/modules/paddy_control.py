# -*- coding: utf-8 -*-
"""
水稻田面水层闭环调控（按生育阶段水层上下限反馈灌水）
=====================================================
替代「固定日期 + 固定定额」的开环日历：**降到阶段下限就灌，一次灌到阶段上限**。

为什么要在项目侧实现（而不是直接用内核灌溉方法）
------------------------------------------------
底层求解库只有 6 种灌溉方法（0 不灌 / 1 SMT / 2 固定间隔 / 3 固定日历 /
4 净灌溉 / 5 每日定深），**没有一种以田面水层 `surface_storage` 为触发量**。
其中 SMT(=1) 的补水逻辑是 `Irr = max(0, 根区亏缺)`，只补到田间持水，
**永远不产生田面积水**，因此对淹水水稻不适用（会退化成湿润灌溉）。

本模块用「逐日钩子 + method=3 日历」实现闭环：
  每天都算前读取上一天日末的 `surface_storage`，改写当日 `Schedule[t]`，
  内核照 method=3 读取 → 不改一行内核代码，行为却等价于反馈控制。

状态量定义（决定「下限」怎么读）
--------------------------------
    有水(storage > 0.5 mm)：状态 d = storage                      （mm 水层）
    无水(storage ≤ 0.5 mm)：状态 d = -（落干累计蒸散量，mm）        （负值）

  ⇒ 下限 lo ≥ 0 ：水层浅到 lo 就复水 —— 浅水勤灌 / 长期淹水
  ⇒ 下限 lo < 0 ：田面先落干，再累计蒸散 |lo| mm 才复水 —— 干湿交替(AWD)

同一张 (lo, hi) 表即可表达从「雨养型补充」到「长期深水」的整条水量梯度。

灌水量换算
----------
    当日灌水定额(毛) = (hi − storage_before) / (AppEff/100)
    AppEff=90 ⇒ 多 11% 输水损失；单次最大受 MaxIrr=300 mm 限制。
    z_bund=200 mm（modules/irrigation.py::build_field_management）确保
    hi ≤ 190 mm 时不因田埂溢流而失控。

⚠ 泡田水仍然无法生效：内核 `solution/irrigation.py` 在 `growing_season=False`
  时强制 Irr=0，而移栽日(05/15)之前不属于生长季。初始水层由
  `bund_water=20 mm` 提供（`reset_initial_conditions.py`）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# =========================================================================== #
#  生育阶段划分（月-日；年份由模拟年份注入，跨年不适用）
# =========================================================================== #

STAGES: List[Tuple[str, str, str]] = [
    ("返青",     "05-15", "05-31"),
    ("分蘖",     "06-01", "06-30"),
    ("晒田",     "07-01", "07-09"),
    ("拔节孕穗", "07-10", "07-31"),
    ("抽穗开花", "08-01", "08-20"),
    ("灌浆",     "08-21", "09-07"),
]
STAGE_NAMES: List[str] = [s[0] for s in STAGES]

# =========================================================================== #
#  5 档「阶段水层上下限」表  ——  (下限 lo, 上限 hi)，单位 mm
#
#  lo ≥ 0 : 水层降到 lo 即复水到 hi          （浅水勤灌）
#  lo < 0 : 田面落干后累计蒸散 |lo| mm 复水到 hi（干湿交替 AWD）
#  hi = 0 : 该阶段不灌水（晒田；09/08 之后强制断水）
#
#  为什么是 5 档而不是 7 档（2026-10-21 判定）
#  ------------------------------------------
#  本模型深层渗漏由常数 Ksat 控制，不随水头升高而增大 ⇒ 一旦田面长期有水：
#      全季灌水量 ≈ ET + Ksat×有水天数
#  即「水层越深 ≠ 灌水越多」，真正能形成梯度的是**淹没天数**。当某档做到
#  每一天都有水（2026 年 L7 = 133/133 天），再抬上限只增加积水与溢流。
#  实测 7 档时 L4–L7 产量完全相同（多灌 319 mm 换来 0 产量），只有 4 个
#  有效信息点；5 档可让相邻档灌水量与产量都真正错开。
#
#  农学依据（黑龙江寒地水稻，上限一律 ≤70 mm = 7 cm）
#  ------------------------------------------------
#  · 黑龙江省农业农村厅《主要粮油作物田间管理技术指导意见》：
#      返青后保持 2-3 cm 浅水；茎数达收获穗数 80% 晒田 5-7 天；晒田后恢复
#      3 cm 浅水、每次灌水 3-5 cm、自然落干再灌；抽穗扬花后浅湿交替，
#      乳熟后干湿交替，黄熟期排水。
#  · DB23《盐碱地水稻高产种植技术规程》：返青 3-5 cm（重盐碱 6 cm）；
#      拔节孕穗 3-5 cm；抽穗开花 3-5 cm；乳熟 ≤3 cm；黄熟 2 cm 薄水。
#  · 《寒地水稻浅湿干节水灌溉栽培技术规程》：插秧至返青灌苗高 2/3；
#      分蘖前期落干后灌 2-3 cm；孕穗至开花 4-6 cm；抽穗扬花 5-7 cm。
#  · 低温护胎：孕穗期遇 <17-18 ℃ 灌 15-20 cm 深水（模型无低温护胎分支，
#      第五档返青/分蘖上限 55/45 mm 只取「深水护苗」的温和档）。
#  ⇒ 各档上限 15-70 mm 全部落在 1.5-7 cm 的寒地常规区间内，200 mm 田埂
#     留足裕度（不再出现 190 mm 那种农学上无法解释的水深）。
# =========================================================================== #

RICE_WATER_BANDS: Dict[str, List[Tuple[float, float]]] = {
    #                        返青        分蘖        晒田     拔节孕穗     抽穗开花      灌浆
    #  L1 雨养型：只在极度缺水时给一次救命水，等价"雨养 + 保苗"
    "雨养型":   [(-60, 15), (-40, 10), (0, 0), (-30, 20), (-30, 20), (-60, 10)],
    #  L2 低水量：干湿交替(AWD)，落干深、复水薄
    "低水量":   [(-40, 20), (-25, 20), (0, 0), (-10, 35), (-10, 35), (-50, 15)],
    #  L3 中低水量：分蘖仍允许落干，关键期不再落干
    "中低水量": [(-20, 30), (-12, 30), (0, 0), (  0, 45), (  0, 45), (-30, 20)],
    #  L4 适宜水量：贴合省级指导意见（返青 2-3cm、关键期 5cm、灌浆 ≤3cm）
    "适宜水量": [ (  0, 45), (  0, 40), (0, 0), ( 10, 55), ( 10, 55), (-15, 30)],
    #  L5 较高水量：常淹 + 上限取 5.5-7cm（浅湿干规程抽穗 5-7cm 的上沿）
    "较高水量": [ ( 10, 55), (  5, 45), (0, 0), ( 20, 70), ( 20, 70), (  0, 30)],
}

# =========================================================================== #
#  正常灌溉对照（CK）—— 官方规程参数化，2026-10-21 起也走水层调控（方案 A）
#
#  为什么改：原先 CK 是「30 个固定日期 + 6 种定额」的开环日历（合计 891 mm，
#  实际入田 816 mm），缺点有三 ——
#    ① 不看田面状态就灌：70 mm 复水定额叠在残留水层上，2026 年水层冲到 197 mm
#       （田埂 200 mm，已开始溢流），农学上无法解释；
#    ② 遇降雨照灌：2026 分蘖期 6/04-6/15 降雨补足，固定日历仍按 6/04、6/09、
#       6/14 各灌 18 mm，白灌 54 mm；
#    ③ 定额 12/18/25/35/45/70 六档跳变，无法用一份文件说清其出处。
#  改为闭环后：定额只剩 33.3 mm(=30/0.9) 与 55.6 mm(=50/0.9) 两档，
#  间隔由水层与降雨自适应，可直接引省级规程。
#
#  依据（与上表 5 档同源）：
#    · 返青后保持 2-3 cm 浅水              → (0, 30)
#    · 分蘖 2-3 cm，茎数达收获穗数 80% 晒田 → (0, 30) / 晒田 (0,0)
#    · 晒田后恢复 3 cm、每次灌水 3-5 cm    → 拔节孕穗 (0, 50)
#    · 抽穗扬花 3-5 cm（浅湿干规程 5-7 cm 取整到 5） → (0, 50)
#    · 乳熟 ≤3 cm、干湿交替                → (0, 30)
#
#  ⚠⚠ 下限 0 的准确含义 = **田面明水层厚度为 0（田面见底）**，不是土壤缺水：
#    2025 实测（outputs/_下限0_土壤水分检验.csv）CK 田面无水的 44 天里，
#    耕作层(0-0.3 m) θ 均值 0.486，仍高于田间持水 FC=0.367（相对饱和 56%），
#    只有 4 天跌破 FC ⇒ 这是「湿」不是「干」，与农户"田面没水就灌"的判据一致。
#  ⚠ 因此 CK 的实质是「浅水勤灌 / 湿润灌溉」。若论文要称「浅湿干」，必须至少让
#    1-2 个非关键期取负下限（如分蘖末 −15、灌浆 −20），否则"干期"名不副实。
#  ⚠ 与晒田 (0, 0) 的区别：`hi=0` = 该阶段**不灌水**（step() 里 `if hi > 0`
#    直接短路）；`lo=0 且 hi>0` = **见底即复水**。同一个 0 在表里是两种语义，
#    论文表格引用时必须写成「下限 0（田面见底）」而不是只写数字。
# =========================================================================== #

CK_BANDS: List[Tuple[float, float]] = [
    (0, 30),   # 返青     2-3 cm 浅水
    (0, 30),   # 分蘖     2-3 cm
    (0,  0),   # 晒田     不灌水（07/01-07/09 落干）
    (0, 50),   # 拔节孕穗 3-5 cm
    (0, 50),   # 抽穗开花 3-5 cm
    (0, 30),   # 灌浆     乳熟 ≤3 cm
]

# 田埂高度(mm)：上限必须低于它，否则单次灌水必溢流
Z_BUND_MM = 200.0


def bands_for(level_label: str) -> List[Tuple[float, float]]:
    """取某档位的水层上下限表（6 个阶段）。"""
    if level_label not in RICE_WATER_BANDS:
        raise KeyError(f"未定义的档位: {level_label}；可选 {list(RICE_WATER_BANDS)}")
    return [tuple(b) for b in RICE_WATER_BANDS[level_label]]


def bands_for_ck() -> List[Tuple[float, float]]:
    """取正常灌溉对照(CK)的上下限表。

    返回新列表：下游 `PaddyLayerController` 会保存引用，若直接返回模块常量，
    一旦有人就地改写就会污染整个进程。故一律给副本。
    """
    return [tuple(b) for b in CK_BANDS]


def validate_bands(bands: Sequence[Tuple[float, float]]) -> None:
    """表校验：长度、下限<上限、上限不超田埂。"""
    if len(bands) != len(STAGES):
        raise ValueError(f"上下限表长度({len(bands)})应为 {len(STAGES)} 个生育阶段")
    for (lo, hi), name in zip(bands, STAGE_NAMES):
        if hi < 0:
            raise ValueError(f"[{name}] 上限为负: {hi}")
        if hi > 0 and lo >= hi:
            raise ValueError(f"[{name}] 下限({lo}) 必须小于上限({hi})")
        if hi > Z_BUND_MM - 5:
            raise ValueError(f"[{name}] 上限({hi}) 超过田埂({Z_BUND_MM}mm)裕度，请下调")


def describe_bands(bands: Sequence[Tuple[float, float]]) -> str:
    """人类可读的一句话概要，用于运行日志。

    三种写法严格区分语义（同一个 0 有两种含义，不能都写成"0"）：
        hi = 0          → 不灌（晒田）
        lo = 0, hi > 0  → 见底→hi（田面明水消失即复水）
        lo > 0          → lo-hi（保水，水层浅到 lo 就复水）
        lo < 0          → 干|lo|mm→hi（落干后累计蒸散 |lo| 才复水）
    """
    parts = []
    for (lo, hi), name in zip(bands, STAGE_NAMES):
        if hi <= 0:
            parts.append(f"{name}:不灌")
        elif lo > 0:
            parts.append(f"{name}:{lo:.0f}-{hi:.0f}")
        elif lo == 0:
            parts.append(f"{name}:见底→{hi:.0f}")
        else:
            parts.append(f"{name}:干{-lo:.0f}mm→{hi:.0f}")
    return " | ".join(parts)


# =========================================================================== #
#  控制器
# =========================================================================== #

def _month_day(s: str) -> Tuple[int, int]:
    m, d = s.split("-")
    return int(m), int(d)


_STAGE_MD = [(_month_day(a), _month_day(b)) for _, a, b in STAGES]


class PaddyLayerController:
    """按生育阶段水层上下限生成当日灌水定额（逐日反馈）。"""

    def __init__(self, bands: Sequence[Tuple[float, float]],
                 app_eff: float = 90.0, max_irr: float = 300.0):
        validate_bands(bands)
        self.bands = [tuple(b) for b in bands]
        self.app_eff = float(app_eff)
        self.max_irr = float(max_irr)
        self.dry_loss = 0.0          # 落干累计蒸散量(mm)
        self.records: List[dict] = []

    # ---- 阶段判定 ---- #
    @staticmethod
    def stage_index(date: pd.Timestamp) -> int:
        """返回阶段索引；断水期/非生育期返回 -1。"""
        md = (date.month, date.day)
        for i, (a, b) in enumerate(_STAGE_MD):
            if a <= md <= b:
                return i
        return -1

    # ---- 单日决策 ---- #
    def step(self, date: pd.Timestamp, storage: float, et0: float) -> float:
        """返回当日应施加的毛灌水定额(mm)。

        Parameters
        ----------
        date    : 当日日期
        storage : 上一天日末的田面水层(mm)
        et0     : 当日参考蒸散量(mm/d)，仅用于「落干期」的状态累积
        """
        i = self.stage_index(date)
        if i < 0:
            name, lo, hi = "断水/非生育期", 0.0, 0.0
        else:
            name = STAGE_NAMES[i]
            lo, hi = self.bands[i]

        storage = max(float(storage), 0.0)
        if storage > 0.5:
            self.dry_loss = 0.0
            state = storage
        else:
            state = -self.dry_loss

        irr = 0.0
        if hi > 0 and state <= lo:
            irr = (hi - storage) / (self.app_eff / 100.0)
            irr = float(min(self.max_irr, max(irr, 0.0)))
            self.dry_loss = 0.0
        elif storage <= 0.5:
            # 仍处于落干期：按 ET0 累积"缺水程度"，使负下限可比
            self.dry_loss += max(float(et0), 0.0)

        self.records.append(dict(
            日期=pd.Timestamp(date).strftime("%Y-%m-%d"), 阶段=name,
            田面水层_前=round(storage, 2), 状态量=round(state, 2),
            下限=lo, 上限=hi, 灌水定额=round(irr, 2),
        ))
        return irr

    # ---- 审计表 ---- #
    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.records)


# =========================================================================== #
#  内核钩子：改写当日 Schedule[time_step_counter]
# =========================================================================== #

from modules.engine_core import core_module as _core  # noqa: E402  —— 底层库统一入口

_ORIGINAL_STEP = None
_ACTIVE: Optional[PaddyLayerController] = None


def _hooked_solution_single_time_step(init_cond, param_struct, clock_struct,
                                      weather_step, outputs):
    ctrl = _ACTIVE
    if ctrl is not None:
        t = int(clock_struct.time_step_counter)
        sched = param_struct.IrrMngt.Schedule
        if 0 <= t < len(sched):
            ts = pd.Timestamp(clock_struct.step_start_time)
            sched[t] = ctrl.step(ts, init_cond.surface_storage,
                                 float(weather_step[3]))
    return _ORIGINAL_STEP(init_cond, param_struct, clock_struct,
                          weather_step, outputs)


def install(ctrl: PaddyLayerController) -> None:
    """启用闭环控制（幂等）。"""
    global _ORIGINAL_STEP, _ACTIVE
    if _ORIGINAL_STEP is None:
        _ORIGINAL_STEP = _core.solution_single_time_step
        _core.solution_single_time_step = _hooked_solution_single_time_step
    _ACTIVE = ctrl


def uninstall() -> None:
    """关闭闭环（幂等）——恢复内核原函数。"""
    global _ORIGINAL_STEP, _ACTIVE
    _ACTIVE = None
    if _ORIGINAL_STEP is not None:
        _core.solution_single_time_step = _ORIGINAL_STEP
        _ORIGINAL_STEP = None


if __name__ == "__main__":
    for lvl, b in RICE_WATER_BANDS.items():
        validate_bands(b)
        print(f"{lvl:6s} {describe_bands(b)}")
