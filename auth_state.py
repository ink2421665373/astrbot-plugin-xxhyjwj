"""Read persisted authentication without logging or exposing credentials."""
import json


def read_saved_auth(path):
    try:
        auth = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}, "missing"
    except (ValueError, UnicodeError):
        return {}, "invalid"
    except OSError:
        return {}, "unreadable"
    if not isinstance(auth, dict):
        return {}, "invalid"
    cookies = auth.get("cookies", {})
    if not isinstance(cookies, dict):
        return {}, "invalid"
    return auth, "saved" if cookies else "empty"
