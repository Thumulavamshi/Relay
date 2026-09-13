"""JSON over HTTPS for the app integrations. Stdlib only, like the rest of the backend.

One helper so Google, HubSpot, Slack and Groq all fail the same way: an ApiError
carrying the service, the HTTP status and the provider's own message. The status
is what callers branch on (409 = already exists), and the message is what lands
in the action ledger - which is where a failed integration gets diagnosed.

The wording is chosen to meet `actions._is_transient`: a network failure reads
"connection error" or "timed out", so the bus retries it; a 4xx carries its code
and the provider's reason, so it fails once, honestly.
"""

import json
import urllib.error
import urllib.parse
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, service, status, detail):
        self.service = service
        self.status = status
        self.detail = detail
        super().__init__(f"{service} {status}: {detail}")


def _detail(raw):
    """The provider's own reason, whichever envelope it arrives in."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip()[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err and data.get("error_description"):
            # OAuth shape: {"error": "invalid_grant", "error_description": "Bad
            # Request"}. The code is the useful half; the description alone hides it.
            return f"{err}: {data['error_description']}"[:300]
        for key in ("message", "error_description", "error"):
            if data.get(key):
                return str(data[key])[:300]
    return str(data)[:300]


def call(service, method, url, headers=None, json_body=None, form=None, timeout=15):
    """One request. Returns the decoded JSON body ({} when empty). Raises ApiError."""
    h = {"User-Agent": "Relay/1.0", "Accept": "application/json"}
    h.update(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        h["Content-Type"] = "application/json; charset=utf-8"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        h["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(service, exc.code,
                       _detail(exc.read().decode("utf-8", "replace"))) from None
    except urllib.error.URLError as exc:
        raise ApiError(service, 0, f"connection error: {exc.reason}") from None
    except TimeoutError:
        raise ApiError(service, 0, f"timed out after {timeout}s") from None

    if not raw.strip():
        return {}
    return json.loads(raw)
