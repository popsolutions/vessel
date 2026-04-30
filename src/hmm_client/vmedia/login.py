"""HMM Web login + per-session embed extraction.

The HMM Web UI auth is a separate context from Redfish. Flow:

  1. GET  /login.html        (server sets SESSID cookie)
  2. POST /loginhandler.php  (actiontype=login + creds; returns XML with csrftoken)
  3. GET  /kvm.html          (returns HTML with <embed> containing per-session keys)

The <embed> attributes contain the AES keys, IV, verify ints, handshake port
(2198), and VMM base port (8500 - per-blade port = base + slot).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

from .crypto import parse_codekey_ext, parse_secretiv, parse_secretkey

_EMBED_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


@dataclass(frozen=True)
class Session:
    """Per-session VirtualMedia/KVM keys + endpoint base."""

    host: str
    handshake_port: int       # `port` attr (2198)
    vmm_base_port: int        # `vmmServerPort` (8500); per-blade = base + slot
    verifyvalue: int          # codekey int (KVM stream)
    verifyvalueext: bytes     # codekey_ext bytes (VirtualMedia, 16 bytes)
    aes_key_kvm: bytes        # 16 bytes
    aes_key_vmedia: bytes     # 16 bytes (= codekey_ext bytes)
    aes_iv: bytes             # 16 bytes
    csrftoken: str
    sessid: str
    secure: bool
    typedata: str
    shelftype: str

    def vmedia_port(self, slot: int) -> int:
        if not 1 <= slot <= 32:
            raise ValueError(f"slot must be 1..32, got {slot}")
        return self.vmm_base_port + slot


def _parse_embed(html: str) -> dict[str, str]:
    m = re.search(r"<embed\b[^>]*>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        raise RuntimeError("no <embed> in kvm.html")
    return {k.lower(): v for k, v in _EMBED_ATTR_RE.findall(m.group(0))}


def login(host: str, user: str, password: str, *, verify_tls: bool = False) -> Session:
    """Run the full login + embed-extract flow against the HMM web."""
    with httpx.Client(
        verify=verify_tls, timeout=15.0,
        base_url=f"https://{host}", follow_redirects=False,
    ) as c:
        # 1. seed SESSID cookie
        c.get("/login.html")

        # 2. authenticate
        r = c.post("/loginhandler.php", data={
            "actiontype": "login",
            "username": user,
            "userpasswd": password,
            "usermode": "1",
            "code": "",
            "language": "en",
        })
        r.raise_for_status()
        try:
            root = ET.fromstring(r.text)
            retcode = int(root.findtext("retcode", "1"))
            csrftoken = root.findtext("csrftoken", "") or ""
        except ET.ParseError as e:
            raise RuntimeError(f"loginhandler returned non-XML: {r.text[:200]}") from e
        if retcode != 0:
            desp = root.findtext("desp", "")
            raise RuntimeError(f"login failed: retcode={retcode} {desp}")
        sessid = c.cookies.get("SESSID", "")

        # 3. fetch the kvm embed
        r = c.get("/kvm.html", headers={"X-CSRF-Token": csrftoken})
        r.raise_for_status()
        emb = _parse_embed(r.text)

    verifyvalue, aes_key_kvm = parse_secretkey(emb["secretkey"])
    aes_iv = parse_secretiv(emb["secretiv"])
    aes_key_vmedia = parse_codekey_ext(emb["codekey_ext"])
    verifyvalueext = parse_secretiv(emb["verifyvalueext"])  # 16 bytes hex

    return Session(
        host=host,
        handshake_port=int(emb["port"]),
        vmm_base_port=int(emb["vmmserverport"]),
        verifyvalue=verifyvalue,
        verifyvalueext=verifyvalueext,
        aes_key_kvm=aes_key_kvm,
        aes_key_vmedia=aes_key_vmedia,
        aes_iv=aes_iv,
        csrftoken=csrftoken,
        sessid=sessid,
        secure=emb.get("securekvm", "0") == "1",
        typedata=emb.get("typedata", ""),
        shelftype=emb.get("shelftype", ""),
    )
