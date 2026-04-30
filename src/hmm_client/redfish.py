from __future__ import annotations

import contextlib
from typing import Any

import httpx


class RedfishClient:
    """Minimal DMTF Redfish client: session login, GET, members helper."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        verify: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self._user = user
        self._password = password
        self._client = httpx.Client(
            base_url=f"https://{host}",
            verify=verify,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._session_url: str | None = None

    def login(self) -> None:
        r = self._client.post(
            "/redfish/v1/SessionService/Sessions",
            json={"UserName": self._user, "Password": self._password},
        )
        r.raise_for_status()
        token = r.headers.get("X-Auth-Token")
        if not token:
            raise RuntimeError("Redfish login succeeded but no X-Auth-Token returned")
        self._session_url = r.headers.get("Location") or r.json().get("@odata.id")
        self._client.headers["X-Auth-Token"] = token

    def logout(self) -> None:
        if self._session_url:
            with contextlib.suppress(Exception):
                self._client.delete(self._session_url)
        self._session_url = None
        self._client.headers.pop("X-Auth-Token", None)

    def get(self, path: str) -> dict[str, Any]:
        r = self._client.get(path)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def members(collection: dict[str, Any]) -> list[str]:
        return [m["@odata.id"] for m in collection.get("Members", []) if "@odata.id" in m]

    def __enter__(self) -> "RedfishClient":
        self.login()
        return self

    def __exit__(self, *_: object) -> None:
        self.logout()
        self._client.close()
