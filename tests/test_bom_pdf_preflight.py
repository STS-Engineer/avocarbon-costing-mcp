import io
import urllib.error

from services import agent_file_proxy_service as proxy
from services import choke_sequential_agent_workflow as workflow


class _Response:
    def __init__(
        self,
        url,
        *,
        status=200,
        content_type="application/pdf",
        body=b"%PDF-1.7\n",
        final_url=None,
    ):
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self._body = body
        self._url = url
        self._final_url = final_url or url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        return self._body

    def geturl(self):
        return self._final_url


def _signed_url(monkeypatch, lifetime=3600):
    monkeypatch.setenv("AGENT_FILE_SIGNING_SECRET", "test-secret")
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000)
    return proxy.build_agent_file_url(
        "https://backend.example.test",
        "P",
        "drawing.pdf",
        expiry_seconds=lifetime,
    )


def test_valid_pdf_preflight_reports_safe_metadata(monkeypatch):
    url = _signed_url(monkeypatch)
    monkeypatch.setattr(
        proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(url),
    )

    result = proxy.verify_agent_pdf_url(url)

    assert result["success"] is True
    assert result["http_status"] == 200
    assert result["content_type"] == "application/pdf"
    assert result["content_length"] > 0
    assert result["pdf_signature_present"] is True
    assert result["safe_url_path"].endswith("/P/drawing.pdf")
    assert "token" not in result["safe_url_path"]
    assert result["remaining_token_lifetime_seconds"] == 3600


def test_url_with_less_than_30_minutes_is_rejected_before_get(monkeypatch):
    url = _signed_url(monkeypatch)
    token_expiry = 1_000 + 1_799
    token = f"{token_expiry}.{proxy._signature('P', 'drawing.pdf', token_expiry)}"
    url = url.split("?", 1)[0] + f"?token={token}"
    called = []
    monkeypatch.setattr(
        proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: called.append(True),
    )

    result = proxy.verify_agent_pdf_url(url)

    assert result["success"] is False
    assert result["error_code"] == "drawing_url_expired"
    assert called == []


def test_forbidden_url_has_specific_error(monkeypatch):
    url = _signed_url(monkeypatch)

    def forbidden(request, timeout):
        raise urllib.error.HTTPError(
            url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b""),
        )

    monkeypatch.setattr(proxy.urllib.request, "urlopen", forbidden)

    result = proxy.verify_agent_pdf_url(url)

    assert result["error_code"] == "drawing_url_forbidden"


def test_non_pdf_content_is_rejected(monkeypatch):
    url = _signed_url(monkeypatch)
    monkeypatch.setattr(
        proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            url,
            content_type="text/html",
            body=b"<html>login</html>",
        ),
    )

    result = proxy.verify_agent_pdf_url(url)

    assert result["error_code"] == "drawing_not_pdf"


def test_empty_pdf_is_rejected(monkeypatch):
    url = _signed_url(monkeypatch)
    monkeypatch.setattr(
        proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(url, body=b""),
    )

    result = proxy.verify_agent_pdf_url(url)

    assert result["error_code"] == "drawing_empty"


def test_authentication_redirect_is_rejected(monkeypatch):
    url = _signed_url(monkeypatch)
    monkeypatch.setattr(
        proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            url,
            final_url="https://login.example.test/sign-in",
        ),
    )

    result = proxy.verify_agent_pdf_url(url)

    assert result["error_code"] == "drawing_url_forbidden"
    assert result["redirected"] is True


def test_partial_content_is_not_accepted(monkeypatch):
    url = _signed_url(monkeypatch)
    monkeypatch.setattr(
        proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(url, status=206),
    )

    result = proxy.verify_agent_pdf_url(url)

    assert result["success"] is False


def test_bom_payload_preserves_ids_and_omits_backend_save_address(monkeypatch):
    monkeypatch.setenv("AGENT_FILE_SIGNING_SECRET", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://backend.example.test")
    payload = workflow._build_bom_trigger_payload(
        "PROJECT",
        "PRODUCT",
        {
            "drawing_file_path": (
                "data/customer_inputs/uploads/PROJECT/drawing.pdf"
            ),
            "drawing_reference": "drawing.pdf",
        },
        trigger_run_id="RUN-ID",
    )

    outbound = payload["payload"]
    assert outbound["project_code"] == "PROJECT"
    assert outbound["product_id"] == "PRODUCT"
    assert outbound["trigger_run_id"] == "RUN-ID"
    assert "save_address" not in outbound
    assert payload["save_address"]

