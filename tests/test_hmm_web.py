"""Tests for the hmm_client.hmm_web package."""

from __future__ import annotations

import pytest

from hmm_client.hmm_web import (
    ComponentVersion,
    FirmwareModule,
    HMMWebClient,
    HMMWebError,
    InventoryModule,
    UpgradeStatus,
    UpgradeTarget,
)
from hmm_client.hmm_web.firmware import _parse_status, bladelist
from hmm_client.hmm_web.inventory import _decode_version_block


@pytest.mark.unit
class TestBladelist:
    def test_smm_pair_uses_alias(self):
        assert bladelist([UpgradeTarget.smm_pair()]) == "bothsmm"

    def test_single_switch_terminated(self):
        assert bladelist([UpgradeTarget.switch("Swi2")]) == "Swi2:fru0;"

    def test_two_switches(self):
        out = bladelist([UpgradeTarget.switch("Swi2"), UpgradeTarget.switch("Swi3")])
        assert out == "Swi2:fru0;Swi3:fru0;"

    def test_blade_helper(self):
        assert UpgradeTarget.blade(7).to_token() == "Slot7:fru0"

    def test_custom_fruid(self):
        assert UpgradeTarget.switch("Swi2", fruid="1").to_token() == "Swi2:fru1"


_VERSION_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<result><retcode>0</retcode><desp>The operation succeeded</desp>"
    "<version>"
    "IPMC               CPU:           SPEAr310&lt;br&gt;"
    "BIOS           Version:           &amp;#40;U41&amp;#41;V501&lt;br&gt;"
    "Active iMana   Version:           &amp;#40;U4005&amp;#41;6.05&lt;br&gt;"
    "CPLD           Version:           &amp;#40;U38&amp;#41;023"
    "</version></result>"
)


@pytest.mark.unit
class TestDecodeVersionBlock:
    def test_extracts_fields(self):
        raw, fields = _decode_version_block(_VERSION_XML)
        assert "BIOS" in raw
        assert fields["BIOS Version"] == "(U41)V501"
        assert fields["Active iMana Version"] == "(U4005)6.05"
        assert fields["CPLD Version"] == "(U38)023"

    def test_returns_empty_on_no_version_tag(self):
        raw, fields = _decode_version_block("<result><retcode>0</retcode></result>")
        assert raw == ""
        assert fields == {}

    def test_handles_html_entities(self):
        raw, _ = _decode_version_block(_VERSION_XML)
        assert "(U41)" in raw
        assert "&amp;" not in raw


_STATUS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<upgrade>'
    '<blade name="smm"><retcode>0</retcode><desp>The operation succeeded</desp>'
    '<progress>42</progress><progressdesp>Upgrading...</progressdesp></blade>'
    '<blade name="othersmm"><retcode>0</retcode><desp>The operation succeeded</desp>'
    '<progress>37</progress><progressdesp>Upgrading...</progressdesp></blade>'
    '</upgrade>'
)

_STATUS_DONE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<upgrade>'
    '<blade name="Swi2:fru0"><retcode>0</retcode><desp>OK</desp>'
    '<progress>100</progress><progressdesp>Done</progressdesp></blade>'
    '</upgrade>'
)

_STATUS_FAIL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<upgrade>'
    '<blade name="Slot1"><retcode>5</retcode><desp>Image checksum mismatch</desp>'
    '<progress>0</progress><progressdesp>Failed</progressdesp></blade>'
    '</upgrade>'
)


@pytest.mark.unit
class TestStatusParser:
    def test_in_progress_two_targets(self):
        st = _parse_status(_STATUS_XML)
        assert len(st.targets) == 2
        assert st.targets[0].name == "smm"
        assert st.targets[0].progress == 42
        assert st.targets[1].progress == 37
        assert not st.all_done
        assert not st.any_failed

    def test_done_target(self):
        st = _parse_status(_STATUS_DONE_XML)
        assert st.targets[0].progress == 100
        assert st.targets[0].is_terminal
        assert st.all_done
        assert not st.any_failed

    def test_failed_target(self):
        st = _parse_status(_STATUS_FAIL_XML)
        assert st.targets[0].retcode == 5
        assert st.any_failed
        assert st.all_done

    def test_unparseable_raises(self):
        with pytest.raises(HMMWebError):
            _parse_status("not xml at all <<")


_LOGIN_VERIFY_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<result><retcode>0</retcode><verify>0</verify></result>"
)
_LOGIN_OK_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<result>"
    "<csrftoken>fake-csrf-abcdef</csrftoken>"
    "<retcode>0</retcode><desp>OK</desp>"
    "</result>"
)


@pytest.mark.unit
class TestClientLogin:
    def test_login_extracts_csrftoken(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_VERIFY_XML
        )
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_OK_XML
        )
        c = HMMWebClient("1.2.3.4", "root", "secret")
        try:
            assert not c.is_logged_in
            c.login()
            assert c.is_logged_in
        finally:
            c.close()

    def test_login_raises_when_csrf_missing(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_VERIFY_XML
        )
        httpx_mock.add_response(
            method="POST",
            url="https://1.2.3.4/loginhandler.php",
            text="<result><retcode>0</retcode></result>",
        )
        c = HMMWebClient("1.2.3.4", "root", "secret")
        try:
            with pytest.raises(HMMWebError):
                c.login()
        finally:
            c.close()

    def test_post_requires_login(self):
        c = HMMWebClient("1.2.3.4", "root", "secret")
        try:
            with pytest.raises(HMMWebError):
                c.post("queryhandler.php", actiontype="status", chassisid="0")
        finally:
            c.close()


@pytest.mark.unit
class TestInventoryModule:
    def test_get_version_returns_present_false_on_400(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_VERIFY_XML
        )
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_OK_XML
        )
        httpx_mock.add_response(
            method="POST",
            url="https://1.2.3.4/versionhandler.php",
            status_code=400,
            text="<result><retcode>5</retcode></result>",
        )
        c = HMMWebClient("1.2.3.4", "root", "secret")
        try:
            c.login()
            cv = InventoryModule(c).get_version("Fan1")
            assert isinstance(cv, ComponentVersion)
            assert cv.present is False
            assert cv.fields == {}
        finally:
            c.close()

    def test_get_version_parses_present(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_VERIFY_XML
        )
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_OK_XML
        )
        httpx_mock.add_response(
            method="POST",
            url="https://1.2.3.4/versionhandler.php",
            text=_VERSION_XML,
        )
        c = HMMWebClient("1.2.3.4", "root", "secret")
        try:
            c.login()
            cv = InventoryModule(c).get_version("Slot1")
            assert cv.present is True
            assert cv.fields["BIOS Version"] == "(U41)V501"
        finally:
            c.close()


@pytest.mark.unit
class TestFirmwareModule:
    def test_upload_rejects_empty(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_VERIFY_XML
        )
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_OK_XML
        )
        c = HMMWebClient("1.2.3.4", "root", "secret")
        try:
            c.login()
            with pytest.raises(HMMWebError):
                FirmwareModule(c).upload("x.hpm", b"")
        finally:
            c.close()

    def test_apply_rejects_no_targets(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_VERIFY_XML
        )
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_OK_XML
        )
        c = HMMWebClient("1.2.3.4", "root", "secret")
        try:
            c.login()
            with pytest.raises(HMMWebError):
                FirmwareModule(c).apply([])
        finally:
            c.close()

    def test_status_returns_typed_progress(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_VERIFY_XML
        )
        httpx_mock.add_response(
            method="POST", url="https://1.2.3.4/loginhandler.php", text=_LOGIN_OK_XML
        )
        httpx_mock.add_response(
            method="POST",
            url="https://1.2.3.4/smmupgradehandler.php",
            text=_STATUS_XML,
        )
        c = HMMWebClient("1.2.3.4", "root", "secret")
        try:
            c.login()
            st = FirmwareModule(c).status()
            assert isinstance(st, UpgradeStatus)
            assert len(st.targets) == 2
            assert st.targets[0].name == "smm"
        finally:
            c.close()
