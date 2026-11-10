# 352 Air - Home Assistant Integration

Home Assistant 自定义集成，支持 [352](https://www.352air.com/) 空气净化器、净水器、加湿器的状态监控和设备控制。

## 支持设备

| 类型 | 传感器 | 控制 |
|------|--------|------|
| 空气净化器 | PM2.5、TVOC、甲醛、CO2、温度、湿度、滤芯寿命 | 电源、童锁、屏幕、负离子、智能模式 |
| 净水器 | 进水TDS、出水TDS、水温、累计净水量、滤芯寿命 | 童锁 |
| 加湿器 | PM2.5、温度、湿度、目标湿度、工作模式、累计运行时间、滤芯寿命、水箱状态（正常/缺水）、补水后已用时长 | 电源、童锁、屏幕、智能模式、风干设置 |

具体实体按设备实际返回的属性创建，不同型号不一定具备表中全部功能。

2.0.4 增加加湿器“目标湿度”（`SetHumidity`，只读，单位 %）、“工作模式”（`WorkMode`，只读，待机/自动/保湿/睡眠/湿度模式/风量模式）、“累计运行时间”（`DeviceUsedTime`，云端累计值，原生单位为分钟，支持累计统计）和“风干设置”开关（`AirDringMode`）。风干设置表示是否启用风干功能，开启不代表设备此刻正在风干。累计运行时间与下面的“补水后已用时长”分别计量，不会在补水后清零。目标湿度和工作模式暂不提供修改操作。

Wi-Fi 信号使用设备的 `WiFI_RSSI` 原始负值，以 dBm 显示，越接近 0 信号越强，不转换为百分比，也不使用模型中的 `WiFI_RSSI1`。该实体默认禁用；可在“设置 → 设备与服务 → 实体”中找到“WiFi信号”并启用。2.0.4 会把非数值、非有限值或超出设备定义范围（−127 至 −1）的数据显示为未知；轮询失败或该设备读取失败时显示不可用，恢复读取后自动恢复。

集成每 10 秒查询一次云端缓存，不代表设备每 10 秒上报一次。H70 物模型注明常规数据约每 5 分钟上报；Wi-Fi 字段也可能保留更早的上报值。信号读数表示最近上报的信号强度，不能单凭它判断设备当前是否在线。

加湿器返回 `WaterShortage` 属性时，会自动创建“水箱状态”传感器：`0` 显示“正常”，`1` 显示“缺水”，随设备状态每 10 秒刷新。属性缺失或返回异常值时显示未知，设备读取失败时显示不可用。更新集成文件并重启 Home Assistant 后即可看到该实体。

“补水后已用时长”根据“缺水 → 正常”的变化判断补水，以小时显示从该次补水到现在的经过时间（保留两位小数），随每次轮询刷新；下次补水时重新计时。它计算自然经过时间，包含关机、待机及再次缺水后的时间，不代表实际运行时长。属性 `last_refilled_at` 记录最近一次识别到的补水时间。

计时记录通过 HA 状态恢复机制保留，正常重启或重新加载集成后继续计时。首次安装且未观察到补水时显示“未知”，不会把安装时间当成补水时间；缺失、异常或过期的属性快照不会触发重置。检测优先使用云端属性上报时间，没有时间戳时使用本地发现时间。HA 离线期间若错过了完整的缺水和补水过程，或在未缺水时中途加水，接口无法识别，因此这类补水不能自动重置计时。

## 安装

### HACS / Marketplace（推荐）

1. 打开 HACS / Marketplace → 右上角三点 → 自定义仓库（旧版 HACS 先进入“集成”）
2. 输入仓库地址 `https://github.com/mcdona1d/hass-352`，类别选“集成”（如界面要求选择）
3. 搜索 "352 Air" 安装
4. 重启 Home Assistant
5. 在“设置 → 设备与服务 → 添加集成”中搜索 "352 Air"，完成账号配置

若提示 `has no hacs.json in its root`，请确认 GitHub 默认分支根目录已包含本项目的 `hacs.json`。Marketplace 会读取所安装版本的该文件，因此使用 GitHub Releases 时，所选 Release 对应的标签中也必须包含它；旧版 Release 不会随默认分支更新而改变。维护者发布包含修复的新 Release 后，再刷新并重试；支持选择默认分支的 HACS 版本也可先安装已包含修复的默认分支。

### 手动安装

将 `custom_components/air352` 目录复制到 Home Assistant 的 `config/custom_components/` 下，重启 HA。

## 项目结构

采用 [HACS 标准集成目录结构](https://hacs.netlify.app/docs/publish/integration/)，一个仓库只提供一个集成：

```text
.
├── hacs.json                     # HACS / Marketplace 仓库元数据，必须位于根目录
├── README.md                     # 使用、安装和开发说明
├── custom_components/
│   └── air352/                   # 实际安装到 HA 的完整集成
│       ├── manifest.json         # HA 集成元数据和版本号
│       ├── __init__.py           # 集成入口
│       ├── api.py                # 352 / 阿里云 API 客户端
│       ├── config_flow.py        # 账号配置流程
│       ├── const.py              # 常量
│       ├── coordinator.py        # 状态轮询
│       ├── fan.py                # 风扇实体
│       ├── sensor.py             # 传感器实体
│       ├── switch.py             # 开关实体
│       ├── water_usage.py        # 补水计时逻辑
│       ├── strings.json          # 界面字符串
│       └── translations/         # 中英文翻译
├── docs/                         # 调研文档
├── scripts/                      # 本地诊断工具
├── tests/                        # 离线回归测试
├── pytest.ini                    # 测试配置
└── requirements-dev.txt          # 本地开发依赖
```

`hacs.json` 的 `content_in_root` 为 `false`，安装内容来自 `custom_components/air352/`。诊断脚本、测试、调研文档和本地账号文件均不属于集成运行文件，无需复制到 HA。

发布时需将根目录 `hacs.json` 和集成目录一起提交到 GitHub 默认分支。如果使用 GitHub Releases，应从包含这些文件的提交创建新标签和 Release，并保持标签版本与 `manifest.json` 的 `version` 一致。当前使用仓库源码安装方式，无需上传 `dist/` 内的 ZIP，也无需设置 `zip_release`。

## 配置

设置 → 设备与服务 → 添加集成 → 搜索 "352 Air" → 输入 352Life App 的手机号和密码。

## 工作原理

1. 通过 352 API 登录获取 access_token
2. 通过阿里云 IoT 生活物联网平台（飞燕）认证获取 iotToken
3. 通过阿里云 IoT API Gateway 获取设备列表和属性
4. 通过 `/thing/properties/set` 下发设备控制指令
5. 每 10 秒轮询一次设备状态

## 本地诊断与测试

无需安装 Home Assistant；脚本直接加载集成的真实 API 客户端。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

# 不需要账号：检查系统 DNS、352 登录接口连通性、阿里云签名和区域查询
.venv/bin/python scripts/test_air352.py --network-only
```

创建本地账号文件（已存在时不要覆盖），权限设为 `600`：

```sh
mkdir -p .local
chmod 700 .local
test -f .local/air352-credentials.json || printf '{"username":"","password":""}\n' > .local/air352-credentials.json
chmod 600 .local/air352-credentials.json
```

用本地编辑器填写 `username` 和 `password`，然后执行：

```sh
# 完整认证 → 设备列表 → 两轮设备属性读取（默认间隔 2 秒）
.venv/bin/python scripts/test_air352.py

# 也可以指定其他本地配置文件
.venv/bin/python scripts/test_air352.py --credentials /absolute/path/account.json

# 离线回归测试
.venv/bin/python -m pytest -q
```

脚本默认只读，不改变设备状态。配置文件由本地进程直接读取，账号密码仅通过验证证书的 HTTPS 发送至 `app.352air.com`；不会输出账号、密码、token、设备标识或完整响应体。不要把账号文件内容粘贴到聊天或日志中。`.local/` 已加入 Git 忽略，不能作为上传附件或安装包内容。

退出码：`0` 表示所选模式成功，`1` 表示连接、认证或设备读取失败，`2` 表示本地配置不完整，`3` 表示认证成功但没有绑定设备。`--network-only` 成功不代表账号登录成功。

## HA 中 DNS 超时的处理

如果日志包含 `aiodns.error.DNSError: Timeout while contacting DNS servers` 或 `ClientConnectorDNSError`，说明请求尚未到达 352 登录服务。

2.0.1 在首次配置、重新认证和日常轮询中统一使用 `aiohttp.ThreadedResolver`，通过线程池调用系统 `getaddrinfo`，避免默认 `aiodns` 直接查询 DNS 服务器的超时路径。保持 HTTPS 证书验证，不使用固定服务器 IP 或公共 DNS。若 HA 主机的系统 DNS 也不可用，仍需要修复 HA 主机/容器的 DNS 或网络。

更新 `config/custom_components/air352` 后重启 HA，再重新添加集成或重试配置。新版会分别提示 DNS、证书、超时和 API 响应问题，日志会标明失败步骤。

本地真实账号验证已完成：2026-11-07，完整认证成功，发现 3 台设备，两轮读取均成功（每台分别为 38、20、65 项属性）。这验证了本机的云端认证与只读链路；HA 环境中的系统 DNS、实体加载和设备控制仍需在更新后实测。

## 依赖

无外部 Python 依赖，仅使用 Home Assistant 内置的 `aiohttp` 和 Python 标准库。

## License

MIT
