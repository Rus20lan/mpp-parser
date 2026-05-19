"""
MPP Parser Microservice — JPype + MPXJ (Maven JARs)
"""

import os
import glob
import tempfile
import traceback
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import jpype
import jpype.imports

# Start JVM with all JARs from /app/lib/
if not jpype.isJVMStarted():
    jars = glob.glob("/app/lib/*.jar")
    jpype.startJVM(classpath=jars, convertStrings=True)

from net.sf.mpxj.reader import UniversalProjectReader  # type: ignore
from java.io import FileInputStream  # type: ignore

app = FastAPI(
    title="MPP Parser for R&D Change Log",
    version="1.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Helpers ---

def _jdate(val):
    """Java date -> ISO string or None."""
    if val is None:
        return None
    try:
        ts = val.getTime() / 1000.0
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        try:
            return str(val)[:10]
        except Exception:
            return None


def _s(val):
    """Java/Python value -> stripped string or None."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _f(val):
    """Value -> float or None."""
    if val is None:
        return None
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return None


def _i(val):
    """Value -> int or None."""
    if val is None:
        return None
    try:
        return int(str(val).split(".")[0])
    except (ValueError, TypeError):
        return None


def _dur(val):
    """MPXJ Duration -> string like '5.0d'."""
    if val is None:
        return None
    try:
        amount = val.getDuration()
        units = str(val.getUnits())
        umap = {
            "DAYS": "d", "HOURS": "h", "WEEKS": "w",
            "MONTHS": "mo", "MINUTES": "min", "YEARS": "y",
            "ELAPSED_DAYS": "ed", "ELAPSED_HOURS": "eh",
        }
        return str(amount) + umap.get(units, units)
    except Exception:
        return str(val)


def _preds(task):
    """Extract predecessor list."""
    out = []
    try:
        rels = task.getPredecessors()
        if rels is None:
            return out
        for rel in rels:
            pt = rel.getTargetTask()
            out.append({
                "task_uid": _i(pt.getUniqueID()) if pt else None,
                "type": _s(rel.getType()),
                "lag": _dur(rel.getLag()),
            })
    except Exception:
        pass
    return out


def _resources(task):
    """Extract resource assignments."""
    out = []
    try:
        for ra in task.getResourceAssignments():
            res = ra.getResource()
            out.append({
                "resource_name": _s(res.getName()) if res else None,
                "resource_uid": _i(res.getUniqueID()) if res else None,
                "work": _dur(ra.getWork()),
                "units": _f(ra.getUnits()),
                "cost": _f(ra.getCost()),
            })
    except Exception:
        pass
    return out


def _notes(task):
    try:
        n = task.getNotes()
        return _s(n)
    except Exception:
        return None


def _is_summary(task):
    try:
        ch = task.getChildTasks()
        return ch is not None and ch.size() > 0
    except Exception:
        return False


def _is_milestone(task):
    try:
        return bool(task.getMilestone())
    except Exception:
        return False


def _parent_uid(task):
    try:
        p = task.getParentTask()
        if p is not None:
            return _i(p.getUniqueID())
    except Exception:
        pass
    return None


def _custom_texts(task, count=5):
    """Read Text1..TextN custom fields."""
    result = {}
    try:
        from net.sf.mpxj import TaskField  # type: ignore
        for i in range(1, count + 1):
            try:
                field = TaskField.valueOf("TEXT" + str(i))
                val = task.getCachedValue(field)
                if val is not None:
                    result["text" + str(i)] = str(val).strip()
            except Exception:
                pass
    except Exception:
        pass
    return result


# --- Core parse ---

def parse_mpp(file_path):
    reader = UniversalProjectReader()
    fis = FileInputStream(file_path)
    project = reader.read(fis)
    fis.close()

    if project is None:
        raise ValueError("MPXJ returned None. File may be corrupt.")

    props = project.getProjectProperties()
    project_name = _s(props.getProjectTitle()) or _s(props.getName()) or "Untitled"
    status_date = _jdate(props.getStatusDate())
    start_date = _jdate(props.getStartDate())
    finish_date = _jdate(props.getFinishDate())

    tasks_out = []
    for task in project.getTasks():
        uid = _i(task.getUniqueID())
        if uid is None or uid == 0:
            continue

        summary = _is_summary(task)
        milestone = _is_milestone(task)

        t = {
            "unique_id": uid,
            "id": _i(task.getID()),
            "wbs": _s(task.getWBS()),
            "name": _s(task.getName()),
            "outline_level": _i(task.getOutlineLevel()),
            "parent_uid": _parent_uid(task),
            "is_summary": summary,
            "is_milestone": milestone,
            "start": _jdate(task.getStart()),
            "finish": _jdate(task.getFinish()),
            "baseline_start": _jdate(task.getBaselineStart()),
            "baseline_finish": _jdate(task.getBaselineFinish()),
            "duration": _dur(task.getDuration()),
            "percent_complete": _f(task.getPercentageComplete()),
            "actual_start": _jdate(task.getActualStart()),
            "actual_finish": _jdate(task.getActualFinish()),
            "cost": _f(task.getCost()),
            "baseline_cost": _f(task.getBaselineCost()),
            "work": _dur(task.getWork()),
            "baseline_work": _dur(task.getBaselineWork()),
            "predecessors": _preds(task),
            "resources": _resources(task),
            "notes": _notes(task),
            "custom_fields": _custom_texts(task),
        }
        tasks_out.append(t)

    resources_out = []
    try:
        for res in project.getResources():
            ruid = _i(res.getUniqueID())
            if ruid is None or ruid == 0:
                continue
            resources_out.append({
                "unique_id": ruid,
                "name": _s(res.getName()),
                "type": _s(res.getType()),
                "cost": _f(res.getCost()),
            })
    except Exception:
        pass

    total = len(tasks_out)
    sm = sum(1 for t in tasks_out if t["is_summary"])
    ml = sum(1 for t in tasks_out if t["is_milestone"])
    bl = sum(1 for t in tasks_out if t["baseline_finish"] is not None)

    return {
        "project_name": project_name,
        "status_date": status_date,
        "start_date": start_date,
        "finish_date": finish_date,
        "parse_timestamp": datetime.utcnow().isoformat() + "Z",
        "summary": {
            "total_tasks": total,
            "summary_tasks": sm,
            "milestones": ml,
            "leaf_tasks": total - sm - ml,
            "with_baseline": bl,
            "total_resources": len(resources_out),
        },
        "tasks": tasks_out,
        "resources": resources_out,
    }


# --- Endpoints ---

@app.get("/")
async def root():
    return {"service": "MPP Parser", "version": "1.2.0", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "jvm": jpype.isJVMStarted()}


@app.post("/parse")
async def parse_endpoint(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file provided.")
    allowed = (".mpp", ".mpt", ".mpx", ".xml", ".xer", ".pmxml")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(400, "Unsupported: " + ext)

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
        raise HTTPException(400, "Unsupported: " + ext)

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
                "Name": t["name"] or "",
                "Level": custom.get("text1", ""),
                "Outline": t["outline_level"],
                "Parent_UID": t["parent_uid"] or "",
                "Summary": "Y" if t["is_summary"] else "N",
                "Milestone": "Y" if t["is_milestone"] else "N",
                "Start": t["start"] or "",
                "Finish": t["finish"] or "",
                "Baseline_Start": t["baseline_start"] or "",
                "Baseline_Finish": t["baseline_finish"] or "",
                "Duration": t["duration"] or "",
                "Pct_Complete": t["percent_complete"] or 0,
                "Actual_Start": t["actual_start"] or "",
                "Actual_Finish": t["actual_finish"] or "",
                "Cost": t["cost"] or "",
                "Baseline_Cost": t["baseline_cost"] or "",
                "Work": t["work"] or "",
                "Baseline_Work": t["baseline_work"] or "",
                "Predecessors": pred_str,
                "Resources": res_str,
                "Notes": t["notes"] or "",
            }
            rows.append(row)

        columns = list(rows[0].keys()) if rows else []

        return JSONResponse(content={
            "project_name": full["project_name"],
            "status_date": full["status_date"],
            "start_date": full["start_date"],
            "finish_date": full["finish_date"],
            "parse_timestamp": full["parse_timestamp"],
            "summary": full["summary"],
            "columns": columns,
            "rows": rows,
        })
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
