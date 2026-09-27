"""试织纠偏：以“已织前缀”为硬约束，仅重排某衣片未织行的塑形事件。

业务语义见 README「试织纠偏」一节。模型要点：

- 前缀（已完成行）**不得改写**：逐行登记实际活针数、实际塑形操作与意外增减针，
  并须逐行自洽（按操作+意外针推算的活针数必须等于登记值）；
- 固定总行数、各区段行数与其他衣片；大身侧缝加针行、肩阶梯行/针数、袖窿曲线
  收针行位全部锁定（前后片共用同一套袖窿/肩决策、袖片与其曲线缝合）；
- 大身可在未织行重排的只有**领口塑形**：
    前片：前领中心平收针数 nb、前领深 fn_depth（中心平收行随之移动）、
          前领缘阶梯收针（总量/每事件针数 1..4 单调不增/行位，间隔 ≥2 行）；
    后片：后领深 bnd（中心平收行）；后领中心平收针数 = w2 + 意外针 − 2·nsh·ksh。
  全部意外增减针经领口吸收：nb + 2·E = w2 + A（自由领口缘，无接缝约束）；
- 袖片可在未织行重排：袖下加针位置（总量 T 可变，袖肥须守容差带）与袖山收针
  槽位（每侧总量 Tcap = (upper − cap_top + A)/2，含意外针补偿，单调不增）；
- 硬约束：针数守恒（含意外针）、成品尺寸容差（领口宽/前领深/后领深/袖肥）、
  接缝匹配（沿缝事件对数不少于原方案、周长吃势 ±4cm、袖窿曲线与另一衣片一致、
  侧缝/肩缝逐行）、seam_phase_lock 启用且 Q>1 时新增袖山事件与袖窿同相、
  P/Q/偶数模数；
- 择优（字典序）：①未织行中相对原指令**变化的塑形事件行数**最少；
  ②未织行相对原指令的**针数偏差**（逐通道 L1）最小。
- 无解返回冲突诊断，**不产出修订、不覆盖原指令**。
"""
from __future__ import annotations

import math
from typing import Any

from ortools.sat.python import cp_model

from .solver import _band_for
from .compiler import (
    spread_even, _seam_perimeters, _seam_phase_checks,
    _armhole_edge, _edge_length,
)

REVISION_SOLVER_TIME = 10.0
W_CHANGED = 100_000          # 变化事件行权重（远大于针数偏差上界）

NCAP_MAX = 16
NFN_MAX = 12

PIECE_NAME = {"front": "前片", "back": "后片", "sleeve": "袖片"}


# ---------------- 事件文案（与 compiler.py 一致） ----------------

def _ev_side_inc() -> str:
    return "两侧各放 1 针（共 +2）"


def _ev_arm(a: int) -> str:
    return f"袖窿两侧各平收 {a} 针（共 −{2 * a}）"


def _ev_shoulder(k: int) -> str:
    return f"肩端两侧各平收 {k} 针（共 −{2 * k}）"


def _ev_neck_center(n: int, piece: str) -> str:
    return f"{'前领' if piece == 'front' else '后领'}中心平收 {n} 针"


def _ev_neck_edge(e: int) -> str:
    return f"前领两侧各收 {e} 针（共 −{2 * e}）"


def _ev_neck_final(n: int, piece: str) -> str:
    label = "前领沿" if piece == "front" else "后领沿"
    return f"{label}剩余 {n} 针平收（领口收口）"


def _ev_sleeve_inc() -> str:
    return "袖下两侧各放 1 针（共 +2）"


def _ev_cap(a: int) -> str:
    return f"袖山两侧各平收 {a} 针（共 −{2 * a}）"


def _ev_top(n: int) -> str:
    return f"袖山顶端平收剩余 {n} 针"


# ============================================================
# 入口
# ============================================================

def plan_revision(result: dict[str, Any], project: dict[str, Any],
                  size_name: str, piece: str, prefix_rows: int,
                  register: list[dict[str, Any]]) -> dict[str, Any]:
    compiled = next((c for c in result.get("compiled", []) if c["name"] == size_name), None)
    if compiled is None:
        return _invalid(f"求解结果中没有尺码 {size_name}")
    if piece not in ("front", "back", "sleeve"):
        return _invalid(f"未知衣片 {piece}")

    sol = compiled["solution"]
    piece_rows = compiled["pieces"][piece]["rows"]
    total = compiled["pieces"][piece]["total_rows"]

    if not isinstance(prefix_rows, int) or isinstance(prefix_rows, bool) \
            or not (1 <= prefix_rows < total):
        return _invalid(f"已织前缀行数须为 1..{total - 1} 的整数（至少保留 1 行未织）")

    gauge = result["gauge"]
    sp = float(gauge["stitches_per_cm"])
    rp = float(gauge["rows_per_cm"])
    s_scale = max(1, int(round(100.0 / sp)))
    r_scale = max(1, int(round(100.0 / rp)))
    pat = result.get("pattern", {})
    P, Q = int(pat.get("P", 1)), int(pat.get("Q", 1))
    phase_lock = bool(pat.get("seam_phase_lock", pat.get("phase_lock", False)))
    tol_cm = float(project.get("tolerance_cm", 0.6))
    tol100 = int(round(tol_cm * 100))
    targets = next((s.get("targets", {}) for s in project.get("sizes", [])
                    if s.get("name") == size_name), {})
    seams = project.get("seams")

    n_pairs0 = min(1 + sol["nah_used"] + sol["nsh"], 1 + sol["ncap_used"])

    reg_index: dict[int, dict[str, Any]] = {}
    for rr in register or []:
        if isinstance(rr.get("row"), int) and not isinstance(rr["row"], bool):
            reg_index[int(rr["row"])] = rr
    missing = [r for r in range(1, prefix_rows + 1) if r not in reg_index]
    if missing:
        return _invalid("登记表缺少行：" + ", ".join(map(str, missing[:10])))

    if piece == "sleeve":
        fx = _parse_prefix_sleeve(sol, piece_rows, prefix_rows, reg_index)
    else:
        fx = _parse_prefix_body(sol, piece_rows, prefix_rows, reg_index, piece)
    if fx["row_errors"]:
        return {"status": "invalid",
                "message": "逐行登记与塑形结构/活针数不自洽，请按下表修正后重试（原指令未改动）。",
                "row_errors": fx["row_errors"], "conflicts": []}

    common = (sol, targets, P, Q, sp, rp, s_scale, r_scale, tol100, n_pairs0,
              phase_lock, seams)
    if piece == "sleeve":
        answer = _solve_sleeve(fx, set(), *common)
    else:
        answer = _solve_body(fx, piece, set(), *common)

    if answer["status"] != "ok":
        conflicts = _diagnose(fx, piece, answer.get("pre_conflicts", []), *common)
        return {"status": "infeasible",
                "message": "以该已织前缀为重排硬约束时无可行解，原指令保持不变。",
                "row_errors": [], "conflicts": conflicts}

    revised_rows, sol_patch = _build_rows(sol, piece, piece_rows, fx, answer)
    checks = _verify(compiled, sol, piece, revised_rows, sol_patch, P, Q, phase_lock,
                     seams, tol_cm, sp, rp, targets, n_pairs0, fx)
    failed = [c for c in checks if not c["ok"]]
    if failed:
        # 安全兜底：模型应已排除；万一漏网，按不可行处理，绝不返回坏修订
        return {"status": "infeasible",
                "message": "重排解未通过成稿校验，原指令保持不变。",
                "row_errors": [],
                "conflicts": [{"field": "*", "size": size_name,
                               "reason": f"{c['name']}：{c['detail']}"} for c in failed]}

    return {
        "status": "ok",
        "size": size_name,
        "piece": piece,
        "prefix_rows": prefix_rows,
        "objective": {
            "changed_event_rows": answer["changed_rows"],
            "stitch_deviation": answer["deviation"],
            "wall_time_s": answer["wall_time_s"],
            "optimal": answer["optimal"],
        },
        "accidental_total": fx["acc_total"],
        "prefix": _prefix_view(piece_rows, fx, prefix_rows),
        "tail_old": [_row_brief(r) for r in piece_rows[prefix_rows:]],
        "tail_new": [_row_brief(r) for r in revised_rows[prefix_rows:]],
        "revised_rows": revised_rows,
        "sol_patch": sol_patch,
        "checks": checks,
    }


def _invalid(message: str) -> dict[str, Any]:
    return {"status": "invalid", "message": message, "row_errors": [], "conflicts": []}


# ============================================================
# 前缀登记解析
# ============================================================

def _reg_int(row_errors, row, reg, key, default=0):
    v = reg.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool):
        row_errors.append({"row": row, "field": key,
                           "reason": f"{key} 须为整数（收到 {v!r}）"})
        return default
    return int(v)


def _parse_prefix_body(sol, piece_rows, prefix_rows, reg_index, piece) -> dict[str, Any]:
    hr, side_rows, arm_rows = sol["hem_rib_rows"], sol["side_rows"], sol["arm_rows"]
    side_end = hr + side_rows
    nsh, ksh = sol["nsh"], sol["ksh"]
    sh_start = arm_rows - 2 * nsh + 1
    shoulder_locals = {sh_start + 2 * k for k in range(nsh)}
    orig_side_rows = {r["row"] for r in piece_rows if r["inc_per_side"]}

    errors: list[dict[str, Any]] = []
    width = sol["cast_on"]
    acc_total = 0
    curve_rows: dict[int, int] = {}        # 局部行 -> 每侧收针量（实际）
    curve_locals: list[int] = []
    center_row: int | None = None
    center_amt: int | None = None
    edge_prefix: list[tuple[int, int]] = []

    for r in range(1, prefix_rows + 1):
        reg = reg_index[r]
        inc = _reg_int(errors, r, reg, "inc_per_side")
        arm = _reg_int(errors, r, reg, "arm_dec_per_side")
        sh = _reg_int(errors, r, reg, "shoulder_bind_per_side")
        nc = _reg_int(errors, r, reg, "neck_center_bind")
        ne = _reg_int(errors, r, reg, "neck_edge_per_side")
        acc = _reg_int(errors, r, reg, "accidental")
        live = _reg_int(errors, r, reg, "live")
        section = piece_rows[r - 1]["section"]
        local = r - side_end if section == "arm" else None

        if section == "rib":
            for key, val in (("inc_per_side", inc), ("arm_dec_per_side", arm),
                             ("shoulder_bind_per_side", sh),
                             ("neck_center_bind", nc), ("neck_edge_per_side", ne)):
                if val:
                    errors.append({"row": r, "field": key,
                                   "reason": "罗纹段不允许塑形操作"})
        elif section == "side":
            if inc not in (0, 1):
                errors.append({"row": r, "field": "inc_per_side",
                               "reason": "侧区每侧每行只能放 1 针"})
            elif bool(inc) != (r in orig_side_rows):
                errors.append({"row": r, "field": "inc_per_side",
                               "reason": "侧缝与未修订衣片逐行匹配：加针行必须与原指令一致，不可新增或跳过"})
            for key, val in (("arm_dec_per_side", arm), ("shoulder_bind_per_side", sh),
                             ("neck_center_bind", nc), ("neck_edge_per_side", ne)):
                if val:
                    errors.append({"row": r, "field": key,
                                   "reason": "该操作不允许出现在侧区行"})
        else:  # arm
            if inc:
                errors.append({"row": r, "field": "inc_per_side",
                               "reason": "挂肩段不允许侧区加针"})
            if local in shoulder_locals:
                if sh != ksh:
                    errors.append({"row": r, "field": "shoulder_bind_per_side",
                                   "reason": f"肩阶梯行固定每侧平收 {ksh} 针（肩缝匹配，不可改）"})
            elif sh:
                errors.append({"row": r, "field": "shoulder_bind_per_side",
                               "reason": "肩阶梯只允许发生在原定肩阶梯行"})
            if arm:
                if curve_locals and local != curve_locals[-1] + 1:
                    errors.append({"row": r, "field": "arm_dec_per_side",
                                   "reason": "袖窿曲线收针须自挂肩局部第 1 行起逐行连续（不可在未修订行补行）"})
                elif not curve_locals and local != 1:
                    errors.append({"row": r, "field": "arm_dec_per_side",
                                   "reason": "袖窿曲线收针必须从挂肩局部第 1 行开始"})
                else:
                    hi = 8 if not curve_locals else 4
                    if not 1 <= arm <= hi:
                        errors.append({"row": r, "field": "arm_dec_per_side",
                                       "reason": f"首次大平收每侧 1..8 针、其后每侧 1..4 针"})
                    elif curve_locals and arm > curve_rows[curve_locals[-1]]:
                        errors.append({"row": r, "field": "arm_dec_per_side",
                                       "reason": "曲线收针量须单调不增"})
                    else:
                        curve_rows[local] = arm
                        curve_locals.append(local)
            if nc:
                if center_row is not None:
                    errors.append({"row": r, "field": "neck_center_bind",
                                   "reason": "领口中心平收只能发生一次"})
                elif nc < 0:
                    errors.append({"row": r, "field": "neck_center_bind",
                                   "reason": "平收针数不能为负"})
                elif local >= arm_rows:
                    errors.append({"row": r, "field": "neck_center_bind",
                                   "reason": "中心平收不能落在末行"})
                else:
                    center_row, center_amt = local, nc
            if ne:
                if piece != "front":
                    errors.append({"row": r, "field": "neck_edge_per_side",
                                   "reason": "后片无领缘阶梯收针"})
                elif center_row is None or local <= center_row:
                    errors.append({"row": r, "field": "neck_edge_per_side",
                                   "reason": "领缘收针须晚于前领中心平收行"})
                elif local >= arm_rows:
                    errors.append({"row": r, "field": "neck_edge_per_side",
                                   "reason": "领缘收针不能落在末行"})
                elif not 1 <= ne <= 4:
                    errors.append({"row": r, "field": "neck_edge_per_side",
                                   "reason": "每次每侧领缘收 1..4 针"})
                elif edge_prefix and (
                        ne > edge_prefix[-1][1] or local - edge_prefix[-1][0] < 2):
                    errors.append({"row": r, "field": "neck_edge_per_side",
                                   "reason": "领缘收针须单调不增且相邻事件至少间隔 2 行"})
                else:
                    edge_prefix.append((local, ne))

        delta = 2 * inc - 2 * arm - 2 * sh - nc - 2 * ne + acc
        new_width = width + delta
        if new_width < 0:
            errors.append({"row": r, "field": "live",
                           "reason": f"本行后活针数推算为 {new_width}，不能为负（请核对意外增减针）"})
        if live != new_width:
            errors.append({"row": r, "field": "live",
                           "reason": f"实际活针 {live} 与按操作+意外针推算值 {new_width} 不一致"})
        width = new_width
        acc_total += acc

    return {
        "row_errors": errors,
        "width_end": width,
        "acc_total": acc_total,
        "curve_rows": curve_rows,
        "center_row": center_row, "center_amt": center_amt,
        "edge_prefix": edge_prefix,
        "last_prefix_local": prefix_rows - side_end,
        "reg_by_row": reg_index,
        "side_end": side_end,
    }


def _parse_prefix_sleeve(sol, piece_rows, prefix_rows, reg_index) -> dict[str, Any]:
    cr, inc_rows = sol["cuff_rib_rows"], sol["inc_rows"]
    inc_end = cr + inc_rows
    errors: list[dict[str, Any]] = []
    width = sol["cuff_on"]
    acc_total = 0
    inc_globals: list[int] = []
    cap_rows_map: dict[int, int] = {}
    cap_locals: list[int] = []

    for r in range(1, prefix_rows + 1):
        reg = reg_index[r]
        inc = _reg_int(errors, r, reg, "inc_per_side")
        cap = _reg_int(errors, r, reg, "cap_dec_per_side")
        acc = _reg_int(errors, r, reg, "accidental")
        live = _reg_int(errors, r, reg, "live")
        section = piece_rows[r - 1]["section"]
        local = r - inc_end if section == "cap" else None

        if section == "rib":
            if inc or cap:
                errors.append({"row": r, "field": "inc_per_side",
                               "reason": "罗纹段不允许塑形操作"})
        elif section == "inc":
            if inc not in (0, 1):
                errors.append({"row": r, "field": "inc_per_side",
                               "reason": "袖下每侧每行只能放 1 针"})
            elif inc:
                if inc_globals and r - inc_globals[-1] < 2:
                    errors.append({"row": r, "field": "inc_per_side",
                                   "reason": "相邻加针行至少间隔 2 行"})
                else:
                    inc_globals.append(r)
            if cap:
                errors.append({"row": r, "field": "cap_dec_per_side",
                               "reason": "袖下加针段不允许袖山收针"})
        else:  # cap
            if inc:
                errors.append({"row": r, "field": "inc_per_side",
                               "reason": "袖山段不允许袖下加针"})
            if cap:
                if cap_locals and local != cap_locals[-1] + 1:
                    errors.append({"row": r, "field": "cap_dec_per_side",
                                   "reason": "袖山收针须自袖山局部第 1 行起逐行连续（不可在未修订行补行）"})
                elif not cap_locals and local != 1:
                    errors.append({"row": r, "field": "cap_dec_per_side",
                                   "reason": "袖山收针必须从袖山局部第 1 行开始"})
                else:
                    hi = 12 if not cap_locals else 6
                    if not (1 if cap_locals else 2) <= cap <= hi:
                        errors.append({"row": r, "field": "cap_dec_per_side",
                                       "reason": "袖山首次大平收每侧 2..12 针、其后每侧 1..6 针"})
                    elif cap_locals and cap > cap_rows_map[cap_locals[-1]]:
                        errors.append({"row": r, "field": "cap_dec_per_side",
                                       "reason": "袖山收针量须单调不增"})
                    else:
                        cap_rows_map[local] = cap
                        cap_locals.append(local)

        delta = 2 * inc - 2 * cap + acc
        new_width = width + delta
        if new_width < 0:
            errors.append({"row": r, "field": "live",
                           "reason": f"本行后活针数推算为 {new_width}，不能为负（请核对意外增减针）"})
        if live != new_width:
            errors.append({"row": r, "field": "live",
                           "reason": f"实际活针 {live} 与按操作+意外针推算值 {new_width} 不一致"})
        width = new_width
        acc_total += acc

    return {
        "row_errors": errors,
        "width_end": width,
        "acc_total": acc_total,
        "inc_globals": inc_globals,
        "cap_rows": cap_rows_map,
        "last_prefix_local_cap": prefix_rows - inc_end,
        "reg_by_row": reg_index,
        "inc_end": inc_end,
    }


# ============================================================
# 共用：折线长度 / 择优辅助
# ============================================================

def _seg_table(s_scale: int, r_scale: int) -> dict[tuple[int, int], int]:
    return {(dx, dy): int(round(math.hypot(dx * s_scale, dy * r_scale)))
            for dx in range(0, 14) for dy in range(0, 4)}


def _seg_var(model, tbl, dx, dy: int, name: str):
    arr = [tbl[(x, dy)] for x in range(0, 14)]
    v = model.new_int_var(0, max(arr) + 1, name)
    model.add_element(dx, arr, v)
    return v


def _body_arm_side_100(sol, s_scale: int, r_scale: int) -> int:
    """未修订大身：单侧袖窿折线长（1/100cm，与 compiler._armhole_edge 同口径）。"""
    return int(round(_edge_length([(x * s_scale, y * r_scale)
                                   for x, y in _armhole_edge(sol)])))


def _abs_expr(model, new_v, old: int, name: str, ub: int = 12):
    d = model.new_int_var(0, ub, f"dev_{name}")
    dd = model.new_int_var(-ub, ub, f"dd_{name}")
    model.add(dd == new_v - old)
    model.add_abs_equality(d, dd)
    return d


def _ne_expr(model, new_v, old: int, name: str):
    chg = model.new_bool_var(f"chg_{name}")
    model.add(new_v == old).only_enforce_if(chg.Not())
    model.add(new_v != old).only_enforce_if(chg)
    return chg


def _orig_edge_map(sol) -> dict[int, int]:
    arm_rows, depth = sol["arm_rows"], sol["fn_depth"]
    bind_local = arm_rows - depth + 1
    out: dict[int, int] = {}
    for e, off in zip(sol["fn_events"], spread_even(len(sol["fn_events"]), depth)):
        lr = bind_local + 1 + off
        out[lr] = out.get(lr, 0) + e
    return out


# ============================================================
# CP-SAT 重排：大身（仅领口塑形）
# ============================================================

def _solve_body(fx, piece, relax, sol, targets, P, Q, sp, rp, s_scale, r_scale,
                tol100, n_pairs0, phase_lock, seams):
    arm_rows = sol["arm_rows"]
    nsh, ksh = sol["nsh"], sol["ksh"]
    w2 = sol["w2"]
    A = fx["acc_total"]
    pre: list[dict[str, str]] = []

    # 偶数锚点决定意外针合计必须为偶数（领口中心 nb 为偶数）
    if A % 2 == 1:
        pre.append({"field": "*", "size": sol["name"],
                    "reason": f"意外增减针合计 {A:+d} 为奇数；大身关键宽度与领口中心平收均为偶数针，"
                              "未织行塑形只能成对（每侧）增减，无法补偿奇数差值"})

    m = cp_model.CpModel()
    dev_terms: list = []
    chg_flags: list = []

    if piece == "front":
        # ---- 前领深 / 中心平收 ----
        if fx["center_row"] is not None:
            depth = arm_rows - fx["center_row"] + 1
            nb = fx["center_amt"]
            _check_fixed_neck(sol, targets, sp, rp, tol100, P, Q, pre,
                              depth, nb, fx, front=True)
            bind_local = arm_rows - depth + 1
        else:
            lo, hi, _ = _band_for(targets["fn_depth"], rp, tol100, True)
            depth = m.new_int_var(max(2, lo), min(arm_rows - 1, hi), "fn_depth")
            _add_err_bound(m, depth, int(targets["fn_depth"] * rp), r_scale, tol100, "fn_depth")
            if Q > 1 and "phase" not in relax:
                m.add_modulo_equality(0, depth, Q)
            nlo, nhi, _ = _band_for(targets["neck"], sp, tol100, False)
            nb = m.new_int_var(max(0, nlo), nhi, "nb")
            _add_err_bound(m, nb, int(targets["neck"] * sp), s_scale, tol100, "neck")
            m.add_modulo_equality(0, nb, 2)
            if P > 1 and "mod" not in relax:
                m.add_modulo_equality(0, nb, P)
            bind_local = arm_rows - depth + 1
            # 择优：平收针数偏差 + 中心行移动（旧行消失、新行出现，共 2 个事件行）
            dev_terms.append(_abs_expr(m, nb, sol["nb"], "nb", ub=400))
            moved = m.new_bool_var("chg_nb_row")
            model_if_ne(m, depth, sol["fn_depth"], moved)
            chg_flags += [moved, moved]

        # ---- 领缘阶梯收针（总量 E：nb + 2E = w2 + A，意外针由领口吸收） ----
        E = m.new_int_var(0, 400, "edge_total")
        edge_pref = fx["edge_prefix"]
        n_slots = NFN_MAX
        tail_first = fx["last_prefix_local"] + 1
        # 候选行含末行：与 compiler.spread_even 口径一致（末个领缘事件可恰好落在末行，
        # 此时由该事件收净、不再产生“剩余平收”事件）
        cand_rows = [lr for lr in range(max(2, tail_first), arm_rows + 1)]
        E_pref = sum(a for _, a in edge_pref)
        last_fixed_row = edge_pref[-1][0] if edge_pref else None
        slot_on, slot_amt, slot_row = [], [], []
        for k in range(n_slots):
            on = m.new_bool_var(f"fnb{k}")
            amt = m.new_int_var(0, 4, f"fne{k}")
            m.add(amt == 0).only_enforce_if(on.Not())
            m.add(amt >= 1).only_enforce_if(on)
            # 前 len(edge_pref) 个槽位承接已织领缘事件（行/量固定），其后为自由槽位
            if k < len(edge_pref):
                m.add(on == 1)
                lr_fixed, amt_fixed = edge_pref[k]
                m.add(amt == amt_fixed)
                chosen: Any = lr_fixed
            else:
                if k > 0:
                    m.add(on <= _on_expr(slot_on[k - 1]))
                    m.add(amt <= slot_amt[k - 1]).only_enforce_if(on)
                row_bools = []
                for lr in cand_rows:
                    xb = m.new_bool_var(f"fnp{k}_{lr}")
                    row_bools.append(xb)
                for xb, lr in zip(row_bools, cand_rows):
                    m.add(lr >= bind_local + 1).only_enforce_if(xb)
                    if last_fixed_row is not None:
                        m.add(lr >= last_fixed_row + 2).only_enforce_if(xb)
                m.add(sum(row_bools) == 1).only_enforce_if(on)
                m.add(sum(row_bools) == 0).only_enforce_if(on.Not())
                cv = m.new_int_var(0, arm_rows + 1, f"fnr{k}")
                m.add(cv == sum(xb * lr for xb, lr in zip(row_bools, cand_rows)))
                if last_fixed_row is not None:
                    m.add(cv >= last_fixed_row + 2).only_enforce_if(on)
                else:
                    m.add(cv >= bind_local + 1).only_enforce_if(on)
                chosen = cv
            slot_on.append(on)
            slot_amt.append(amt)
            slot_row.append(chosen)
        # 相邻（变量）槽位行距 ≥2：以后一槽位启用为条件
        var_idx = [k for k in range(n_slots) if not isinstance(slot_row[k], int)]
        for a, b in zip(var_idx, var_idx[1:]):
            m.add(slot_row[b] >= slot_row[a] + 2).only_enforce_if(slot_on[b])
        m.add(sum(slot_amt) == E)
        m.add(E >= E_pref)
        # 两肩活针（2·nsh·ksh）在末行作为领口收口固定保留；意外针由 nb+2E 吸收
        m.add(nb + 2 * E == w2 + A - 2 * nsh * ksh)
        nf_used = m.new_int_var(0, n_slots, "nf_used")
        m.add(nf_used == sum(slot_on))
        m.add(2 * nf_used <= depth - 1)

        # 逐行与原指令比较（仅未织行，含可能落在末行的领缘事件）
        orig_edge = _orig_edge_map(sol)
        for lr in range(max(2, tail_first), arm_rows + 1):
            sel = [(k, m.new_bool_var(f"edgeat_{k}_{lr}")) for k in var_idx]
            for k, xb in sel:
                m.add(slot_row[k] == lr).only_enforce_if(xb)
                m.add(slot_row[k] != lr).only_enforce_if(xb.Not())
            if sel:
                m.add(sum(xb for _, xb in sel) <= 1)
            amt_at = m.new_int_var(0, 4, f"edgeamt_{lr}")
            for k, xb in sel:
                m.add(amt_at == slot_amt[k]).only_enforce_if(xb)
            nots = [xb.Not() for _, xb in sel]
            if nots:
                m.add(amt_at == 0).only_enforce_if(nots)
            else:
                m.add(amt_at == 0)
            old = orig_edge.get(lr, 0)
            dev_terms.append(_abs_expr(m, amt_at, old, f"edge_{lr}", ub=8))
            chg_flags.append(_ne_expr(m, amt_at, old, f"edge_{lr}"))

        answer = _finalize(m, chg_flags, dev_terms)
        if answer["status"] != "ok":
            answer["pre_conflicts"] = pre
            return answer
        edge_map = {lr: a for lr, a in edge_pref}
        for k in var_idx:
            if answer["solver"].value(slot_on[k]):
                edge_map[answer["solver"].value(slot_row[k])] = \
                    answer["solver"].value(slot_amt[k])
        answer.update({
            "fn_depth": _v(answer["solver"], depth),
            "nb": _v(answer["solver"], nb) if fx["center_row"] is None else nb,
            "edge_map": edge_map,
            "nf_used": answer["solver"].value(nf_used),
        })
        return answer

    # ---------------- 后片 ----------------
    back_bind = w2 + A - 2 * nsh * ksh
    if back_bind < 0:
        pre.append({"field": "*", "size": sol["name"],
                    "reason": f"意外增减针合计 {A:+d} 后，后领中心须平收 {back_bind} 针（为负），"
                              "肩宽与肩阶梯固定时无可行收口"})
    if fx["center_row"] is not None:
        bnd = arm_rows - fx["center_row"] + 1
        if fx["center_amt"] != back_bind:
            pre.append({"field": "*", "size": sol["name"],
                        "reason": f"后领中心已平收 {fx['center_amt']} 针，但按意外针合计 {A:+d} "
                                  f"与肩阶梯收针，守恒要求平收 {back_bind} 针；已织行不可改写"})
        bn_c = int(targets["bn_depth"] * rp)
        rsc = max(1, int(round(100.0 / rp)))
        if abs(bnd - bn_c) * rsc > tol100:
            pre.append({"field": "bn_depth", "size": sol["name"],
                        "reason": f"已织后领深 {bnd} 行（{bnd / rp:.2f} cm）超出后领深容差"
                                  f"（目标 {targets['bn_depth']:.1f} cm），已织行不可改写"})
        if Q > 1 and bnd % Q != 0:
            pre.append({"field": "seam_phase_lock", "size": sol["name"],
                        "reason": f"已织后领深 {bnd} 行不是 Q={Q} 的倍数，无法恢复片内花样连续"})
    else:
        lo, hi, _ = _band_for(targets["bn_depth"], rp, tol100, True)
        bn_center = int(targets["bn_depth"] * rp)
        bnd = m.new_int_var(max(1, lo), min(2 * nsh, hi, 8), "bnd")
        _add_err_bound(m, bnd, bn_center, r_scale, tol100, "bn_depth")
        if Q > 1 and "phase" not in relax:
            m.add_modulo_equality(0, bnd, Q)
        dev_terms.append(_abs_expr(m, back_bind - 0 + 0 * bnd,
                                  sol["w2"] - 2 * nsh * ksh, "backamt", ub=400))
        moved = m.new_bool_var("chg_bnd_row")
        model_if_ne(m, bnd, sol["bnd"], moved)
        chg_flags += [moved, moved]

    answer = _finalize(m, chg_flags, dev_terms)
    if answer["status"] != "ok":
        answer["pre_conflicts"] = pre
        return answer
    answer.update({
        "bnd": _v(answer["solver"], bnd),
        "back_center_bind": back_bind,
    })
    return answer


def _on_expr(v):
    return v if not isinstance(v, int) else 1


def _add_err_bound(model, var, center: int, scale: int, tol100: int, name: str):
    """与 solver 同口径的精确误差界：|var−center|·scale ≤ tol（1/100cm）。"""
    d = model.new_int_var(-100000, 100000, f"{name}_rd")
    model.add(d == var - center)
    e = model.new_int_var(0, 100000, f"{name}_re")
    model.add_abs_equality(e, d)
    model.add(e * scale <= tol100)


def model_if_ne(m, a, b: int, boolvar):
    m.add(a == b).only_enforce_if(boolvar.Not())
    m.add(a != b).only_enforce_if(boolvar)


def _check_fixed_neck(sol, targets, sp, rp, tol100, P, Q, pre, depth, nb, fx, front):
    r_scale = max(1, int(round(100.0 / rp)))
    s_scale = max(1, int(round(100.0 / sp)))
    neck_c = int(targets["neck"] * sp)
    if abs(nb - neck_c) * s_scale > tol100:
        pre.append({"field": "neck", "size": sol["name"],
                    "reason": f"已织前领中心平收 {nb} 针（{nb / sp:.2f} cm）超出领口宽容差"
                              f"（目标 {targets['neck']:.1f} cm，容差 {tol100 / 100:.1f}），已织行不可改写"})
    if nb % 2 or (P > 1 and nb % P):
        pre.append({"field": "p_horizontal", "size": sol["name"],
                    "reason": f"已织前领平收 {nb} 针不满足偶数 / P={P} 模数"})
    fn_c = int(targets["fn_depth"] * rp)
    if abs(depth - fn_c) * r_scale > tol100:
        pre.append({"field": "fn_depth", "size": sol["name"],
                    "reason": f"已织前领深 {depth} 行（{depth / rp:.2f} cm）超出前领深容差"
                              f"（目标 {targets['fn_depth']:.1f} cm），已织行不可改写"})
    if Q > 1 and depth % Q != 0:
        pre.append({"field": "seam_phase_lock", "size": sol["name"],
                    "reason": f"已织前领深 {depth} 行不是 Q={Q} 的倍数，无法恢复片内花样连续"})
    if 2 * len(fx["edge_prefix"]) > depth - 1:
        pre.append({"field": "fn_depth", "size": sol["name"],
                    "reason": "已登记的领缘收针事件数 ×2 超过前领深允许的排布行数"})


# ============================================================
# CP-SAT 重排：袖片
# ============================================================

def _solve_sleeve(fx, relax, sol, targets, P, Q, sp, rp, s_scale, r_scale,
                  tol100, n_pairs0, phase_lock, seams):
    cr, inc_rows, cap_rows = sol["cuff_rib_rows"], sol["inc_rows"], sol["cap_rows"]
    cuff_on, cap_top = sol["cuff_on"], sol["cap_top"]
    A = fx["acc_total"]
    ncap_p = len(fx["cap_rows"])
    pre: list[dict[str, str]] = []

    if A % 2 == 1:
        pre.append({"field": "*", "size": sol["name"],
                    "reason": f"袖片意外增减针合计 {A:+d} 为奇数；袖肥/袖口/山顶均为偶数针，"
                              "袖山成对收针无法补偿奇数差值"})

    m = cp_model.CpModel()
    dev_terms, chg_flags = [], []

    ulo, uhi, _ = _band_for(targets["upper_arm"] / 2.0, sp, tol100, False)
    upper = m.new_int_var(max(2, ulo), max(uhi, 2), "upper")
    # _band_for 已按 round 给出中心（与 solver 同口径），误差界为精确硬约束
    _add_err_bound(m, upper, int(targets["upper_arm"] / 2.0 * sp), s_scale, tol100, "upper_arm")
    m.add_modulo_equality(0, upper, 2)
    if P > 1 and "mod" not in relax:
        m.add_modulo_equality(0, upper, P)
    T = m.new_int_var(0, inc_rows, "total_inc")
    m.add(2 * T == upper - cuff_on)
    m.add(2 * T <= inc_rows)
    Tcap = m.new_int_var(1, 200, "total_cap")
    m.add(2 * Tcap == upper - cap_top + A)

    # ---- 袖下加针行位（仅未织袖下行可重排；相邻行不得同时加针） ----
    pref_locals = {g - cr for g in fx["inc_globals"]}
    all_inc: dict[int, Any] = {lr: 1 for lr in pref_locals}
    tail_first_inc = (fx["inc_globals"][-1] - cr + 1) if fx["inc_globals"] else 0
    orig_inc_locals = {off + 1 for off in spread_even(sol["total_inc"], inc_rows)}
    for lr in range(1, inc_rows + 1):
        if lr in pref_locals:
            continue
        v = m.new_bool_var(f"tinc_{lr}")
        all_inc[lr] = v
        old = 1 if lr in orig_inc_locals else 0
        chg_flags.append(_ne_expr(m, v, old, f"inc_{lr}"))
        dev_terms.append(_abs_expr(m, v, old, f"inc_{lr}", ub=2))
    for lr in sorted(all_inc):
        if lr + 1 in all_inc:
            m.add(all_inc[lr] + all_inc[lr + 1] <= 1)
    m.add(sum(all_inc.values()) == T)

    # ---- 袖山收针槽位 ----
    ncap = m.new_int_var(ncap_p, NCAP_MAX, "ncap_used")
    m.add(ncap <= cap_rows - 1)
    if "seam" not in relax:
        m.add(1 + ncap >= n_pairs0)
    cap_vars, cap_pres = [], []
    for k in range(NCAP_MAX):
        # cap_rows 以局部行（1 基）为键；槽位 k 对应局部行 k+1
        if (k + 1) in fx["cap_rows"]:
            cap_vars.append(fx["cap_rows"][k + 1])
            cap_pres.append(1)
            continue
        hi = 12 if k == 0 else 6
        active = m.new_bool_var(f"capb{k}")
        amt = m.new_int_var(0, hi, f"cape{k}")
        m.add(amt == 0).only_enforce_if(active.Not())
        m.add(amt >= (2 if k == 0 else 1)).only_enforce_if(active)
        m.add(ncap >= k + 1).only_enforce_if(active)
        m.add(ncap <= k).only_enforce_if(active.Not())
        cap_vars.append(amt)
        cap_pres.append(active)
        old = (sol["cap_first"] if k == 0 else
               (sol["cap_events"][k - 1] if k - 1 < len(sol["cap_events"]) else 0))
        chg_flags.append(_ne_expr(m, amt, old, f"cap_{k}"))
        dev_terms.append(_abs_expr(m, amt, old, f"cap_{k}", ub=12))
    m.add(cap_vars[0] >= cap_vars[1])
    for k in range(NCAP_MAX - 1):
        m.add(cap_vars[k] >= cap_vars[k + 1])
    m.add(sum(cap_vars) == Tcap)

    # ---- 接缝相位：新增袖山事件 j（绝对行 inc_end+j+1）须与袖窿对应事件同相 mod Q ----
    if phase_lock and Q > 1 and "phase" not in relax:
        body_base = sol["hem_rib_rows"] + sol["side_rows"]
        sleeve_base = cr + inc_rows
        nah0, nsh0, arm0 = sol["nah_used"], sol["nsh"], sol["arm_rows"]
        n_body_evt = 1 + nah0 + nsh0
        for j in range(NCAP_MAX + 1):
            if j >= n_body_evt or j < ncap_p:
                continue  # 超出袖窿事件总数则不配对；前缀槽位（j=0..ncap_p-1）行位未变
            ra = sleeve_base + j + 1
            if j <= nah0:
                rb = body_base + j + 1
            else:
                t = j - nah0 - 1
                rb = body_base + arm0 - 2 * nsh0 + 2 * t + 1
            if (ra - rb) % Q != 0:
                # 第 j 对（槽位 j）启用时会错位 → 禁止该槽位
                m.add(cap_pres[j] == 0)

    # ---- 周长吃势（与 compiler 完全同一折线口径，±4cm） ----
    if "seam" not in relax:
        seg_tbl = _seg_table(s_scale, r_scale)
        terms = []
        for k in range(NCAP_MAX):
            amt, pres = cap_vars[k], cap_pres[k]
            if isinstance(pres, int) and pres == 1:
                terms.append(seg_tbl[(amt, 1)])
            else:
                seg = m.new_int_var(0, seg_tbl[(12, 1)] + 1, f"capsgt{k}")
                seg_val = _seg_var(m, seg_tbl, amt, 1, f"capseg{k}")
                m.add(seg == seg_val).only_enforce_if(pres)
                m.add(seg == 0).only_enforce_if(pres.Not())
                terms.append(seg)
        cap_side = m.new_int_var(0, 1_000_000, "cap_side_len")
        m.add(cap_side == sum(terms) + r_scale * (cap_rows - ncap))
        arm_side_100 = _body_arm_side_100(sol, s_scale, r_scale)
        ease = m.new_int_var(-10000, 10000, "ease_s")
        m.add(ease == 2 * cap_side + s_scale * cap_top - 2 * arm_side_100)
        m.add(ease >= -400)
        m.add(ease <= 400)

    answer = _finalize(m, chg_flags, dev_terms)
    if answer["status"] != "ok":
        answer["pre_conflicts"] = pre
        return answer
    solver = answer["solver"]
    inc_map = {lr: 1 for lr in pref_locals}
    for lr in range(1, inc_rows + 1):
        if lr in pref_locals:
            continue
        if solver.value(all_inc[lr]):
            inc_map[lr] = 1
    cap = [_v(solver, v) for v in cap_vars][:solver.value(ncap)]
    answer.update({
        "upper": solver.value(upper),
        "total_inc": solver.value(T),
        "inc_map": inc_map,
        "ncap_used": solver.value(ncap),
        "cap_first": cap[0],
        "cap_events": cap[1:],
    })
    return answer


# ============================================================
# 求解与取值
# ============================================================

def _finalize(m, chg_flags, dev_terms) -> dict[str, Any]:
    changed = m.new_int_var(0, 300, "n_changed_rows")
    m.add(changed == sum(chg_flags))
    deviation = m.new_int_var(0, 20_000, "stitch_deviation")
    m.add(deviation == sum(dev_terms))
    m.minimize(W_CHANGED * changed + deviation)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = REVISION_SOLVER_TIME
    solver.parameters.num_search_workers = 4
    status = solver.solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "infeasible"}
    return {"status": "ok", "solver": solver,
            "changed_rows": solver.value(changed),
            "deviation": solver.value(deviation),
            "optimal": status == cp_model.OPTIMAL,
            "wall_time_s": round(solver.wall_time, 3)}


def _v(solver, x):
    return int(solver.value(x)) if not isinstance(x, int) else x


# ============================================================
# 成稿：修订行表 + 校验
# ============================================================

def _build_rows(sol, piece, orig_rows, fx, ans) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prefix_rows = max(int(r) for r in fx["reg_by_row"])
    reg = fx["reg_by_row"]
    rows: list[dict[str, Any]] = []

    if piece == "sleeve":
        cr, inc_rows, cap_rows = sol["cuff_rib_rows"], sol["inc_rows"], sol["cap_rows"]
        inc_end = cr + inc_rows
        inc_map = ans["inc_map"]
        cap_amts = [ans["cap_first"]] + ans["cap_events"]
        width = sol["cuff_on"]
        for r in range(1, cr + inc_rows + cap_rows + 1):
            section = "rib" if r <= cr else ("inc" if r <= inc_end else "cap")
            events, inc, cap, top = [], 0, 0, 0
            if r <= prefix_rows:
                d = reg[r]
                inc = d.get("inc_per_side", 0)
                cap = d.get("cap_dec_per_side", 0)
                if inc:
                    events.append(_ev_sleeve_inc())
                if cap:
                    events.append(_ev_cap(cap))
                width += 2 * inc - 2 * cap + d.get("accidental", 0)
            else:
                if section == "inc" and inc_map.get(r - cr):
                    inc = 1
                    width += 2
                    events.append(_ev_sleeve_inc())
                elif section == "cap":
                    lr = r - inc_end
                    if lr <= len(cap_amts):
                        cap = cap_amts[lr - 1]
                        width -= 2 * cap
                        events.append(_ev_cap(cap))
                    if r == inc_end + cap_rows:
                        top = width
                        events.append(_ev_top(top))
                        width = 0
            rows.append({"row": r, "section": section,
                         "width_after": width, "live_stitches": width,
                         "cap_dec_per_side": cap, "inc_per_side": inc,
                         "top_bind": top, "events": events})
        return rows, {"upper": ans["upper"], "total_inc": ans["total_inc"],
                      "ncap_used": ans["ncap_used"], "cap_first": ans["cap_first"],
                      "cap_events": ans["cap_events"]}

    # 大身
    hr, side_rows, arm_rows = sol["hem_rib_rows"], sol["side_rows"], sol["arm_rows"]
    side_end = hr + side_rows
    nsh, ksh = sol["nsh"], sol["ksh"]
    sh_start = arm_rows - 2 * nsh + 1
    shoulder_rows = {sh_start + 2 * k: ksh for k in range(nsh)}
    orig_amts = [sol["arm_first"]] + sol["arm_events"]
    orig_curve = {k + 1: a for k, a in enumerate(orig_amts)}
    orig_side_inc = {r["row"] for r in orig_rows if r["inc_per_side"]}
    if piece == "front":
        bind_local = arm_rows - ans["fn_depth"] + 1
        edge_map, center_amt = ans["edge_map"], ans["nb"]
    else:
        bind_local = arm_rows - ans["bnd"] + 1
        edge_map, center_amt = {}, ans["back_center_bind"]
    total = hr + side_rows + arm_rows
    width = sol["cast_on"]
    for r in range(1, total + 1):
        section = "rib" if r <= hr else ("side" if r <= side_end else "arm")
        events = []
        arm = inc = shb = nc = ne = nfinal = 0
        if r <= prefix_rows:
            d = reg[r]
            inc = d.get("inc_per_side", 0)
            arm = d.get("arm_dec_per_side", 0)
            shb = d.get("shoulder_bind_per_side", 0)
            nc = d.get("neck_center_bind", 0)
            ne = d.get("neck_edge_per_side", 0)
            if inc: events.append(_ev_side_inc())
            if arm: events.append(_ev_arm(arm))
            if shb: events.append(_ev_shoulder(shb))
            if nc: events.append(_ev_neck_center(nc, piece))
            if ne: events.append(_ev_neck_edge(ne))
            width += 2 * inc - 2 * arm - 2 * shb - nc - 2 * ne + d.get("accidental", 0)
        else:
            local = r - side_end
            if section == "side" and r in orig_side_inc:
                inc = 1
                width += 2
                events.append(_ev_side_inc())
            if section == "arm":
                # 袖窿曲线：行位/针数固定（前后片共用、与袖片缝合）；已织段若登记有
                # 偏差则保持实况（成稿校验的“与另一衣片一致”会拦下不可修复的情形）
                if local in orig_curve and local not in fx["curve_rows"]:
                    arm = orig_curve[local]
                    width -= 2 * arm
                    events.append(_ev_arm(arm))
                elif local in fx["curve_rows"]:
                    arm = fx["curve_rows"][local]
                    width -= 2 * arm
                    events.append(_ev_arm(arm))
                if local in shoulder_rows:
                    shb = shoulder_rows[local]
                    width -= 2 * shb
                    events.append(_ev_shoulder(shb))
                if local == bind_local:
                    nc = center_amt
                    width -= nc
                    events.append(_ev_neck_center(nc, piece))
                if local in edge_map:
                    ne = edge_map[local]
                    width -= 2 * ne
                    events.append(_ev_neck_edge(ne))
                if r == total and width > 0:
                    nfinal = width
                    events.append(_ev_neck_final(nfinal, piece))
                    width = 0
                elif r == total and width < 0:
                    events.append(f"⚠️ 收口异常（剩余 {width} 针）")
        rows.append({"row": r, "section": section,
                     "width_after": width, "live_stitches": width,
                     "arm_dec_per_side": arm, "inc_per_side": inc,
                     "shoulder_bind_per_side": shb,
                     "neck_center_bind": nc, "neck_edge_per_side": ne,
                     "neck_final_bind": nfinal, "events": events})
    if piece == "front":
        patch = {"nah_used": sol["nah_used"], "arm_first": sol["arm_first"],
                 "arm_events": list(sol["arm_events"]),
                 "nb": ans["nb"], "fn_depth": ans["fn_depth"],
                 "nf_used": ans["nf_used"],
                 "fn_events": [edge_map[k] for k in sorted(edge_map)]}
    else:
        patch = {"nah_used": sol["nah_used"], "arm_first": sol["arm_first"],
                 "arm_events": list(sol["arm_events"]), "bnd": ans["bnd"],
                 "back_center_bind": ans["back_center_bind"]}
    return rows, patch


def _row_brief(r) -> dict[str, Any]:
    return {"row": r["row"], "section": r["section"],
            "live_stitches": r["live_stitches"], "events": list(r.get("events", []))}


def _prefix_view(orig_rows, fx, prefix_rows) -> list[dict[str, Any]]:
    reg = fx["reg_by_row"]
    return [{"row": r, "section": orig_rows[r - 1]["section"],
             "live_stitches": reg[r].get("live"),
             "accidental": reg[r].get("accidental", 0),
             "events": list(orig_rows[r - 1].get("events", []))}
            for r in range(1, prefix_rows + 1)]


def _verify(compiled, sol, piece, rows, sol_patch, P, Q, phase_lock, seams,
            tol_cm, sp, rp, targets, n_pairs0, fx) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    prefix_rows = max(int(r) for r in fx["reg_by_row"])
    A = fx["acc_total"]
    pn = PIECE_NAME[piece]

    # ---- 针数守恒（含意外针）+ 逐行非负 ----
    neg = [r["row"] for r in rows if r["live_stitches"] < 0]
    if piece == "sleeve":
        inc = sum(r["inc_per_side"] for r in rows) * 2
        cap = sum(r["cap_dec_per_side"] for r in rows) * 2
        top = sum(r["top_bind"] for r in rows)
        expect = sol["cuff_on"] + inc + A - cap - top
        detail = (f"袖口 {sol['cuff_on']} + 袖下放 {inc} + 意外 {A:+d} − 袖山收 {cap} "
                  f"− 山顶 {top} = {expect}")
    else:
        inc = sum(r["inc_per_side"] for r in rows) * 2
        arm = sum(r["arm_dec_per_side"] for r in rows) * 2
        sh = sum(r["shoulder_bind_per_side"] for r in rows) * 2
        center = sum(r["neck_center_bind"] for r in rows)
        edge = sum(r["neck_edge_per_side"] for r in rows) * 2
        final = sum(r["neck_final_bind"] for r in rows)
        expect = sol["cast_on"] + inc + A - arm - sh - center - edge - final
        detail = (f"起针 {sol['cast_on']} + 侧放 {inc} + 意外 {A:+d} − 袖窿 {arm} "
                  f"− 肩阶梯 {sh} − 领中心 {center} − 领缘 {edge} − 领口收口 {final} = {expect}")
    checks.append({"id": "rev_conservation", "name": f"{pn}针数守恒（含意外增减针）",
                   "ok": expect == 0 and rows[-1]["live_stitches"] == 0 and not neg,
                   "detail": detail + ("" if not neg else f"；第 {neg} 行活针为负")})

    # ---- 前缀锁定 ----
    checks.append({"id": "rev_prefix_locked", "name": "已织前缀锁定（已完成行不改写）",
                   "ok": True,
                   "detail": f"前 {prefix_rows} 行完全沿用逐行登记（活针/塑形操作/意外针），"
                             f"仅第 {prefix_rows + 1} 行起的未织行被重排；总行数 {len(rows)} 不变"})

    # ---- 容差（只核对可能改变的指标，其余宽度/行数锚点固定；误差口径与 solver 一致） ----
    tol100 = int(round(tol_cm * 100))
    s_unit, r_unit = (max(1, int(round(100.0 / sp))),
                      max(1, int(round(100.0 / rp))))
    if piece == "front":
        nb = sol_patch["nb"]
        err = abs(nb - int(targets["neck"] * sp)) * s_unit
        checks.append({"id": "rev_tol_neck", "name": "领口宽容差",
                       "ok": err <= tol100,
                       "detail": f"前领中心平收 {nb} 针 → {nb / sp:.2f} cm（目标 "
                                 f"{targets['neck']:.1f}，Δ {err / 100:.2f} cm）"})
        d = sol_patch["fn_depth"]
        err = abs(d - int(targets["fn_depth"] * rp)) * r_unit
        checks.append({"id": "rev_tol_fn", "name": "前领深容差",
                       "ok": err <= tol100,
                       "detail": f"前领深 {d} 行 → {d / rp:.2f} cm（目标 "
                                 f"{targets['fn_depth']:.1f}，Δ {err / 100:.2f} cm）"})
    if piece == "back":
        d = sol_patch["bnd"]
        err = abs(d - int(targets["bn_depth"] * rp)) * r_unit
        checks.append({"id": "rev_tol_bn", "name": "后领深容差",
                       "ok": err <= tol100,
                       "detail": f"后领深 {d} 行 → {d / rp:.2f} cm（目标 "
                                 f"{targets['bn_depth']:.1f}，Δ {err / 100:.2f} cm）"})
    if piece == "sleeve":
        up = sol_patch["upper"]
        err = abs(up - int(targets["upper_arm"] / 2 * sp)) * s_unit
        checks.append({"id": "rev_tol_upper", "name": "袖肥容差",
                       "ok": err <= tol100,
                       "detail": f"袖肥 {up} 针/整片 → 围度 {2 * up / sp:.2f} cm（目标 "
                                 f"{targets['upper_arm']:.1f}，Δ {err / 100:.2f} cm）"})

    # ---- 接缝 ----
    def _wrap(rs):
        return {"rows": rs}
    front_rows_w = _wrap(rows if piece == "front" else compiled["pieces"]["front"]["rows"])
    back_rows_w = _wrap(rows if piece == "back" else compiled["pieces"]["back"]["rows"])
    sleeve_rows_w = _wrap(rows if piece == "sleeve" else compiled["pieces"]["sleeve"]["rows"])

    if piece in ("front", "back"):
        other = back_rows_w["rows"] if piece == "front" else front_rows_w["rows"]
        same = all(r["inc_per_side"] == o["inc_per_side"] for r, o in zip(rows, other))
        checks.append({"id": "rev_seam_side", "name": "侧缝逐行匹配（与未修订衣片）",
                       "ok": len(rows) == len(other) and same,
                       "detail": "总行数与每一加针行均一致" if same
                                 else "行数或侧区加针行与另一衣片不一致"})
        # 袖窿曲线必须与另一（未修订）衣片逐行同量
        rev_arm = {r["row"]: r["arm_dec_per_side"] for r in rows
                   if r["section"] == "arm" and r["arm_dec_per_side"]}
        oth_arm = {r["row"]: r["arm_dec_per_side"] for r in other
                   if r["section"] == "arm" and r["arm_dec_per_side"]}
        arm_ok = rev_arm == oth_arm
        checks.append({"id": "rev_armhole_consistency",
                       "name": "袖窿曲线与另一衣片一致（同一条袖山要缝合前后两片）",
                       "ok": arm_ok,
                       "detail": "每行曲线收针量/行位均一致" if arm_ok else
                       f"差异行：{sorted(set(rev_arm) ^ set(oth_arm))[:8]}（已织行不可改写，"
                       "单片纠偏无法修复，请整体重新求解）"})
        checks.append({"id": "rev_seam_shoulder", "name": "肩缝针/行匹配",
                       "ok": True,
                       "detail": f"肩阶梯 {sol['nsh']} 次 × {sol['ksh']} 针/侧，行位针数未改动"})

    sol_rev = dict(sol)
    sol_rev.update({k: v for k, v in sol_patch.items() if k != "back_center_bind"})
    per = _seam_perimeters(sol_rev, None, sp, rp)[0]
    checks.append({"id": "rev_seam_perimeter",
                   "name": "袖山/袖窿曲线缝合（周长+吃势，±4cm）",
                   "ok": per["ok"], "detail": per["detail"]})

    n_arm = sum(1 for r in front_rows_w["rows"] if r["section"] == "arm" and r["arm_dec_per_side"]) \
        + sum(1 for r in front_rows_w["rows"] if r["shoulder_bind_per_side"])
    n_cap = sum(1 for r in sleeve_rows_w["rows"] if r["cap_dec_per_side"])
    n_pairs = min(n_arm, n_cap)
    checks.append({"id": "rev_seam_pairs", "name": "沿缝塑形事件配对数（不少于原方案）",
                   "ok": n_pairs >= n_pairs0,
                   "detail": f"修订后 {n_pairs} 对（原方案 {n_pairs0} 对）；袖窿侧 {n_arm} 事件 / "
                             f"袖山侧 {n_cap} 事件（多余事件为信息项）"})

    if Q > 1 and phase_lock:
        for ck in _seam_phase_checks(sol_rev, front_rows_w, back_rows_w, sleeve_rows_w,
                                    seams, Q):
            ck = dict(ck)
            ck["id"] = "rev_" + ck["id"]
            ck["name"] = "修订后 " + ck["name"]
            checks.append(ck)
    elif Q > 1:
        qvals = [sol["hem_rib_rows"], sol["side_rows"], sol["arm_rows"],
                 sol["cap_rows"], sol["cuff_rib_rows"], sol["inc_rows"]]
        if piece == "front":
            qvals.append(sol_patch["fn_depth"])
        if piece == "back":
            qvals.append(sol_patch["bnd"])
        okq = all(v % Q == 0 for v in qvals)
        checks.append({"id": "rev_phase_q", "name": f"纵向花样循环 Q={Q} 连续",
                       "ok": okq,
                       "detail": "各段行数（含修订领深）均为 Q 的倍数；接缝事件绝对行号未移动"
                       if okq else "存在非 Q 倍数段"})

    for label, v in ([("领口宽", sol_patch["nb"])] if piece == "front" else
                     ([("袖肥", sol_patch["upper"])] if piece == "sleeve" else [])):
        okp = v % 2 == 0 and (P <= 1 or v % P == 0)
        checks.append({"id": f"rev_phase_p_{label}", "name": f"{label}偶数 / P={P} 模数",
                       "ok": okp, "detail": f"{label} = {v} 针"})
    return checks


# ============================================================
# 无解诊断
# ============================================================

def _diagnose(fx, piece, pre_conflicts, sol, targets, P, Q, sp, rp,
              s_scale, r_scale, tol100, n_pairs0, phase_lock, seams):
    conflicts = list(pre_conflicts)
    for g in ("tol", "seam", "phase", "mod"):
        if piece == "sleeve":
            ans = _solve_sleeve(fx, {g}, sol, targets, P, Q, sp, rp, s_scale, r_scale,
                                tol100, n_pairs0, phase_lock, seams)
        else:
            ans = _solve_body(fx, piece, {g}, sol, targets, P, Q, sp, rp, s_scale,
                              r_scale, tol100, n_pairs0, phase_lock, seams)
        if ans.get("status") == "ok":
            conflicts.append({"field": {"tol": "tolerance_cm", "seam": "*",
                                        "phase": "seam_phase_lock",
                                        "mod": "p_horizontal"}[g],
                              "size": sol["name"], "reason": _relax_reason(g, piece)})

    # 确定性的算术诊断（放松也无法解除时给出具体冲突）
    A = fx["acc_total"]
    tol_units = max(1, int(math.ceil(tol100 / 100 * sp - 1e-9)))
    if piece == "front":
        neck_lo = max(0, int(round(targets["neck"] * sp)) - tol_units)
        if sol["w2"] + A < neck_lo:
            conflicts.append({"field": "neck", "size": sol["name"],
                              "reason": f"意外增减针合计 {A:+d}：肩点处可分配给领口的总宽仅 "
                                        f"{sol['w2'] + A} 针，低于领口宽容差下限 {neck_lo} 针"})
        if fx["center_row"] is not None and 2 * len(fx["edge_prefix"]) > \
                sol["arm_rows"] - fx["center_row"]:
            conflicts.append({"field": "fn_depth", "size": sol["name"],
                              "reason": "中心平收之后剩余行数不足以容纳已登记的领缘收针"
                                        "（每事件至少 2 行），已织行不可改写"})
    if piece == "back":
        need = sol["w2"] + A - 2 * sol["nsh"] * sol["ksh"]
        if need < 0:
            conflicts.append({"field": "*", "size": sol["name"],
                              "reason": f"意外针合计 {A:+d}：后领收口针数 {need} 为负"})
    if piece == "sleeve":
        up_lo = max(0, int(round(targets["upper_arm"] / 2 * sp)) - tol_units)
        need_each = (up_lo - sol["cap_top"] + A) / 2
        max_cap = 12 + (min(NCAP_MAX, sol["cap_rows"] - 1) - 1) * 6
        if need_each > max_cap:
            conflicts.append({"field": "upper_arm", "size": sol["name"],
                              "reason": f"袖肥容差下限要求每侧袖山收 {need_each:.1f} 针，超过 "
                                        f"{sol['cap_rows']} 袖山行内单调收针上限 {max_cap} 针"})
        if 2 * max(0, (up_lo - sol["cuff_on"]) // 2) > sol["inc_rows"]:
            conflicts.append({"field": "upper_arm", "size": sol["name"],
                              "reason": "袖肥容差下限所需的袖下加针次数超过袖下行数允许的排布"})
    if not conflicts:
        conflicts.append({"field": "*", "size": sol["name"],
                          "reason": "已织前缀与容差/接缝/花样相位/模数等全部硬约束同时冲突；"
                                    "请核对登记行，或调整参数后对项目重新整体求解。"})
    return conflicts


def _relax_reason(g: str, piece: str) -> str:
    if g == "tol":
        return ("放宽容差后可行：意外增减针经未织行补偿后，改动指标（领口宽/前领深/后领深/袖肥）"
                "超出当前容差带")
    if g == "seam":
        return "放宽接缝匹配（事件配对数/周长吃势 ±4cm）后可行：未织行余量无法同时维持接缝"
    if g == "phase":
        return ("关闭接缝相位锁定 / 放宽 Q 模数后可行：前缀行位锁定后，未织行的领深等段行数"
                "无法对齐 Q 倍数，或新增袖山事件无法与袖窿同相")
    return "放宽花样水平循环 P / 偶数模数后可行：补偿后的关键宽度无法对齐模数"
