"""
MPP Parser Microservice — JPype + MPXJ (Maven JARs)

Fixes:
- Works with MPXJ 15.x package: org.mpxj
- Keeps fallback for legacy package: net.sf.mpxj
- Does not import Java classes before JVM startup
- Uses jpype.JClass instead of Python-style Java imports for better reliability
"""

import os
import glob
import tempfile
import traceback
from datetime import datetime

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import jpype
import jpype.imports


MPXJ_LIB_DIR = os.getenv("MPXJ_LIB_DIR", "/app/lib")


app = FastAPI(
    title="MPP Parser for R&D Change Log",
    version="1.3.2",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- JVM / MPXJ bootstrap ---

def _find_jars():
    """
    Find MPXJ jars and dependencies.

    If /app/lib is empty, JPype can start, but MPXJ classes will not be visible.
    """
    jars = glob.glob(os.path.join(MPXJ_LIB_DIR, "*.jar"))
    return sorted(jars)


def start_jvm():
    """
    Start JVM once with all MPXJ jars in classpath.
    """
    if jpype.isJVMStarted():
        return

    jars = _find_jars()

    if not jars:
        raise RuntimeError(
            f"MPXJ jars not found in {MPXJ_LIB_DIR}. "
            "Check Dockerfile Maven dependency download step."
        )

    mpxj_jars = [
        j for j in jars
        if "mpxj" in os.path.basename(j).lower()
    ]

    if not mpxj_jars:
        raise RuntimeError(
            f"MPXJ main jar not found in {MPXJ_LIB_DIR}. "
            f"Found jars: {jars}"
        )

    jpype.startJVM(classpath=jars, convertStrings=True)


def get_mpxj_classes():
    """
    Load Java classes after JVM startup.

    MPXJ 15.x uses:
        org.mpxj.reader.UniversalProjectReader

    Older MPXJ versions used:
        net.sf.mpxj.reader.UniversalProjectReader
    """
    start_jvm()

    errors = []

    try:
        UniversalProjectReader = jpype.JClass(
            "org.mpxj.reader.UniversalProjectReader"
        )
        FileInputStream = jpype.JClass("java.io.FileInputStream")

        return UniversalProjectReader, FileInputStream

    except Exception as exc:
        errors.append(f"org.mpxj failed: {exc}")

    try:
        UniversalProjectReader = jpype.JClass(
            "net.sf.mpxj.reader.UniversalProjectReader"
        )
        FileInputStream = jpype.JClass("java.io.FileInputStream")

        return UniversalProjectReader, FileInputStream

    except Exception as exc:
        errors.append(f"net.sf.mpxj failed: {exc}")

    jars = _find_jars()
    mpxj_jars = [
        os.path.basename(j)
        for j in jars
        if "mpxj" in os.path.basename(j).lower()
    ]

    raise RuntimeError(
        "JVM is running, but MPXJ UniversalProjectReader is not visible. "
        "Tried both 'org.mpxj.reader.UniversalProjectReader' and "
        "'net.sf.mpxj.reader.UniversalProjectReader'. "
        f"jar_count={len(jars)}, mpxj_jars={mpxj_jars}, errors={errors}"
    )


def get_task_field_class():
    """
    Load TaskField class for custom fields.

    MPXJ 15.x:
        org.mpxj.TaskField

    Legacy MPXJ:
        net.sf.mpxj.TaskField
    """
    start_jvm()

    try:
        return jpype.JClass("org.mpxj.TaskField")
    except Exception:
        pass

    try:
        return jpype.JClass("net.sf.mpxj.TaskField")
    except Exception:
        return None


# --- Helpers ---

def _safe_str(val):
    """
    Safely convert any Java/Python value to string,
    handling UUID and other non-String types.
    """
    if val is None:
        return None

    try:
        return str(val)
    except Exception:
        pass

    try:
        return val.toString()
    except Exception:
        pass

    try:
        return repr(val)
    except Exception:
        return None


def _jdate(val):
    """
    Java date -> ISO string or None.
    """
    if val is None:
        return None

    try:
        ts = val.getTime() / 1000.0
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        try:
            s = _safe_str(val)
            return s[:10] if s else None
        except Exception:
            return None


def _s(val):
    """
    Java/Python value -> stripped string or None.
    """
    if val is None:
        return None

    try:
        s = _safe_str(val)

        if s is None:
            return None

        s = s.strip()
        return s if s else None

    except Exception:
        return None


def _f(val):
    """
    Value -> float or None.
    """
    if val is None:
        return None

    try:
        return float(_safe_str(val))
    except (ValueError, TypeError):
        return None


def _i(val):
    """
    Value -> int or None.
    """
    if val is None:
        return None

    try:
        return int(_safe_str(val).split(".")[0])
    except (ValueError, TypeError, AttributeError):
        return None


def _dur(val):
    """
    MPXJ Duration -> string like '5.0d'.
    """
    if val is None:
        return None

    try:
        amount = val.getDuration()
        units = _safe_str(val.getUnits())

        umap = {
            "DAYS": "d",
            "HOURS": "h",
            "WEEKS": "w",
            "MONTHS": "mo",
            "MINUTES": "min",
            "YEARS": "y",
            "ELAPSED_DAYS": "ed",
            "ELAPSED_HOURS": "eh",
        }

        return str(amount) + umap.get(units, units)

    except Exception:
        return _safe_str(val)


def _preds(task):
    """
    Extract predecessor list.
    """
    out = []

    try:
        rels = task.getPredecessors()

        if rels is None:
            return out

        for rel in rels:
            try:
                pt = rel.getTargetTask()

                out.append({
                    "task_uid": _i(pt.getUniqueID()) if pt else None,
                    "type": _s(rel.getType()),
                    "lag": _dur(rel.getLag()),
                })

            except Exception:
                pass

    except Exception:
        pass

    return out


def _resources(task):
    """
    Extract resource assignments.
    """
    out = []

    try:
        for ra in task.getResourceAssignments():
            try:
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
    """
    Read Text1..TextN custom fields.
    """
    result = {}

    try:
        TaskField = get_task_field_class()

        if TaskField is None:
            return result

        for i in range(1, count + 1):
            try:
                field = TaskField.valueOf("TEXT" + str(i))
                val = task.getCachedValue(field)

                if val is not None:
                    s = _safe_str(val)

                    if s:
                        result["text" + str(i)] = s.strip()

            except Exception:
                pass

    except Exception:
        pass

    return result


# --- Core parse ---

def parse_mpp(file_path):
    UniversalProjectReader, FileInputStream = get_mpxj_classes()

    reader = UniversalProjectReader()
    fis = FileInputStream(file_path)

    try:
        project = reader.read(fis)
    finally:
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

        try:
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

        except Exception as e:
            print(f"WARN: skipping task UID={uid}: {e}")
            continue

    resources_out = []

    try:
        for res in project.getResources():
            try:
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
    return {
        "service": "MPP Parser",
        "version": "1.3.2",
        "status": "running",
    }


@app.get("/health")
async def health():
    jars = _find_jars()

    mpxj_jars = [
        os.path.basename(j)
        for j in jars
        if "mpxj" in os.path.basename(j).lower()
    ]

    return {
        "status": "ok",
        "jvm": jpype.isJVMStarted(),
        "lib_dir": MPXJ_LIB_DIR,
        "jar_count": len(jars),
        "mpxj_jars": mpxj_jars,
    }


@app.get("/health/deep")
async def health_deep():
    try:
        UniversalProjectReader, FileInputStream = get_mpxj_classes()
        TaskField = get_task_field_class()

        return {
            "status": "ok",
            "jvm": jpype.isJVMStarted(),
            "mpxj_import": True,
            "universal_reader": str(UniversalProjectReader),
            "file_input_stream": str(FileInputStream),
            "task_field": str(TaskField) if TaskField is not None else None,
            "jar_count": len(_find_jars()),
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "jvm": jpype.isJVMStarted(),
                "mpxj_import": False,
                "error": str(e),
                "jar_count": len(_find_jars()),
            },
        )


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
