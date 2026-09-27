"""FastAPI 入口：项目 CRUD、求解/编译、静态前端。"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .compiler import compile_size
from .default_data import FIELD_LABELS, default_project
from .revision import plan_revision
from .solver import solve_project

FRONTEND_DIR = os.environ.get("KNIT_FRONTEND_DIR", "/app/frontend")

app = FastAPI(title="针织版型分码与行程编译台", version="1.0.0")


# ---------------- 模型 ----------------

class Gauge(BaseModel):
    stitches_per_10cm: float = Field(gt=0)
    rows_per_10cm: float = Field(gt=0)


class SizeInput(BaseModel):
    name: str
    targets: dict[str, float]
    hem_rib: float = 5.0
    cuff_rib: float = 5.0


class ProjectPayload(BaseModel):
    name: str = "未命名项目"
    gauge: Gauge
    ease: dict[str, Any] = Field(default_factory=dict)
    tolerance_cm: float = 0.6
    rib: dict[str, str] = Field(default_factory=lambda: {"hem_rib_type": "1x1", "cuff_rib_type": "1x1"})
    pattern: dict[str, Any] = Field(default_factory=lambda: {"p_horizontal": 1, "q_vertical": 1, "seam_phase_lock": False})
    seams: list[dict[str, Any]] = Field(default_factory=list)
    sizes: list[SizeInput]


class ProjectIn(BaseModel):
    payload: ProjectPayload


class SolveIn(BaseModel):
    persist: bool = True


class RevisionIn(BaseModel):
    result_id: int
    size: str
    piece: str
    prefix_rows: int
    rows_register: list[dict[str, Any]] = Field(alias="register")
    note: str | None = None

    model_config = {"populate_by_name": True}


class RevisionNote(BaseModel):
    note: str | None = None


# ---------------- 生命周期 ----------------

@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/default")
def get_default() -> dict[str, Any]:
    return {"payload": default_project(), "labels": FIELD_LABELS}


@app.get("/api/labels")
def labels() -> dict[str, str]:
    return FIELD_LABELS


# ---------------- 项目 ----------------

@app.get("/api/projects")
def projects_list() -> list[dict[str, Any]]:
    return db.list_projects()


@app.post("/api/projects")
def projects_create(body: ProjectIn) -> dict[str, Any]:
    return db.create_project(body.payload.model_dump())


@app.get("/api/projects/{pid}")
def project_get(pid: int) -> dict[str, Any]:
    p = db.get_project(pid)
    if p is None:
        raise HTTPException(404, "项目不存在")
    return p


@app.put("/api/projects/{pid}")
def project_update(pid: int, body: ProjectIn) -> dict[str, Any]:
    if not db.update_project(pid, body.payload.model_dump()):
        raise HTTPException(404, "项目不存在")
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def project_delete(pid: int) -> dict[str, bool]:
    if not db.delete_project(pid):
        raise HTTPException(404, "项目不存在")
    return {"ok": True}


# ---------------- 求解 + 编译 ----------------

def _run(project: dict[str, Any]) -> dict[str, Any]:
    raw = solve_project(project)
    if raw.get("status") != "ok":
        return raw
    gauge = raw["gauge"]
    pattern = raw["pattern"]
    tol = float(project.get("tolerance_cm", 0.6))
    seams = project.get("seams") or None
    compiled = []
    for sol, sz in zip(raw["sizes"], project["sizes"]):
        c = compile_size(
            sol, gauge, pattern,
            targets=sz["targets"],
            hem_rib_cm=float(sz.get("hem_rib", 5.0)),
            cuff_rib_cm=float(sz.get("cuff_rib", 5.0)),
            tolerance_cm=tol,
            seams=seams,
        )
        compiled.append(c)
    raw["compiled"] = compiled
    return raw


@app.post("/api/projects/{pid}/solve")
def project_solve(pid: int, body: SolveIn) -> dict[str, Any]:
    p = db.get_project(pid)
    if p is None:
        raise HTTPException(404, "项目不存在")
    result = _run(p["payload"])
    result_id = None
    if body.persist:
        result_id = db.save_result(pid, result)
    result["result_id"] = result_id
    return result


@app.post("/api/solve")
def solve_direct(body: ProjectPayload) -> dict[str, Any]:
    """不入库的即时求解（编辑器实时检查用）。"""
    return _run(body.model_dump())


@app.get("/api/projects/{pid}/result")
def project_result(pid: int) -> dict[str, Any]:
    r = db.latest_result(pid)
    if r is None:
        raise HTTPException(404, "尚无求解结果")
    return r


@app.get("/api/projects/{pid}/results")
def project_results(pid: int) -> list[dict[str, Any]]:
    """项目的全部历史求解结果（重新求解只追加，旧结果与其纠偏修订不被覆盖）。"""
    if db.get_project(pid) is None:
        raise HTTPException(404, "项目不存在")
    return db.list_results(pid)


@app.get("/api/projects/{pid}/results/{rid}")
def project_result_one(pid: int, rid: int) -> dict[str, Any]:
    r = db.get_result(pid, rid)
    if r is None:
        raise HTTPException(404, "求解结果不存在")
    return r


# ---------------- 试织纠偏：余下行程重排 ----------------

@app.post("/api/projects/{pid}/revisions")
def project_revision_create(pid: int, body: RevisionIn) -> dict[str, Any]:
    """以已保存求解结果为基准，锁定已织前缀，仅重排指定衣片的未织塑形事件。

    只有可行修订才持久化（绑定 result_id）；登记错误返回 422，无解返回 200 +
    status=infeasible（冲突说明），二者都不会改动原求解结果。
    """
    project = db.get_project(pid)
    if project is None:
        raise HTTPException(404, "项目不存在")
    result_row = db.get_result(pid, body.result_id)
    if result_row is None:
        raise HTTPException(404, "求解结果不存在或不属于该项目")
    outcome = plan_revision(
        result_row["payload"], project["payload"],
        body.size, body.piece, body.prefix_rows, body.rows_register,
    )
    if outcome["status"] == "invalid":
        raise HTTPException(422, detail={"message": outcome["message"],
                                         "row_errors": outcome["row_errors"]})
    if outcome["status"] == "infeasible":
        # 无解：不持久化、不覆盖原指令，仅回传冲突约束说明
        return {"status": "infeasible", "message": outcome["message"],
                "conflicts": outcome["conflicts"]}
    record = {
        "size": outcome["size"], "piece": outcome["piece"],
        "prefix_rows": outcome["prefix_rows"],
        "objective": outcome["objective"],
        "accidental_total": outcome["accidental_total"],
        "prefix": outcome["prefix"], "tail_old": outcome["tail_old"],
        "tail_new": outcome["tail_new"], "revised_rows": outcome["revised_rows"],
        "sol_patch": outcome["sol_patch"], "checks": outcome["checks"],
        "note": body.note,
    }
    rev_id = db.save_revision(pid, body.result_id, record, note=body.note)
    return {"status": "ok", "revision_id": rev_id, "revision": record}


@app.get("/api/projects/{pid}/revisions")
def project_revision_list(pid: int, result_id: int | None = None) -> list[dict[str, Any]]:
    if db.get_project(pid) is None:
        raise HTTPException(404, "项目不存在")
    return db.list_revisions(pid, result_id)


@app.get("/api/projects/{pid}/revisions/{rev_id}")
def project_revision_get(pid: int, rev_id: int) -> dict[str, Any]:
    r = db.get_revision(pid, rev_id)
    if r is None:
        raise HTTPException(404, "修订不存在")
    return r


@app.delete("/api/projects/{pid}/revisions/{rev_id}")
def project_revision_delete(pid: int, rev_id: int) -> dict[str, bool]:
    if not db.delete_revision(pid, rev_id):
        raise HTTPException(404, "修订不存在")
    return {"ok": True}


# ---------------- 静态前端 ----------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
