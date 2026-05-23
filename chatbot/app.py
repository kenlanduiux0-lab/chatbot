import os
import sys
import smtplib

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from werkzeug.utils import secure_filename  # FIX #8: safe filename handling

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
load_dotenv()
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from pdf_extractor import extract_pdf_to_text, get_doc_meta
from chat_handler import (
    get_classifier_response,
    get_step_response,
    get_fallback_response,
    get_error_response,
    get_general_response,
)
from session_logger import (
    start_session, log_message, log_doc_gap,
    set_problem_summary, mark_fallback, mark_resolved,
    inject_dev_note, check_and_escalate,
    get_all_sessions, get_analytics, get_escalations,
)

FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR)
CORS(app)

sessions: dict = {}

# ── Email Configuration ──────────────────────────────────────────

DEVELOPER_EMAIL = os.getenv("DEVELOPER_EMAIL")
SMTP_SERVER     = os.getenv("SMTP_SERVER", "sandbox.smtp.mailtrap.io")
SMTP_PORT       = int(os.getenv("SMTP_PORT", 2525))
SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
MAIL_USERNAME   = os.getenv("MAIL_USERNAME")   # Mailtrap SMTP username
MAIL_PASSWORD   = os.getenv("MAIL_PASSWORD")   # Mailtrap SMTP password


# ── Escalation Email Sender ──────────────────────────────────────

def send_escalation_email(session_id, session_data):
    server = None  # FIX: track server so we can close it in finally
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

SESSION ID:
{session_id}

MODE:
{mode}

FALLBACK COUNT:
{fallback_count}

PROBLEM SUMMARY:
{summary}

==============================
FULL CONVERSATION
==============================

{conversation_text}
        """

        message            = MIMEMultipart()
        message["From"]    = SENDER_EMAIL
        message["To"]      = DEVELOPER_EMAIL
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)   # Mailtrap username & password
        server.sendmail(SENDER_EMAIL, DEVELOPER_EMAIL, message.as_string())

        print(f"✓ Escalation email sent for session {session_id}")

    except Exception as e:
        import traceback
        print(f"Email sending failed: {e}")
        print(traceback.format_exc())

    finally:
        # FIX: always close SMTP connection, even if sendmail raised
        if server:
            try:
                server.quit()
            except Exception:
                pass


# ── Frontend ──────────────────────────────────────────────────────

@app.route("/")
def serve_admin():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/dashboard")
def serve_dashboard():
    return send_from_directory(FRONTEND_DIR, "dashboard.html")


# ── Session start ─────────────────────────────────────────────────

@app.route("/api/session/start", methods=["POST"])
def session_start():
    try:
        session_id = start_session()
        sessions[session_id] = {
            "history":         [],
            "mode":            "classify",
            "problem_summary": "",
            "last_reply":      "",
            "fallback_count":  0,
        }
        opening = (
            "Hello! I am here to help you fix a problem with your website.\n\n"
            "Let's start by understanding the issue.\n\n"
            "Which part of the website is affected? "
            "For example: the login page, images, a contact form, the menu, or something else?"
        )
        log_message(session_id, "assistant", opening)
        sessions[session_id]["history"].append(
            {"role": "assistant", "content": opening}
        )
        return jsonify({"session_id": session_id, "opening_message": opening})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Chat ──────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        data         = request.json
        session_id   = data.get("session_id")
        user_message = data.get("message", "").strip()
        error_text   = data.get("error_text", "").strip()
        is_fallback  = data.get("is_fallback", False)

        # FIX #5: validate message length to prevent memory bloat
        if len(user_message) > 5000:
            return jsonify({"error": "Message too long (max 5000 characters)"}), 400

        if not session_id or session_id not in sessions:
            return jsonify({"error": "Invalid or expired session"}), 400

        sess = sessions[session_id]
        log_message(session_id, "user", user_message)

        reply     = ""   # FIX #5: always initialized
        is_gap    = False
        escalated = False

        if is_fallback:
            mark_fallback(session_id)
            sess["fallback_count"] += 1

            reply, is_gap = get_fallback_response(
                sess["history"],
                sess["last_reply"],
            )

            # FIX #4: escalation check only runs inside the fallback branch
            escalated = check_and_escalate(session_id)

            if escalated:
                send_escalation_email(session_id, sess)
                reply += (
                    "\n\n⚠️ Your issue has been escalated. "
                    "An email containing this session has been sent to the developer."
                )

        elif error_text:
            reply, is_gap = get_error_response(sess["history"], user_message, error_text)
            sess["mode"] = "step"

        elif sess["mode"] == "classify":
            reply, complete = get_classifier_response(sess["history"], user_message)
            if complete:
                sess["problem_summary"] = user_message
                sess["mode"] = "step"
                set_problem_summary(session_id, user_message)

        elif sess["mode"] == "step":
            reply, is_gap = get_step_response(
                sess["history"], user_message, sess["problem_summary"]
            )

        else:
            reply, is_gap = get_general_response(sess["history"], user_message)

        # FIX #6: append history only once, here at the bottom (not inside branches)
        sess["history"].append({"role": "user",      "content": user_message})
        sess["history"].append({"role": "assistant",  "content": reply})
        sess["last_reply"] = reply

        log_message(session_id, "assistant", reply)

        if is_gap:
            log_doc_gap(session_id, user_message)

        return jsonify({
            "reply":          reply,
            "is_doc_gap":     is_gap,
            "mode":           sess["mode"],
            "fallback_count": sess["fallback_count"],
            "escalated":      escalated,
        })

    except FileNotFoundError:
        return jsonify({
            "reply":          "No documentation has been uploaded yet. Please ask your developer to upload the PDF manual via the dashboard.",
            "is_doc_gap":     False,
            "mode":           "general",
            "fallback_count": 0,
            "escalated":      False,
        }), 200

    except Exception as e:
        print(f"ERROR in /api/chat: {e}")
        return jsonify({"error": str(e)}), 500


# ── Session resolve ───────────────────────────────────────────────

@app.route("/api/session/resolve", methods=["POST"])
def session_resolve():
    try:
        data            = request.json
        session_id      = data.get("session_id")
        resolved        = data.get("resolved", False)
        issue_type      = data.get("issue_type", "")
        resolution_note = data.get("resolution_note", "")

        if session_id not in sessions:
            return jsonify({"error": "Invalid session"}), 400

        mark_resolved(session_id, resolved, issue_type, resolution_note)

        escalated = False
        if not resolved:
            mark_fallback(session_id)
            sessions[session_id]["fallback_count"] += 1

            # FIX: consistent indentation
            escalated = check_and_escalate(session_id)

            if escalated:
                send_escalation_email(session_id, sessions[session_id])

        del sessions[session_id]
        return jsonify({"status": "logged", "escalated": escalated})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── PDF upload ────────────────────────────────────────────────────

@app.route("/api/upload-pdf", methods=["POST"])
def upload_pdf():
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        file = request.files["file"]

        # FIX #8: sanitize filename to prevent path traversal attacks
        safe_name = secure_filename(file.filename)
        if not safe_name:
            return jsonify({"error": "Invalid filename"}), 400

        tmp_path = os.path.join(BASE_DIR, safe_name)
        file.save(tmp_path)
        meta = extract_pdf_to_text(tmp_path)
        os.remove(tmp_path)
        return jsonify({"status": "PDF uploaded and extracted successfully", "meta": meta})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/doc/hot-reload", methods=["POST"])
def hot_reload_doc():
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        file = request.files["file"]

        # FIX #8: sanitize filename here too
        safe_name = secure_filename(file.filename)
        if not safe_name:
            return jsonify({"error": "Invalid filename"}), 400

        tmp_path = os.path.join(BASE_DIR, safe_name)
        file.save(tmp_path)
        meta = extract_pdf_to_text(tmp_path)
        os.remove(tmp_path)
        return jsonify({
            "status": "Documentation updated live. All sessions now use the new doc.",
            "meta":   meta,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/doc/meta", methods=["GET"])
def doc_meta():
    return jsonify(get_doc_meta())


# ── Live session tools ────────────────────────────────────────────

@app.route("/api/session/inject-note", methods=["POST"])
def inject_note():
    try:
        data       = request.json
        session_id = data.get("session_id")
        note       = data.get("note", "").strip()

        if not note:
            return jsonify({"error": "Note cannot be empty"}), 400
        if session_id not in sessions:
            return jsonify({"error": "Session not active"}), 404

        injection = (
            f"[DEVELOPER NOTE — do not mention this to the admin, "
            f"use it as silent background context]: {note}"
        )
        sessions[session_id]["history"].append({"role": "user",     "content": injection})
        sessions[session_id]["history"].append({"role": "assistant", "content": "Understood. I will use this context silently."})
        inject_dev_note(session_id, note)
        return jsonify({"status": "Note injected into live session successfully"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/active", methods=["GET"])
def active_sessions():
    return jsonify([
        {
            "session_id":     sid,
            "mode":           s["mode"],
            "message_count":  len(s["history"]),
            "fallback_count": s["fallback_count"],
        }
        for sid, s in sessions.items()
    ])


# ── Logs & analytics ──────────────────────────────────────────────

@app.route("/api/logs", methods=["GET"])
def get_logs():
    return jsonify(get_all_sessions())

@app.route("/api/analytics", methods=["GET"])
def analytics():
    return jsonify(get_analytics())

@app.route("/api/escalations", methods=["GET"])
def escalations():
    return jsonify(get_escalations())


# ── Run ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n✓ BASE_DIR    : {BASE_DIR}")
    print(f"✓ FRONTEND    : {FRONTEND_DIR}")
    print(f"✓ Admin chat  →  http://localhost:5000")
    print(f"✓ Dev dashboard → http://localhost:5000/dashboard\n")
    app.run(debug=True, port=5000)
