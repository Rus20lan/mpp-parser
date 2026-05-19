"""
MPP Parser Microservice
=======================
FastAPI service that accepts .mpp files (Microsoft Project)
and returns structured JSON with full task hierarchy.

Uses MPXJ library via JPype for reliable .mpp parsing.
Designed for deployment on Hugging Face Spaces (Docker SDK, port 7860).

Target consumer: Claude.ai SKILL for R&D Change Log agent.
"""

import os
import io
import json
import tempfile
import traceback
from datetime import datetime, date
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import jpype
import jpype.imports

# ---------------------------------------------------------------------------
# JVM startup (once per process)
# ---------------------------------------------------------------------------
MPXJ_JAR = "/app/lib/mpxj.jar"

if not jpype.isJVMStarted():
    jpype.startJVM(
        classpath=[MPXJ_JAR],
        convertStrings=True,          # auto-convert Java strings to Python str
    )

from net.sf.mpxj.reader import UniversalProjectReader   # type: ignore
from java.io import FileInputStream                       # type: ignore

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="MPP Parser for R&D Change Log",
    version="1.0.0",
    description=(
        "Parses Microsoft Project .mpp files and returns JSON "
        "with tasks, dates, resources, costs, and hierarchy."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _java_date_to_iso(jdate) -> Optional[str]:
    """Convert a Java Date / LocalDateTime to ISO string, or None."""
    if jdate is None:
        return None
    try:
        # MPXJ 13+ returns java.time.LocalDateTime for some fields
        return str(jdate)[:10]   # YYYY-MM-DD
    except Exception:
        try:
            ts = jdate.getTime() / 1000.0
            return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            return str(jdate)


def _safe_str(val) -> Optional[str]:
    if val is None:
        return None
    return str(val).strip() or None


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return None


def _duration_to_str(dur) -> Optional[str]:
    """MPXJ Duration → human-readable string like '5d', '12h'."""
    if dur is None:
        return None
    try:
        amount = dur.getDuration()
        units = str(dur.getUnits())
        unit_map = {
            "DAYS": "d", "HOURS": "h", "WEEKS": "w",
            "MONTHS": "mo", "MINUTES": "min", "YEARS": "y",
            "ELAPSED_DAYS": "ed", "ELAPSED_HOURS": "eh",
        }
        suffix = unit_map.get(units, units)
        return f"{amount}{suffix}"
    except Exception:
        return str(dur)


def _extract_predecessors(task) -> list[dict]:
    """Return list of {task_uid, type, lag} for each predecessor."""
    preds = []
    try:
        relations = task.getPredecessors()
        if relations is None:
            return preds
        for rel in relations:
            pred_task = rel.getTargetTask()
            preds.append({
                "task_uid": _safe_int(pred_task.getUniqueID()) if pred_task else None,
                "type": _safe_str(rel.getType()),
                "lag": _duration_to_str(rel.getLag()),
            })
    except Exception:
        pass
    return preds


def _extract_resources(task) -> list[dict]:
    """Return resource assignments for a task."""
    assignments = []
    try:
        for ra in task.getResourceAssignments():
            res = ra.getResource()
            assignments.append({
                "resource_name": _safe_str(res.getName()) if res else None,
                "resource_uid": _safe_int(res.getUniqueID()) if res else None,
                "work": _duration_to_str(ra.getWork()),
                "units": _safe_float(ra.getUnits()),
                "cost": _safe_float(ra.getCost()),
            })
    except Exception:
        pass
    return assignments


def _get_outline_level(task) -> Optional[int]:
    try:
        return _safe_int(task.getOutlineLevel())
    except Exception:
        return None


def _get_wbs(task) -> Optional[str]:
    try:
        return _safe_str(task.getWBS())
    except Exception:
        return None


def _is_milestone(task) -> bool:
    try:
        return bool(task.getMilestone())
    except Exception:
        return False


def _is_summary(task) -> bool:
    try:
        # MPXJ: summary tasks have child tasks
        children = task.getChildTasks()
        return children is not None and children.size() > 0
    except Exception:
        return False


def _get_parent_uid(task) -> Optional[int]:
    try:
        parent = task.getParentTask()
        if parent is not None:
            uid = parent.getUniqueID()
            return _safe_int(uid) if uid else None
    except Exception:
        pass
    return None


def _extract_notes(task) -> Optional[str]:
    try:
        notes = task.getNotes()
        if notes:
            return str(notes).strip() or None
    except Exception:
        pass
    return None


# Custom / extended fields commonly used in R&D project exports
# MS Project "Text1"…"Text30" often carry domain-specific data.
def _extract_custom_text_fields(task, field_count: int = 10) -> dict:
    """Extract Text1…TextN custom fields (used for Control_Level etc.)."""
    result = {}
    try:
        from net.sf.mpxj import TaskField  # type: ignore
        for i in range(1, field_count + 1):
            field_name = f"TEXT{i}"
            try:
                field = TaskField.valueOf(field_name)
                val = task.getCachedValue(field)
                if val is not None:
                    result[f"text{i}"] = str(val).strip()
            except Exception:
                pass
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Core parse logic
# ---------------------------------------------------------------------------

def parse_mpp(file_path: str) -> dict:
    """
    Parse an .mpp file and return a dict with:
      - project_name
      - report_date
      - tasks: list of task dicts
      - resources: list of project-level resources
      - summary: basic stats
    """
    reader = UniversalProjectReader()
    fis = FileInputStream(file_path)
    project = reader.read(fis)
    fis.close()

    if project is None:
        raise ValueError("MPXJ returned None — file may be corrupt or unsupported format.")

    # Project-level metadata
    props = project.getProjectProperties()
    project_name = _safe_str(props.getProjectTitle()) or _safe_str(props.getName()) or "Untitled"
    status_date = _java_date_to_iso(props.getStatusDate())
    start_date = _java_date_to_iso(props.getStartDate())
    finish_date = _java_date_to_iso(props.getFinishDate())
    baseline_date = _java_date_to_iso(props.getBaselineDate()) if hasattr(props, 'getBaselineDate') else None

    # --- Tasks ---
    tasks_out = []
    all_tasks = project.getTasks()

    for task in all_tasks:
        uid = _safe_int(task.getUniqueID())
        if uid is None or uid == 0:
            continue  # skip project summary (UID=0)

        task_dict = {
            # Identity
            "unique_id":       uid,
            "id":              _safe_int(task.getID()),
            "wbs":             _get_wbs(task),
            "name":            _safe_str(task.getName()),
            "outline_level":   _get_outline_level(task),
            "parent_uid":      _get_parent_uid(task),
            "is_summary":      _is_summary(task),
            "is_milestone":    _is_milestone(task),

            # Dates — current forecast
            "start":           _java_date_to_iso(task.getStart()),
            "finish":          _java_date_to_iso(task.getFinish()),

            # Dates — baseline
            "baseline_start":  _java_date_to_iso(task.getBaselineStart()),
            "baseline_finish": _java_date_to_iso(task.getBaselineFinish()),

            # Duration & progress
            "duration":        _duration_to_str(task.getDuration()),
            "percent_complete": _safe_float(task.getPercentageComplete()),
            "actual_start":    _java_date_to_iso(task.getActualStart()),
            "actual_finish":   _java_date_to_iso(task.getActualFinish()),

            # Cost & work
            "cost":            _safe_float(task.getCost()),
            "baseline_cost":   _safe_float(task.getBaselineCost()),
            "work":            _duration_to_str(task.getWork()),
            "baseline_work":   _duration_to_str(task.getBaselineWork()),

            # Dependencies
            "predecessors":    _extract_predecessors(task),

            # Resources assigned
            "resources":       _extract_resources(task),

            # Notes
            "notes":           _extract_notes(task),

            # Custom text fields (Text1…Text10)
            "custom_fields":   _extract_custom_text_fields(task),
        }
        tasks_out.append(task_dict)

    # --- Project-level resources list ---
    resources_out = []
    try:
        for res in project.getResources():
            res_uid = _safe_int(res.getUniqueID())
            if res_uid is None or res_uid == 0:
                continue
            resources_out.append({
                "unique_id":   res_uid,
                "name":        _safe_str(res.getName()),
                "type":        _safe_str(res.getType()),
                "cost":        _safe_float(res.getCost()),
                "email":       _safe_str(res.getEmailAddress()) if hasattr(res, 'getEmailAddress') else None,
            })
    except Exception:
        pass

    # --- Summary ---
    summary = {
        "total_tasks":      len(tasks_out),
        "summary_tasks":    sum(1 for t in tasks_out if t["is_summary"]),
        "milestones":       sum(1 for t in tasks_out if t["is_milestone"]),
        "leaf_tasks":       sum(1 for t in tasks_out if not t["is_summary"] and not t["is_milestone"]),
        "with_baseline":    sum(1 for t in tasks_out if t["baseline_finish"] is not None),
        "total_resources":  len(resources_out),
    }

    return {
        "project_name":  project_name,
        "status_date":   status_date,
        "start_date":    start_date,
        "finish_date":   finish_date,
        "baseline_date": baseline_date,
        "parse_timestamp": datetime.utcnow().isoformat() + "Z",
        "summary":       summary,
        "tasks":         tasks_out,
        "resources":     resources_out,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "service": "MPP Parser for R&D Change Log",
        "version": "1.0.0",
        "status": "running",
        "usage": "POST /parse with file=<your.mpp>",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "jvm": jpype.isJVMStarted()}


@app.post("/parse")
async def parse_file(file: UploadFile = File(...)):
    """
    Accept an .mpp file upload, parse it via MPXJ, return JSON.

    Returns:
        JSON with project metadata, tasks array, resources array, and summary stats.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    # Accept .mpp, .mpt, .mpx, .xml (MPXJ supports several formats)
    allowed_ext = (".mpp", ".mpt", ".mpx", ".xml", ".xer", ".pmxml")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_ext:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(allowed_ext)}"
        )

    # Save upload to temp file (MPXJ needs a file path)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.flush()
        tmp.close()

        result = parse_mpp(tmp.name)
        return JSONResponse(content=result)

    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Parse error: {type(e).__name__}: {str(e)}"
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.post("/parse/compact")
async def parse_file_compact(file: UploadFile = File(...)):
    """
    Same as /parse but returns a flattened table-ready format
    (one dict per task, no nested objects) suitable for direct
    insertion into Google Sheets.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".mpp", ".mpt", ".mpx", ".xml", ".xer", ".pmxml"):
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'.")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.flush()
        tmp.close()

        full = parse_mpp(tmp.name)

        # Flatten tasks for spreadsheet
        rows = []
        for t in full["tasks"]:
            # Predecessors → comma-separated UIDs
            pred_str = ", ".join(
                str(p.get("task_uid", ""))
                for p in (t.get("predecessors") or [])
                if p.get("task_uid") is not None
            )
            # Resources → comma-separated names
            res_str = ", ".join(
                r.get("resource_name", "") or ""
                for r in (t.get("resources") or [])
            )
            # Custom text fields → concatenated
            custom = t.get("custom_fields") or {}
            control_level = custom.get("text1", "")  # Convention: Text1 = Control Level

            rows.append({
                "UID":              t["unique_id"],
                "ID":               t["id"],
                "WBS":              t["wbs"] or "",
                "Название":         t["name"] or "",
                "Уровень":          control_level,
                "Уровень_WBS":      t["outline_level"],
                "Родитель_UID":     t["parent_uid"] or "",
                "Суммарная":        "Да" if t["is_summary"] else "Нет",
                "Веха":             "Да" if t["is_milestone"] else "Нет",
                "Начало":           t["start"] or "",
                "Окончание":        t["finish"] or "",
                "Базовое_начало":   t["baseline_start"] or "",
                "Базовое_окончание": t["baseline_finish"] or "",
                "Длительность":     t["duration"] or "",
                "Процент_выполнения": t["percent_complete"] or 0,
                "Факт_начало":      t["actual_start"] or "",
                "Факт_окончание":   t["actual_finish"] or "",
                "Стоимость":        t["cost"] or "",
                "Базовая_стоимость": t["baseline_cost"] or "",
                "Трудозатраты":     t["work"] or "",
                "Базовые_трудозатраты": t["baseline_work"] or "",
                "Предшественники":  pred_str,
                "Ресурсы":          res_str,
                "Заметки":          t["notes"] or "",
            })

        return JSONResponse(content={
            "project_name":    full["project_name"],
            "status_date":     full["status_date"],
            "start_date":      full["start_date"],
            "finish_date":     full["finish_date"],
            "baseline_date":   full["baseline_date"],
            "parse_timestamp": full["parse_timestamp"],
            "summary":         full["summary"],
            "columns": list(rows[0].keys()) if rows else [],
            "rows":            rows,
        })

    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Parse error: {type(e).__name__}: {str(e)}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
