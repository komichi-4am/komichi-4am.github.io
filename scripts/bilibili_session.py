#!/usr/bin/env python3
"""Persistent, read-only Bilibili web session support.

The cookie file is a local secret. This module never prints cookie values and
stores the file with user-only permissions. It uses only Python's standard
library so scheduled runs do not need a separate runtime.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_COOKIE_PATH = Path("~/.config/bilibili/cookies.json").expanduser()
BILIBILI_HOME_URL = "https://www.bilibili.com/"
BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILIBILI_QR_GENERATE_URL = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
)
BILIBILI_QR_POLL_URL = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
BROWSER_SEC_CH_UA = (
    '"Chromium";v="148", "Google Chrome";v="148", "Not_A Brand";v="99"'
)


class BilibiliSessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def browser_headers(
    *,
    referer: str,
    origin: str,
    accept: str = "application/json, text/plain, */*",
) -> dict[str, str]:
    return {
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": origin,
        "Referer": referer,
        "User-Agent": BROWSER_USER_AGENT,
        "sec-ch-ua": BROWSER_SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "priority": "u=1, i",
    }


def _cookie_record(cookie: http.cookiejar.Cookie) -> dict[str, Any]:
    return {
        "version": cookie.version,
        "name": cookie.name,
        "value": cookie.value,
        "port": cookie.port,
        "portSpecified": cookie.port_specified,
        "domain": cookie.domain,
        "domainSpecified": cookie.domain_specified,
        "domainInitialDot": cookie.domain_initial_dot,
        "path": cookie.path,
        "pathSpecified": cookie.path_specified,
        "secure": cookie.secure,
        "expires": cookie.expires,
        "discard": cookie.discard,
        "comment": cookie.comment,
        "commentUrl": cookie.comment_url,
        "rest": dict(getattr(cookie, "_rest", {}) or {}),
        "rfc2109": cookie.rfc2109,
    }


def _cookie_from_record(record: dict[str, Any]) -> http.cookiejar.Cookie:
    required = ("name", "value", "domain", "path")
    if any(not isinstance(record.get(key), str) for key in required):
        raise BilibiliSessionError(
            "bilibili_cookie_invalid", "Bilibili cookie file contains an invalid cookie"
        )
    return http.cookiejar.Cookie(
        version=int(record.get("version", 0)),
        name=record["name"],
        value=record["value"],
        port=record.get("port"),
        port_specified=bool(record.get("portSpecified", False)),
        domain=record["domain"],
        domain_specified=bool(record.get("domainSpecified", True)),
        domain_initial_dot=bool(record.get("domainInitialDot", False)),
        path=record["path"],
        path_specified=bool(record.get("pathSpecified", True)),
        secure=bool(record.get("secure", False)),
        expires=record.get("expires"),
        discard=bool(record.get("discard", False)),
        comment=record.get("comment"),
        comment_url=record.get("commentUrl"),
        rest=record.get("rest") if isinstance(record.get("rest"), dict) else {},
        rfc2109=bool(record.get("rfc2109", False)),
    )


def _prepare_secret_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError as exc:
        raise BilibiliSessionError(
            "bilibili_cookie_permissions",
            f"Could not secure Bilibili cookie directory: {path.parent}",
        ) from exc


class BilibiliSession:
    def __init__(
        self,
        cookie_jar: http.cookiejar.CookieJar | None = None,
        *,
        account: dict[str, Any] | None = None,
    ) -> None:
        self.cookie_jar = cookie_jar or http.cookiejar.CookieJar()
        self.account = account
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    @classmethod
    def empty(cls) -> "BilibiliSession":
        return cls()

    @classmethod
    def load(cls, path: Path = DEFAULT_COOKIE_PATH) -> "BilibiliSession":
        resolved = path.expanduser()
        try:
            with resolved.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError as exc:
            raise BilibiliSessionError(
                "bilibili_auth_missing",
                f"Bilibili login is not configured. Run bilibili_login.py login; expected {resolved}",
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BilibiliSessionError(
                "bilibili_cookie_invalid",
                f"Could not read Bilibili cookie file: {resolved}",
            ) from exc
        if not isinstance(document, dict) or document.get("schemaVersion") != 1:
            raise BilibiliSessionError(
                "bilibili_cookie_invalid",
                f"Unsupported Bilibili cookie file format: {resolved}",
            )
        records = document.get("cookies")
        if not isinstance(records, list) or not records:
            raise BilibiliSessionError(
                "bilibili_cookie_invalid", f"Bilibili cookie file is empty: {resolved}"
            )
        jar = http.cookiejar.CookieJar()
        for record in records:
            if not isinstance(record, dict):
                raise BilibiliSessionError(
                    "bilibili_cookie_invalid", "Bilibili cookie file contains invalid data"
                )
            jar.set_cookie(_cookie_from_record(record))
        try:
            resolved.chmod(0o600)
        except OSError as exc:
            raise BilibiliSessionError(
                "bilibili_cookie_permissions",
                f"Could not secure Bilibili cookie file: {resolved}",
            ) from exc
        account = document.get("account")
        return cls(jar, account=account if isinstance(account, dict) else None)

    def save(self, path: Path = DEFAULT_COOKIE_PATH) -> None:
        resolved = path.expanduser()
        _prepare_secret_directory(resolved)
        document = {
            "schemaVersion": 1,
            "updatedAt": utc_now(),
            "account": self.account,
            "cookies": [_cookie_record(cookie) for cookie in self.cookie_jar],
        }
        if not document["cookies"]:
            raise BilibiliSessionError(
                "bilibili_cookie_empty", "Refusing to save an empty Bilibili cookie session"
            )
        payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=resolved.parent, delete=False
            ) as handle:
                temp_path = Path(handle.name)
                os.chmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(resolved)
            resolved.chmod(0o600)
        except OSError as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise BilibiliSessionError(
                "bilibili_cookie_write_failed",
                f"Could not save Bilibili cookie session: {resolved}",
            ) from exc

    def _request_bytes(
        self,
        url: str,
        *,
        label: str,
        headers: dict[str, str],
        timeout: int = 25,
    ) -> bytes:
        request = urllib.request.Request(url, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
                payload = json.loads(body)
                detail = str(payload.get("message") or payload.get("msg") or exc.reason)
            except (json.JSONDecodeError, AttributeError, TypeError):
                detail = str(exc.reason)
            code = "bilibili_blocked" if exc.code == 412 else "bilibili_http_error"
            raise BilibiliSessionError(
                code, f"{label} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BilibiliSessionError(
                "bilibili_network_error", f"{label} network error: {exc.reason}"
            ) from exc

    def get_json(
        self,
        url: str,
        *,
        label: str,
        referer: str,
        origin: str,
        timeout: int = 25,
    ) -> dict[str, Any]:
        payload = self._request_bytes(
            url,
            label=label,
            headers=browser_headers(referer=referer, origin=origin),
            timeout=timeout,
        )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BilibiliSessionError(
                "bilibili_invalid_response", f"{label} returned invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise BilibiliSessionError(
                "bilibili_invalid_response", f"{label} returned a non-object response"
            )
        return value

    def check_login(self) -> dict[str, Any]:
        payload = self.get_json(
            BILIBILI_NAV_URL,
            label="Bilibili login check",
            referer=BILIBILI_HOME_URL,
            origin="https://www.bilibili.com",
        )
        code = payload.get("code")
        data = payload.get("data") or {}
        if code == -101 or not data.get("isLogin"):
            raise BilibiliSessionError(
                "bilibili_auth_expired",
                "Bilibili login has expired. Run bilibili_login.py login again.",
            )
        if code != 0:
            raise BilibiliSessionError(
                "bilibili_login_check_failed",
                f"Bilibili login check returned code {code}: {payload.get('message') or 'unknown'}",
            )
        self.account = {
            "mid": str(data.get("mid") or ""),
            "name": str(data.get("uname") or ""),
        }
        return dict(self.account)

    def refresh_home_session(self) -> None:
        self._request_bytes(
            BILIBILI_HOME_URL,
            label="Bilibili session refresh",
            headers=browser_headers(
                referer=BILIBILI_HOME_URL,
                origin="https://www.bilibili.com",
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            ),
        )


def generate_qr_challenge(session: BilibiliSession) -> tuple[str, str]:
    payload = session.get_json(
        BILIBILI_QR_GENERATE_URL,
        label="Bilibili QR generation",
        referer="https://passport.bilibili.com/login",
        origin="https://passport.bilibili.com",
    )
    if payload.get("code") != 0:
        raise BilibiliSessionError(
            "bilibili_qr_generate_failed",
            f"Bilibili QR generation returned code {payload.get('code')}",
        )
    data = payload.get("data") or {}
    login_url = data.get("url")
    key = data.get("qrcode_key")
    if not isinstance(login_url, str) or not isinstance(key, str) or not login_url or not key:
        raise BilibiliSessionError(
            "bilibili_qr_generate_failed", "Bilibili QR generation returned incomplete data"
        )
    return login_url, key


def poll_qr_challenge(session: BilibiliSession, key: str) -> tuple[str, str]:
    url = f"{BILIBILI_QR_POLL_URL}?{urllib.parse.urlencode({'qrcode_key': key})}"
    payload = session.get_json(
        url,
        label="Bilibili QR polling",
        referer="https://passport.bilibili.com/login",
        origin="https://passport.bilibili.com",
    )
    if payload.get("code") != 0:
        raise BilibiliSessionError(
            "bilibili_qr_poll_failed",
            f"Bilibili QR polling returned code {payload.get('code')}",
        )
    data = payload.get("data") or {}
    status_code = data.get("code")
    if status_code == 0:
        return "success", str(data.get("message") or "Login confirmed")
    if status_code == 86101:
        return "waiting_scan", str(data.get("message") or "Waiting for scan")
    if status_code == 86090:
        return "waiting_confirmation", str(
            data.get("message") or "Scanned; waiting for confirmation"
        )
    if status_code == 86038:
        return "expired", str(data.get("message") or "QR code expired")
    return "failed", f"Bilibili QR login returned status {status_code}"
