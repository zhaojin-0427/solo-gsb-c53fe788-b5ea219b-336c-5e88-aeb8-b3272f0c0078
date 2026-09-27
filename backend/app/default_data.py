"""内置默认项目：女式圆领装袖套头衫，S/M/L 三个尺码。

所有尺寸单位为 cm；横密 = 每 10cm 针数 / 10（针/cm），纵密 = 每 10cm 行数 / 10（行/cm）。
"""
from __future__ import annotations

from typing import Any

# 每个尺码的目标成品尺寸（cm）：
#   围度类（整件量一圈）：bust 胸围 / hem 下摆围 / upper_arm 上臂围 / cuff 袖口围
#       -> 前/后片整片宽 = 围度 / 2；袖片整片宽 = 围度 / 2
#   半幅类（左右肩点之间）：shoulder 肩宽 / neck 领口宽
#   长度类：length 衣长 / armhole 袖窿直深 / sleeve_len 袖下长 / cap_height 袖山高
#           fn_depth 前领深 / bn_depth 后领深
SIZE_TARGETS = {
    "S": {
        "bust": 90.0, "hem": 84.0, "shoulder": 36.0, "neck": 18.0,
        "fn_depth": 7.0, "bn_depth": 2.0, "length": 60.0, "armhole": 19.0,
        "sleeve_len": 40.0, "cap_height": 14.0, "upper_arm": 28.0, "cuff": 18.0,
    },
    "M": {
        "bust": 96.0, "hem": 90.0, "shoulder": 38.0, "neck": 19.0,
        "fn_depth": 7.5, "bn_depth": 2.0, "length": 62.0, "armhole": 20.0,
        "sleeve_len": 41.0, "cap_height": 15.0, "upper_arm": 30.0, "cuff": 19.0,
    },
    "L": {
        "bust": 102.0, "hem": 96.0, "shoulder": 40.0, "neck": 20.0,
        "fn_depth": 8.0, "bn_depth": 2.5, "length": 64.0, "armhole": 21.0,
        "sleeve_len": 42.0, "cap_height": 15.5, "upper_arm": 32.0, "cuff": 20.0,
    },
}

# 各尺码罗纹长度（cm）
RIB = {"S": {"hem_rib": 5.0, "cuff_rib": 5.0},
       "M": {"hem_rib": 5.0, "cuff_rib": 5.0},
       "L": {"hem_rib": 6.0, "cuff_rib": 5.0}}


# 默认缝合关系（a/b 为端点引用，kind 为缝合方式）：
#   side      前片.侧缝 ↔ 后片.侧缝：逐行匹配（前后片共用行结构决策，天然同相）
#   shoulder  前片.肩   ↔ 后片.肩  ：阶梯数×针数一致，阶梯事件一一对应
#   cap_arm   袖片.袖山 ↔ 大身.袖窿：装袖曲线缝合，按周长+吃势匹配，
#                                   相位锁定时对应收针事件绝对行号须同余 mod Q
DEFAULT_SEAMS: list[dict[str, Any]] = [
    {"id": "side", "a": "front.side", "b": "back.side", "kind": "row_match"},
    {"id": "shoulder", "a": "front.shoulder", "b": "back.shoulder", "kind": "stitch_row"},
    {"id": "cap_arm", "a": "sleeve.cap", "b": "body.armhole", "kind": "row_match"},
]


def default_project() -> dict[str, Any]:
    sizes = []
    for name in ("S", "M", "L"):
        t = SIZE_TARGETS[name]
        sizes.append({
            "name": name,
            "targets": dict(t),
            "hem_rib": RIB[name]["hem_rib"],
            "cuff_rib": RIB[name]["cuff_rib"],
        })
    return {
        "name": "女式圆领装袖套头衫（默认样例）",
        "gauge": {
            "stitches_per_10cm": 22.0,   # 横密
            "rows_per_10cm": 30.0,       # 纵密
        },
        "ease": {
            "bust_ease": 8.0,
            "sleeve_ease": 4.0,
            "note": "目标尺寸已按净体 + 松量给出，松量数值仅作记录参考",
        },
        "tolerance_cm": 0.6,
        "rib": {
            "hem_rib_type": "1x1",   # 循环 P=2
            "cuff_rib_type": "1x1",
        },
        "pattern": {
            # 花样循环：水平 P 针、垂直 Q 行（1 表示无循环约束）
            "p_horizontal": 1,
            "q_vertical": 1,
            "seam_phase_lock": False,  # 纵向接缝两侧花样相位是否强制对齐
        },
        "seams": [dict(s) for s in DEFAULT_SEAMS],
        "sizes": sizes,
    }


# 字段的中文释义，供冲突标注与表格表头复用
FIELD_LABELS: dict[str, str] = {
    "bust": "胸围（整片宽）",
    "hem": "下摆宽",
    "shoulder": "肩宽（整片肩点间）",
    "neck": "领口宽",
    "fn_depth": "前领深",
    "bn_depth": "后领深",
    "length": "衣长",
    "armhole": "袖窿深",
    "sleeve_len": "袖长（袖口至腋下）",
    "cap_height": "袖山高",
    "upper_arm": "袖肥",
    "cuff": "袖口宽",
    "hem_rib": "下摆罗纹长",
    "cuff_rib": "袖口罗纹长",
    "stitches_per_10cm": "横向针密",
    "rows_per_10cm": "纵向行密",
    "tolerance_cm": "成品容差",
    "p_horizontal": "花样水平循环",
    "q_vertical": "花样垂直循环",
    "seam_phase_lock": "接缝相位锁定",
    "hem_rib_type": "下摆罗纹类型",
    "cuff_rib_type": "袖口罗纹类型",
}
