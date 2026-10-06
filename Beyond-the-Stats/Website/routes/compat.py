"""
FastAPI route compatibility layer for Beyond the Stats.

Provides `request`, `current_app`, `jsonify`, `FlaskCompatRouter`, and
middleware so route blueprints written for Flask run cleanly and natively
on FastAPI with zero URL route breaking or behavior changes.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
from contextvars import ContextVar
from typing import Any, Callable, Optional

from fastapi import APIRouter as _FastAPIRouter
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

_request_ctx: ContextVar[Optional[Request]] = ContextVar("_request_ctx", default=None)


class RequestProxy:
    """Thread-safe and async-safe proxy to the current active Request."""

    def __getattr__(self, name: str) -> Any:
        req = _request_ctx.get()
        if req is None:
            raise RuntimeError("Working outside of request context.")
        return getattr(req, name)

    def __getitem__(self, key: str) -> Any:
        req = _request_ctx.get()
        if req is None:
            raise RuntimeError("Working outside of request context.")
        return req[key]

    @property
    def args(self) -> Any:
        req = _request_ctx.get()
        if req is None:
            return {}
        return req.query_params

    @property
    def form(self) -> dict:
        req = _request_ctx.get()
        if req is None:
            return {}
        return getattr(req, "_cached_form", {})

    def get_json(self, silent: bool = True) -> Any:
        req = _request_ctx.get()
        if req is None:
            return {} if silent else None
        if hasattr(req, "_cached_json"):
            return req._cached_json
        try:
            body = getattr(req, "_cached_body", b"")
            if not body:
                return {} if silent else None
            data = json.loads(body.decode("utf-8"))
            req._cached_json = data
            return data
        except Exception:
            if silent:
                return {}
            raise


class AppProxy:
    """Thread-safe proxy to the current FastAPI application."""

    def __getattr__(self, name: str) -> Any:
        req = _request_ctx.get()
        if req is not None and hasattr(req, "app"):
            return getattr(req.app, name)
        try:
            import app as website_app
            if hasattr(website_app, "app"):
                return getattr(website_app.app, name)
        except Exception:
            pass
        if name == "config":
            return {}
        raise RuntimeError("Working outside of application context.")


request = RequestProxy()
current_app = AppProxy()


# Attach .args and .get_json directly to Starlette Request class as well
if not hasattr(Request, "args"):
    Request.args = property(lambda self: self.query_params)

if not hasattr(Request, "get_json"):
    def _req_get_json(self: Request, silent: bool = True) -> Any:
        if hasattr(self, "_cached_json"):
            return self._cached_json
        try:
            body = getattr(self, "_cached_body", b"")
            if not body:
                return {} if silent else None
            data = json.loads(body.decode("utf-8"))
            self._cached_json = data
            return data
        except Exception:
            if silent:
                return {}
            raise
    Request.get_json = _req_get_json


def jsonify(content: Any = None, status_code: int = 200, **kwargs) -> JSONResponse:
    """FastAPI equivalent of Flask's jsonify, returning a Starlette JSONResponse."""
    payload = content if content is not None else kwargs
    return JSONResponse(content=payload, status_code=status_code)


def _wrap_compat_endpoint(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap endpoint to handle Flask-style (body, status_code) tuples."""
    if inspect.iscoroutinefunction(endpoint):
        @functools.wraps(endpoint)
        async def async_wrapper(*args, **kwargs):
            res = await endpoint(*args, **kwargs)
            if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], int):
                body, code = res
                if isinstance(body, Response):
                    body.status_code = code
                    return body
                if isinstance(body, str):
                    return HTMLResponse(content=body, status_code=code)
                return JSONResponse(content=body, status_code=code)
            return res
        return async_wrapper
    else:
        @functools.wraps(endpoint)
        def sync_wrapper(*args, **kwargs):
            res = endpoint(*args, **kwargs)
            if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], int):
                body, code = res
                if isinstance(body, Response):
                    body.status_code = code
                    return body
                if isinstance(body, str):
                    return HTMLResponse(content=body, status_code=code)
                return JSONResponse(content=body, status_code=code)
            return res
        return sync_wrapper


class FlaskCompatRoute(APIRoute):
    """APIRoute that wraps handlers with Flask response tuple compatibility."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs):
        super().__init__(path, _wrap_compat_endpoint(endpoint), **kwargs)


def _flask_to_fastapi_path(path: str) -> str:
    """Convert Flask-style path parameters (<path:x>, <int:x>, <x>) to FastAPI ({x:path}, {x:int}, {x})."""
    path = re.sub(r"<path:([^>]+)>", r"{\1:path}", path)
    path = re.sub(r"<int:([^>]+)>", r"{\1:int}", path)
    path = re.sub(r"<float:([^>]+)>", r"{\1:float}", path)
    path = re.sub(r"<(?:string:)?([^>]+)>", r"{\1}", path)
    return path


class FlaskCompatRouter(_FastAPIRouter):
    """APIRouter configured with FlaskCompatRoute by default."""

    def __init__(self, *args, **kwargs):
        # Ignore Flask-specific Blueprint kwargs like template_folder, static_folder
        kwargs.pop("template_folder", None)
        kwargs.pop("static_folder", None)
        kwargs.pop("static_url_path", None)
        if "url_prefix" in kwargs:
            kwargs.setdefault("prefix", kwargs.pop("url_prefix"))
        kwargs.setdefault("route_class", FlaskCompatRoute)
        super().__init__(**kwargs)

    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kwargs):
        converted_path = _flask_to_fastapi_path(path)
        return super().add_api_route(converted_path, endpoint, **kwargs)

    def route(self, path: str, methods: list[str] | None = None, **kwargs):
        """Flask-compatible route decorator."""
        return self.api_route(path, methods=methods or ["GET"], **kwargs)


Blueprint = FlaskCompatRouter


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Middleware that sets the current Request in ContextVar and caches raw body."""

    async def dispatch(self, req: Request, call_next: Callable) -> Response:
        try:
            body = await req.body()
            req._cached_body = body
            if body:
                try:
                    req._cached_json = json.loads(body.decode("utf-8"))
                except Exception:
                    req._cached_json = {}
            else:
                req._cached_json = {}
        except Exception:
            req._cached_body = b""
            req._cached_json = {}

        token = _request_ctx.set(req)
        try:
            response = await call_next(req)
            return response
        finally:
            _request_ctx.reset(token)


# ── Jinja2 & File Response Helpers ───────────────────────────────────
_templates: Optional[Any] = None

def get_templates():
    global _templates
    if _templates is None:
        from fastapi.templating import Jinja2Templates
        website_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _templates = Jinja2Templates(directory=os.path.join(website_dir, "templates"))
    return _templates


def render_template(template_name: str, **context) -> HTMLResponse:
    """Render a Jinja2 HTML template using current request context."""
    req = _request_ctx.get()
    templates = get_templates()
    if req is not None:
        context["request"] = req
        return templates.TemplateResponse(template_name, context)
    # Fallback to direct Jinja2 rendering if outside request
    template = templates.env.get_template(template_name)
    content = template.render(**context)
    return HTMLResponse(content=content)


def redirect(location: str, code: int = 302) -> Response:
    from starlette.responses import RedirectResponse
    return RedirectResponse(url=location, status_code=code)


def send_from_directory(directory: str, filename: str, **kwargs) -> Response:
    from starlette.responses import FileResponse
    full_path = os.path.join(directory, filename)
    return FileResponse(full_path)


__all__ = [
    "Request",
    "Response",
    "JSONResponse",
    "HTMLResponse",
    "PlainTextResponse",
    "jsonify",
    "request",
    "current_app",
    "FlaskCompatRoute",
    "FlaskCompatRouter",
    "Blueprint",
    "RequestContextMiddleware",
    "render_template",
    "redirect",
    "send_from_directory",
]

