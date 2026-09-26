"""HTTP hardening, following the OWASP Secure Headers Project and the OWASP API Security Top 10.

One middleware, applied to every response (including early rejections):
  * security headers: HSTS (HTTPS only), a strict Content-Security-Policy for the app and a separate, narrower
    exception for the Swagger UI at /docs, frame/sniffing/referrer/permissions/cross-origin isolation policies
  * Cache-Control: no-store on API responses (they carry per-visitor history and decisions)
  * request-size limits on write requests (API4: unrestricted resource consumption)
  * a general per-client request rate limit across all routes, on top of the stricter analysis limit
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from app.limits import RequestRateLimiter

MAX_BODY_BYTES = 64 * 1024  # a claim is < 1 KB; anything near this is abuse

# The app serves only same-origin assets: no inline scripts or styles, no third parties.
APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
# Swagger UI (FastAPI's /docs) loads its bundle from jsDelivr and bootstraps with one inline script.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
DOCS_PATHS = ("/docs", "/docs/oauth2-redirect")
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
    "payment=(), usb=(), browsing-topics=()"
)


def _is_api(path: str) -> bool:
    return not (path == "/" or path.startswith("/static/") or path in DOCS_PATHS)


def _headers(request: Request) -> dict[str, str]:
    https = request.url.scheme == "https"
    docs = request.url.path in DOCS_PATHS
    csp = DOCS_CSP if docs else APP_CSP
    headers = {
        "Content-Security-Policy": csp + ("; upgrade-insecure-requests" if https else ""),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": PERMISSIONS_POLICY,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if not docs:  # Swagger's CDN assets would need CORP headers we don't control
        headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    if https:  # browsers ignore HSTS over plain HTTP; sending it there would only confuse local runs
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if _is_api(request.url.path):
        headers["Cache-Control"] = "no-store"
    if request.url.path.startswith("/admin"):
        headers["X-Robots-Tag"] = "noindex, nofollow"
        headers["Cache-Control"] = "no-store"
    return headers


def install(app, limiter: RequestRateLimiter) -> None:
    @app.middleware("http")
    async def security(request: Request, call_next):
        response = _reject(request, limiter) or await call_next(request)
        response.headers.update(_headers(request))
        return response


def _reject(request: Request, limiter: RequestRateLimiter) -> JSONResponse | None:
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        return JSONResponse({"detail": "Too many requests. Please slow down."}, status_code=429,
                            headers={"Retry-After": "60"})
    if request.method in ("POST", "PUT", "PATCH"):
        length = request.headers.get("content-length")
        if length is None:
            return JSONResponse({"detail": "Content-Length required."}, status_code=411)
        if not length.isdigit() or int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": f"Request body too large (max {MAX_BODY_BYTES} bytes)."}, status_code=413)
    return None
