"""
jira_ticket.py -- Jira ticket creation for Starfire.

starfire.py imports this module directly, and it is bundled into the
binary. What is optional is the config: Jira is switched on only if a
jira_config.json exists in the CURRENT WORKING DIRECTORY at startup
(cwd is where .test/.camp files and captured photos already live; the
config is user-specific, not part of the app install). Without it
Starfire behaves as if Jira didn't exist -- no error, no missing-feature
message. See CONFIG_TEMPLATE below for the shape.

Dependencies: stdlib only.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

CONFIG_FILENAME = "jira_config.json"

CONFIG_TEMPLATE = {
    "base_url": "https://yourteam.atlassian.net",
    "email": "you@example.com",
    "api_token": "",
    "project_key": "PROJ",
    # Separate issue types: a test point's ticket is found-during-execution
    # work (Bug); a Test row's ticket is planning-level work (Task).
    "issue_type": "Bug",
    "test_issue_type": "Task",
    # Filled in automatically after the first ticket with an Epic key --
    # "parent" (team-managed) or a "customfield_XXXXX" id (classic). Safe to leave alone.
    "epic_link_field": None,
}


def _config_path() -> str:
    # Deliberately cwd-relative, not __file__-relative: jira_ticket.py's
    # own code is bundled into the starfire binary (see starfire.py's
    # `import jira_ticket`), so __file__ would point inside the frozen
    # bundle, not somewhere a person could actually edit. The config
    # stays a genuinely external file, found the same way .test/.camp
    # files and the measure binary already are -- wherever the app is run from.
    return os.path.join(os.getcwd(), CONFIG_FILENAME)


def load_config() -> dict | None:
    path = _config_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    required = ("base_url", "email", "api_token", "project_key")
    if not all(cfg.get(k) for k in required):
        return None
    cfg["issue_type"] = cfg.get("issue_type") or "Bug"
    cfg["test_issue_type"] = cfg.get("test_issue_type") or "Task"
    cfg["epic_link_field"] = cfg.get("epic_link_field") or None
    return cfg


def _save_epic_link_field(field_name: str) -> None:
    # Best-effort cache write -- never worth failing a ticket creation over;
    # the next ticket just re-detects it if this fails.
    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["epic_link_field"] = field_name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def _auth_header(cfg: dict) -> str:
    token = base64.b64encode(f"{cfg['email']}:{cfg['api_token']}".encode()).decode()
    return f"Basic {token}"


def _api_request(cfg: dict, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = cfg["base_url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header(cfg))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"errorMessages": [raw.decode(errors="replace")]}
    except urllib.error.URLError as e:
        return 0, {"errorMessages": [str(e.reason)]}


def _jira_error_text(body: dict) -> str:
    parts = list(body.get("errorMessages") or [])
    for field, msg in (body.get("errors") or {}).items():
        parts.append(f"{field}: {msg}")
    return "; ".join(parts) if parts else "unknown Jira error"


def _find_epic_link_custom_field(cfg: dict) -> str | None:
    """Company-managed/classic project fallback: look for a custom field
    literally named 'Epic Link' in this project's create-issue metadata."""
    status, body = _api_request(
        cfg, "GET",
        f"/rest/api/2/issue/createmeta?projectKeys={cfg['project_key']}&expand=projects.issuetypes.fields",
    )
    if status != 200:
        return None
    for project in body.get("projects", []):
        for issuetype in project.get("issuetypes", []):
            for field_id, field_meta in issuetype.get("fields", {}).items():
                if (field_meta.get("name") or "").strip().lower() == "epic link":
                    return field_id
    return None


def create_ticket(summary: str, description: str, epic_key: str = "",
                    issue_type: str | None = None) -> dict:
    """Creates a Jira issue. Returns {"success": True, "key": ..., "url": ...}
    or {"success": False, "error": ...} -- never raises, so a UI caller can
    always show *something* meaningful rather than a stack trace.

    issue_type overrides cfg["issue_type"] for this one call -- the caller
    decides which config key applies (a test point's ticket vs. a Test
    Series/Ancillary Tests row's ticket use different ones; see
    CONFIG_TEMPLATE). Defaults to cfg["issue_type"] when not given, so
    existing point-ticket callers don't need to change."""
    cfg = load_config()
    if not cfg:
        return {"success": False,
                 "error": f"No usable {CONFIG_FILENAME} found next to jira_ticket.py "
                          f"(or it's missing base_url/email/api_token/project_key)."}

    fields = {
        "project": {"key": cfg["project_key"]},
        "summary": summary,
        "description": description,
        "issuetype": {"name": issue_type or cfg["issue_type"]},
    }

    epic_key = (epic_key or "").strip()
    if epic_key:
        cached = cfg.get("epic_link_field")
        if cached:
            fields[cached] = epic_key if cached != "parent" else {"key": epic_key}
        else:
            # Try the modern (team-managed) approach first -- more common,
            # no metadata lookup needed.
            fields["parent"] = {"key": epic_key}

    status, body = _api_request(cfg, "POST", "/rest/api/2/issue", {"fields": fields})

    if status == 201:
        if epic_key and not cfg.get("epic_link_field"):
            _save_epic_link_field("parent")
        key = body.get("key", "")
        return {"success": True, "key": key, "url": f"{cfg['base_url'].rstrip('/')}/browse/{key}"}

    # "parent" didn't work -- likely a company-managed/classic project
    # where Epic Link is a custom field instead. Look it up once and retry.
    if epic_key and not cfg.get("epic_link_field") and "parent" in fields:
        custom_field = _find_epic_link_custom_field(cfg)
        if custom_field:
            fields.pop("parent")
            fields[custom_field] = epic_key
            status2, body2 = _api_request(cfg, "POST", "/rest/api/2/issue", {"fields": fields})
            if status2 == 201:
                _save_epic_link_field(custom_field)
                key = body2.get("key", "")
                return {"success": True, "key": key,
                         "url": f"{cfg['base_url'].rstrip('/')}/browse/{key}"}
            return {"success": False, "error": _jira_error_text(body2)}

    return {"success": False, "error": _jira_error_text(body)}