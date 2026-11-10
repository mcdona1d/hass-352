"""Offline regressions for the DNS failure and authentication chain."""

import asyncio
import gzip
import json
import ssl
from types import SimpleNamespace

import aiohttp
import pytest

from scripts._air352_loader import load_api

api = load_api()


class Response:
    def __init__(self, body, status=200):
        self.status = status
        self.body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def read(self):
        await asyncio.sleep(0)
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def success(payload):
    return Response({"code": 200, "data": payload})


def auth_responses(**session_fields):
    return [
        success({"access_token": "access-secret"}),
        success({"apiGatewayEndpoint": "iot.cn-shanghai.aliyuncs.com",
                 "oaApiGatewayEndpoint": "living-account.cn-shanghai.aliyuncs.com"}),
        success({"data": {"loginSuccessResult": {"sid": "sid-secret"}}}),
        success({"iotToken": "iot-secret", "refreshToken": "refresh-secret",
                 "iotTokenExpire": "72000", **session_fields}),
    ]


@pytest.mark.asyncio
async def test_session_uses_system_dns_even_with_broken_default_resolver(monkeypatch):
    def fail_default(*args, **kwargs):
        raise AssertionError("DefaultResolver/aiodns must not be used")

    monkeypatch.setattr(aiohttp.connector, "DefaultResolver", fail_default)
    async with api.create_client_session() as session:
        assert isinstance(session.connector._resolver, aiohttp.ThreadedResolver)
        assert session.connector._ssl is True
        assert not session.trust_env
    assert session.closed


@pytest.mark.asyncio
async def test_dns_timeout_is_reported_as_dns_error():
    key = SimpleNamespace(host="app.352air.com", port=443, ssl=True, is_ssl=True)
    error = aiohttp.ClientConnectorDNSError(key, OSError(None, "Timeout while contacting DNS servers"))
    client = api.Air352ApiClient(Session(error), "test-user", "test-password")
    with pytest.raises(api.Air352ConnectionError) as result:
        await client.authenticate()
    assert result.value.reason == "dns_error"
    assert "352 login" in str(result.value)
    assert "app.352air.com" in str(result.value)
    assert "test-password" not in str(result.value)


@pytest.mark.asyncio
async def test_certificate_failure_is_reported_without_disabling_verification():
    key = SimpleNamespace(host="app.352air.com", port=443, ssl=True, is_ssl=True)
    error = aiohttp.ClientConnectorCertificateError(key, ssl.SSLCertVerificationError("untrusted"))
    client = api.Air352ApiClient(Session(error), "", "")
    with pytest.raises(api.Air352ConnectionError) as result:
        await client.authenticate()
    assert result.value.reason == "ssl_error"


@pytest.mark.asyncio
async def test_authentication_and_device_reads_use_discovered_endpoints():
    session = Session(*auth_responses(), success({"data": [{"iotId": "device-1"}]}),
                      success({"PowerSwitch": {"value": 1}}))
    client = api.Air352ApiClient(session, "test-user", "test-password")
    await client.authenticate()
    assert client.is_iot_token_valid()
    assert client._iot_token_expire == 72000
    assert await client.get_device_list() == [{"iotId": "device-1"}]
    assert await client.get_device_properties("device-1") == {"PowerSwitch": {"value": 1}}
    assert len(session.calls) == 6
    assert session.calls[3][1].startswith("https://iot.cn-shanghai.aliyuncs.com/")
    request = json.loads(session.calls[4][2]["data"])
    assert request["request"]["iotToken"] == "iot-secret"
    assert all(call[2]["allow_redirects"] is False for call in session.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/living/account/region/get", "/account/createSessionByAuthCode"])
async def test_auth_endpoint_error_never_recurses(path):
    session = Session(Response({"code": "401", "message": None}))
    client = api.Air352ApiClient(session, "test-user", "test-password")
    with pytest.raises(api.Air352AuthError):
        await asyncio.wait_for(client._ali_gw_request(path, {}), timeout=1)
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_region_auth_error_does_not_deadlock():
    session = Session(auth_responses()[0], Response({"code": 401, "message": "invalid token"}))
    client = api.Air352ApiClient(session, "test-user", "test-password")
    with pytest.raises(api.Air352AuthError):
        await asyncio.wait_for(client.authenticate(), timeout=1)
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_invalid_iot_token_reauthenticates_once():
    session = Session(Response({"code": 2001, "message": "invalid token"}),
                      *auth_responses(), success({"PowerSwitch": {"value": 1}}))
    client = api.Air352ApiClient(session, "test-user", "test-password")
    client._iot_token = "old-token"
    result = await asyncio.wait_for(client._ali_gw_request(
        "/thing/properties/get", {"iotId": "device-1"}, iot_token="old-token",
    ), timeout=1)
    assert result["data"]["PowerSwitch"]["value"] == 1
    assert len(session.calls) == 6
    assert json.loads(session.calls[-1][2]["data"])["request"]["iotToken"] == "iot-secret"


@pytest.mark.asyncio
async def test_repeated_token_failure_stops_after_one_retry():
    error = Response({"code": 2001, "message": "invalid token"})
    session = Session(error, *auth_responses(), error)
    client = api.Air352ApiClient(session, "test-user", "test-password")
    client._iot_token = "old-token"
    with pytest.raises(api.Air352AuthError):
        await asyncio.wait_for(client._ali_gw_request(
            "/thing/properties/get", {}, iot_token="old-token",
        ), timeout=1)
    assert len(session.calls) == 6


@pytest.mark.asyncio
async def test_concurrent_polling_authenticates_only_once():
    session = Session(*auth_responses())
    client = api.Air352ApiClient(session, "test-user", "test-password")
    await asyncio.gather(*(client.ensure_authenticated() for _ in range(5)))
    assert len(session.calls) == 4


@pytest.mark.asyncio
async def test_concurrent_expired_token_failures_share_one_authentication():
    class TokenSession(Session):
        def request(self, method, url, **kwargs):
            if url.endswith("/thing/properties/get"):
                self.calls.append((method, url, kwargs))
                token = json.loads(kwargs["data"])["request"]["iotToken"]
                if token == "old-token":
                    return Response({"code": 2001, "message": "invalid token"})
                assert token == "iot-secret"
                return success({"PowerSwitch": {"value": 1}})
            return super().request(method, url, **kwargs)

    session = TokenSession(*auth_responses())
    client = api.Air352ApiClient(session, "", "")
    client._iot_token = "old-token"
    results = await asyncio.wait_for(asyncio.gather(*(
        client._ali_gw_request("/thing/properties/get", {}, iot_token="old-token")
        for _ in range(3)
    )), timeout=1)
    assert all(r["code"] == 200 for r in results)
    assert len([call for call in session.calls if call[1].endswith("/api/v1/enduser/login")]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response, expected", [
    (Response(b"<html>upstream error</html>"), api.Air352ApiError),
    (Response(b""), api.Air352ApiError),
    (Response([]), api.Air352ApiError),
    (Response(b"unavailable", 503), api.Air352ConnectionError),
    (Response(b"redirect", 302), api.Air352ConnectionError),
    (TimeoutError(), api.Air352ConnectionError),
])
async def test_transport_and_response_errors_have_stage(response, expected):
    client = api.Air352ApiClient(Session(response), "test-user", "test-password")
    with pytest.raises(expected, match="352 login"):
        await client.authenticate()


@pytest.mark.asyncio
async def test_json_with_wrong_content_type_or_gzip_is_accepted():
    payload = gzip.compress(json.dumps({"code": 200, "data": {}}).encode())
    client = api.Air352ApiClient(Session(Response(payload)), "", "")
    result = await client._ali_gw_request("/living/account/region/get", {})
    assert result["code"] == 200


@pytest.mark.asyncio
async def test_missing_login_token_is_protocol_error():
    client = api.Air352ApiClient(Session(success({})), "test-user", "test-password")
    with pytest.raises(api.Air352ApiError, match="missing access_token"):
        await client.authenticate()


@pytest.mark.asyncio
async def test_server_errors_redact_credentials_and_tokens():
    response = Response({"code": 12345, "message": "test-user test-password access-secret"})
    client = api.Air352ApiClient(Session(response), "test-user", "test-password")
    client._access_token = "access-secret"
    with pytest.raises(api.Air352AuthError) as result:
        await client.authenticate()
    for secret in ("test-user", "test-password", "access-secret"):
        assert secret not in str(result.value)


@pytest.mark.asyncio
async def test_oauth_error_never_dumps_response_body():
    response = success({"sensitive": "secret-payload"})
    client = api.Air352ApiClient(Session(response), "", "")
    with pytest.raises(api.Air352AuthError) as result:
        await client._ali_oa_login("access-secret")
    assert "secret-payload" not in str(result.value)


@pytest.mark.asyncio
async def test_empty_device_list_is_valid_but_malformed_list_is_not():
    client = api.Air352ApiClient(Session(*auth_responses(), success({"data": []}), success({})), "", "")
    assert await client.get_device_list() == []
    with pytest.raises(api.Air352ApiError, match="invalid devices"):
        await client.get_device_list()


@pytest.mark.parametrize("host", ["http://api.link.aliyun.com", "https://evil.example",
                                  "api.link.aliyun.com@evil.example", 123])
def test_region_endpoint_validation(host):
    with pytest.raises(api.Air352ApiError):
        api.Air352ApiClient._endpoint(host, api.ALI_DOMAIN)
