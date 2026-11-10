#!/usr/bin/env python3
"""Read-only live diagnostics using the integration's actual API client.

Credentials are read inside this process and sent only to the official 352
login endpoint over verified HTTPS. Never print credentials, tokens or bodies.
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import socket
import sys

from _air352_loader import load_api

api = load_api()
DEFAULT_CREDENTIALS = Path(__file__).resolve().parents[1] / ".local" / "air352-credentials.json"


def credentials_from_file(path: Path) -> tuple[str, str]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ValueError("无法读取账号配置，请检查文件路径和 JSON 格式。") from None
    if not isinstance(values, dict) or any(
        not isinstance(values.get(key), str) or not values[key].strip()
        for key in ("username", "password")
    ):
        raise ValueError("请先在本地配置文件中填写 username 和 password。")
    return values["username"].strip(), values["password"]


async def diagnose(args) -> int:
    if args.network_only:
        username, password = "", ""
    else:
        try:
            username, password = credentials_from_file(args.credentials)
        except ValueError as exc:
            print(f"配置未就绪：{exc}", file=sys.stderr)
            return 2

    async with api.create_client_session() as session:
        client = api.Air352ApiClient(session, username, password)
        try:
            for host in ("app.352air.com", api.ALI_DOMAIN, api.ALI_OA_DOMAIN):
                resolver = session.connector._resolver
                await asyncio.wait_for(resolver.resolve(host, 443, socket.AF_UNSPEC), 10)
                print(f"[OK] 系统 DNS：{host}", flush=True)

            if args.network_only:
                path = "/api/v1/enduser/login"
                data = await client._request_json(
                    "POST", api.BASE_URL_352 + path, stage="352 login connectivity",
                    headers=client._352_headers(path), json={},
                )
                if client._code(data) != 10011:
                    raise api.Air352ApiError(-1, "352 login probe: unexpected response")
                print("[OK] 352 登录接口可达，返回预期的缺少账号校验结果", flush=True)
                await client._ali_gw_request(
                    "/living/account/region/get", {"type": "COUNTRY_CODE", "countryCode": "CN"},
                )
                print("[OK] 阿里云网关签名和区域查询", flush=True)
                print("网络预检通过；此模式没有验证账号登录或设备读取。")
                return 0

            await client.authenticate()
            print("[OK] 352 登录 → 阿里云区域 → OAuth → IoT 会话", flush=True)
            devices = await client.get_device_list()
            print(f"[OK] 获取设备列表：{len(devices)} 台", flush=True)
            if not devices:
                print("账号认证成功，但没有绑定设备，尚未验证设备属性读取。")
                return 3

            failures = 0
            for round_no in range(1, args.repeat + 1):
                for index, device in enumerate(devices, 1):
                    try:
                        properties = await client.get_device_properties(device["iotId"])
                        if not properties:
                            failures += 1
                            print(f"[FAIL] 第 {round_no} 轮设备 #{index} 返回空属性", flush=True)
                        else:
                            print(f"[OK] 第 {round_no} 轮设备 #{index}：读取 {len(properties)} 项属性", flush=True)
                    except (api.Air352AuthError, api.Air352ConnectionError, api.Air352ApiError) as exc:
                        failures += 1
                        print(f"[FAIL] 第 {round_no} 轮设备 #{index}：{exc}", flush=True)
                if round_no < args.repeat:
                    await asyncio.sleep(args.interval)
            if failures:
                print(f"测试未通过：{failures} 次设备读取失败。")
                return 1
            print("测试通过：真实账号登录、设备列表和属性读取均成功。未更改设备状态。")
            return 0
        except (api.Air352AuthError, api.Air352ConnectionError, api.Air352ApiError) as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
        except (OSError, TimeoutError):
            print("[FAIL] 系统 DNS 预检失败或超时，请检查当前主机的 DNS 设置。", file=sys.stderr)
        except Exception as exc:
            # Do not dump locals, response bodies or arbitrary exception text.
            print(f"[FAIL] 未预期错误：{type(exc).__name__}（未输出敏感上下文）", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--network-only", action="store_true", help="只检查 DNS、HTTPS 和网关签名，不读取账号文件")
    parser.add_argument("--repeat", type=int, default=2, help="设备属性读取轮数，默认 2")
    parser.add_argument("--interval", type=float, default=2, help="读取间隔（秒），默认 2")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20 or args.interval < 0:
        parser.error("repeat 必须为 1–20，interval 不能小于 0")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[STEP] %(message)s"))
    logger = logging.getLogger(api.__name__)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        return asyncio.run(diagnose(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
