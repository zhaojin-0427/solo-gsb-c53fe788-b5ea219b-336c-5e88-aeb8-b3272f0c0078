"""分码联合整数求解器（OR-Tools CP-SAT）。

约定（整件宽度均以“针”计，前/后片宽度为整件的一半）：
  大身: cast_on(下摆整片, 偶数) -> chest(腋下整片, 偶数) -> 挂肩两侧收针 -> 肩
  袖:   cuff_on(袖口整片, 偶数) -> upper(袖肥整片, 偶数) -> 袖山两侧收针 -> 山顶平收
  前领: 腋下挂肩内，先平收若干针，再单侧阶梯收；后领仅顶端平收
  肩:   两侧交替阶梯平收（前后同 nsh、每阶梯同 ksh 针 -> 肩缝针/行匹配）

行结构（行，均为整数；前后片总高相同，袖山行 = 挂肩行）：
  body : hem_rib_rows + side_rows + arm_rows
          arm_rows 内: 先挂肩收针，再肩阶梯（2*nsh 行）；前领深度落在该区
  sleeve: cuff_rib_rows + inc_rows + arm_rows   (袖山行严格等于挂肩行)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

from .default_data import DEFAULT_SEAMS

# ---- 目标字段顺序（与误差、冲突标注一致） ----
MEASUREMENT_KEYS = [
    "bust", "hem", "shoulder", "neck", "fn_depth", "bn_depth",
    "length", "armhole", "sleeve_len", "cap_height", "upper_arm", "cuff",
]
ROW_MEASUREMENTS = {"fn_depth", "bn_depth", "length", "armhole", "sleeve_len", "cap_height"}

RIB_PERIOD = {"1x1": 2, "2x2": 4}

# 求解器计时（秒）
SOLVER_TIME_LIMIT = 20.0

# 目标权重：最大尺寸误差 >> 塑形事件总数 >> 相邻尺码跳变
W_ERR = 1_000_000
W_EVENT = 10
W_JUMP = 1


@dataclass
class SizeVars:
    """单个尺码的决策变量。"""
    idx: int
    name: str
    cast_on: cp_model.IntVar
    chest: cp_model.IntVar
    w2: cp_model.IntVar          # 整片肩点间宽
    nb: cp_model.IntVar          # 前/后领平收（整片针，偶数）
    ksh: cp_model.IntVar         # 每肩阶梯每次平收针数（每侧）
    nsh: cp_model.IntVar         # 每侧肩阶梯次数
    bnd: cp_model.IntVar         # 后领平收行（1 或 2）
    fn_depth: cp_model.IntVar    # 前领深度（行）
    arm_rows: cp_model.IntVar
    cap_rows: cp_model.IntVar     # 袖山行数（独立于挂肩行）
    side_rows: cp_model.IntVar
    hem_rib_rows: cp_model.IntVar
    cuff_on: cp_model.IntVar
    upper: cp_model.IntVar
    cap_top: cp_model.IntVar     # 袖山顶端平收整片针（偶数）
    inc_rows: cp_model.IntVar
    cuff_rib_rows: cp_model.IntVar
    # 事件槽（Nah=12, Ncap=16, 前领 Nf=12）
    nah_used: cp_model.IntVar
    arm_first: cp_model.IntVar          # 挂肩首次大平收（每侧 1..5）
    arm_events: list[cp_model.IntVar]   # 其余每次（1..3），槽位 0..Nah-1
    ncap_used: cp_model.IntVar
    cap_first: cp_model.IntVar          # 袖山首次大平收（每侧 1..8）
    cap_events: list[cp_model.IntVar]
    nf_used: cp_model.IntVar
    fn_events: list[cp_model.IntVar]    # 前领阶梯每次 1..3
    # 误差（百分之一 cm 绝对值）
    errors: dict[str, cp_model.IntVar] = field(default_factory=dict)
    # 派生量（便于编译器取值）
    total_side_inc: cp_model.IntVar | None = None
    total_arm_dec: cp_model.IntVar | None = None
    total_cap_dec: cp_model.IntVar | None = None
    total_fn_dec: cp_model.IntVar | None = None
    total_inc: cp_model.IntVar | None = None


def _add_mod_equal(model: cp_model.CpModel,
                   a: cp_model.IntVar | int, b: cp_model.IntVar | int,
                   m: int, name: str,
                   enforcer: cp_model.IntVar | None = None) -> None:
    """约束 a ≡ b (mod m)（a、b 均为正），可用 bool 变量条件启用。

    CP-SAT 的 add_modulo_equality 要求余数域非负；这里为两侧各建一个 [0, m-1]
    的余数变量再令其相等。
    """
    ra = model.new_int_var(0, m - 1, f"{name}_ra")
    rb = model.new_int_var(0, m - 1, f"{name}_rb")
    if enforcer is None:
        model.add_modulo_equality(ra, a, m)
        model.add_modulo_equality(rb, b, m)
        model.add(ra == rb)
    else:
        model.add_modulo_equality(ra, a, m).only_enforce_if(enforcer)
        model.add_modulo_equality(rb, b, m).only_enforce_if(enforcer)
        model.add(ra == rb).only_enforce_if(enforcer)


def _band_for(cm_value: float, per_cm: float, tol_100th_cm: int,
               is_row: bool) -> tuple[int, int, int]:
    """把 cm 目标换算为整数针/行的可行带 [lo, hi]，并返回中心值（针/行）。

    注意：不夹入绝对 bounds——超界目标应显式成为不可行冲突，而不是静默截断。
    """
    exact = cm_value * per_cm
    center = int(round(exact))
    tol_units = max(1, int(math.ceil(tol_100th_cm / 100.0 * per_cm - 1e-9)))
    lo = center - tol_units
    hi = center + tol_units
    if is_row:
        lo = max(lo, 1)
    else:
        lo = max(lo, 0)
    return lo, hi, center


def solve_project(project: dict[str, Any], _in_diagnosis: bool = False,
                  _loose_fields: set[tuple[int, str]] | None = None) -> dict[str, Any]:
    """联合求解所有尺码，返回编译/模拟所需的完整结果。"""
    sizes_in = project.get("sizes", [])
    if not sizes_in:
        raise ValueError("至少需要一个尺码")

    g = project["gauge"]
    sp = float(g["stitches_per_10cm"]) / 10.0   # 针 / cm
    rp = float(g["rows_per_10cm"]) / 10.0       # 行 / cm
    tol_cm = float(project.get("tolerance_cm", 0.5))
    tol100 = int(round(tol_cm * 100))           # 容差（1/100 cm）

    pat = project.get("pattern", {})
    P = int(pat.get("p_horizontal", 1))
    Q = int(pat.get("q_vertical", 1))
    phase_lock = bool(pat.get("seam_phase_lock", False))
    seams = project.get("seams") or DEFAULT_SEAMS
    rib = project.get("rib", {})
    hem_period = RIB_PERIOD.get(rib.get("hem_rib_type", "1x1"), 2)
    cuff_period = RIB_PERIOD.get(rib.get("cuff_rib_type", "1x1"), 2)

    # 诊断模式：被放宽字段使用 50cm 的有效容差（域与误差界同时放开）
    def eff_tol(idx: int, key: str) -> int:
        if _loose_fields and (idx, key) in _loose_fields:
            return 5000
        return tol100

    s_scale = max(1, int(round(100.0 / sp)))   # 每针 = 100/sp 个 1/100cm
    r_scale = max(1, int(round(100.0 / rp)))   # 每行 = 100/rp 个 1/100cm

    model = cp_model.CpModel()
    svars: list[SizeVars] = []

    Nah, Ncap, Nfn, Nsh_max = 12, 16, 12, 8

    for i, sz in enumerate(sizes_in):
        t = sz["targets"]
        tag = sz.get("name", f"#{i+1}")
        pfx = f"s{i}_"

        def iv(key: str, lo: int, hi: int) -> cp_model.IntVar:
            return model.new_int_var(lo, hi, f"{pfx}{key}")

        # ---- 宽度带（针，偶数由结构约束保证） ----
        # 围度类目标（量一圈）先折半为衣片整片宽
        bust_lo, bust_hi, bust_c = _band_for(t["bust"] / 2.0, sp, eff_tol(i, "bust"), False)
        hem_lo, hem_hi, hem_c = _band_for(t["hem"] / 2.0, sp, eff_tol(i, "hem"), False)
        sh_lo, sh_hi, sh_c = _band_for(t["shoulder"], sp, eff_tol(i, "shoulder"), False)
        neck_lo, neck_hi, neck_c = _band_for(t["neck"], sp, eff_tol(i, "neck"), False)
        up_lo, up_hi, up_c = _band_for(t["upper_arm"] / 2.0, sp, eff_tol(i, "upper_arm"), False)
        cuff_lo, cuff_hi, cuff_c = _band_for(t["cuff"] / 2.0, sp, eff_tol(i, "cuff"), False)
        fn_lo, fn_hi, fn_c = _band_for(t["fn_depth"], rp, eff_tol(i, "fn_depth"), True)
        bn_lo, bn_hi, bn_c = _band_for(t["bn_depth"], rp, eff_tol(i, "bn_depth"), True)
        len_lo, len_hi, len_c = _band_for(t["length"], rp, eff_tol(i, "length"), True)
        arm_lo, arm_hi, arm_c = _band_for(t["armhole"], rp, eff_tol(i, "armhole"), True)
        sl_lo, sl_hi, sl_c = _band_for(t["sleeve_len"], rp, eff_tol(i, "sleeve_len"), True)
        cap_lo, cap_hi, cap_c = _band_for(t["cap_height"], rp, eff_tol(i, "cap_height"), True)

        chest = iv("chest", bust_lo, bust_hi)
        cast_on = iv("cast_on", hem_lo, hem_hi)
        w2 = iv("w2", sh_lo, sh_hi)
        nb = iv("nb", max(0, neck_lo), neck_hi)
        cuff_on = iv("cuff_on", cuff_lo, cuff_hi)
        upper = iv("upper", up_lo, up_hi)
        fn_depth = iv("fn_depth", fn_lo, fn_hi)
        arm_rows = iv("arm_rows", arm_lo, arm_hi)
        # 袖山行独立决策（装袖为曲线缝合，山高通常为袖窿深的 0.7~1.05 倍）
        cap_rows = iv("cap_rows", cap_lo, cap_hi)

        # 罗纹行带：目标罗纹长 ±2 行（不参与最大误差，只要求整针整行可行）
        hr_target = float(sz.get("hem_rib", 5.0)) * rp
        cr_target = float(sz.get("cuff_rib", 5.0)) * rp
        hem_rib_rows = iv("hem_rib_rows", max(1, int(hr_target) - 2), int(hr_target) + 3)
        cuff_rib_rows = iv("cuff_rib_rows", max(1, int(cr_target) - 2), int(cr_target) + 3)

        # 后领深：bnd 行
        bnd = iv("bnd", max(1, bn_lo), min(8, bn_hi))

        # ---- 挂肩收针（曲线段；肩阶梯也在外边缘收针，共同完成腋下→肩点减量） ----
        total_arm_dec = model.new_int_var(0, 200, f"{pfx}tarm")
        nah_used = iv("nah_used", 2, Nah)
        arm_first = iv("arm_first", 1, 8)
        arm_events = [iv(f"arme{k}", 0, 4) for k in range(Nah)]
        arm_slot = [model.new_bool_var(f"{pfx}armb{k}") for k in range(Nah)]
        for k in range(Nah):
            model.add(nah_used >= k + 1).only_enforce_if(arm_slot[k])
            model.add(nah_used <= k).only_enforce_if(arm_slot[k].Not())
            model.add(arm_events[k] == 0).only_enforce_if(arm_slot[k].Not())
            model.add(arm_events[k] >= 1).only_enforce_if(arm_slot[k])
        # 袖窿曲线：收针量整体不增（快速挖角后放缓）
        model.add(arm_first >= arm_events[0])
        for k in range(Nah - 1):
            model.add(arm_events[k] >= arm_events[k + 1])
        model.add(total_arm_dec == arm_first + cp_model.LinearExpr.sum(arm_events))
        # 排布可行性：曲线收针 nah 行 + 实际肩阶梯 2*nsh 行 ≤ arm_rows（nsh 在下方定义后约束）
        model.add(arm_rows >= 6)

        # ---- 肩阶梯 ----
        nsh = iv("nsh", 1, Nsh_max)
        ksh = iv("ksh", 1, 4)
        nshk = model.new_int_var(1, 4 * Nsh_max, f"{pfx}nshk")
        model.add_multiplication_equality(nshk, [nsh, ksh])
        # 外边缘总减量（每侧）：挂肩曲线收针 + 肩阶梯 = 腋下到肩点
        model.add(2 * (total_arm_dec + nshk) == chest - w2)
        model.add(nah_used + 2 * nsh <= arm_rows)
        # 前领：中心平收 + 两侧阶梯收 + 两肩活针
        nf_used = iv("nf_used", 0, Nfn)
        fn_events = [iv(f"fne{k}", 0, 4) for k in range(Nfn)]
        fn_slot = [model.new_bool_var(f"{pfx}fnb{k}") for k in range(Nfn)]
        for k in range(Nfn):
            model.add(nf_used >= k + 1).only_enforce_if(fn_slot[k])
            model.add(nf_used <= k).only_enforce_if(fn_slot[k].Not())
            model.add(fn_events[k] == 0).only_enforce_if(fn_slot[k].Not())
            model.add(fn_events[k] >= 1).only_enforce_if(fn_slot[k])
        total_fn_dec = model.new_int_var(0, 200, f"{pfx}tfn")
        model.add(total_fn_dec == cp_model.LinearExpr.sum(fn_events))
        # 前领几何：nb + 2*单侧阶梯收 + 2*每肩活针(nsh*ksh) = w2
        model.add(nb + 2 * total_fn_dec + 2 * nshk == w2)
        # 前领收针与肩阶梯均须落在袖窿行内（二者在同一时间窗并行，不强制嵌套）
        model.add(fn_depth >= 2)
        model.add(fn_depth <= arm_rows - 1)
        # 前领单侧阶梯事件在领深范围内排布可行（每事件 2 行）
        model.add(2 * nf_used <= fn_depth)
        # 后领：后领台深度不超过肩阶梯高度
        model.add(bnd <= 2 * nsh)
        model.add(2 * nsh <= arm_rows)

        # ---- 袖：加针 ----
        total_side_inc = model.new_int_var(0, 200, f"{pfx}tside")
        model.add(2 * total_side_inc == chest - cast_on)
        side_rows = iv("side_rows", 0, 1000)
        model.add(side_rows >= 2 * total_side_inc)

        total_inc = model.new_int_var(0, 200, f"{pfx}tinc")
        model.add(2 * total_inc == upper - cuff_on)
        inc_rows = iv("inc_rows", 0, 1000)
        model.add(inc_rows >= 2 * total_inc)

        # ---- 袖山 ----
        cap_top = iv("cap_top", 4, max(6, up_hi))
        model.add_modulo_equality(0, cap_top, 2)
        total_cap_dec = model.new_int_var(0, 200, f"{pfx}tcap")
        model.add(2 * total_cap_dec == upper - cap_top)
        ncap_used = iv("ncap_used", 1, Ncap)
        cap_first = iv("cap_first", 2, 12)
        cap_events = [iv(f"cape{k}", 0, 6) for k in range(Ncap)]
        cap_slot = [model.new_bool_var(f"{pfx}capb{k}") for k in range(Ncap)]
        for k in range(Ncap):
            model.add(ncap_used >= k + 1).only_enforce_if(cap_slot[k])
            model.add(ncap_used <= k).only_enforce_if(cap_slot[k].Not())
            model.add(cap_events[k] == 0).only_enforce_if(cap_slot[k].Not())
            model.add(cap_events[k] >= 1).only_enforce_if(cap_slot[k])
        # 山型曲线：收针量单调不增（首次大平收之后逐步放缓）
        for k in range(Ncap - 1):
            model.add(cap_events[k] >= cap_events[k + 1])
        model.add(total_cap_dec == cap_first + cp_model.LinearExpr.sum(cap_events))
        # 装袖为曲线缝合：山高为袖窿深的 0.7~1.05 倍（周长匹配在编译器/模拟器中校验）
        model.add(cap_rows * 100 >= 70 * arm_rows)
        model.add(cap_rows * 100 <= 105 * arm_rows)
        # cap 事件排布可行性：前 ncap 行连续
        model.add(ncap_used <= cap_rows - 1)
        model.add(cap_top <= upper - 2)

        # ---- 罗纹 / 花样模数 ----
        for v in (chest, cast_on, w2, nb, cuff_on, upper):
            model.add_modulo_equality(0, v, 2)
        model.add_modulo_equality(0, cast_on, hem_period)
        model.add_modulo_equality(0, cuff_on, cuff_period)
        if P > 1:
            for v in (cast_on, chest, w2, nb, cuff_on, upper, cap_top):
                model.add_modulo_equality(0, v, P)
        if Q > 1:
            for v in (hem_rib_rows, side_rows, arm_rows, cap_rows,
                      cuff_rib_rows, inc_rows, fn_depth, bnd):
                model.add_modulo_equality(0, v, Q)

        # ---- 长度行（行方向尺寸） ----
        total_body_rows = model.new_int_var(1, 2000, f"{pfx}tb")
        model.add(total_body_rows == hem_rib_rows + side_rows + arm_rows)
        total_sleeve_rows = model.new_int_var(1, 2000, f"{pfx}ts")
        model.add(total_sleeve_rows == cuff_rib_rows + inc_rows + cap_rows)

        # ---- 误差变量（1/100 cm） ----
        err_ub = max(tol100 + s_scale * 2, 6000)

        def err_for(var: cp_model.IntVar, center: int, scale: int, key: str,
                     hard: bool = True) -> cp_model.IntVar:
            # 误差以 1/100 cm 计：|var - center| * scale（每针/行折合 1/100cm）
            d = model.new_int_var(-100000, 100000, f"{pfx}{key}_d")
            model.add(d == var - center)
            e = model.new_int_var(0, err_ub, f"{pfx}{key}_e")
            model.add_abs_equality(e, d)
            ec = model.new_int_var(0, err_ub, f"{pfx}{key}_ec")
            model.add(ec == e * scale)
            if hard:
                bound = 5000 if (_loose_fields and (i, key) in _loose_fields) else tol100
                model.add(ec <= bound)
            return ec

        errors = {
            "bust": err_for(chest, bust_c, s_scale, "bust"),
            "hem": err_for(cast_on, hem_c, s_scale, "hem"),
            "shoulder": err_for(w2, sh_c, s_scale, "shoulder"),
            "neck": err_for(nb, neck_c, s_scale, "neck"),
            "fn_depth": err_for(fn_depth, fn_c, r_scale, "fn_depth"),
            "bn_depth": err_for(bnd, bn_c, r_scale, "bn_depth"),
            "length": err_for(total_body_rows, len_c, r_scale, "length"),
            "armhole": err_for(arm_rows, arm_c, r_scale, "armhole"),
            "sleeve_len": err_for(total_sleeve_rows, sl_c, r_scale, "sleeve_len"),
            "cap_height": err_for(cap_rows, cap_c, r_scale, "cap_height"),
            "upper_arm": err_for(upper, up_c, s_scale, "upper_arm"),
            "cuff": err_for(cuff_on, cuff_c, s_scale, "cuff"),
        }

        sv = SizeVars(
            idx=i, name=tag, cast_on=cast_on, chest=chest, w2=w2, nb=nb,
            ksh=ksh, nsh=nsh, bnd=bnd, fn_depth=fn_depth, arm_rows=arm_rows,
            cap_rows=cap_rows,
            side_rows=side_rows, hem_rib_rows=hem_rib_rows,
            cuff_on=cuff_on, upper=upper, cap_top=cap_top,
            inc_rows=inc_rows, cuff_rib_rows=cuff_rib_rows,
            nah_used=nah_used, arm_first=arm_first, arm_events=arm_events,
            ncap_used=ncap_used, cap_first=cap_first, cap_events=cap_events,
            nf_used=nf_used, fn_events=fn_events, errors=errors,
            total_side_inc=total_side_inc, total_arm_dec=total_arm_dec,
            total_cap_dec=total_cap_dec, total_fn_dec=total_fn_dec,
            total_inc=total_inc,
        )
        svars.append(sv)

    # ---- 接缝：前后片共用决策天然逐行匹配；袖山/袖窿为曲线缝合，按周长+吃势 ----
    # 用逐段折线长度的整数定点（1/100 cm）精确近似：
    #   seg_len(dx,dy)=round(sqrt((dx*s_scale)^2+(dy*r_scale)^2))
    #   dx∈0..12 针, dy∈0..2 行；s_scale/r_scale 为每针/每行的 1/100cm
    s_scale = max(1, int(round(100.0 / sp)))
    r_scale = max(1, int(round(100.0 / rp)))
    seg_tbl: dict[tuple[int, int], int] = {}
    for dx in range(0, 13):
        for dy in range(0, 3):
            seg_tbl[(dx, dy)] = int(round(math.hypot(dx * s_scale, dy * r_scale)))

    def seg_var(dx: cp_model.IntVar, dy: int, name: str) -> cp_model.IntVar:
        tbl = [seg_tbl[(x, dy)] for x in range(0, 13)]
        v = model.new_int_var(0, max(tbl) + 1, name)
        model.add_element(dx, tbl, v)
        return v

    # 目标吃势（袖山周长 − 前后袖窿周长，1/100 cm）≈ +50（0.5 cm 正吃势）
    # 袖窿侧折线含曲线收针段与肩阶梯段（dy=2,dx=ksh），与跨肩配对的事件序数口径一致。
    seam_devs = []
    for sv in svars:
        # 单侧袖窿：arm_first(1行) + arm_events 各段(1行) + nsh 个阶梯段(dy=2,dx=ksh)
        arm_segs = [seg_var(sv.arm_first, 1, f"s{sv.idx}_arms0")]
        for k, ev in enumerate(sv.arm_events):
            arm_segs.append(seg_var(ev, 1, f"s{sv.idx}_arms{k+1}"))
        # 阶梯段按 nsh 条件求和
        sh_terms = []
        for k in range(8):
            active = model.new_bool_var(f"s{sv.idx}_shact{k}")
            model.add(sv.nsh >= k + 1).only_enforce_if(active)
            model.add(sv.nsh <= k).only_enforce_if(active.Not())
            seg = model.new_int_var(0, seg_tbl[(4, 2)] + 1, f"s{sv.idx}_shseg{k}")
            seg_ksh = seg_var(sv.ksh, 2, f"s{sv.idx}_shsegv{k}")
            model.add(seg == seg_ksh).only_enforce_if(active)
            model.add(seg == 0).only_enforce_if(active.Not())
            sh_terms.append(seg)
        arm_side = model.new_int_var(0, 1000000, f"s{sv.idx}_armside")
        # 平肩段（无横向收针）行数：arm_rows - nah_used - 2*nsh，每行垂高 r_scale
        arm_flat = model.new_int_var(0, 1000000, f"s{sv.idx}_armflat")
        model.add(arm_flat == r_scale * (sv.arm_rows - sv.nah_used - 2 * sv.nsh))
        model.add(arm_side == cp_model.LinearExpr.sum(arm_segs + sh_terms) + arm_flat)

        # 单侧袖山：cap_first(1行) + cap_events 各段(1行) + 收针结束后的平高段
        cap_segs = [seg_var(sv.cap_first, 1, f"s{sv.idx}_caps0")]
        for k, ev in enumerate(sv.cap_events):
            cap_segs.append(seg_var(ev, 1, f"s{sv.idx}_caps{k+1}"))
        cap_side = model.new_int_var(0, 1000000, f"s{sv.idx}_capside")
        cap_flat = model.new_int_var(0, 1000000, f"s{sv.idx}_capflat")
        model.add(cap_flat == r_scale * (sv.cap_rows - sv.ncap_used))
        model.add(cap_side == cp_model.LinearExpr.sum(cap_segs) + cap_flat)
        # 山顶平台
        cap_top_len = model.new_int_var(0, 1000000, f"s{sv.idx}_caplen")
        model.add(cap_top_len == s_scale * sv.cap_top)

        # 吃势 = [2*袖山侧 + 山顶] - 2*袖窿侧
        ease100 = model.new_int_var(-2000000, 2000000, f"s{sv.idx}_ease")
        model.add(ease100 == 2 * cap_side + cap_top_len - 2 * arm_side)
        target_ease = 50  # 0.5 cm 正吃势
        dev = model.new_int_var(0, 2000000, f"s{sv.idx}_easedev")
        d = model.new_int_var(-2000000, 2000000, f"s{sv.idx}_eased")
        model.add(d == ease100 - target_ease)
        model.add_abs_equality(dev, d)
        seam_devs.append(dev)
    seam_dev = model.new_int_var(0, 10000000, "seam_dev")
    model.add_max_equality(seam_dev, seam_devs)

    # ---- 可选：接缝花样相位锁定 ----
    # 仅靠“各段行数为 Q 的倍数”并不等于接缝两侧相位一致：模拟器逐条核对时，
    # 必须比较接缝两侧*对应塑形事件*的绝对行号（从各自起针行计 1 基行号）模 Q。
    #   侧缝（前片↔后片）：前后片共用全部行结构决策，事件绝对行号天然逐行相同；
    #   肩缝（前片↔后片）：共用 nsh/ksh 决策，阶梯行天然一一对应；
    #   袖山↔袖窿（独立衣片）：是唯一需要显式约束的跨片接缝。
    # 袖山/袖窿自腋下起缝，事件须按*沿缝实际发生的序数*连续配对——大身袖窿侧
    # 先发生曲线收针（arm_first + nah_used 个槽位），曲线结束后继续发生 nsh 个
    # 肩阶梯事件；袖山侧只有连续收针（cap_first + ncap_used 个槽位）。因此第 j 对
    # （j=0 为首次大平收）：
    #   山侧绝对行 = cuff_rib_rows + inc_rows + j + 1               （j ≤ ncap_used）
    #   窿侧 j ≤ nah_used（曲线段）:
    #       绝对行 = hem_rib_rows + side_rows + j + 1
    #   窿侧 j > nah_used（跨肩阶梯段，t = j-nah_used-1）:
    #       局部行 = arm_rows - 2*nsh + 1 + 2*t
    #       绝对行 = hem_rib_rows + side_rows + arm_rows
    #               - 2*nsh + 2*j - 2*nah_used - 1
    # 第 j 对仅当两侧都至少有 j+1 个沿缝事件时存在（布尔条件约束）。
    # 注意：不能只约束曲线槽位——袖山收针序数超过袖窿曲线数后会落到肩阶梯上，
    # 漏掉这些“跨肩阶梯对”会让不同相的事件被误判为全部通过。
    if phase_lock and Q > 1:
        def _piece(ref: str) -> str:
            return str(ref).split(".", 1)[0]

        def _is_cap_arm(s: dict[str, Any]) -> bool:
            sides = {_piece(s.get("a", "")), _piece(s.get("b", ""))}
            return "sleeve" in sides and bool(sides & {"body", "front", "back"})

        cap_arm_seams = [s for s in seams if _is_cap_arm(s)]
        if cap_arm_seams:
            # 前/后片共用袖窿与肩阶梯决策，多条 cap↔armhole 接缝的约束完全相同，
            # 每个尺码只需施加一次。
            for sv in svars:
                pfx = f"s{sv.idx}_"
                body_base = sv.hem_rib_rows + sv.side_rows
                sleeve_base = sv.cuff_rib_rows + sv.inc_rows
                # 大身沿缝事件总数 = 1(首次) + nah_used(曲线) + nsh(肩阶梯)，
                # 袖山沿缝事件总数 = 1 + ncap_used；j 取 0..Ncap 覆盖全部袖山事件，
                # 每对是否生效由两侧存在性布尔（含“身体侧是曲线还是肩阶梯”）决定。
                for j in range(Ncap + 1):
                    # ---- 两侧第 j 个事件是否存在 ----
                    # 大身沿缝序数 0..nah_used 为曲线，nah_used+1..nah_used+nsh
                    # 为肩阶梯：第 j 个事件存在 iff nah_used+nsh >= j（与曲线/阶梯
                    # 分界无关，统一表达式；事件类型由下方 is_curve 判定）。
                    body_has = model.new_bool_var(f"{pfx}sebody{j}")
                    model.add(sv.nah_used + sv.nsh >= j).only_enforce_if(body_has)
                    model.add(sv.nah_used + sv.nsh <= j - 1).only_enforce_if(body_has.Not())
                    cap_has = model.new_bool_var(f"{pfx}secap{j}")
                    model.add(sv.ncap_used >= j).only_enforce_if(cap_has)
                    model.add(sv.ncap_used <= j - 1).only_enforce_if(cap_has.Not())
                    pair_active = model.new_bool_var(f"{pfx}sepair{j}")
                    model.add_bool_and(body_has, cap_has).only_enforce_if(pair_active)
                    model.add_bool_or(body_has.Not(), cap_has.Not()).only_enforce_if(pair_active.Not())

                    # ---- 窿侧第 j 个事件的绝对行（曲线行 or 肩阶梯行，二选一） ----
                    # 第 j 个是曲线事件 iff nah_used >= j，否则为肩阶梯事件
                    # （pair_active 时 body_has 保证肩阶梯下标 t=j-nah_used-1 在 [0,nsh) 内）。
                    is_curve = model.new_bool_var(f"{pfx}securve{j}")
                    model.add(sv.nah_used >= j).only_enforce_if(is_curve)
                    model.add(sv.nah_used <= j - 1).only_enforce_if(is_curve.Not())
                    row_body = model.new_int_var(1, 2000, f"{pfx}sebodyrow{j}")
                    curve_row = body_base + j + 1
                    shoulder_row = (body_base + sv.arm_rows - 2 * sv.nsh
                                    + 2 * j - 2 * sv.nah_used - 1)
                    model.add(row_body == curve_row).only_enforce_if(
                        pair_active, is_curve)
                    model.add(row_body == shoulder_row).only_enforce_if(
                        pair_active, is_curve.Not())

                    # ---- 山侧第 j 个事件的绝对行（恒为连续收针行） ----
                    row_cap = sleeve_base + j + 1
                    _add_mod_equal(model, row_body, row_cap, Q,
                                   f"{pfx}se{j}", enforcer=pair_active)

    # ---- 目标：字典序三层 ----
    # 1) 全局最大误差（1/100 cm）
    all_errs = [e for sv in svars for e in sv.errors.values()]
    max_err = model.new_int_var(0, max(tol100 + 500, 2000), "max_err")
    model.add_max_equality(max_err, all_errs)

    # 2) 塑形事件总数（含肩阶梯×2片、挂肩、袖山、前领、侧加针、袖加针）
    event_terms = []
    per_size_events: list[cp_model.IntVar] = []
    for sv in svars:
        cnt = model.new_int_var(0, 10000, f"s{sv.idx}_events")
        model.add(cnt ==
                  sv.nah_used + sv.ncap_used + sv.nf_used +
                  2 * sv.nsh + 2 * sv.total_side_inc +
                  2 * sv.total_inc + 3)  # 首次大平收/领平收/山顶平收计为事件
        event_terms.append(cnt)
        per_size_events.append(cnt)
    total_events = model.new_int_var(0, 100000, "total_events")
    model.add(total_events == cp_model.LinearExpr.sum(event_terms))

    # 3) 相邻尺码跳变（关键针数/行数/事件数的绝对差之和）
    jump_keys: list[tuple[str, Any]] = [
        ("cast_on", lambda s: s.cast_on),
        ("chest", lambda s: s.chest),
        ("w2", lambda s: s.w2),
        ("nb", lambda s: s.nb),
        ("cuff_on", lambda s: s.cuff_on),
        ("upper", lambda s: s.upper),
        ("arm_rows", lambda s: s.arm_rows),
        ("cap_rows", lambda s: s.cap_rows),
        ("side_rows", lambda s: s.side_rows),
        ("inc_rows", lambda s: s.inc_rows),
        ("nsh", lambda s: s.nsh),
        ("ksh", lambda s: s.ksh),
        ("nah_used", lambda s: s.nah_used),
        ("ncap_used", lambda s: s.ncap_used),
    ]
    jump_terms = []
    for a, b in zip(svars, svars[1:]):
        for key, getter in jump_keys:
            d = model.new_int_var(-2000, 2000, f"j_{a.idx}_{key}")
            model.add(d == getter(b) - getter(a))
            j = model.new_int_var(0, 2000, f"ja_{a.idx}_{key}")
            model.add_abs_equality(j, d)
            jump_terms.append(j)
    total_jump = model.new_int_var(0, 100000, "total_jump")
    model.add(total_jump == cp_model.LinearExpr.sum(jump_terms))

    if _loose_fields:
        # 诊断模式：最小化“超过原始容差的字段数”及超差量，得到最贴近原目标的可行解
        violation_terms = []
        for sv in svars:
            for key, ev in sv.errors.items():
                v = model.new_int_var(0, 1, f"viol_{sv.idx}_{key}")
                model.add(ev > tol100).only_enforce_if(v)
                model.add(ev <= tol100).only_enforce_if(v.Not())
                violation_terms.append(v)
        n_viol = model.new_int_var(0, 1000, "n_viol")
        model.add(n_viol == cp_model.LinearExpr.sum(violation_terms))
        sum_err = model.new_int_var(0, 1000000, "sum_err_diag")
        model.add(sum_err == cp_model.LinearExpr.sum(all_errs))
        model.minimize(100000 * n_viol + sum_err)
    else:
        model.minimize(W_ERR * max_err + 5000 * seam_dev + W_EVENT * total_events + W_JUMP * total_jump)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT
    solver.parameters.num_search_workers = 4
    status = solver.solve(model)

    if status == cp_model.UNKNOWN:
        return {
            "status": "timeout",
            "message": f"求解器在 {SOLVER_TIME_LIMIT:.0f}s 内未找到可行方案，可放宽容差或检查尺码跳变。",
            "conflicts": [],
        }
    if status in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
        if _in_diagnosis:
            return {"status": "infeasible", "message": "当前参数下无可行解。", "conflicts": []}
        conflicts = _diagnose_infeasible(project)
        return {"status": "infeasible", "message": "当前参数下无可行解。", "conflicts": conflicts}
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "error", "message": f"求解器状态异常：{solver.status_name(status)}", "conflicts": []}

    # ---- 取值 ----
    def val(v: cp_model.IntVar) -> int:
        return int(solver.value(v))

    solved_sizes = []
    for sv, sz in zip(svars, sizes_in):
        d = {
            "name": sv.name,
            "cast_on": val(sv.cast_on),
            "chest": val(sv.chest),
            "w2": val(sv.w2),
            "nb": val(sv.nb),
            "ksh": val(sv.ksh),
            "nsh": val(sv.nsh),
            "bnd": val(sv.bnd),
            "fn_depth": val(sv.fn_depth),
            "arm_rows": val(sv.arm_rows),
            "cap_rows": val(sv.cap_rows),
            "side_rows": val(sv.side_rows),
            "hem_rib_rows": val(sv.hem_rib_rows),
            "cuff_on": val(sv.cuff_on),
            "upper": val(sv.upper),
            "cap_top": val(sv.cap_top),
            "inc_rows": val(sv.inc_rows),
            "cuff_rib_rows": val(sv.cuff_rib_rows),
            "nah_used": val(sv.nah_used),
            "arm_first": val(sv.arm_first),
            "arm_events": [val(x) for x in sv.arm_events[: val(sv.nah_used)]],
            "ncap_used": val(sv.ncap_used),
            "cap_first": val(sv.cap_first),
            "cap_events": [val(x) for x in sv.cap_events[: val(sv.ncap_used)]],
            "nf_used": val(sv.nf_used),
            "fn_events": [val(x) for x in sv.fn_events[: val(sv.nf_used)]],
            "errors_100cm": {k: val(v) for k, v in sv.errors.items()},
        }
        d["total_side_inc"] = (d["chest"] - d["cast_on"]) // 2
        d["total_inc"] = (d["upper"] - d["cuff_on"]) // 2
        solved_sizes.append(d)

    objective = {
        "max_err_100cm": val(max_err),
        "seam_dev_100": val(seam_dev),
        "total_events": val(total_events),
        "total_jump": val(total_jump),
        "optimal": status == cp_model.OPTIMAL,
        "wall_time_s": round(solver.wall_time, 3),
    }

    return {
        "status": "ok",
        "message": ("已求得最优解" if status == cp_model.OPTIMAL
                    else "已求得可行解（未证明最优，可能是计时到达）"),
        "objective": objective,
        "gauge": {"stitches_per_cm": sp, "rows_per_cm": rp,
                  "s_scale_100cm": s_scale, "r_scale_100cm": r_scale},
        "pattern": {"P": P, "Q": Q, "phase_lock": phase_lock,
                    "seam_phase_lock": phase_lock,
                    "hem_period": hem_period, "cuff_period": cuff_period},
        "sizes": solved_sizes,
    }


def _diagnose_infeasible(project: dict[str, Any]) -> list[dict[str, str]]:
    """不可行时分两级定位冲突字段：

    1) 逐组放宽（容差 / 罗纹周期 / 花样 P / 花样 Q），定位冲突的“全局组”；
    2) 若放宽全部尺寸容差即可行，则逐尺码逐字段放宽误差带，精确到具体指标
       （典型情形：加深袖窿却没加衣长，armhole 与 length 同时被标出）。
    """
    import copy

    def resolve(p2: dict[str, Any], loose: set[tuple[int, str]] | None = None,
                time_limit: float = 8.0) -> str:
        global SOLVER_TIME_LIMIT
        old = SOLVER_TIME_LIMIT
        SOLVER_TIME_LIMIT = time_limit
        try:
            r = solve_project(p2, _in_diagnosis=True, _loose_fields=loose)
            return str(r.get("status"))
        finally:
            SOLVER_TIME_LIMIT = old

    conflicts: list[dict[str, str]] = []
    sizes = project.get("sizes", [])

    # ---- 全局组探测 ----
    pat0 = project.get("pattern", {})
    groups = [
        ("tol", lambda p: p.__setitem__("tolerance_cm", 5.0),
         ["tolerance_cm"], "放宽容差后可行：尺寸目标与整数针行/结构约束冲突"),
        ("rib", lambda p: (p.setdefault("rib", {}).__setitem__("hem_rib_type", "1x1"),
                            p["rib"].__setitem__("cuff_rib_type", "1x1")),
         ["hem_rib_type", "cuff_rib_type"], "放宽罗纹周期后可行：起针数与罗纹周期冲突"),
        ("ph", lambda p: p.setdefault("pattern", {}).__setitem__("p_horizontal", 1),
         ["p_horizontal"], "取消横向花样循环后可行：关键宽度无法对齐 P 模数"),
    ]
    # 接缝相位锁定（专用约束）单独探测：仅关闭该开关即可行时，开关本身是冲突字段。
    # 注意：关闭开关保留普通 Q 周期规则（q_vertical 不动），以便区分两类冲突。
    if bool(pat0.get("seam_phase_lock", False)) and int(pat0.get("q_vertical", 1)) > 1:
        groups.append((
            "lock",
            lambda p: p.setdefault("pattern", {}).__setitem__("seam_phase_lock", False),
            ["seam_phase_lock"],
            "关闭接缝相位锁定后可行：接缝两侧对应塑形事件的绝对行号无法同余 mod Q"
            "（普通 Q 周期规则仍保留）",
        ))
    groups.append((
        "qv", lambda p: (p.setdefault("pattern", {}).__setitem__("q_vertical", 1),
                          p["pattern"].__setitem__("seam_phase_lock", False)),
        ["q_vertical"], "取消纵向花样循环后可行：段行数无法对齐 Q 模数（接缝相位锁定一并取消）"))
    for gid, mod, fields, reason in groups:
        p2 = copy.deepcopy(project)
        mod(p2)
        if resolve(p2) == "ok":
            for f in fields:
                conflicts.append({"field": f, "size": "*", "reason": reason})

    # ---- 逐尺码逐字段精确探测 ----
    # 思路：把“全部字段放宽（域+误差界 50cm）”得到的解作为最近可行点，
    # 然后贪心收缩——逐字段恢复原容差，仍可行就保持恢复，必须放宽的字段即冲突字段。
    all_fields = {(i, k) for i in range(len(sizes)) for k in MEASUREMENT_KEYS}

    def solve_loose(loose: set[tuple[int, str]]):
        global SOLVER_TIME_LIMIT
        old = SOLVER_TIME_LIMIT
        SOLVER_TIME_LIMIT = 6.0
        try:
            return solve_project(copy.deepcopy(project), _in_diagnosis=True, _loose_fields=loose)
        finally:
            SOLVER_TIME_LIMIT = old

    r_all = solve_loose(all_fields)
    if r_all.get("status") == "ok":
        # 贪心求最小（近似）必须放宽集
        required: set[tuple[int, str]] = set(all_fields)
        # 先按“全部放宽解”中的误差从大到小尝试恢复（偏差大的更可能必须放宽，最后再确认）
        order = sorted(all_fields,
                       key=lambda fk: r_all["sizes"][fk[0]]["errors_100cm"][fk[1]])
        for fk in order:
            trial = required - {fk}
            if solve_loose(trial).get("status") == "ok":
                required = trial
        # 报告必须放宽字段及其在全部放宽解中的实际偏差
        for i, key in sorted(required):
            e100 = r_all["sizes"][i]["errors_100cm"][key]
            conflicts.append({
                "field": key, "size": sizes[i].get("name", f"#{i+1}"),
                "reason": f"该尺寸与其他目标冲突（最近可行解偏差 {e100/100:.2f} cm）；"
                          f"请调整该值或其关联尺寸（袖窿↔衣长/袖山↔袖长），或加大容差",
            })
        if required:
            conflicts = [c for c in conflicts if c["field"] != "tolerance_cm"]

    if not conflicts:
        for i, sz in enumerate(sizes):
            conflicts.append({
                "field": "*", "size": sz.get("name", f"#{i+1}"),
                "reason": "该尺码在所有已放宽条件下仍不可行，请检查各尺寸间的几何一致性",
            })
    return conflicts
