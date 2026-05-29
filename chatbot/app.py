import os
import sys
import smtplib
import traceback

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

load_dotenv()

from flask import Flask, request, jsonify, send_from_directory, session as flask_session
from flask_cors import CORS

from pdf_extractor import (
    extract_pdf_to_text,
    get_doc_meta,
    load_doc_text,
)

from chat_handler import (
    get_classifier_response,
    get_step_response,
    get_fallback_response,
    get_error_response,
    get_general_response,
)

from session_logger import (
    start_session,
    log_message,
    log_doc_gap,
    set_problem_summary,
    mark_fallback,
    mark_resolved,
    inject_dev_note,
    check_and_escalate,
    get_all_sessions,
    get_live_sessions,
    get_analytics,
    get_escalations,
)

# ──────────────────────────────────────────────────────────────
# App Setup
# ──────────────────────────────────────────────────────────────

FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
CORS(app, supports_credentials=True, origins=["http://localhost:5000", "http://127.0.0.1:5000"])

# ──────────────────────────────────────────────────────────────
# Runtime session storage
# ──────────────────────────────────────────────────────────────

sessions: dict = {}

# ──────────────────────────────────────────────────────────────
# Email Configuration  (keys match .env exactly)
# ──────────────────────────────────────────────────────────────

DEVELOPER_EMAIL = os.getenv("DEVELOPER_EMAIL", "")
SMTP_SERVER     = os.getenv("SMTP_SERVER", "sandbox.smtp.mailtrap.io")
SMTP_PORT       = int(os.getenv("SMTP_PORT", 2525))
SENDER_EMAIL    = os.getenv("SENDER_EMAIL", "")
SMTP_USERNAME   = os.getenv("MAIL_USERNAME", "")   # matches .env MAIL_USERNAME
SMTP_PASSWORD   = os.getenv("MAIL_PASSWORD", "")   # matches .env MAIL_PASSWORD

if not all([DEVELOPER_EMAIL, SENDER_EMAIL, SMTP_USERNAME, SMTP_PASSWORD]):
    print("⚠️  WARNING: Email not fully configured — escalation emails will NOT be sent.")
    print("   Set DEVELOPER_EMAIL, SENDER_EMAIL, MAIL_USERNAME, MAIL_PASSWORD in .env")

# ──────────────────────────────────────────────────────────────
# Escalation Email
# ──────────────────────────────────────────────────────────────

def send_escalation_email(session_id, session_data):
    if not all([DEVELOPER_EMAIL, SENDER_EMAIL, SMTP_USERNAME, SMTP_PASSWORD]):
        print(f"⚠️  Escalation email skipped for session {session_id} — email not configured.")
        return

    server = None

    try:
        history        = session_data.get("history", [])
        mode           = session_data.get("mode", "")
        fallback_count = session_data.get("fallback_count", 0)
        summary        = session_data.get("problem_summary", "")

        conversation_text = ""
        for msg in history:
            role    = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            conversation_text += f"{role}: {content}\n\n"

        subject = f"[ESCALATION] Support Session {session_id}"
        body = f"""
A chatbot escalation has occurred.

SESSION ID:      {session_id}
MODE:            {mode}
FALLBACK COUNT:  {fallback_count}

PROBLEM SUMMARY:
{summary}

===================================
FULL CONVERSATION
===================================

{conversation_text}
        """

        message            = MIMEMultipart()
        message["From"]    = SENDER_EMAIL
        message["To"]      = DEVELOPER_EMAIL
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15)
        server.ehlo()
        if server.has_extn("STARTTLS"):
            server.starttls()
            server.ehlo()

        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SENDER_EMAIL, DEVELOPER_EMAIL, message.as_string())
        print(f"✓ Escalation email sent → {DEVELOPER_EMAIL} (session {session_id})")

    except smtplib.SMTPAuthenticationError:
        print(f"❌ SMTP auth failed for session {session_id} — check MAIL_USERNAME / MAIL_PASSWORD in .env")
    except smtplib.SMTPConnectError:
        print(f"❌ Cannot connect to {SMTP_SERVER}:{SMTP_PORT} — check SMTP_SERVER / SMTP_PORT in .env")
    except Exception as e:
        print(f"❌ Escalation email failed for session {session_id}: {type(e).__name__}: {e}")
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass

# ──────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────

USERS = {
    os.getenv("ADMIN_USERNAME", "admin"):     {"password": os.getenv("ADMIN_PASSWORD", "admin123"),  "role": "admin"},
    os.getenv("DEV_USERNAME",   "developer"): {"password": os.getenv("DEV_PASSWORD",   "dev123"),    "role": "developer"},
}

@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        data     = request.json or {}
        username = data.get("username", "").strip()
        password = data.get("password", "")

        if not username or not password:
            return jsonify({"error": "Username and password are required"}), 400

        user = USERS.get(username)
        if not user or user["password"] != password:
            return jsonify({"error": "Incorrect username or password"}), 401

        flask_session["user"]   = username
        flask_session["role"]   = user["role"]
        flask_session.permanent = True

        redirect = "/dashboard.html" if user["role"] == "developer" else "/index.html"
        return jsonify({"status": "ok", "role": user["role"], "redirect": redirect})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    flask_session.clear()
    return jsonify({"status": "logged_out"})

# ──────────────────────────────────────────────────────────────
# Frontend Routes
# ──────────────────────────────────────────────────────────────

@app.route("/")
def serve_root():
    return send_from_directory(FRONTEND_DIR, "login.html")

@app.route("/index.html")
def serve_chat():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/login.html")
def serve_login():
    return send_from_directory(FRONTEND_DIR, "login.html")

@app.route("/dashboard")
@app.route("/dashboard.html")
def serve_dashboard():
    return send_from_directory(FRONTEND_DIR, "dashboard.html")

# ──────────────────────────────────────────────────────────────
# Session Start
# ──────────────────────────────────────────────────────────────

@app.route("/api/session/start", methods=["POST"])
def session_start():
    try:
        session_id = start_session()

        sessions[session_id] = {
            "history":             [],
            "mode":                "classify",
            "problem_summary":     "",
            "last_reply":          "",
            "fallback_count":      0,
            "current_step":        0,
            "awaiting_done":       False,
            "awaiting_resolution": False,
            "stuck_mode":          False,
            "stuck_count":         0,       # tracks consecutive STUCKs on same step
            "classifier_stage":    1,
        }

        opening = (
            "Hello! I am here to help you fix a problem with your website.\n\n"
            "Let's start by understanding the issue.\n\n"
            "Which part of the website is affected? "
            "For example: login page, images, forms, dashboard, menu, or something else?"
        )

        log_message(session_id, "assistant", opening)
        sessions[session_id]["history"].append({"role": "assistant", "content": opening})

        return jsonify({"session_id": session_id, "opening_message": opening})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ──────────────────────────────────────────────────────────────
# Chat Route
# ──────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        data         = request.json or {}
        session_id   = data.get("session_id")
        user_message = data.get("message", "").strip()
        error_text   = data.get("error_text", "").strip()
        is_fallback  = data.get("is_fallback", False)

        if not session_id:
            return jsonify({"error": "Missing session_id"}), 400

        if session_id not in sessions:
            return jsonify({"error": "Invalid or expired session"}), 400

        if len(user_message) > 5000:
            return jsonify({"error": "Message too long (max 5000 chars)"}), 400

        sess               = sessions[session_id]
        normalized_message = user_message.strip().upper()

        try:
            log_message(session_id, "user", user_message)
        except Exception:
            traceback.print_exc()

        reply     = ""
        is_gap    = False
        escalated = False
        complete  = False

        # ── FALLBACK MODE ─────────────────────────────────────
        
        if is_fallback:
            mark_fallback(session_id)
            sess["fallback_count"] += 1
            sess["current_step"]        = 0
            sess["awaiting_done"]       = False
            sess["awaiting_resolution"] = False
            sess["stuck_mode"]          = False
            sess["stuck_count"]         = 0

            reply, is_gap = get_fallback_response(sess["history"], sess["last_reply"])
            escalated     = check_and_escalate(session_id)

            if escalated:

                sess["mode"] = "escalated"

                send_escalation_email(session_id, sess)

                reply += "\n\n⚠️ Your issue has been escalated to the developer."

                # save escalation message before terminating
                sess["history"].append({
                    "role": "assistant",
                    "content": reply
                })

                sess["last_reply"] = reply

                log_message(session_id, "assistant", reply)

                # TERMINATE SESSION
                del sessions[session_id]

                return jsonify({
                    "reply": reply,
                    "terminated": True,
                    "escalated": True,
                    "mode": "escalated"
                })

        # ── ERROR CONTEXT MODE ────────────────────────────────
        elif error_text:
            reply, is_gap = get_error_response(sess["history"], user_message, error_text)
            sess["mode"]  = "step"

        # ── CLASSIFIER MODE ───────────────────────────────────
        elif sess["mode"] == "classify":
            reply, complete = get_classifier_response(sess["history"], user_message)

            if sess["classifier_stage"] < 3:
                sess["classifier_stage"] += 1

            if complete:
                classifier_answers = [m["content"] for m in sess["history"] if m["role"] == "user"]
                classifier_answers.append(user_message)
                sess["problem_summary"] = "\n".join(classifier_answers)
                sess["mode"]            = "step"
                set_problem_summary(session_id, user_message)

        # ── STEP MODE ─────────────────────────────────────────
        elif sess["mode"] == "step":

            # Enforce DONE/STUCK only
            if sess["awaiting_done"] and normalized_message not in ["DONE", "STUCK"]:
                return jsonify({
                    "reply":               "Please reply DONE after completing the step, or STUCK if you need help.",
                    "mode":                sess["mode"],
                    "fallback_count":      sess["fallback_count"],
                    "current_step":        sess["current_step"],
                    "awaiting_done":       sess["awaiting_done"],
                    "awaiting_resolution": sess["awaiting_resolution"],
                    "classifier_stage":    sess["classifier_stage"],
                    "escalated":           False,
                })

            # DONE
            if normalized_message == "DONE":
                sess["current_step"] += 1
                sess["awaiting_done"] = False
                sess["stuck_mode"]    = False
                sess["stuck_count"]   = 0       # reset consecutive STUCKs on success

            # STUCK — with auto-fallback after 2 consecutive STUCKs
            elif normalized_message == "STUCK":
                sess["stuck_mode"]  = True
                sess["stuck_count"] = sess.get("stuck_count", 0) + 1

                if sess["stuck_count"] >= 2:
                    # Auto-trigger fallback — no more looping
                    sess["stuck_count"]         = 0
                    sess["stuck_mode"]          = False
                    sess["awaiting_done"]       = False

                    mark_fallback(session_id)
                    sess["fallback_count"] += 1

                    reply, is_gap = get_fallback_response(sess["history"], sess["last_reply"])
                    escalated     = check_and_escalate(session_id)

                    if escalated:
                        sess["mode"] = "escalated"
                        send_escalation_email(session_id, sess)
                        reply += "\n\n⚠️ Your issue has been escalated to the developer."

                    sess["history"].append({"role": "user",      "content": user_message})
                    sess["history"].append({"role": "assistant",  "content": reply})
                    sess["last_reply"] = reply
                    log_message(session_id, "assistant", reply)

                    return jsonify({
                        "reply":               reply,
                        "is_doc_gap":          is_gap,
                        "mode":                sess["mode"],
                        "fallback_count":      sess["fallback_count"],
                        "escalated":           escalated,
                        "problem_summary":     sess["problem_summary"],
                        "current_step":        sess["current_step"],
                        "awaiting_done":       sess["awaiting_done"],
                        "awaiting_resolution": sess["awaiting_resolution"],
                        "classifier_stage":    sess["classifier_stage"],
                    })

            # YES — resolved
            elif normalized_message == "YES" and sess["awaiting_resolution"]:
                sess["awaiting_resolution"] = False
                sess["mode"]                = "resolved"

                return jsonify({
                    "reply":               "Excellent! Your issue has been resolved successfully.",
                    "resolved":            True,
                    "mode":                "resolved",
                    "fallback_count":      sess["fallback_count"],
                    "current_step":        sess["current_step"],
                    "awaiting_done":       False,
                    "awaiting_resolution": False,
                    "classifier_stage":    sess["classifier_stage"],
                    "escalated":           False,
                })

            # NO — trigger fallback
            elif normalized_message == "NO" and sess["awaiting_resolution"]:
                sess["awaiting_resolution"] = False

                mark_fallback(session_id)
                sess["fallback_count"] += 1

                reply, is_gap = get_fallback_response(sess["history"], sess["last_reply"])
                escalated     = check_and_escalate(session_id)

                if escalated:

                    sess["mode"] = "escalated"

                    send_escalation_email(session_id, sess)

                    reply += "\n\n⚠️ Your issue has been escalated to the developer."

                    # save escalation message before terminating
                    sess["history"].append({
                        "role": "assistant",
                        "content": reply
                    })

                    sess["last_reply"] = reply

                    log_message(session_id, "assistant", reply)

                    # TERMINATE SESSION
                    del sessions[session_id]

                    return jsonify({
                        "reply": reply,
                        "terminated": True,
                        "escalated": True,
                        "mode": "escalated"
                    })

                return jsonify({
                    "reply":               reply,
                    "is_doc_gap":          is_gap,
                    "mode":                sess["mode"],
                    "fallback_count":      sess["fallback_count"],
                    "escalated":           escalated,
                    "problem_summary":     sess["problem_summary"],
                    "current_step":        sess["current_step"],
                    "awaiting_done":       sess["awaiting_done"],
                    "awaiting_resolution": sess["awaiting_resolution"],
                    "classifier_stage":    sess["classifier_stage"],
                })

            # Normal step generation
            reply, is_gap = get_step_response(sess["history"], user_message, sess["problem_summary"])

            if "has this resolved your issue" in reply.lower():
                sess["awaiting_resolution"] = True

            if "reply done when you have completed this step" in reply.lower():
                sess["awaiting_done"] = True

        # ── GENERAL MODE ──────────────────────────────────────
        else:
            reply, is_gap = get_general_response(sess["history"], user_message)

        # ── Persist history ───────────────────────────────────
        sess["history"].append({"role": "user",      "content": user_message})
        sess["history"].append({"role": "assistant",  "content": reply})
        sess["last_reply"] = reply
        log_message(session_id, "assistant", reply)

        if is_gap:
            log_doc_gap(session_id, user_message)

        return jsonify({
            "reply":               reply,
            "is_doc_gap":          is_gap,
            "mode":                sess["mode"],
            "fallback_count":      sess["fallback_count"],
            "escalated":           escalated,
            "problem_summary":     sess["problem_summary"],
            "current_step":        sess["current_step"],
            "awaiting_done":       sess["awaiting_done"],
            "awaiting_resolution": sess["awaiting_resolution"],
            "classifier_stage":    sess["classifier_stage"],
        })

    except FileNotFoundError:
        return jsonify({
            "reply":         "No documentation has been uploaded yet. Please upload a PDF from the dashboard.",
            "is_doc_gap":    False,
            "mode":          "general",
            "fallback_count": 0,
            "escalated":     False,
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e), "reply": "An internal error occurred."}), 500

# ──────────────────────────────────────────────────────────────
# Session Resolve
# ──────────────────────────────────────────────────────────────

@app.route("/api/session/resolve", methods=["POST"])
def session_resolve():
    try:
        data            = request.json or {}
        session_id      = data.get("session_id")
        resolved        = data.get("resolved", False)
        issue_type      = data.get("issue_type", "")
        resolution_note = data.get("resolution_note", "")

        live_session_exists = session_id in sessions

        mark_resolved(session_id, resolved, issue_type, resolution_note)

        if resolved and live_session_exists:

            sessions[session_id]["mode"] = "resolved"

            sessions[session_id]["resolved"] = True
            sessions[session_id]["escalated"] = False
            sessions[session_id]["fallback_used"] = False
            sessions[session_id]["doc_gap"] = False

            sessions[session_id]["awaiting_done"] = False
            sessions[session_id]["awaiting_resolution"] = False
                
        escalated = False
        if not resolved:

            mark_fallback(session_id)

            if live_session_exists:
                sessions[session_id]["fallback_count"] += 1
            escalated = check_and_escalate(session_id)
            if escalated and live_session_exists:
                 sessions[session_id]["mode"] = "escalated"
                 send_escalation_email(session_id, sessions[session_id])

        # Remove from live sessions on logout/resolve
        if not resolved and live_session_exists:
             del sessions[session_id]
        return jsonify({"status": "logged", "escalated": escalated})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ──────────────────────────────────────────────────────────────
# Live session cleanup on user logout
# ──────────────────────────────────────────────────────────────

@app.route("/api/session/end", methods=["POST"])
def session_end():
    """Called when a user logs out from the chat — removes from live sessions."""
    try:
        data       = request.json or {}
        session_id = data.get("session_id")

        if session_id and session_id in sessions:
            del sessions[session_id]

        return jsonify({"status": "session_ended"})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ──────────────────────────────────────────────────────────────
# PDF Upload
# ──────────────────────────────────────────────────────────────

@app.route("/api/upload-pdf", methods=["POST"])
def upload_pdf():
    tmp_path = None
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "No file selected"}), 400

        safe_name = secure_filename(file.filename)
        if not safe_name:
            return jsonify({"error": "Invalid filename"}), 400
        if not safe_name.lower().endswith(".pdf"):
            return jsonify({"error": "Only PDF files are allowed"}), 400

        tmp_path = os.path.join(BASE_DIR, safe_name)
        file.save(tmp_path)
        meta = extract_pdf_to_text(tmp_path)
        doc_text = load_doc_text()

        if not doc_text.strip():
            raise Exception("PDF extracted successfully but contains no readable text")
        if not meta:
            raise Exception("Metadata generation failed")

        return jsonify({"status": "PDF uploaded and extracted successfully", "meta": meta, "document_loaded": True})

    except Exception as e:
        print(f"PDF upload error: {e}")
        return jsonify({"error": str(e), "document_loaded": False}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

# ──────────────────────────────────────────────────────────────
# Hot Reload
# ──────────────────────────────────────────────────────────────

@app.route("/api/doc/hot-reload", methods=["POST"])
def hot_reload_doc():
    tmp_path = None
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "No file selected"}), 400

        safe_name = secure_filename(file.filename)
        if not safe_name:
            return jsonify({"error": "Invalid filename"}), 400
        if not safe_name.lower().endswith(".pdf"):
            return jsonify({"error": "Only PDF files are allowed"}), 400

        tmp_path = os.path.join(BASE_DIR, safe_name)
        file.save(tmp_path)
        meta     = extract_pdf_to_text(tmp_path)
        doc_text = load_doc_text()

        if not doc_text.strip():
            raise Exception("PDF extracted but contains no readable text")
        if not meta:
            raise Exception("Failed to generate metadata")

        return jsonify({
            "status":          "Documentation updated live. All active sessions now use the updated document.",
            "meta":            meta,
            "document_loaded": True,
            "hot_reload":      True,
        })

    except Exception as e:
        print(f"Hot reload error: {e}")
        return jsonify({"error": str(e), "document_loaded": False, "hot_reload": False}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

# ──────────────────────────────────────────────────────────────
# Doc meta / status
# ──────────────────────────────────────────────────────────────

@app.route("/api/doc/meta", methods=["GET"])
def doc_meta():
    return jsonify(get_doc_meta())

@app.route("/api/doc/status", methods=["GET"])
def doc_status():
    try:
        meta = get_doc_meta()
        if not meta:
            return jsonify({"loaded": False, "message": "No documentation loaded"})
        return jsonify({"loaded": True, "meta": meta})
    except Exception as e:
        return jsonify({"loaded": False, "error": str(e)}), 500

# ──────────────────────────────────────────────────────────────
# Inject Note
# ──────────────────────────────────────────────────────────────

@app.route("/api/session/inject-note", methods=["POST"])
def inject_note():
    try:
        data       = request.json or {}
        session_id = data.get("session_id")
        note       = data.get("note", "").strip()

        if not note:
            return jsonify({"error": "Note cannot be empty"}), 400
        if session_id not in sessions:
            return jsonify({"error": "Session not active"}), 404

        injection = f"[DEVELOPER NOTE — silent context only]: {note}"
        sessions[session_id]["history"].append({"role": "user",      "content": injection})
        sessions[session_id]["history"].append({"role": "assistant",  "content": "Understood. I will use this silently."})
        inject_dev_note(session_id, note)

        return jsonify({"status": "Developer note injected successfully"})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ──────────────────────────────────────────────────────────────
# Analytics + Logs
# ──────────────────────────────────────────────────────────────

@app.route("/api/sessions/active", methods=["GET"])
def active_sessions():
    try:
        return jsonify(get_live_sessions(sessions))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/logs", methods=["GET"])
def get_logs():
    return jsonify(get_all_sessions())

@app.route("/api/analytics", methods=["GET"])
def analytics():
    return jsonify(get_analytics())

@app.route("/api/escalations", methods=["GET"])
def escalations():
    return jsonify(get_escalations())

# ──────────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n✓ BASE_DIR       : {BASE_DIR}")
    print(f"✓ FRONTEND_DIR   : {FRONTEND_DIR}")
    print(f"✓ Server Running : http://127.0.0.1:5000")
    print(f"✓ OpenRouter AI  : Connected")

    app.run(
        debug=True,
        host="127.0.0.1",
        port=5000
    )