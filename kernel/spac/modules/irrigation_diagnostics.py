"""Explain simulated rainfed and supplementary-irrigation outcomes.

No clipping of yields, forced optimum, or rainfall alteration is used here.
Tr/TrPot is a transpiration limitation indicator, not isolated water stress.
"""
import numpy as np
import pandas as pd
from config.crop_calendar import active_reference_date
from datetime import timedelta


def irrigation_evidence(results, summary):
    records=[]
    for crop in sorted({r.crop for r in results}):
        if crop!="corn":
            continue
        group=[r for r in results if r.crop==crop]
        rainfed=next((r for r in group if r.mode=="rainfed"),None)
        if rainfed is None:
            continue
        if abs(rainfed.summary["累计灌溉量(mm)"])>1e-8:
            raise ValueError("雨养对照存在灌水，不能标记为零灌溉")
        peak=max(group,key=lambda r:r.summary["最终籽粒产量(t/ha)"])
        yf=float(rainfed.summary["最终籽粒产量(t/ha)"])
        yp=float(peak.summary["最终籽粒产量(t/ha)"])
        retention=yf/yp
        d=rainfed.daily
        flux=rainfed.raw_water_flux.iloc[:len(d)].reset_index(drop=True)
        dates=pd.to_datetime(d["模拟日期"])
        active=flux["dap"].gt(0).to_numpy()
        first = active_reference_date(crop, "07-11", rainfed.sim_year)
        stop = active_reference_date(crop, "08-11", rainfed.sim_year)
        critical=((dates >= pd.Timestamp(first)) & (dates < pd.Timestamp(stop))).to_numpy() & active
        tr=flux["Tr"].to_numpy();pot=flux["TrPot"].to_numpy()
        limited=(pot>.1)&(tr<.95*pot)
        growth=rainfed.raw_crop_growth.iloc[:len(d)]
        hi_col="harvest_index" if "harvest_index" in growth else "harvest_index_adj"
        positives=np.flatnonzero(growth[hi_col].to_numpy()>1e-8)
        max_gain=(yp-yf)/yf*100 if yf>0 else None
        optimum = next((r for r in group if "墒情优化" in r.summary), None)
        message = f"雨养产量{yf:.4f} t/ha，最高模拟产量{yp:.4f} t/ha；相差{(yp-yf):.4f} t/ha。"
        if optimum is not None:
            message += f"最佳灌溉方式直接采用模型最优档位{optimum.label}，净灌水{optimum.summary['累计灌溉量(mm)']:.2f} mm。"
        record={"作物":"玉米","年份":rainfed.sim_year,"零灌溉产量(t/ha)":yf,
            "最高产方案":peak.label,"最高产量(t/ha)":yp,
            "最高产方案净灌水(mm)":float(peak.summary["累计灌溉量(mm)"]),
            "雨养达峰值(%)":retention*100,"雨养较峰值减产(%)":(1-retention)*100,
            "最大补灌增产(%)":max_gain,
            "生育期降雨(mm)":float(d.loc[active,"降雨量(mm)"].sum()),
            "生育期ET0(mm)":float(d.loc[active,"参考蒸散ET0(mm)"].sum()),
            "雨养生育期实际ET(mm)":float(d.loc[active,"蒸腾蒸发量(mm)"].sum()),
            "关键期降雨(mm)":float(d.loc[critical,"降雨量(mm)"].sum()),
            "雨养蒸腾受限天数":int((limited&active).sum()),
            "关键期蒸腾受限天数":int((limited&critical).sum()),
            "雨养最大冠层覆盖度(%)":float(rainfed.summary["最大冠层覆盖度(%)"]),
            "峰值方案最大冠层覆盖度(%)":float(peak.summary["最大冠层覆盖度(%)"]),
            "雨养生育期蒸腾(mm)":float(tr[active].sum()),
            "峰值方案生育期蒸腾(mm)":float(peak.raw_water_flux.loc[peak.raw_water_flux["dap"].gt(0),"Tr"].sum()),
            "关键期定义":f"{first:%m/%d}—{stop-timedelta(days=1):%m/%d}，抽雄吐丝授粉的旬级代表窗口（随种植及收获日期分配）",
            "首次正收获指数日期":str(dates.iloc[positives[0]].date()) if len(positives) else None,
            "结论":message,
            "限制":"降雨非全部有效降雨；ET为实际耗水。TrPot基于当时冠层，蒸腾受限天数少不能排除早期冠层受旱减产。初墒FC及土壤格网参数待验证。"}
        records.append(record)
    return pd.DataFrame(records)


def attach_source_demand(summary, settings):
    """Convert net demand once; never change crop growth with a conveyance loss."""
    table=summary.copy()
    crop_keys={"水稻":"rice","玉米":"corn"}
    for index,row in table.iterrows():
        item=settings[crop_keys[row["作物"]]]
        eta=item["coefficient"]
        net=float(row["累计灌溉量(mm)"])
        table.at[index,"利用系数类型"]=item["label"]
        table.at[index,"灌溉水利用系数"]=np.nan if eta is None else eta
        table.at[index,"水源取水量估算(mm)"]=np.nan if eta is None else net/eta
        table.at[index,"取水量估算(m³/亩)"]=np.nan if eta is None else net/eta/1.5
        table.at[index,"取水口径"]="未指定供水类型，不估算水源取水" if eta is None else "田间净量÷总利用系数，未重复折减田间效率"
    return table
