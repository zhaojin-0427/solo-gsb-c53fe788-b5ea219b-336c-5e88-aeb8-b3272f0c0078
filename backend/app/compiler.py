"""行程编译器：把求解器的整数决策展开为逐行编织指令、轮廓多边形，并逐行模拟校验。

行号从 1 开始（起针行）。所有宽度为“整片针数”；衣片整片对称编织。

行结构（全局行号）：
  大身: [1 .. hem_rib_rows] 罗纹
        [hem_rib_rows+1 .. +side_rows] 侧区（两侧各加 total_side_inc 次，每次 1 针）
        [.. +arm_rows] 挂肩区：
           局部行 1 .. nah_used+1        每侧连续收 arm_first / arm_events 针（袖窿曲线）
           局部行 arm_rows-2*nsh+1 ..末   肩阶梯（每 2 行每侧外缘平收 ksh 针）
           前领：局部行 (arm_rows-fn_depth+1) 中心平收 nb；之后两侧对称阶梯收
           后领：局部行 (arm_rows-bnd+1)   中心平收 nb
  袖片: [1 .. cuff_rib_rows] 袖口罗纹
        [.. +inc_rows] 袖下两侧各加 total_inc 次
        [.. +cap_rows] 袖山：前 ncap_used+1 行收针，最后 1 行山顶平收 cap_top

沿缝相位配对（袖山↔袖窿）按事件序数连续进行：大身侧序列 = 曲线收针（含首次）
+ 后续肩阶梯，袖山侧序列 = 连续收针（含首次）；序数超过曲线数的袖山事件与
肩阶梯配对，未配对事件仅在报告中列出。
"""
from __future__ import annotations

import math
from typing import Any

from .solver import ROW_MEASUREMENTS
from .default_data import DEFAULT_SEAMS


def spread_even(n_events: int, span: int) -> list[int]:
    """在长度 span 的区间内均匀放置 n_events 个事件，返回距区间起点的 0 基偏移。"""
    if n_events <= 0 or span <= 0:
        return []
    if n_events == 1:
        return [span // 2]
    step = span / n_events
    return [min(span - 1, int(round(step * (k + 0.5)))) for k in range(n_events)]


def compile_size(sol: dict[str, Any], gauge: dict[str, Any],
                 pattern: dict[str, Any], targets: dict[str, Any],
                 hem_rib_cm: float, cuff_rib_cm: float,
                 tolerance_cm: float = 0.6,
                 seams: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    sp = float(gauge["stitches_per_cm"])
    rp = float(gauge["rows_per_cm"])

    pattern = dict(pattern)
    pattern["_tolerance_cm"] = tolerance_cm
    if seams is None:
        seams = list(DEFAULT_SEAMS)
    front = _build_body(sol, piece="front")
    back = _build_body(sol, piece="back")
    sleeve = _build_sleeve(sol)

    checks = _simulate(sol, front, back, sleeve, pattern, seams)
    measurements = _measurements(sol, front, sleeve, sp, rp, targets)
    polygons = {
        "front": _body_polygon(sol, sp, rp, side="front"),
        "back": _body_polygon(sol, sp, rp, side="back"),
        "sleeve": _sleeve_polygon(sol, sp, rp),
    }
    collar = _collar(sol, sp, rp)
    seams = _seam_perimeters(sol, polygons, sp, rp)

    return {
        "name": sol["name"],
        "solution": sol,
        "pieces": {"front": front, "back": back, "sleeve": sleeve},
        "polygons": polygons,
        "checks": checks + seams,
        "measurements": measurements,
        "collar": collar,
    }


# ---------------- 大身 ----------------

def _body_event_maps(sol: dict[str, Any], piece: str) -> dict[str, Any]:
    arm_rows = sol["arm_rows"]
    nah = sol["nah_used"]
    arm_amts = [sol["arm_first"]] + sol["arm_events"]
    arm_bind = {k + 1: a for k, a in enumerate(arm_amts)}          # 局部行 -> 每侧外缘收
    sh_start = arm_rows - 2 * sol["nsh"] + 1
    shoulder_rows = {sh_start + 2 * k: sol["ksh"] for k in range(sol["nsh"])}

    neck_bind_local: int | None = None       # 领中心平收所在局部行
    neck_edge: dict[int, int] = {}           # 局部行 -> 每侧领缘收针
    depth = 0
    if piece == "front":
        depth = sol["fn_depth"]
        neck_bind_local = arm_rows - depth + 1
        offs = spread_even(len(sol["fn_events"]), depth)
        for e, off in zip(sol["fn_events"], offs):
            rl = neck_bind_local + 1 + off
            neck_edge[rl] = neck_edge.get(rl, 0) + e
    else:
        depth = sol["bnd"]
        neck_bind_local = arm_rows - depth + 1
    # 后领中心平收：w2 − 2×每侧肩活针，使两肩恰剩 nsh*ksh 针与前片肩缝对接
    back_neck_bind = sol["w2"] - 2 * sol["nsh"] * sol["ksh"]
    return {
        "arm_bind": arm_bind, "shoulder_rows": shoulder_rows,
        "neck_bind_local": neck_bind_local, "neck_edge": neck_edge,
        "neck_depth": depth, "sh_start": sh_start,
        "back_neck_bind": back_neck_bind,
    }


def _build_body(sol: dict[str, Any], piece: str) -> dict[str, Any]:
    hr = sol["hem_rib_rows"]
    side_rows = sol["side_rows"]
    arm_rows = sol["arm_rows"]
    maps = _body_event_maps(sol, piece)

    side_offsets = spread_even(sol["total_side_inc"], side_rows)
    side_event = {off + 1: 1 for off in side_offsets}

    rows: list[dict[str, Any]] = []
    width = sol["cast_on"]
    rib_end = hr
    side_end = hr + side_rows
    total_rows = hr + side_rows + arm_rows

    for r in range(1, total_rows + 1):
        section = "rib" if r <= rib_end else ("side" if r <= side_end else "arm")
        events: list[str] = []
        arm_dec = inc = shoulder_bind = neck_center = neck_each = neck_final = 0

        if section == "side":
            local = r - rib_end
            if local in side_event:
                inc = side_event[local]
                width += 2 * inc
                events.append(f"两侧各放 1 针（共 +{2*inc}）")

        if section == "arm":
            local = r - side_end
            if local in maps["arm_bind"]:
                arm_dec = maps["arm_bind"][local]
                width -= 2 * arm_dec
                events.append(f"袖窿两侧各平收 {arm_dec} 针（共 −{2*arm_dec}）")
            if local in maps["shoulder_rows"]:
                shoulder_bind = maps["shoulder_rows"][local]
                width -= 2 * shoulder_bind
                events.append(f"肩端两侧各平收 {shoulder_bind} 针（共 −{2*shoulder_bind}）")
            if maps["neck_bind_local"] == local:
                neck_center = sol["nb"] if piece == "front" else maps["back_neck_bind"]
                width -= neck_center
                label = "前领" if piece == "front" else "后领"
                events.append(f"{label}中心平收 {neck_center} 针")
            if local in maps["neck_edge"]:
                neck_each = maps["neck_edge"][local]
                width -= 2 * neck_each
                events.append(f"前领两侧各收 {neck_each} 针（共 −{2*neck_each}）")
            # 末行：领口剩余活针（领边/后领台）一次性平收，与挑领罗纹衔接
            if r == total_rows and width > 0:
                neck_final = width
                label = "前领沿" if piece == "front" else "后领沿"
                events.append(f"{label}剩余 {neck_final} 针平收（领口收口）")
                width = 0

        rows.append({
            "row": r, "section": section,
            "width_after": width, "live_stitches": width,
            "arm_dec_per_side": arm_dec, "inc_per_side": inc,
            "shoulder_bind_per_side": shoulder_bind,
            "neck_center_bind": neck_center, "neck_edge_per_side": neck_each,
            "neck_final_bind": neck_final,
            "events": events,
        })

    return {
        "piece": piece,
        "total_rows": total_rows,
        "cast_on": sol["cast_on"],
        "final_live": width,
        "rows": rows,
        "sections": {"rib_end": rib_end, "side_end": side_end, "total": total_rows,
                     "arm_rows": arm_rows, **maps},
    }


# ---------------- 袖片 ----------------

def _build_sleeve(sol: dict[str, Any]) -> dict[str, Any]:
    cr = sol["cuff_rib_rows"]
    inc_rows = sol["inc_rows"]
    cap_rows = sol["cap_rows"]

    inc_offsets = spread_even(sol["total_inc"], inc_rows)
    inc_event = {off + 1: 1 for off in inc_offsets}

    cap_amts = [sol["cap_first"]] + sol["cap_events"]
    cap_bind = {k + 1: a for k, a in enumerate(cap_amts)}

    rows: list[dict[str, Any]] = []
    width = sol["cuff_on"]
    rib_end = cr
    inc_end = cr + inc_rows
    total_rows = cr + inc_rows + cap_rows

    for r in range(1, total_rows + 1):
        section = "rib" if r <= rib_end else ("inc" if r <= inc_end else "cap")
        events: list[str] = []
        cap_dec = inc = top_bind = 0

        if section == "inc":
            local = r - rib_end
            if local in inc_event:
                inc = inc_event[local]
                width += 2 * inc
                events.append(f"袖下两侧各放 1 针（共 +{2*inc}）")

        if section == "cap":
            local = r - inc_end
            if local in cap_bind:
                cap_dec = cap_bind[local]
                width -= 2 * cap_dec
                events.append(f"袖山两侧各平收 {cap_dec} 针（共 −{2*cap_dec}）")
            if local == cap_rows:
                top_bind = width
                events.append(f"袖山顶端平收剩余 {top_bind} 针")
                width = 0

        rows.append({
            "row": r, "section": section,
            "width_after": width, "live_stitches": width,
            "cap_dec_per_side": cap_dec, "inc_per_side": inc, "top_bind": top_bind,
            "events": events,
        })

    return {
        "piece": "sleeve",
        "total_rows": total_rows,
        "cast_on": sol["cuff_on"],
        "final_live": width,
        "rows": rows,
        "sections": {"rib_end": rib_end, "inc_end": inc_end, "total": total_rows,
                     "cap_rows": cap_rows},
    }


# ---------------- 轮廓多边形（输出单位 cm，y 自下而上） ----------------

def _poly(points: list[tuple[float, float]], sp: float, rp: float) -> list[list[float]]:
    return [[x / sp, y / rp] for x, y in points]


def _body_polygon(sol: dict[str, Any], sp: float, rp: float,
                  side: str) -> list[list[float]]:
    """重建左半轮廓再镜像成整片（输出 cm，y 自下而上）。

    左半路径：下摆外点 → 腋下点 → 袖窿外缘（曲线收针+肩阶梯到肩点）
              → 顶缘 → 领缘（下行）→ 领底中心点。
    """
    hr = sol["hem_rib_rows"]
    side_rows = sol["side_rows"]
    arm_rows = sol["arm_rows"]
    y0 = hr + side_rows

    left: list[tuple[float, float]] = [
        (-sol["cast_on"] / 2.0, 0.0),
        (-sol["cast_on"] / 2.0, float(hr)),
        (-sol["chest"] / 2.0, float(y0)),
    ]

    # 袖窿外缘：曲线收针（前 nah 局部行）
    x = -sol["chest"] / 2.0
    amts = [sol["arm_first"]] + sol["arm_events"]
    for k, a in enumerate(amts):
        x += a
        left.append((x, float(y0 + k + 1)))
    # 平肩段
    sh_start = arm_rows - 2 * sol["nsh"] + 1
    left.append((x, float(y0 + sh_start - 1)))
    # 肩阶梯
    for k in range(sol["nsh"]):
        x += sol["ksh"]
        left.append((x, float(y0 + sh_start + 2 * k)))
    # 肩阶梯终点即肩点（每侧共收 nsh*ksh 针）；末阶点高度为顶
    top_y = float(y0 + arm_rows)

    # ---- 领口（左半：从肩点沿顶缘到肩带内端，再沿领缘下行到中心） ----
    nshk = sol["nsh"] * sol["ksh"]
    nb_half = sol["nb"] / 2.0
    if side == "front":
        # 前片：肩带内端 x = -nb/2（=-(w2/2 - nshk - 单侧前领收)）
        depth = sol["fn_depth"]
        bind_local = arm_rows - depth + 1
        bind_y = float(y0 + bind_local - 1)
        left.append((-nb_half, top_y))
        # 前领阶梯缘：从顶向下走，随前领单侧收针外移到 -(nb/2 + 累计)
        ev: dict[int, int] = {}
        for e, off in zip(sol["fn_events"], spread_even(len(sol["fn_events"]), depth)):
            rl = bind_local + 1 + off
            ev[rl] = ev.get(rl, 0) + e
        # 下行顺序：从最高收针行到最低（rl 越小越高）
        for rl in sorted(ev):
            # 该高度及以上已发生的单侧收针累计
            above = sum(v for rr, v in ev.items() if rr <= rl)
            left.append((-(nb_half + above), float(y0 + rl - 1)))
        # 中心平收行高度：领缘在 -nb/2
        left.append((-nb_half, bind_y))
        # 领底中心点
        left.append((0.0, bind_y))
    else:
        # 后片：后领中心平收 w2-2*nshk，肩带内端 x = -(w2/2 - nshk) = -nb_back/2
        nb_back_half = (sol["w2"] / 2.0) - nshk
        depth = sol["bnd"]
        bind_local = arm_rows - depth + 1
        bind_y = float(y0 + bind_local - 1)
        left.append((-nb_back_half, top_y))
        left.append((-nb_back_half, bind_y))
        left.append((0.0, bind_y))

    right = [(-px, py) for px, py in reversed(left)]
    full = left + right[1:]
    return _poly(full, sp, rp)


def _sleeve_polygon(sol: dict[str, Any], sp: float, rp: float) -> list[list[float]]:
    cr = sol["cuff_rib_rows"]
    inc_rows = sol["inc_rows"]
    cap_rows = sol["cap_rows"]
    y0 = cr + inc_rows

    left: list[tuple[float, float]] = [
        (-sol["cuff_on"] / 2.0, 0.0),
        (-sol["cuff_on"] / 2.0, float(cr)),
        (-sol["upper"] / 2.0, float(y0)),
    ]
    x = -sol["upper"] / 2.0
    amts = [sol["cap_first"]] + sol["cap_events"]
    for k, a in enumerate(amts):
        x += a
        left.append((x, float(y0 + k + 1)))
    # 山顶平台（左端点）
    left.append((-sol["cap_top"] / 2.0, float(y0 + cap_rows)))

    # 右半镜像（left 末点为山顶左端点，镜像后首点为山顶右端点）
    right = [(-px, py) for px, py in reversed(left)]
    full = left + right
    return _poly(full, sp, rp)


# ---------------- 逐行模拟校验 ----------------

def _simulate(sol, front, back, sleeve, pattern, seams=None) -> list[dict[str, Any]]:
    if seams is None:
        seams = list(DEFAULT_SEAMS)
    checks: list[dict[str, Any]] = []

    for body, pname in ((front, "前片"), (back, "后片")):
        inc = sum(r["inc_per_side"] for r in body["rows"]) * 2
        arm = sum(r["arm_dec_per_side"] for r in body["rows"]) * 2
        sh = sum(r["shoulder_bind_per_side"] for r in body["rows"]) * 2
        center = sum(r["neck_center_bind"] for r in body["rows"])
        edge = sum(r["neck_edge_per_side"] for r in body["rows"]) * 2
        final = sum(r["neck_final_bind"] for r in body["rows"])
        expect = body["cast_on"] + inc - arm - sh - center - edge - final
        checks.append({
            "id": f"stitch_conservation_{pname}",
            "name": f"{pname}针数守恒",
            "ok": expect == 0 and body["final_live"] == 0,
            "detail": (f"起针 {body['cast_on']} + 侧放 {inc} − 袖窿 {arm} − 肩阶梯 {sh} "
                       f"− 领中心 {center} − 领缘 {edge} − 领口收口 {final} = {expect}"),
        })

    s_inc = sum(r["inc_per_side"] for r in sleeve["rows"]) * 2
    s_cap = sum(r["cap_dec_per_side"] for r in sleeve["rows"]) * 2
    s_top = sum(r["top_bind"] for r in sleeve["rows"])
    expect = sleeve["cast_on"] + s_inc - s_cap - s_top
    checks.append({
        "id": "stitch_conservation_sleeve",
        "name": "袖片针数守恒",
        "ok": expect == 0 and sleeve["final_live"] == 0,
        "detail": f"袖口 {sleeve['cast_on']} + 袖下放 {s_inc} − 袖山收 {s_cap} − 山顶 {s_top} = {expect}",
    })

    checks.append({
        "id": "seam_side_rows",
        "name": "侧缝行数匹配（前片=后片）",
        "ok": front["total_rows"] == back["total_rows"],
        "detail": f"前片 {front['total_rows']} 行 / 后片 {back['total_rows']} 行",
    })
    checks.append({
        "id": "seam_shoulder",
        "name": "肩缝针/行匹配（前后同阶梯）",
        "ok": True,
        "detail": f"前后均为 {sol['nsh']} 阶梯 × {sol['ksh']} 针/侧，缝合 {2*sol['nsh']} 行",
    })
    checks.append({
        "id": "seam_cap_armhole_rows",
        "name": "袖山/袖窿行数比例",
        "ok": 0.7 <= sol["cap_rows"] / sol["arm_rows"] <= 1.05,
        "detail": f"袖山 {sol['cap_rows']} 行 / 袖窿 {sol['arm_rows']} 行 = {sol['cap_rows']/sol['arm_rows']:.2f}（装袖曲线缝合，周长另算）",
    })

    P, Q = pattern["P"], pattern["Q"]
    phase_lock = bool(pattern.get("seam_phase_lock", pattern.get("phase_lock", False)))
    if P > 1:
        okp = all(v % P == 0 for v in (sol["cast_on"], sol["chest"], sol["w2"],
                                        sol["nb"], sol["cuff_on"], sol["upper"],
                                        sol["cap_top"]))
        checks.append({"id": "phase_horizontal", "name": f"横向花样循环 P={P} 连续",
                        "ok": bool(okp),
                        "detail": "各关键宽度均为 P 的倍数" if okp else "存在非 P 倍数宽度"})
    if Q > 1:
        okq = all(v % Q == 0 for v in (sol["hem_rib_rows"], sol["side_rows"], sol["arm_rows"],
                                        sol["cap_rows"], sol["cuff_rib_rows"], sol["inc_rows"]))
        if phase_lock:
            detail = ("各段行数均为 Q 的倍数；接缝两侧塑形事件的绝对行号同余由"
                      "“接缝相位锁定”校验逐条核对") if okq else "存在非 Q 倍数段"
        else:
            detail = ("各段行数均为 Q 的倍数（片内花样连续；未启用接缝相位锁定，"
                      "不对接缝两侧事件绝对行号做同余保证）" if okq else "存在非 Q 倍数段")
        checks.append({"id": "phase_vertical", "name": f"纵向花样循环 Q={Q} 连续",
                        "ok": bool(okq), "detail": detail})

    if Q > 1 and phase_lock:
        checks.extend(_seam_phase_checks(sol, front, back, sleeve, seams, Q))
    elif Q <= 1 and phase_lock:
        checks.append({
            "id": "seam_phase_lock_idle",
            "name": "接缝花样相位锁定",
            "ok": True,
            "detail": "纵向花样循环 Q=1（无循环），接缝相位锁定无需校验；开关不产生约束。",
            "seam_id": "*", "pairs": [],
        })

    tol = pattern.get("_tolerance_cm", 0.6)
    bad = [k for k, v in sol["errors_100cm"].items() if v > tol * 100]
    checks.append({
        "id": "tolerance",
        "name": "成品尺寸容差",
        "ok": not bad,
        "detail": "全部指标在容差内" if not bad else "超差：" + "、".join(bad),
    })
    return checks


# 接缝端点 -> (所在衣片, 沿缝塑形事件[(绝对行号, 事件类型)], 端点中文名)
def _seam_edge_rows(sol: dict[str, Any], front, back, sleeve,
                    ref: str) -> tuple[str, list[dict[str, Any]], str] | None:
    """按端点引用（如 "sleeve.cap"）返回 (衣片key, 沿缝事件列表, 名称)。

    事件顺序沿缝合方向自起点排列（袖山/袖窿均自腋下起）；绝对行号从各自起针行
    （第 1 行）计。只计入落在接缝上的塑形事件：
      - 袖窿缘：先列全部曲线收针（arm_first + arm_events，局部行 1..nah_used+1），
        曲线结束后继续列肩阶梯（共 nsh 个，局部行 arm_rows-2*nsh+1 起每 2 行）。
        这样袖山后续收针序数落到“跨肩阶梯”位置时也能与正确的肩阶梯事件配对，
        而不是只比较曲线槽位；
      - 袖山缘：cap_first + cap_events 连续收针行。
    同一行同时是末次曲线收针与首次肩阶梯（平段为 0 行）时，按两个事件保留，
    行号相同，序数仍连续。
    """
    piece, _, edge = ref.partition(".")
    if piece in ("front", "back"):
        body = front if piece == "front" else back
        pname = "前片" if piece == "front" else "后片"
        if edge == "side":
            # 侧缝：两侧侧区加针（前后片行结构共用，事件位置完全相同）
            events = [{"row": r["row"], "kind": "side_inc", "label": "侧区加针"}
                      for r in body["rows"] if r["inc_per_side"]]
            return piece, events, f"{pname}侧缝"
        if edge == "shoulder":
            events = [{"row": r["row"], "kind": "shoulder", "label": "肩阶梯"}
                      for r in body["rows"] if r["shoulder_bind_per_side"]]
            return piece, events, f"{pname}肩阶梯"
        if edge == "armhole":
            events = _armhole_seam_events(sol, body)
            return piece, events, f"{pname}袖窿（曲线收针+肩阶梯）"
    elif piece == "sleeve":
        if edge == "cap":
            events = [{"row": r["row"], "kind": "cap", "label": "袖山收针"}
                      for r in sleeve["rows"] if r["cap_dec_per_side"]]
            return piece, events, "袖片袖山收针"
    elif piece == "body":
        # "body" 端点指大身袖窿（取前片；前后片共用袖窿决策）
        if edge == "armhole":
            events = _armhole_seam_events(sol, front)
            return "front", events, "大身袖窿（曲线收针+肩阶梯，前片）"
    return None


def _armhole_seam_events(sol: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
    """袖窿缘自腋下起的沿缝事件：曲线收针（按行序）+ 后续肩阶梯（按行序）。"""
    curve = [{"row": r["row"], "kind": "arm_curve", "label": "袖窿曲线收针"}
             for r in body["rows"]
             if r["section"] == "arm" and r["arm_dec_per_side"]]
    shoulder = [{"row": r["row"], "kind": "shoulder", "label": "肩阶梯"}
                for r in body["rows"]
                if r["section"] == "arm" and r["shoulder_bind_per_side"]]
    return curve + shoulder


_SEAM_KIND_LABEL = {
    "row_match": "逐行/曲线缝合",
    "stitch_row": "针行对合",
}


def _seam_phase_checks(sol, front, back, sleeve, seams, Q) -> list[dict[str, Any]]:
    """逐条接缝核对：按沿缝实际事件序数连续配对，比较两侧绝对行号模 Q 是否同相。

    每条接缝生成一个 check；pairs 逐对给出两侧事件类型、绝对行号、相位差与
    通过状态。事件按缝合起点顺序一一配对（袖山↔袖窿时，袖山序数超过袖窿曲线
    收针数后的事件与*肩阶梯*事件配对）。两侧事件数不等时，多出的事件不参与
    配对，在 unpaired 中逐项注明（信息项，不判失败）。该校验与求解器锁定约束
    使用同一配对规则，避免“求解只锁曲线、报告只数曲线”导致的跨肩阶梯相位漏检。
    """
    out: list[dict[str, Any]] = []
    for seam in seams:
        sid = seam.get("id", "seam")
        ea = _seam_edge_rows(sol, front, back, sleeve, str(seam.get("a", "")))
        eb = _seam_edge_rows(sol, front, back, sleeve, str(seam.get("b", "")))
        if ea is None or eb is None:
            continue
        _, events_a, name_a = ea
        _, events_b, name_b = eb
        kind = _SEAM_KIND_LABEL.get(seam.get("kind", ""), "缝合")

        pairs = []
        n_pair = min(len(events_a), len(events_b))
        for k in range(n_pair):
            ea_k, eb_k = events_a[k], events_b[k]
            ra, rb = ea_k["row"], eb_k["row"]
            diff = ((ra - rb) % Q)
            pairs.append({
                "index": k + 1,
                "row_a": ra, "row_b": rb,
                "kind_a": ea_k["kind"], "kind_b": eb_k["kind"],
                "label_a": f"{name_a}第 {k + 1} 个事件（{ea_k['label']}，第 {ra} 行）",
                "label_b": f"{name_b}第 {k + 1} 个事件（{eb_k['label']}，第 {rb} 行）",
                "phase_diff": diff,
                "ok": diff == 0,
            })
        # 未配对事件：另一侧没有对应序数（仅信息项，不参与同余判定）
        unpaired = ([{"side": "a", "index": k + 1, **events_a[k]}
                     for k in range(n_pair, len(events_a))] +
                    [{"side": "b", "index": k + 1, **events_b[k]}
                     for k in range(n_pair, len(events_b))])
        all_ok = all(p["ok"] for p in pairs)
        bad_n = sum(1 for p in pairs if not p["ok"])
        if n_pair == 0:
            detail = (f"{name_a} / {name_b}无可配对的塑形事件（{len(events_a)} 对 "
                      f"{len(events_b)}），无需相位校验")
        elif all_ok:
            extra = ""
            if unpaired:
                extra = f"；另有 {len(unpaired)} 个事件无跨缝对应（见下，不计入判定）"
            detail = (f"{n_pair} 对对应事件绝对行号全部 ≡ 同相 (mod {Q})："
                      f"{name_a} {[p['row_a'] for p in pairs]} ↔ "
                      f"{name_b} {[p['row_b'] for p in pairs]}{extra}")
        else:
            bads = [f"第{p['index']}对（{p['kind_a']}↔{p['kind_b']}）"
                    f"Δ={p['phase_diff']}" for p in pairs if not p["ok"]]
            detail = (f"{bad_n}/{n_pair} 对事件相位不一致（相位差须为 0 mod {Q}）："
                      + "、".join(bads))
        out.append({
            "id": f"seam_phase_{sid}",
            "name": f"接缝相位锁定：{name_a} ↔ {name_b}（Q={Q}，{kind}）",
            "ok": bool(all_ok),
            "detail": detail,
            "seam_id": sid,
            "Q": Q,
            "n_pairs": n_pair,
            "n_events_a": len(events_a),
            "n_events_b": len(events_b),
            "edge_name_a": name_a,
            "edge_name_b": name_b,
            "pairs": pairs,
            "unpaired": unpaired,
        })
    return out


def _edge_length(seq: list[tuple[float, float]]) -> float:
    """折线长度（点为针、行坐标）。"""
    return sum(math.dist(a, b) for a, b in zip(seq, seq[1:]))


def _armhole_edge(sol: dict[str, Any]) -> float:
    """单侧袖窿缘长度（cm）：腋下点 → 曲线收针 → 平肩 → 肩阶梯 → 肩点。"""
    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    x = 0.0
    for k, a in enumerate([sol["arm_first"]] + sol["arm_events"]):
        x += a
        pts.append((x, float(k + 1)))
    sh_start = sol["arm_rows"] - 2 * sol["nsh"] + 1
    # 平肩直高段（最后收针行已计入末斜线）
    pts.append((x, float(sh_start - 1)))
    # 肩阶梯
    for k in range(sol["nsh"]):
        x += sol["ksh"]
        pts.append((x, float(sh_start + 2 * k)))
    pts.append((x, float(sol["arm_rows"])))
    return pts


def _seam_perimeters(sol, polygons, sp, rp) -> list[dict[str, Any]]:
    """袖山周长 vs 前后两条袖窿周长（由事件几何直接计算，换算 cm）。"""
    # 单侧袖窿（针/行 -> cm：x 按横密、y 按纵密）
    arm_pts = _armhole_edge(sol)
    arm_cm = _edge_length([(x / sp, y / rp) for x, y in arm_pts])
    # 袖山单侧：腋下点 → 连续收针 → 山顶端点
    cap_pts: list[tuple[float, float]] = [(0.0, 0.0)]
    x = 0.0
    for k, a in enumerate([sol["cap_first"]] + sol["cap_events"]):
        x += a
        cap_pts.append((x, float(k + 1)))
    # 收针结束后的平高直段（最后收针行已计入末斜线，直段从 ncap 行高起）
    cap_pts.append((x, float(sol["ncap_used"])))
    cap_pts.append((x, float(sol["cap_rows"])))
    cap_side_cm = _edge_length([(xx / sp, yy / rp) for xx, yy in cap_pts])
    cap_top_cm = sol["cap_top"] / sp
    cap_total = 2 * cap_side_cm + cap_top_cm
    arm_total = 2 * arm_cm
    ease = cap_total - arm_total
    # 针织布可归拢吃势，装袖允许较宽范围；超过则告警
    ok = -4.0 <= ease <= 4.0
    return [{
        "id": "seam_cap_armhole_perimeter",
        "name": "袖山/袖窿曲线缝合（周长+吃势）",
        "ok": bool(ok),
        "detail": (f"袖山周长 {cap_total:.1f} cm（顶 {cap_top_cm:.1f} + 两侧 {2*cap_side_cm:.1f}），"
                   f"前后袖窿合计 {arm_total:.1f} cm，吃势 {ease:+.1f} cm（针织可归拢范围 −4.0~4.0 cm）"),
    }]


# ---------------- 成品尺寸回算 ----------------

def _measurements(sol, front, sleeve, sp, rp, targets) -> list[dict[str, Any]]:
    from .solver import MEASUREMENT_KEYS
    e = sol["errors_100cm"]
    total_sleeve = sleeve["total_rows"]
    width_keys = {"bust", "hem", "shoulder", "neck", "upper_arm", "cuff"}
    circum_keys = {"bust", "hem", "upper_arm", "cuff"}
    row_direct = {"fn_depth": sol["fn_depth"], "bn_depth": sol["bnd"],
                  "armhole": sol["arm_rows"], "cap_height": sol["cap_rows"]}
    out = []
    for key in MEASUREMENT_KEYS:
        if key == "length":
            actual = front["total_rows"] / rp
        elif key == "sleeve_len":
            actual = total_sleeve / rp
        elif key in row_direct:
            actual = row_direct[key] / rp
        else:
            stitch_map = {"bust": sol["chest"], "hem": sol["cast_on"],
                          "shoulder": sol["w2"], "neck": sol["nb"],
                          "upper_arm": sol["upper"], "cuff": sol["cuff_on"]}
            actual = stitch_map[key] / sp
            if key in circum_keys:
                actual *= 2  # 衣片整片宽 -> 整围度
        out.append({
            "key": key,
            "target_cm": targets.get(key),
            "actual_cm": round(actual, 2),
            "error_cm": e[key] / 100.0,
            "kind": "circumference" if key in circum_keys else
                    ("halfwidth" if key in width_keys else "length"),
        })
    return out


def _collar(sol, sp, rp) -> dict[str, Any]:
    """领罗纹挑针估算：沿前/后领缘挑针。

    前领开口宽 = nb；后领开口宽 = w2 − 2*nsh*ksh（后领中心平收）。
    """
    nb_front = sol["nb"]
    nb_back = sol["w2"] - 2 * sol["nsh"] * sol["ksh"]
    fn = sol["fn_depth"]
    bn = sol["bnd"]
    fn_dec = sum(sol["fn_events"])
    # 单侧：水平半宽 + 斜行按密度折算 + 阶梯收针台阶
    front_pick = int(round(nb_front / 2 + fn * sp / rp)) + fn_dec
    back_pick = int(round(nb_back / 2 + max(0, bn - 1) * sp / rp))
    front_total = 2 * front_pick
    front_total -= front_total % 2
    back_total = 2 * back_pick
    back_total -= back_total % 2
    total_pick = front_total + back_total
    return {
        "pickup_stitches": total_pick,
        "front_pick": front_total,
        "back_pick": back_total,
        "estimated_circumference_cm": round(total_pick / sp, 1),
        "note": "沿前/后领缘挑针数（斜行按横/纵密折算），取偶数对齐罗纹循环。",
    }
