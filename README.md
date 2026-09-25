# FranklyBuilds-Register（FB 注册机）

面向 Windows 本机运行的账号工作流控制台。项目使用 Vue 3、FastAPI 和 MongoDB，
将邮箱、代理、指纹浏览器任务、账号状态、支付工具和成品管理放在一个可视化界面中。

## 项目组成与上游来源

本项目是两个开源项目方向的融合与二次开发。下面列出每个部分的**上游项目仓库**；
上游仓库不是本项目自身的发布仓库，功能、提交和许可证应以对应上游仓库及其版本为准。

| 融合部分 | 上游项目仓库 | 本项目中的用途 | 说明 |
| --- | --- | --- | --- |
| GPT 注册工作流 | [2951461586/GPT-Register-Tool](https://github.com/2951461586/GPT-Register-Tool) | 注册流程、账号工作流和相关服务的基准实现 | 本地记录的核对提交：`7fb6ecb12ee4c2087887b3069593c65f110b65e7` |
| Outlook 注册工作流 | [LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister) | Outlook 注册流程的二次开发来源 | OutlookRegisterPlus 的 README 和许可证均保留该来源声明 |

### 配套项目

代理池组件不是上述两个注册项目之一，但本项目支持与以下开源项目配合使用：

- [daimon3332/easy-proxies](https://github.com/daimon3332/easy-proxies)：当前配套的 Easy Proxies 二次开发版本。
- [jasonwong1991/easy_proxies](https://github.com/jasonwong1991/easy_proxies)：Easy Proxies 的上游项目。
- [SagerNet/sing-box](https://github.com/SagerNet/sing-box)：Easy Proxies 使用的代理平台与协议实现。

如需核对来源、版本和许可证，请优先查看 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)
以及各上游仓库的 LICENSE 文件。

### 项目赞助与推荐：FranklyBuilds 中转站

感谢 [FranklyBuilds](https://franklybuilds.com) 对本项目的支持！如果你正在寻找稳定、
方便接入的 AI API 中转服务，可以试试 FranklyBuilds：

**[立即访问 franklybuilds.com →](https://franklybuilds.com)**

- OpenAI 兼容接口，接入现有客户端和工具即可使用
- 支持按控制台提供的模型列表进行选择
- 统一管理 API Key、模型和用量
- 适合个人开发、自动化脚本和项目集成

OpenAI 兼容接口根地址：

```text
https://franklybuilds.com/v1
```

通用配置示例：

```text
Base URL: https://franklybuilds.com/v1
API Key:  从 franklybuilds.com 控制台创建的密钥
Model:    以中转站控制台实际提供的模型为准
```

注册、套餐、可用模型、请求额度和计费详情请以
[franklybuilds.com 控制台](https://franklybuilds.com) 的最新说明为准。API Key 只写入
本机环境变量或未提交的 `.env` 文件，不要写进 README、源码、截图或 Git 历史。

> 本项目默认只监听 `127.0.0.1`。运行数据、邮箱凭据、代理凭据、Access Token、TOTP
> Secret 和第三方 API Key 均不应提交到 Git。

## 基准功能迁移范围

基准注册机 1 的行为以源码仓库
[`2951461586/GPT-Register-Tool`](https://github.com/2951461586/GPT-Register-Tool)
为准，当前审阅 commit 为
`7fb6ecb12ee4c2087887b3069593c65f110b65e7`。`gpt/artifacts/reverse/` 下的安装包归档只
用于历史对照，不用于否定源码仓库中已经存在的功能。

源码仓库的 `v2026.08.22` 已包含独立邮箱换绑流程：资格检查、目标邮箱提交、目标邮箱 OTP
验证、目标邮箱重新登录、账号测活，以及 SQLite/Session JSON 迁移。FB 后续如需复现，
应以 `sms_tool/account_email_change.py`、`sms_tool/commands/email_change.py` 和
`sms_tool/storage.py` 为主要参考；现有注册链路仍保持独立。

## 功能

- 邮箱池、代理池与账号池管理
- 代理按国家和分组管理，可供不同工作流选择
- Easy Proxies / Resin 节点订阅转换并导入现有代理池
- RoxyBrowser 临时指纹窗口调度、并发控制、清理和故障熔断
- 多种邮箱验证码接口与 MailCom Hub 对接
- 注册后套餐和 Plus 试用资格查询
- Access Token 解析、支付链接任务与分阶段代理
- 可配置的提炼、支付重试和自动流水线
- HeroSMS 国家配置、价格限制、换号次数和等待时间
- 成品管理、导出标记、邮箱到账确认和统计
- JSONL 任务日志、敏感字段脱敏和运行恢复

## 界面

| 页面 | 用途 |
| --- | --- |
| 启动界面 | 选择邮箱来源、注册国家、代理分组、数量和并发 |
| 账号池 | 查询、筛选、套餐检查、复制和导出账号 |
| 邮箱池 | 导入接码地址、同步 MailCom 别名、管理来源 |
| 配置栏 | RoxyBrowser、任务并发和代理分组 |
| 支付工具 | Access Token 解析与支付链接任务 |
| Plus 流水线 | 资格筛选、提炼、接码和支付编排 |
| 成品管理 | 支付结果、到账状态、邮箱入口和导出状态 |
| HeroSMS | 独立管理短信国家与订单参数 |
| 协议授权 | 隔离运行的协议授权工作台 |

## 安装包归档与比较

安装包不进入控制台页面。静态证据保存在
`artifacts/reverse/<版本名>/`，每个版本包含原始 `.exe`、提取出的 `payload.zip`、
`payload/`、`MANIFEST.json`、`STATIC-ANALYSIS.json`、`STATIC-ANALYSIS.md`、
`DIFF.json` 和 `DIFF.md`。历史支付报告仍保留为 `PROTOCOL-PAYMENT-ANALYSIS.md`。

收到新安装包时，将文件放到任意本机路径并执行：

```powershell
cd <仓库根目录>
& .\register_env\Scripts\python.exe .\app\scripts\analyze_installer.py `
  .\path\to\GPT-Register-Tool-Setup-v2026.08.20.exe
```

脚本默认按安装包文件名建立版本目录，并自动选择已有归档作为上一版本；也可以显式指定：

```powershell
& .\register_env\Scripts\python.exe .\app\scripts\analyze-installer.py `
  .\new-installer.exe `
  --version GPT-Register-Tool-Setup-v2026.08.20 `
  --previous .\artifacts\reverse\GPT-Register-Tool-Setup-v2026.08.05
```

`DIFF.md` 会列出新增、删除和变更文件，以及新增/删除的 Python 函数、类、模块和外部
主机；`DIFF.json` 供后续自动化读取。分析只读取 PE/ZIP 字节和源码 AST，不启动安装器、
不加载凭据、不创建子进程、不联网。`artifacts/reverse/INDEX.md` 是所有已归档版本的索引，
`artifacts/reverse/LATEST-DIFF.md` 汇总最近一次比较。

脚本默认限制安装包 1 GiB、单个解包文件 512 MiB、总解包大小 2 GiB、ZIP 条目 100,000
项和压缩比 1,000；受控测试可通过同名 `--max-*` 参数调整。

## 架构

```mermaid
flowchart LR
    UI["Vue 3 / Vite"] --> API["FastAPI"]
    API --> DB["MongoDB"]
    API --> ROXY["RoxyBrowser Local API"]
    API --> MAIL["Mailbox providers"]
    API --> PAY["Payment workers"]
    API --> SMS["SMS providers"]
    API --> IMAP["mail.com IMAP"]
```

所有服务默认绑定回环地址：

- 统一控制台与后端：<http://127.0.0.1:8000/launch>
- API 文档：<http://127.0.0.1:8000/api/docs>
- 开发前端（仅 `-Development`）：<http://127.0.0.1:5173>
- RoxyBrowser OpenAPI：`127.0.0.1:50000`

## 环境要求

- Windows 10/11 x64
- PowerShell 5.1 或 PowerShell 7
- Python 3.13
- Node.js `22.18+` 或 `24.12+`
- MongoDB 8.0
- RoxyBrowser（仅真实浏览器任务需要）
- Chrome/Chromium（MailCom 别名自动创建需要）

## 快速开始

```powershell
git clone git@github.com:maile456/codex-auto-register.git
cd codex-auto-register\app

Copy-Item .env.example .env
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

日常使用通过根目录统一入口启动：

```powershell
cd <仓库根目录>
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-autoregister.ps1 -NoBrowser
# 控制台：http://127.0.0.1:8000/launch
```

默认模式由 FastAPI 托管已构建的 Vue 控制台，不启动独立 MailCom 管理器或旧 Outlook 服务。
开发时可显式保留 Vite：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-autoregister.ps1 -Development
# 开发控制台：http://127.0.0.1:5173/launch
```

停止使用 `stop-autoregister.ps1`。首次部署或构建前端时，先运行 `cd app; npm.cmd run build-only`。

在配置栏打开“注册时设置 2FA”后，浏览器注册和协议注册都会在账号创建完成后
执行 TOTP 重认证、激活认证器并把 Secret 保存到账号记录；“注册时设置密码”可
单独控制密码注册流程。2FA 需要邮箱池能接收注册后的第二封重认证验证码。若
TOTP 阶段暂时失败，账号和 Access Token 仍会保留，worker 标记为
`partial_success`，任务日志会保留 2FA 错误码，便于后续补做。

### 首次配置 RoxyBrowser

1. 启动 RoxyBrowser并登录。
2. 在 RoxyBrowser 的 API 设置中启用本地 OpenAPI。
3. 在本项目“配置栏”填写 RoxyBrowser 路径、端口和 API Key。
4. 至少创建一个 workspace；任务会在所选 workspace 中创建临时窗口。
5. 先使用一个邮箱、并发 `1` 做连通性验证。

RoxyBrowser 的套餐、API 请求频率和窗口创建配额由 RoxyBrowser 服务端控制。
`browser_create` 返回 `416` 时，应先在其客户端检查当前套餐、可用窗口及当天创建额度。

## 数据导入

邮箱格式：

```text
user@example.com----https://mail.example.test/latest?email=user%40example.com
```

代理支持 HTTP/HTTPS/SOCKS/SOCKS5/SOCKS5H URL、无认证 `host:port`、CSV（支持引号
包裹逗号/空格）和常见四字段形式：

```text
socks5://username:password@proxy.example.test:1080
proxy.example.test:1080:username:password
```

浏览器所需的代理兼容桥已内置在后端进程中，不依赖 EasyProxy 或 Clash。
默认 `AUTOREGISTER_PROXY_BRIDGE=auto` 会桥接 Ant/Chromium 的认证
HTTP(S)/SOCKS 及 `socks5h` 节点；Roxy 使用原生代理字段。设置为 `always`
可让 Roxy/Ant 的认证、HTTP 或 `socks5h` 浏览器代理统一走临时本机 SOCKS5，
`never` 则完全使用原生配置。

敏感数据保存在本机 MongoDB 或 `data/` 下的本地文件中。API 响应和任务日志会尽量
隐藏 Access Token、代理密码、短信 Key、TOTP Secret 和浏览器连接地址。

## MailCom 集成

MailCom 账号、别名、IMAP、验证码读取和服务器推送现在由主 FastAPI 服务管理，数据保存于主 MongoDB。控制台入口为 `/mailcom`，接口位于 `/api/mailcom/*`；普通列表和日志不会返回密码或 DPAPI 密文。

迁移旧 SQLite：

```powershell
cd <仓库根目录>
# 启动主服务后，在 MailCom 页面点击“迁移”，或调用：
Invoke-WebRequest http://127.0.0.1:8000/api/mailcom/migrate -Method Post
```

迁移会在旧数据库旁创建只读备份，从备份副本读取，解密旧 DPAPI 凭据后以主服务前缀重新保护；重复执行按规范化邮箱和别名幂等，不覆盖 Mongo 中已有记录，也不修改原 SQLite。验证计数和 IMAP 连通性后，旧 `mailcom-manager/` 仅作为迁移与回滚材料保留，不作为运行时服务。回滚时停止主服务，保留 Mongo 数据和只读备份，并按部署记录恢复旧管理器；不要删除原 SQLite。

旧的 `3211` 邮箱 URL 仅为迁移/回滚兼容格式。新同步数据使用 `mailcom://account/<id>` 或 `mailcom://alias/<id>` 内部句柄，主流水线不会请求旧端口。

## Outlook 注册与邮箱池集成

Outlook 注册、OAuth/Graph 校验、任务状态、日志、失败统计、代理分组和邮箱池现在都由
Register 主服务统一管理，控制台入口为 `/outlook-register`，接口位于
`/api/outlook/*`。Outlook 任务使用独立的 MongoDB 集合，不复用 GPT 任务状态。

共享代理查询 `/api/outlook/proxies` 的 `pageSize` 与主代理接口统一为 10、20、50、100（默认 50）；不支持的页大小返回 422，不触发服务器错误。

任务监控卡展示后端实际提交、运行中、成功、失败、成功率、耗时及批次，
并保留失败分类原始键和错误代码。分类按诊断事件计数，不应相加当作失败任务数。
`completed` 只表示流程结束；含失败或没有执行任务时会明确标注，不等于注册成功。
页面打开期间每 2 秒刷新任务状态与日志（包括空闲状态），可发现其他窗口启动的任务。
刷新失败会保留最后快照并提示数据可能过期，恢复连接后自动更新；停止/重置后的状态
不会被迟到的旧请求覆盖。离开页面停止轮询。

主控制台的任务配置现可编辑注册后的 OAuth 开关、Client ID、回调地址和权限范围。
该开关仅控制浏览器注册后的授权步骤；已有账号的授权校验仍由执行模式决定。
Client ID 输入框不会回显已保存值；留空表示保留原值，保存成功后清空输入框。
权限范围支持换行、空格或逗号分隔，保存后以列表持久化；清空表示保存空列表。
有未保存配置时，状态轮询和数据刷新会保留草稿，启动任务前必须先保存。
运行中的任务继续使用启动时的配置快照，保存配置只影响下次任务。

任务执行模式：

- `auto`：Mongo 中存在完整 `Client ID + Refresh Token` 时执行授权校验，否则进入浏览器注册引擎。
- `registration`：只运行现有 Outlook 浏览器注册引擎。
- `authorized`：只校验 Mongo 中已有 OAuth/Graph 账号。
- `both`：先执行浏览器注册，再校验已有授权账号。

运行时结果写入 MongoDB 的 `outlook_accounts` 和邮箱池集合；旧
`app/outlook_register/Results/` 下的 `oauth2.txt`、`registered.txt` 和 `pool.json` 只作为
迁移/回滚来源，不作为主服务运行时数据源。真实浏览器任务需要 RoxyBrowser、可用的主
MongoDB 代理分组和受控测试邮箱；未配置可用代理时任务会在启动浏览器前失败，不回退到
本地默认端口。

普通账号列表、任务日志、统计和邮箱池接口不会返回密码、Client ID、Access Token 或
Refresh Token。子邮箱接码地址由主服务生成并通过 `/r/<token>` 提供无缓存访问；读取邮件
时会校验主邮箱或子邮箱收件人，避免跨邮箱读取。

## 环境变量

复制 `app/.env.example` 后按需配置。常用变量：

| 变量 | 说明 |
| --- | --- |
| `HEROSMS_API_KEY` | HeroSMS API Key |
| `OPLL_WEB_PASSWORD` | 支付工作台请求密码 |
| `OPLL_CHECKOUT_PROXY` | 默认 Checkout 代理 |
| `OPLL_UPDATE_PROXY` | 默认资格查询/Update 代理 |
| `IPROCKET_BRIDGE_PORT` | 特殊代理本机桥端口 |
| `PAP_PORT` | 协议授权 sidecar 端口 |
| `PAYPAL_PROXY_POOL` | 协议授权代理池 |
| `AUTOREGISTER_MONGO_URI` | 覆盖 MongoDB URI |
| `EASY_PROXIES_ROOT` | Easy Proxies 项目目录，默认 `D:\baiduProject\代理池\easy-proxies` |
| `RESIN_ROOT` | Resin 项目目录，默认 `D:\baiduProject\代理池\Resin` |
| `AUTOREGISTER_RESIN_ADMIN_TOKEN` | 本机 Resin 管理令牌，首次启动自动生成 |
| `AUTOREGISTER_RESIN_PROXY_TOKEN` | 本机 Resin 代理令牌，首次启动自动生成 |

不要把 `.env`、`data/settings.json` 或浏览器导出的凭据文件提交到仓库。

新克隆首次运行 `app\setup.ps1` 时，会把 `.env.example` 复制为本机
`app\.env`。当前私有仓库模板已经配置远程 MongoDB，安装脚本会跳过本机
MongoDB 安装。云服务器安全组需要允许运行电脑访问 TCP `37017`。模板包含
受限数据库账号，仓库必须保持私有。

## 开发

安装依赖：

```powershell
cd app
& ..\register_env\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
npm.cmd ci
```

验证：

```powershell
cd app
npm.cmd run type-check
npm.cmd test -- --run
npm.cmd run build
& ..\register_env\Scripts\python.exe -m pytest tests\backend -q
& ..\register_env\Scripts\python.exe -m pytest tests\backend\test_installer_archive.py -q

cd ..\mailcom-manager
& ..\register_env\Scripts\python.exe -m pytest tests -q
```

MongoDB 集成测试默认跳过；需要时设置 `AUTOREGISTER_RUN_MONGO_TESTS=1`。

## 项目结构

```text
.
├─ app/
│  ├─ backend/              FastAPI、任务调度和服务集成
│  ├─ src/                  Vue 前端
│  ├─ tests/backend/        后端测试
│  ├─ scripts/              MongoDB、发布与安装包静态分析脚本
│  └─ .env.example          环境变量模板
├─ mailcom-manager/         MailCom 迁移与回滚来源（legacy）
├─ start-autoregister.ps1   Windows 一键启动
└─ README.md
```

运行数据、虚拟环境、依赖目录、备用项目、构建产物和本机日志均由根目录
`.gitignore` 排除。

## 常见问题

### 端口 8000 已占用

说明已有后端实例正在运行。打开 <http://127.0.0.1:8000/api/health> 检查状态，
不要重复启动第二个实例。

### RoxyBrowser 创建窗口失败

先确认 RoxyBrowser 正在运行、OpenAPI 已开启、API Key 与端口正确、workspace 存在。
错误码 `416` 通常表示服务端窗口创建额度问题，不是代理连通性错误。

### 代理 CONNECT 中止

先直接测试本地网络能否连接代理入口，再确认代理协议。部分动态 SOCKS5 网关会通过
本机桥转换后交给 RoxyBrowser；桥端口和代理本身是两层独立状态。

### 邮箱 URL 很慢

IMAP 查询可能依次检查多个文件夹并下载邮件正文。批量别名任务、注册取码和到账检查
同时运行时会争用 IMAP 并发槽位，建议错峰执行。

## 安全

公开部署前阅读 [`SECURITY.md`](SECURITY.md)。管理 API 默认面向本机使用；若通过反向
代理暴露，必须增加身份验证、TLS、访问控制和请求速率限制。

## 贡献

提交方式、测试要求和敏感信息检查见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 许可证与第三方代码

项目原创代码采用 [MIT License](LICENSE)。内置或迁移的第三方代码不自动适用 MIT，
其来源、固定版本和许可边界见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
