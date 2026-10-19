"""Hand written OpenAPI 3.0 description of /api/v1 (kept next to the code, checked by tests)."""

ERR = {"description": "Error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
SEC = [{"bearerAuth": []}]


def _op(summary, body=None, ok="OK", params=None, stream=False, auth=True):
    op = {"summary": summary, "responses": {"200": {"description": ok}, "401": ERR}}
    if stream:
        op["responses"]["200"]["content"] = {"text/event-stream": {"schema": {"type": "string"}}}
    if body:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    if params:
        op["parameters"] = params
    if auth:
        op["security"] = SEC
    return op


def _id_param():
    return [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}]


def spec(server_url="/"):
    return {
        "openapi": "3.0.3",
        "info": {"title": "wgetpanel API", "version": "1.0.0",
                 "description": "Mobile / third party API. Authenticate with POST /api/v1/auth/token and send "
                                "`Authorization: Bearer <token>`. Live updates: GET /api/v1/events (SSE) emitting "
                                "`snapshot`, `status`, `removed` and `progress` events; after every (re)connect the first "
                                "event is a full `snapshot`, so a client never has to reconcile missed events."},
        "servers": [{"url": server_url.rstrip("/")}],
        "paths": {
            "/api/v1/auth/token": {
                "post": {**_op("Log in, returns a bearer token", {"type": "object", "required": ["username", "password"], "properties": {
                    "username": {"type": "string"}, "password": {"type": "string"}, "device_name": {"type": "string"}}}, auth=False),
                    "responses": {"201": {"description": "Token (shown once)"}, "401": ERR, "429": ERR}},
                "delete": _op("Revoke the token used for this request (log out)"),
            },
            "/api/v1/auth/me": {"get": _op("Current user and token")},
            "/api/v1/auth/tokens": {"get": _op("List own tokens (devices)")},
            "/api/v1/auth/tokens/{id}": {"delete": _op("Revoke one of own tokens", params=_id_param())},
            "/api/v1/summary": {"get": _op("Counts per status, current speed, speed series (no download list)")},
            "/api/v1/downloads": {
                "get": _op("List downloads (active + last 200 finished)", params=[{"name": "status", "in": "query", "schema": {
                    "type": "string", "enum": ["queued", "running", "paused", "completed", "error", "canceled"]}}]),
                "post": {**_op("Add one or more downloads", {"type": "object", "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}}, "url": {"type": "string"}, "directory": {"type": "string"},
                    "filename": {"type": "string"}, "not_before": {"type": "string", "format": "date-time"},
                    "http_user": {"type": "string"}, "http_password": {"type": "string"},
                    "options": {"type": "object", "description": "limit_rate (bytes/s), tries, timeout, user_agent, referer, headers[], check_certificate"}}}),
                    "responses": {"201": {"description": "created ids (+ per URL errors)"}, "400": ERR, "401": ERR}},
            },
            "/api/v1/downloads/actions": {"post": _op("pause, resume, retry, cancel, remove, purge, top, bottom, up, down", {
                "type": "object", "required": ["action"], "properties": {"action": {"type": "string"}, "ids": {"type": "array", "items": {"type": "integer"}},
                                                                         "delete_files": {"type": "boolean"}}})},
            "/api/v1/downloads/{id}": {"get": _op("Detail: speed series, wget log tail", params=_id_param())},
            "/api/v1/downloads/{id}/file": {"get": _op("Download the finished file", params=_id_param())},
            "/api/v1/events": {"get": _op("Server-Sent Events stream", stream=True,
                                          ok="Stream. The server closes it after WGP_SSE_MAX_SECONDS (default 1 h); reconnect.")},
        },
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {"Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {
                "code": {"type": "string"}, "message": {"type": "string"}}}}}},
        },
    }
