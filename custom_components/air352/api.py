import asyncio
import hashlib
import hmac
import base64
import json
import logging
import time
import uuid
from email.utils import formatdate
from gzip import decompress as gzip_decompress
from urllib.parse import quote

import aiohttp

from .const import (
    APPID_352, BASE_URL_352, ALI_APP_KEY, ALI_APP_SECRET,
    ALI_DOMAIN, ALI_OA_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class Air352AuthError(Exception):
    pass


class Air352ConnectionError(Exception):
    def __init__(self, message: str, *, reason: str = "cannot_connect"):
        self.reason = reason
        super().__init__(message)


class Air352ApiError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(message)


def create_client_session() -> aiohttp.ClientSession:
    """Use the system resolver, including when HA has aiodns installed.

    AsyncResolver queries DNS servers itself and can time out in HA networks
    where the system resolver still works. ThreadedResolver runs getaddrinfo
    off the event loop. The caller owns and must close this session.
    """
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    return aiohttp.ClientSession(connector=connector)


class Air352ApiClient:

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str):
        self._session = session
        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._iot_token: str | None = None
        self._iot_refresh_token: str | None = None
        self._oauth_sid: str | None = None
        self._iot_token_ts: float = 0
        self._iot_token_expire: int = 0
        self._auth_lock = asyncio.Lock()
        self._ali_domain = ALI_DOMAIN
        self._oa_domain = ALI_OA_DOMAIN
        self.devices: list[dict] = []

    async def async_close(self) -> None:
        """Close the session owned by the integration."""
        await self._session.close()

    def _safe_message(self, message: object) -> str:
        text = str(message or "unknown error")
        for secret in (self._username, self._password, self._access_token,
                       self._iot_token, self._iot_refresh_token, self._oauth_sid):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text[:300]

    async def _request_json(self, method: str, url: str, *, stage: str, **kwargs) -> dict:
        """Translate transport failures without logging credentials or bodies."""
        _LOGGER.debug("352 request: %s", stage)
        try:
            async with self._session.request(
                method, url, timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=False, **kwargs,
            ) as resp:
                raw = await resp.read()
                if resp.status >= 300:
                    raise Air352ConnectionError(f"{stage}: HTTP {resp.status}")
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip_decompress(raw)
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise Air352ApiError(-1, f"{stage}: empty or non-JSON response") from exc
                if not isinstance(data, dict):
                    raise Air352ApiError(-1, f"{stage}: expected a JSON object")
                return data
        except aiohttp.ClientConnectorDNSError as exc:
            raise Air352ConnectionError(
                f"{stage}: DNS resolution failed for {exc.host}; check the HA host DNS",
                reason="dns_error",
            ) from exc
        except aiohttp.ClientSSLError as exc:
            raise Air352ConnectionError(
                f"{stage}: TLS certificate verification failed", reason="ssl_error",
            ) from exc
        except (TimeoutError, aiohttp.ServerTimeoutError) as exc:
            raise Air352ConnectionError(f"{stage}: request timed out", reason="timeout") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise Air352ConnectionError(f"{stage}: connection failed ({type(exc).__name__})") from exc

    @staticmethod
    def _code(data: dict) -> int:
        try:
            return int(data.get("code", -1))
        except (ValueError, TypeError):
            return -1

    @staticmethod
    def _required_data(data: dict, stage: str) -> dict:
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise Air352ApiError(-1, f"{stage}: missing data object")
        return payload

    @staticmethod
    def _endpoint(value: object, default: str) -> str:
        """Accept only Alibaba HTTPS hosts from region discovery."""
        if value is None:
            return default
        if not isinstance(value, str):
            raise Air352ApiError(-1, "region: invalid endpoint")
        host = value.removeprefix("https://").rstrip("/")
        if not host.endswith((".aliyuncs.com", ".aliyun.com", ".aliplus.com")) or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in host
        ):
            raise Air352ApiError(-1, "region: invalid endpoint")
        return host

    # ── 352 API helpers ──

    def _352_sign(self, path: str) -> tuple[str, str]:
        ts = str(int(time.time()))
        return ts, hashlib.md5((APPID_352 + path + ts).encode()).hexdigest()

    def _352_headers(self, path: str, token: str = "") -> dict:
        ts, sign = self._352_sign(path)
        return {
            "Content-Type": "application/json;charset=utf-8",
            "Authorization": f"Token {token}",
            "ts": ts,
            "sign": sign,
        }

    async def _352_request(self, method: str, path: str, json_data: dict | None = None) -> dict:
        url = BASE_URL_352 + path
        headers = self._352_headers(path, self._access_token or "")
        data = await self._request_json(method, url, stage=f"352 {path}", headers=headers, json=json_data)
        if self._code(data) not in (0, 200):
            raise Air352ApiError(self._code(data), f"352 {path}: {self._safe_message(data.get('message'))}")
        return data

    # ── Alibaba IoT API Gateway helpers ──

    def _ali_gw_sign(self, method: str, path: str, params: dict, api_ver: str, iot_token: str | None = None) -> tuple[dict, bytes]:
        """Build signed request for api.link.aliyun.com (JSON body)."""
        req_id = str(uuid.uuid4())
        body_obj = {
            "id": req_id,
            "version": "1.0",
            "request": {"apiVer": api_ver, "language": "zh-CN"},
            "params": params,
        }
        if iot_token:
            body_obj["request"]["iotToken"] = iot_token
        body_bytes = json.dumps(body_obj).encode()

        nonce = str(uuid.uuid4())
        ts_ms = str(int(time.time() * 1000))
        ct = "application/json; charset=utf-8"
        accept = "application/json; charset=utf-8"
        content_md5 = base64.b64encode(hashlib.md5(body_bytes).digest()).decode()

        sign_headers = {"x-ca-key": ALI_APP_KEY, "x-ca-nonce": nonce, "x-ca-timestamp": ts_ms}
        sh_str = "".join(f"{k}:{v}\n" for k, v in sorted(sign_headers.items()))
        sts = f"{method}\n{accept}\n{content_md5}\n{ct}\n\n{sh_str}{path}"
        sig = base64.b64encode(hmac.new(ALI_APP_SECRET.encode(), sts.encode(), hashlib.sha1).digest()).decode()

        headers = {
            "content-type": ct, "accept": accept,
            "x-ca-key": ALI_APP_KEY, "x-ca-nonce": nonce, "x-ca-timestamp": ts_ms,
            "x-ca-signature": sig,
            "x-ca-signature-headers": ",".join(sorted(sign_headers.keys())),
            "x-ca-signature-method": "HmacSHA1",
            "content-md5": content_md5,
        }
        return headers, body_bytes

    _ALI_AUTH_ERROR_CODES = {401, 2001, 2002, 2459, 26101, 26102}

    async def _ali_gw_request(self, path: str, params: dict, api_ver: str = "1.0.2", iot_token: str | None = None, _retried: bool = False) -> dict:
        headers, body = self._ali_gw_sign("POST", path, params, api_ver, iot_token)
        url = f"https://{self._ali_domain}{path}"
        data = await self._request_json("POST", url, stage=f"Alibaba {path}", headers=headers, data=body)
        code = self._code(data)
        if code in (200,):
            return data
        msg = self._safe_message(data.get("message") or data.get("localizedMsg"))
        if code in self._ALI_AUTH_ERROR_CODES or "identity" in msg.lower() or "token" in msg.lower() or "session" in msg.lower():
            # Authentication endpoints have no IoT token yet. Retrying those by
            # calling authenticate() used to recurse until the request failed.
            if iot_token and not _retried:
                _LOGGER.info("IoT token invalid (code %s), re-authenticating", code)
                async with self._auth_lock:
                    if self._iot_token == iot_token:
                        self._iot_token = None
                    if not self.is_iot_token_valid():
                        await self._authenticate()
                return await self._ali_gw_request(path, params, api_ver, self._iot_token, _retried=True)
            raise Air352AuthError(f"Alibaba {path}: code {code}: {msg}")
        raise Air352ApiError(code, f"Alibaba {path}: code {code}: {msg}")

    async def _ali_oa_login(self, access_token: str) -> str:
        """Call loginbyoauth on living-account endpoint, return sid."""
        nonce = str(uuid.uuid4())
        ts_ms = str(int(time.time() * 1000))
        date_str = formatdate(timeval=None, localtime=False, usegmt=True)
        ct = "application/x-www-form-urlencoded; charset=utf-8"
        accept = "application/json; charset=utf-8"
        path = "/api/prd/loginbyoauth.json"

        oa_json = json.dumps({
            "country": "CN", "authCode": access_token,
            "oauthPlateform": 23, "oauthAppKey": ALI_APP_KEY,
            "riskControlInfo": {
                "appVersion": "1070001", "USE_OA_PWD_ENCRYPT": "true",
                "utdid": "ffffffffffffffffffffffff", "netType": "wifi",
                "umidToken": "", "locale": "zh_CN", "appVersionName": "1.7.1",
                "deviceId": str(uuid.uuid4()), "routerMac": "02:00:00:00:00:00",
                "platformVersion": "36", "appAuthToken": "",
                "appID": "com.mxchip.project352", "signType": "RSA",
                "sdkVersion": "3.4.2", "model": "HA",
                "USE_H5_NC": "true", "platformName": "android",
                "brand": "HomeAssistant", "yunOSId": "",
            },
        }, separators=(",", ":"))

        body_bytes = ("loginByOauthRequest=" + quote(oa_json, safe="")).encode()
        sign_headers = {
            "x-ca-key": ALI_APP_KEY, "x-ca-nonce": nonce,
            "x-ca-signature-method": "HmacSHA1", "x-ca-timestamp": ts_ms,
        }
        sh_str = "".join(f"{k}:{v}\n" for k, v in sorted(sign_headers.items()))
        sts = f"POST\n{accept}\n\n{ct}\n{date_str}\n{sh_str}{path}?loginByOauthRequest={oa_json}"
        sig = base64.b64encode(hmac.new(ALI_APP_SECRET.encode(), sts.encode(), hashlib.sha1).digest()).decode()

        headers = {
            "content-type": ct, "accept": accept,
            "x-ca-key": ALI_APP_KEY, "x-ca-nonce": nonce, "x-ca-timestamp": ts_ms,
            "x-ca-signature": sig,
            "x-ca-signature-headers": ",".join(sorted(sign_headers.keys())),
            "x-ca-signature-method": "HmacSHA1",
            "ca_version": "1", "user-agent": "ALIYUN-ANDROID-DEMO",
            "date": date_str, "Accept-Encoding": "gzip",
        }

        url = f"https://{self._oa_domain}{path}"
        data = await self._request_json("POST", url, stage="Alibaba OAuth login", headers=headers, data=body_bytes)

        try:
            sid = data["data"]["data"]["loginSuccessResult"]["sid"]
        except (KeyError, TypeError) as e:
            raise Air352AuthError(f"Alibaba OAuth login failed (code {self._code(data)})") from e
        if not isinstance(sid, str) or not sid:
            raise Air352AuthError("Alibaba OAuth login: missing sid")
        return sid

    # ── Public API ──

    async def authenticate(self) -> None:
        """Full auth: 352 login → region → OA login → IoT session."""
        async with self._auth_lock:
            await self._authenticate()

    async def _authenticate(self) -> None:
        self._iot_token = None
        self._ali_domain = ALI_DOMAIN
        self._oa_domain = ALI_OA_DOMAIN
        # Step 1: 352 login
        login_path = "/api/v1/enduser/login"
        headers = self._352_headers(login_path)
        url = BASE_URL_352 + login_path
        data = await self._request_json(
            "POST", url, stage="352 login", headers=headers,
            json={"account": self._username, "password": self._password},
        )
        if self._code(data) not in (0, 200):
            raise Air352AuthError(f"352 login: code {self._code(data)}: {self._safe_message(data.get('message'))}")
        self._access_token = self._required_data(data, "352 login").get("access_token")
        if not isinstance(self._access_token, str) or not self._access_token:
            raise Air352ApiError(-1, "352 login: missing access_token")

        # Step 2: region get
        region = await self._ali_gw_request(
            "/living/account/region/get",
            {"type": "THIRD_AUTHCODE", "countryCode": "CN", "authCode": self._access_token},
        )
        endpoints = self._required_data(region, "region")
        self._ali_domain = self._endpoint(endpoints.get("apiGatewayEndpoint"), ALI_DOMAIN)
        self._oa_domain = self._endpoint(endpoints.get("oaApiGatewayEndpoint"), ALI_OA_DOMAIN)

        # Step 3: OA login
        sid = await self._ali_oa_login(self._access_token)
        self._oauth_sid = sid

        # Step 4: create IoT session
        r = await self._ali_gw_request(
            "/account/createSessionByAuthCode",
            {"request": {"authCode": sid, "accountType": "OA_SESSION", "appKey": ALI_APP_KEY}},
            api_ver="1.0.4",
        )
        payload = self._required_data(r, "IoT session")
        token = payload.get("iotToken")
        if not isinstance(token, str) or not token:
            raise Air352ApiError(-1, "IoT session: missing iotToken")
        try:
            expire = int(payload.get("iotTokenExpire", 72000))
            if expire <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise Air352ApiError(-1, "IoT session: invalid token lifetime") from exc
        self._iot_token = token
        self._iot_refresh_token = payload.get("refreshToken")
        self._iot_token_expire = expire
        self._iot_token_ts = time.time()
        _LOGGER.debug("IoT auth complete, token expires in %ds", self._iot_token_expire)

    def is_iot_token_valid(self) -> bool:
        if not self._iot_token:
            return False
        elapsed = time.time() - self._iot_token_ts
        return elapsed < (self._iot_token_expire - min(3600, self._iot_token_expire * 0.1))

    async def ensure_authenticated(self) -> None:
        async with self._auth_lock:
            if not self.is_iot_token_valid():
                await self._authenticate()

    async def get_device_list(self) -> list[dict]:
        await self.ensure_authenticated()
        r = await self._ali_gw_request(
            "/uc/listBindingByAccount",
            {"pageNo": 1, "pageSize": 50},
            iot_token=self._iot_token,
        )
        self.devices = self._required_data(r, "device list").get("data")
        if not isinstance(self.devices, list) or any(
            not isinstance(device, dict) or not device.get("iotId") for device in self.devices
        ):
            raise Air352ApiError(-1, "device list: missing or invalid devices")
        return self.devices

    async def get_device_properties(self, iot_id: str) -> dict:
        await self.ensure_authenticated()
        r = await self._ali_gw_request(
            "/thing/properties/get",
            {"iotId": iot_id},
            iot_token=self._iot_token,
        )
        return self._required_data(r, "device properties")

    async def set_device_properties(self, iot_id: str, items: dict) -> None:
        await self.ensure_authenticated()
        await self._ali_gw_request(
            "/thing/properties/set",
            {"iotId": iot_id, "items": items},
            iot_token=self._iot_token,
        )

    async def get_device_info(self, iot_id: str) -> dict:
        """Get device info from 352 API (includes firmware, mac, etc.)."""
        return (await self._352_request("GET", f"/api/device/info/{iot_id}")).get("data", {})
