import json
import os
from functools import wraps
from flask import session, jsonify, redirect, url_for
from werkzeug.security import check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_PATH = os.path.join(BASE_DIR, "users.json")


def load_users() -> dict:
    with open(USERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_login(username: str, password: str) -> dict | None:
    """Return user dict if credentials valid, else None."""
    users = load_users()
    user = users.get(username)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def login_required(f):
    """Redirect to login page if user is not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            if _is_api_route():
                return jsonify({"error": "Unauthorised"}), 401
            return redirect(url_for("serve_login"))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """Allow only users with one of the specified roles.
    Usage: @role_required('developer')
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "username" not in session:
                if _is_api_route():
                    return jsonify({"error": "Unauthorised"}), 401
                return redirect(url_for("serve_login"))
            if session.get("role") not in roles:
                if _is_api_route():
                    return jsonify({"error": "Forbidden"}), 403
                return redirect(url_for("serve_login"))
            return f(*args, **kwargs)
        return decorated
    return decorator


def _is_api_route() -> bool:
    from flask import request
    return request.path.startswith("/api/")
