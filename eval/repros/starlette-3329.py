# starlette-3329.py — Request.form(max_fields=N) limit silently ignored
#
# Bug: Request.form() accepts max_fields and max_part_size keyword arguments,
# but in the application/x-www-form-urlencoded branch, it creates FormParser
# with ONLY self.headers and self.stream() — the limit parameters are never
# forwarded. FormParser itself also lacked those parameters.
#
# Fix: add max_fields and max_part_size to FormParser.__init__() (formparsers.py)
# and pass them through in Request.form() (requests.py).
import asyncio
import pytest


def _make_environ(body: bytes) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ],
    }


async def _parse_form(body: bytes, max_fields: int) -> dict:
    from starlette.requests import Request

    scope = _make_environ(body)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(scope, receive)
    form = await request.form(max_fields=max_fields)
    return dict(form)


def test_max_fields_limit_is_enforced():
    """Request.form(max_fields=1) must raise when 2+ fields are submitted.

    Before fix: max_fields is accepted but silently ignored; both fields are
    parsed without error.
    After fix: MultiPartException or HTTPException is raised when the field
    count exceeds max_fields.
    """
    body = b"field1=value1&field2=value2"
    raised = False

    try:
        asyncio.run(_parse_form(body, max_fields=1))
    except Exception:
        raised = True

    assert raised, (
        "Bug: Request.form(max_fields=1) accepted 2 form fields without raising. "
        "The max_fields limit is accepted by Request.form() but never forwarded "
        "to FormParser — the limit is silently ignored."
    )
