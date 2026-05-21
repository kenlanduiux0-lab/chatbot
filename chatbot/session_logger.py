import json
import os
import uuid
from collections import Counter
from datetime import datetime

# Use absolute path based on this file's location
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR  = os.path.join(BASE_DIR, "logs")


def _path(session_id: str) -> str:
    return os.path.join(LOGS_DIR, f"{session_id}.json")


def _read(session_id: str) -> dict:
    with open(_path(session_id), "r", encoding="utf-8") as f:
        return json.load(f)


def _write(session_id: str, log: dict):
    with open(_path(session_id), "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def start_session() -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    session_id = str(uuid.uuid4())[:8]
    log = {
        "session_id":      session_id,
        "started_at":      datetime.now().isoformat(),
        "ended_at":        None,
        "resolved":        False,
        "escalated":       False,
        "fallback_used":   False,
        "fallback_count":  0,
        "issue_type":      None,
        "problem_summary": None,
        "resolution_note": None,
        "doc_gaps":        [],
        "messages":        [],
    }
    _write(session_id, log)
    return session_id


def log_message(session_id: str, role: str, content: str):
    log = _read(session_id)
    log["messages"].append({
        "role":      role,
        "content":   content,
        "timestamp": datetime.now().isoformat(),
    })
    _write(session_id, log)


def log_doc_gap(session_id: str, question: str):
    log = _read(session_id)
    if question not in log["doc_gaps"]:
        log["doc_gaps"].append(question)
    _write(session_id, log)


def set_problem_summary(session_id: str, summary: str):
    log = _read(session_id)
    log["problem_summary"] = summary
    _write(session_id, log)


def mark_fallback(session_id: str):
    log = _read(session_id)
    log["fallback_used"] = True
    log["fallback_count"] = log.get("fallback_count", 0) + 1
    _write(session_id, log)


def mark_resolved(session_id: str, resolved: bool, issue_type: str = None, resolution_note: str = None):
    log = _read(session_id)
    log["resolved"]  = resolved
    log["ended_at"]  = datetime.now().isoformat()
    if issue_type:
        log["issue_type"] = issue_type
    if resolution_note:
        log["resolution_note"] = resolution_note
    _write(session_id, log)


def inject_dev_note(session_id: str, note: str):
    log = _read(session_id)
    log["messages"].append({
        "role":      "dev_note",
        "content":   note,
        "timestamp": datetime.now().isoformat(),
    })
    _write(session_id, log)


def check_and_escalate(session_id: str) -> bool:
    log = _read(session_id)
    if log.get("escalated"):
        return False
    if log.get("fallback_count", 0) >= 2:
        log["escalated"]    = True
        log["escalated_at"] = datetime.now().isoformat()
        _write(session_id, log)
        _write_escalation_file(session_id, log)
        return True
    return False


def _write_escalation_file(session_id: str, log: dict):
    alert = {
        "session_id":      session_id,
        "escalated_at":    log.get("escalated_at"),
        "fallback_count":  log.get("fallback_count", 0),
        "problem_summary": log.get("problem_summary"),
        "doc_gaps":        log.get("doc_gaps", []),
        "message_count":   len(log.get("messages", [])),
    }
    path = os.path.join(LOGS_DIR, f"ESCALATION_{session_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(alert, f, indent=2)


def get_all_sessions() -> list:
    sessions = []
    if not os.path.exists(LOGS_DIR):
        return []
    for filename in sorted(os.listdir(LOGS_DIR), reverse=True):
        if filename.endswith(".json") and not filename.startswith("ESCALATION"):
            with open(os.path.join(LOGS_DIR, filename), encoding="utf-8") as f:
                sessions.append(json.load(f))
    return sessions


def get_escalations() -> list:
    result = []
    if not os.path.exists(LOGS_DIR):
        return []
    for filename in sorted(os.listdir(LOGS_DIR), reverse=True):
        if filename.startswith("ESCALATION_") and filename.endswith(".json"):
            with open(os.path.join(LOGS_DIR, filename), encoding="utf-8") as f:
                result.append(json.load(f))
    return result


def get_analytics() -> dict:
    sessions = get_all_sessions()
    total         = len(sessions)
    resolved      = sum(1 for s in sessions if s.get("resolved"))
    escalated     = sum(1 for s in sessions if s.get("escalated"))
    fallback_used = sum(1 for s in sessions if s.get("fallback_used"))

    issue_types = [s["issue_type"] for s in sessions if s.get("issue_type")]
    type_counts = Counter(issue_types)
    top_issues  = [{"type": k, "count": v} for k, v in type_counts.most_common(5)]
    recurring   = [i for i in top_issues if i["count"] >= 3]

    all_gaps = []
    for s in sessions:
        for gap in s.get("doc_gaps", []):
            all_gaps.append({"question": gap, "session_id": s["session_id"]})

    return {
        "total_sessions":   total,
        "resolved":         resolved,
        "unresolved":       total - resolved,
        "escalated":        escalated,
        "fallback_used":    fallback_used,
        "resolution_rate":  round(resolved / total * 100, 1) if total else 0,
        "top_issue_types":  top_issues,
        "recurring_issues": recurring,
        "doc_gaps":         all_gaps[:20],
    }
