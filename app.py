"""
MPP Parser Microservice
=======================
FastAPI service that parses .mpp files via the mpxj Python package
and returns structured JSON with full task hierarchy.

Deployed on Hugging Face Spaces (Docker SDK, port 7860).
"""

import os
import tempfile
import traceback
from datetime import datetime, date
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from mpxj import ProjectReader

app = FastAPI(
    title="MPP Parser for R&D Change Log",
    version="1.1.0",
    description="Parses .mpp files and returns JSON with tasks, dates, resources, costs, hierarchy.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _to_iso(val):
    if val is None:
        return None
    try:
        if isinstance(val, (date, datetime)):
            return val.strftime("%Y-%m-%d")
        return str(val)[:10]
    except Exception:
        return str(val)


def _safe_str(val):
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        try:
            return float(str(val))
        except (ValueError, TypeError):
            return None


def _safe_int(val):
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return int(float(str(val)))
        except (ValueError, TypeError):
            return None


def _duration_to_str(dur):
    if dur is None:
        return None
    try:
        amount = dur.duration
        units = str(dur.units)
        unit_map = {
            "DAYS": "d",
            "HOURS": "h",
            "WEEKS": "w",
            "MONTHS": "mo",
            "MINUTES": "min",
            "YEARS": "y",
            "ELAPSED_DAYS": "ed",
            "ELAPSED_HOURS": "eh",
        }
        suffix = unit_map.get(units, units)
        return str(amount) + suffix
    except Exception:
        return str(dur)


def _extract_predecessors(task):
    preds = []
    try:
        relations = task.predecessors
        if not relations:
            return preds
        for rel in relations:
            pred_task = rel.target_task
            preds.append({
                "task_uid": _safe_int(pred_task.unique_id) if pred_task else None,
                "type": _safe_str(rel.type),
                "lag": _duration_to_str(rel.lag),
            })
    except Exception:
        pass
    return preds


def _extract_resources(task):
    assignments = []
    try:
        for ra in task.resource_assignments:
            res = ra.resource
            assignments.append({
                "resource_name": _safe_str(res.name) if res else None,
                "resource_uid": _safe_int(res.unique_id) if res else None,
                "work": _duration_to_str(ra.work),
                "units": _safe_float(ra.units),
                "cost": _safe_float(ra.cost),
            })
    except Exception:
        pass
    return assignments


def _has_children(task):
    try:
        children = task.child_tasks
        return children is not None and len(children) > 0
    except Exception:
        return False


def _is_milestone(task):
    try:
        return bool(task.milestone)
    except Exception:
        return False


def _get_parent_uid(task):
    try:
        parent = task.parent_task
        if parent is not None:
            return _safe_int(parent.unique_id)
    except Exception:
        pass
    return None


def _get_notes(task):
    try:
        notes = task.notes
        if notes:
            s = str(notes).strip()
            return s if s else None
    except Exception:
        pass
    return None


def _get_custom_text(task, field_name):
    try:
        val = getattr(task, field_name, None)
        if val is not None:
            return str(val).strip() or None
    except Exception:
        pass
    return None


def parse_mpp(file_path):
    reader = ProjectReader()
    project = reader.read(file_path)

    if project is None:
        raise ValueError("MPXJ returned None. File may be corrupt or unsupported.")

    props = project.project_properties
    project_name = _safe_str(getattr(props, 'project_title', None))
    if not project_name:
        project_name = _safe_str(getattr(props, 'name', None))
    if not project_name:
        project_name = "Untitled"

    status_date = _to_iso(getattr(props, 'status_date', None))
    start_date = _to_iso(getattr(props, 'start_date', None))
    finish_date = _to_iso(getattr(props, 'finish_date', None))
    baseline_date = _to_iso(getattr(props, 'baseline_date', None))

    tasks_out = []
    for task in project.tasks:
        uid = _safe_int(task.unique_id)
        if uid is None or uid == 0:
            continue

        is_summary = _has_children(task)

        task_dict = {
            "unique_id": uid,
            "id": _safe_int(task.id),
            "wbs": _safe_str(getattr(task, 'wbs', None)),
            "name": _safe_str(task.name),
            "outline_level": _safe_int(getattr(task, 'outline_level', None)),
            "parent_uid": _get_parent_uid(task),
            "is_summary": is_summary,
            "is_milestone": _is_milestone(task),
            "start": _to_iso(task.start),
            "finish": _to_iso(task.finish),
            "baseline_start": _to_iso(getattr(task, 'baseline_start', None)),
            "baseline_finish": _to_iso(getattr(task, 'baseline_finish', None)),
            "duration": _duration_to_str(getattr(task, 'duration', None)),
            "percent_complete": _safe_float(getattr(task, 'percentage_complete', None)),
            "actual_start": _to_iso(getattr(task, 'actual_start', None)),
            "actual_finish": _to_iso(getattr(task, 'actual_finish', None)),
            "cost": _safe_float(getattr(task, 'cost', None)),
            "baseline_cost": _safe_float(getattr(task, 'baseline_cost', None)),
            "work": _duration_to_str(getattr(task, 'work', None)),
            "baseline_work": _duration_to_str(getattr(task, 'baseline_work', None)),
            "predecessors": _extract_predecessors(task),
            "resources": _extract_resources(task),
            "notes": _get_notes(task),
            "custom_fields": {
                "text1": _get_custom_text(task, 'text1'),
                "text2": _get_custom_text(task, 'text2'),
                "text3": _get_custom_text(task, 'text3'),
            },
        }
        tasks_out.append(task_dict)

    resources_out = []
    try:
        for res in project.resources:
            res_uid = _safe_int(res.unique_id)
            if res_uid is None or res_uid == 0:
                continue
            resources_out.append({
                "unique_id": res_uid,
                "name": _safe_str(res.name),
                "type": _safe_str(getattr(res, 'type', None)),
                "cost": _safe_float(getattr(res, 'cost', None)),
                "email": _safe_str(getattr(res, 'email_address', None)),
            })
    except Exception:
        pass

    summary = {
        "total_tasks": len(tasks_out),
        "summary_tasks": sum(1 for t in tasks_out if t["is_summary"]),
        "milestones": sum(1 for t in tasks_out if t["is_milestone"]),
        "leaf_tasks": sum(1 for t in tasks_out if not t["is_summary"] and not t["is_milestone"]),
        "with_baseline": sum(1 for t in tasks_out if t["baseline_finish"] is not None),
        "total_resources": len(resources_out),
    }

    return {
        "project_name": project_name,
        "status_date": status_date,
        "start_date": start_date,
        "finish_date": finish_date,
        "baseline_date": baseline_date,
        "parse_timestamp": datetime.utcnow().isoformat() + "Z",
        "summary": summary,
        "tasks": tasks_out,
        "resources": resources_out,
    }


@app.get("/")
async def root():
    return {
        "service": "MPP Parser for R&D Change Log",
        "version": "1.1.0",
        "status": "running",
        "usage": "POST /parse with file=<your.mpp>",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/parse")
async def parse_endpoint(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file provided.")

    allowed = (".mpp", ".mpt", ".mpx", ".xml", ".xer", ".pmxml")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(400, "Unsupported file type: " + ext)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.flush()
        tmp.close()
        result = parse_mpp(tmp.name)
        return JSONResponse(content=result)
    except ValueError as ve:
        raise HTTPException(422, str(ve))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, "Parse error: " + str(e))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.post("/parse/compact")
async def parse_compact(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file provided.")

    allowed = (".mpp", ".mpt", ".mpx", ".xml", ".xer", ".pmxml")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(400, "Unsupported file type: " + ext)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.flush()
        tmp.close()
        full = parse_mpp(tmp.name)

        rows = []
        for t in full["tasks"]:
            pred_str = ", ".join(
                str(p["task_uid"])
                for p in (t.get("predecessors") or [])
                if p.get("task_uid") is not None
            )
            res_str = ", ".join(
                r.get("resource_name") or ""
                for r in (t.get("resources") or [])
            )
            custom = t.get("custom_fields") or {}

            row = {
                "UID": t["unique_id"],
                "ID": t["id"],
                "WBS": t["wbs"] or "",
                "Название": t["name"] or "",
                "Уровень": custom.get("text1") or "",
                "Уровень_WBS": t["outline_level"],
                "Родитель_UID": t["parent_uid"] or "",
                "Суммарная": "Да" if t["is_summary"] else "Нет",
                "Веха": "Да" if t["is_milestone"] else "Нет",
                "Начало": t["start"] or "",
                "Окончание": t["finish"] or "",
                "Базовое_начало": t["baseline_start"] or "",
                "Базовое_окончание": t["baseline_finish"] or "",
                "Длительность": t["duration"] or "",
                "Процент_выполнения": t["percent_complete"] or 0,
                "Факт_начало": t["actual_start"] or "",
                "Факт_окончание": t["actual_finish"] or "",
                "Стоимость": t["cost"] or "",
                "Базовая_стоимость": t["baseline_cost"] or "",
                "Трудозатраты": t["work"] or "",
                "Базовые_трудозатраты": t["baseline_work"] or "",
                "Предшественники": pred_str,
                "Ресурсы": res_str,
                "Заметки": t["notes"] or "",
            }
            rows.append(row)

        columns = list(rows[0].keys()) if rows else []

        result = {
            "project_name": full["project_name"],
            "status_date": full["status_date"],
            "start_date": full["start_date"],
            "finish_date": full["finish_date"],
            "baseline_date": full["baseline_date"],
            "parse_timestamp": full["parse_timestamp"],
            "summary": full["summary"],
            "columns": columns,
            "rows": rows,
        }
        return JSONResponse(content=result)
    except ValueError as ve:
        raise HTTPException(422, str(ve))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, "Parse error: " + str(e))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
