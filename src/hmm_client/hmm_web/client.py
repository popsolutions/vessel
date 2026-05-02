"""HMM proprietary web-API low-level client.

Wraps the ``loginhandler.php`` flow + the common ``Custom-Token`` /
``Cookie: SESSID=...`` envelope used by every other dispatcher. Higher
modules (``inventory``, ``firmware``) build typed verbs on top.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from types import TracebackType
from typing import Any
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger(__name__)

_CSRF_RE = re.compile(r"<csrftoken>([^<]+)</csrftoken>")


class HMMWebError(RuntimeError):
    """Raised when a dispatcher returns a non-zero ``retcode`` or HTTP fails.

    The exception carries the parsed retcode and the dispatcher's
    description so callers can branch on specific values.
    """

    def __init__(self, message: str, *, retcode: int | None = None, desp: str = "") -> None:
        super().__init__(message)
        self.retcode = retcode
        self.desp = desp


@dataclass(frozen=True)
class HMMWebResult:
    """Parsed response from a dispatcher call."""

    retcode: int | None
    desp: str
    body: str
    root: ET.Element | None

    def raise_for_retcode(self) -> None:
        if self.retcode not in (0, None):
            raise HMMWebError(
                f"dispatcher returned retcode={self.retcode} ({self.desp!r})",
                retcode=self.retcode,
                desp=self.desp,
            )


def _parse_xml(body: str) -> tuple[int | None, str, ET.Element | None]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, "", None
    rc_el = root.find("retcode")
    desp_el = root.find("desp")
    rc = (
        int(rc_el.text)
        if rc_el is not None and rc_el.text and rc_el.text.lstrip("-").isdigit()
        else None
    )
    desp = desp_el.text or "" if desp_el is not None else ""
    return rc, desp, root


class HMMWebClient:
    """Synchronous client that holds login state across calls.

    Use as a context manager so the underlying ``httpx.Client`` is
    closed deterministically::

        with HMMWebClient.from_settings(settings) as c:
            c.login()
            print(c.post("versionhandler.php",
                         actiontype="get",
                         bladename="Swi2", fruid="0").body)
    """

    DEFAULT_TIMEOUT = 30.0

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        verify_tls: bool = False,
        timeout: float | None = None,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._base = f"https://{host}"
        self._http = httpx.Client(
            verify=verify_tls,
            timeout=timeout if timeout is not None else self.DEFAULT_TIMEOUT,
            follow_redirects=False,
        )
        self._csrftoken: str | None = None

    # ----- lifecycle -----

    def __enter__(self) -> "HMMWebClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @classmethod
    def from_settings(cls, settings: Any) -> "HMMWebClient":
        return cls(
            host=settings.hmm_host,
            username=settings.hmm_user,
            password=settings.hmm_password,
            verify_tls=settings.verify_tls,
        )

    # ----- auth -----

    @property
    def is_logged_in(self) -> bool:
        return self._csrftoken is not None

    def login(self) -> None:
        self._http.post(
            f"{self._base}/loginhandler.php",
            data={"actiontype": "queryverify", "chassisid": "0"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        ).raise_for_status()
        r = self._http.post(
            f"{self._base}/loginhandler.php",
            data={
                "actiontype": "login",
                "username": self._username,
                "userpasswd": self._password,
                "usermode": "1",
                "code": "",
                "language": "en",
            },
            headers={"Custom-Token": "0", "X-Requested-With": "XMLHttpRequest"},
        )
        r.raise_for_status()
        m = _CSRF_RE.search(r.text)
        if not m:
            raise HMMWebError(
                f"login: no csrftoken in response (first 200 chars): {r.text[:200]!r}"
            )
        self._csrftoken = m.group(1)
        log.info("hmm-web logged in to %s", self._host)

    def logout(self) -> None:
        self._csrftoken = None
        self._http.cookies.clear()

    # ----- core dispatcher call -----

    def _common_headers(self, *, referer_path: str = "/index.html?chassisid=0") -> dict[str, str]:
        if self._csrftoken is None:
            raise HMMWebError("not logged in (call .login() first)")
        return {
            "Custom-Token": self._csrftoken,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self._base,
            "Referer": f"{self._base}{referer_path}",
        }

    def post(
        self,
        handler: str,
        *,
        referer_path: str = "/index.html?chassisid=0",
        **params: str,
    ) -> HMMWebResult:
        """POST a dispatcher call. Returns parsed result; does not raise on retcode≠0."""
        if not handler.endswith(".php"):
            handler = handler + ".php"
        r = self._http.post(
            f"{self._base}/{handler}",
            data=params,
            headers=self._common_headers(referer_path=referer_path),
        )
        r.raise_for_status()
        rc, desp, root = _parse_xml(r.text)
        return HMMWebResult(retcode=rc, desp=desp, body=r.text, root=root)

    def post_multipart(
        self,
        handler: str,
        *,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, str] | None = None,
        referer_path: str = "/index.html?chassisid=0",
    ) -> HMMWebResult:
        """POST a multipart upload (firmware images)."""
        if not handler.endswith(".php"):
            handler = handler + ".php"
        if self._csrftoken is None:
            raise HMMWebError("not logged in (call .login() first)")
        headers = {
            "Custom-Token": self._csrftoken,
            "Origin": self._base,
            "Referer": f"{self._base}{referer_path}",
        }
        r = self._http.post(
            f"{self._base}/{handler}",
            data=data or {},
            files=files,
            headers=headers,
        )
        r.raise_for_status()
        rc, desp, root = _parse_xml(r.text)
        return HMMWebResult(retcode=rc, desp=desp, body=r.text, root=root)
