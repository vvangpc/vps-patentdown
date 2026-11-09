"""Patent PDF relay. Public API: GET /health, POST /download (Bearer token).
Run under Gunicorn; configuration is supplied by systemd.
"""
import hmac
import logging
import html as html_module
import os
import re
import threading
import time
from tempfile import SpooledTemporaryFile
from urllib.parse import urljoin, urlsplit

import requests
from flask import Flask, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
if not AUTH_TOKEN or AUTH_TOKEN == "CHANGE_ME_TO_A_RANDOM_TOKEN":
    raise RuntimeError("AUTH_TOKEN must be configured")
MAX_PDF_BYTES = int(os.environ.get("MAX_PDF_BYTES", str(64 * 1024 * 1024)))
MAX_HTML_BYTES = 8 * 1024 * 1024
FETCH_BUDGET = float(os.environ.get("FETCH_BUDGET_SECONDS", "100"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
if min(MAX_PDF_BYTES, FETCH_BUDGET, MAX_CONCURRENT) <= 0:
    raise RuntimeError("Download limits must be positive")
MIN_PDF_SIZE = 50 * 1024
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
LOCAL = threading.local()

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": _UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    # 去掉 br：避免 VPS 上缺 brotli 解码库导致响应体乱码
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://patents.google.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

PDF_PATTERN = r'https://patentimages\.storage\.googleapis\.com/[^"\'>\s]+\.pdf'

CITATION_PATTERN = (
    r'<meta\s+name=["\']citation_pdf_url["\']\s+content=["\']([^"\']+)["\']')

def _extract_pdf_url(html):
    """优先解析 citation_pdf_url meta 标签，兜底扫描 patentimages 直链。"""
    m = re.search(CITATION_PATTERN, html, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(PDF_PATTERN, html)
    return m.group(0) if m else None

def _is_blocked_page(html, final_url=""):
    """是否为 Google 拦截页（与客户端 downloader.is_blocked_page 一致）。

    不能简单搜 "captcha"：主题与验证码相关的专利（如 US8494854B2）正文里就有这个词。
    拦截页两种措辞：带验证码的 "unusual traffic"，以及直接拒绝的 "sending automated queries"。
    """
    low = html.lower()
    return ("/sorry/" in (final_url or "")
            or "unusual traffic" in low
            or "sending automated queries" in low
            or 'id="captcha-form"' in low)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.logger.setLevel(logging.INFO)

class FetchError(Exception):
    def __init__(self, message, code="failed"):
        super().__init__(message)
        self.code = code

def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise FetchError("上游下载超时，请稍后重试", "timeout")
    return seconds

def _session():
    if not hasattr(LOCAL, "session"):
        LOCAL.session = requests.Session()
        LOCAL.session.headers.update(HEADERS)
    return LOCAL.session

def _valid_upstream(url, hosts):
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname in hosts
                and parts.port in (None, 443) and not parts.username
                and not parts.password and not parts.fragment)
    except ValueError:
        return False

def _open(url, hosts, deadline):
    """Validate every redirect before opening its target."""
    for _ in range(6):
        if not _valid_upstream(url, hosts):
            raise FetchError("上游返回了不受支持的下载地址")
        left = _remaining(deadline)
        response = _session().get(url, stream=True, allow_redirects=False,
                                  timeout=(min(5, left), min(15, left)))
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise FetchError("上游重定向缺少地址")
        url = urljoin(url, location)
    raise FetchError("上游重定向次数过多")

def _copy_limited(response, target, limit, deadline):
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        _remaining(deadline)
        size += len(chunk)
        if size > limit:
            raise FetchError("上游文件超过服务大小限制", "too_large")
        target.write(chunk)
    target.seek(0)
    return size

def _get_page(url, deadline):
    for attempt, delay in enumerate((2, 5, 0)):
        with _open(url, {"patents.google.com"}, deadline) as response:
            with SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as body:
                _copy_limited(response, body, MAX_HTML_BYTES, deadline)
                page = body.read().decode(response.encoding or "utf-8", errors="replace")
            status, final_url = response.status_code, response.url
        if (status not in (429, 503) or attempt == 2
                or _is_blocked_page(page, final_url)):
            return status, page, final_url
        if _remaining(deadline) <= delay:
            raise FetchError("上游请求超时，请稍后重试", "timeout")
        time.sleep(delay)

def fetch_pdf(patent_number):
    deadline = time.monotonic() + FETCH_BUDGET
    url = f"https://patents.google.com/patent/{patent_number}/en"
    try:
        status, page, final_url = _get_page(url, deadline)
        pdf_url = _extract_pdf_url(page)
        if _is_blocked_page(page, final_url) and not pdf_url:
            raise FetchError("Google 暂时拒绝自动请求，请稍后重试", "denied")
        if status in (429, 503):
            raise FetchError("Google 限流，请稍后重试", "blocked")
        if status == 404:
            raise FetchError("Google Patents 无此公开号，请核对号码及类型码", "not_found")
        if status != 200:
            raise FetchError(f"上游页面返回 HTTP {status}")
        if not pdf_url:
            raise FetchError("该专利未提供 PDF，请使用其他专利数据渠道", "no_pdf")
        pdf_url = html_module.unescape(pdf_url)
        target = SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b")
        try:
            with _open(pdf_url, {"patentimages.storage.googleapis.com"}, deadline) as response:
                if response.status_code != 200:
                    raise FetchError(f"上游 PDF 返回 HTTP {response.status_code}")
                size = _copy_limited(response, target, MAX_PDF_BYTES, deadline)
            if not target.read(5).startswith(b"%PDF-"):
                raise FetchError("上游返回内容不是有效 PDF")
            if size < MIN_PDF_SIZE:
                raise FetchError("上游 PDF 文件过小，可能不完整")
            target.seek(0)
            return target, size
        except BaseException:
            target.close()
            raise
    except requests.exceptions.Timeout as exc:
        raise FetchError("上游连接或读取超时，请稍后重试", "timeout") from exc
    except requests.exceptions.RequestException as exc:
        raise FetchError("上游连接失败，请稍后重试", "blocked") from exc

def _error(message, status, code):
    return jsonify(ok=False, error=message, code=code), status

@app.after_request
def _no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@app.errorhandler(RequestEntityTooLarge)
def _too_large(_error):
    return jsonify(ok=False, error="请求体过大", code="invalid_request"), 413

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.post("/download")
def download():
    auth = request.headers.get("Authorization", "")
    if (not auth.startswith("Bearer ")
            or not hmac.compare_digest(auth[7:].encode("utf-8"), AUTH_TOKEN.encode("utf-8"))):
        return _error("未授权（token 错误或缺失）", 401, "unauthorized")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("请求体必须是 JSON 对象", 400, "invalid_request")
    number = body.get("patent_number")
    if number is None or number == "":
        return _error("缺少 patent_number", 400, "invalid_request")
    if not isinstance(number, str) or len(number) > 100:
        return _error("patent_number 必须是公开号字符串", 400, "invalid_request")
    number = number.strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{1,30}", number, flags=re.ASCII):
        return _error("公开号格式无效", 400, "invalid_request")
    if not SLOTS.acquire(blocking=False):
        response = jsonify(ok=False, error="下载服务繁忙，请稍后重试", code="busy")
        response.status_code = 429
        response.headers["Retry-After"] = "5"
        return response
    started = time.monotonic()
    try:
        pdf, size = fetch_pdf(number)
        try:
            response = send_file(pdf, mimetype="application/pdf", as_attachment=True,
                                 download_name=f"{number}.pdf", conditional=False)
            response.content_length = size
            response.headers["X-Patent-Number"] = number
            response.call_on_close(pdf.close)
            # Retain the slot until the WSGI iterable closes, including slow clients.
            original_iterable = response.response
            from werkzeug.wsgi import ClosingIterator
            response.response = ClosingIterator(original_iterable, SLOTS.release)
        except BaseException:
            pdf.close()
            raise
        app.logger.info("download %s bytes=%s seconds=%.2f", number, size, time.monotonic()-started)
        return response
    except FetchError as exc:
        SLOTS.release()
        app.logger.warning("download %s code=%s", number, exc.code)
        return _error(str(exc), 502, exc.code)
    except Exception:
        SLOTS.release()
        app.logger.exception("download %s unexpected failure", number)
        return _error("服务暂时无法完成下载，请稍后重试", 500, "internal_error")

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
