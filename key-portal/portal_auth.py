"""Key Portal session and role helpers."""


def normalize_email(value):
    return str(value or "").strip().lower()


def admin_emails(config):
    return {
        normalize_email(email)
        for email in getattr(config, "KEY_PORTAL_ADMIN_EMAILS", [])
        if normalize_email(email)
    }


def is_admin_email(email, config):
    return normalize_email(email) in admin_emails(config)


def session_context(session_data, config):
    if not session_data:
        return None
    email = normalize_email(session_data.get("email"))
    user = dict(session_data.get("user") or {})
    if not email:
        email = normalize_email(user.get("email"))
    user["email"] = email
    is_admin = is_admin_email(email, config)
    return {
        **session_data,
        "email": email,
        "user": user,
        "is_admin": is_admin,
        "role": "admin" if is_admin else "user",
    }


def can_access_email(session_data, target_email):
    if not session_data:
        return False
    target = normalize_email(target_email)
    current = normalize_email(session_data.get("email"))
    return bool(current and (session_data.get("is_admin") or target == current))
