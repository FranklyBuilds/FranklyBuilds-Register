# 当前任务状态

## 项目主线

构建 Windows 本机账号工作流控制台，使用 Vue 3、FastAPI 和 MongoDB 统一管理邮箱、代理、浏览器任务、账号状态、支付工具和成品。

## 当前任务

完成 `outlook所有功能都要在register里面完整执行`：Outlook 注册浏览器引擎、OAuth/Graph、邮箱池/Plus 子邮箱、接码与 OTP、主 Mongo 代理、独立任务生命周期、MailCom 联动和 Vue 控制台全部由 Register 主服务承载。

## 任务范围

- 涉及：`app/backend` Outlook 独立任务状态/配置/日志/结果写入、旧注册引擎适配、主 Mongo 代理分组接入、邮箱池/子邮箱/OTP/OAuth 定时检查、MailCom 账号/别名/IMAP/OTP/服务器推送服务化、SQLite 幂等迁移与 DPAPI 重新保护、Vue 控制台、Windows 启停与生产静态前端服务、相关测试和文档。
- 不涉及：改变既有平台注册流程、验证码或反滥用规避策略；本任务只把现有 Outlook 执行引擎接入 Register 的任务、结果、代理和日志边界，不新增规避行为。
- 当前日期：2026-09-25。
- 验收：Outlook 任务与 GPT 任务完全隔离；配置可读/写/校验；启动/停止/进度/日志/失败统计独立持久化；注册结果不依赖旧 `Results` 文件并写入 Mongo；代理来自主 Mongo 且支持分组；邮箱池和接码数据来自 Mongo；MailCom SQLite 重复迁移不重复建档、不覆盖源文件，凭据解密后重新使用主服务 DPAPI 保护；普通 API/日志/统计不返回账号凭据或 refresh token；主服务可提供生产 Vue 控制台且启停流程不依赖 3211/8001；前后端检查与测试通过。

## 当前阶段

代码融合已进入“主服务可运行、真实外部注册待受控验证”阶段。Outlook 任务现在使用 `auto / registration / authorized / both` 四种执行模式：`auto` 优先校验 Mongo 中已有 OAuth 账号，没有候选时进入现有浏览器注册引擎；注册结果通过回调写入 Mongo，并在 OAuth/Graph 校验通过后发布邮箱池。

## 已完成

- 已新增独立 Outlook 注册任务 Mongo 服务：配置校验/脱敏、独立任务状态、启动/停止/重置、日志和失败统计；API 不再读取 GPT `runs`。
- Outlook 页面已增加独立任务控制、配置保存、代理组/代理计数和日志展示；前端 gateway 已补齐任务接口。
- 已移除“注册执行适配器未启用”的默认路径；主服务通过统一适配器按 `auto / registration / authorized / both` 分流，明确记录执行模式。
- 旧浏览器注册引擎已通过动态加载适配到主任务，接收主 Mongo 代理候选；Mongo 代理源无可用候选时会在启动浏览器前失败，不回退到本地 7890。
- 运行期 `result_sink` 存在时，旧引擎结果直接进入 `outlook_accounts`，并等待浏览器线程收尾后再清理 sink；`Results` 文件仅保留迁移/回滚来源。
- 已接入 Outlook Mongo store、按规范化邮箱幂等的旧 `Results` 文件导入、只读校验备份与迁移统计；迁移只读取旧 `Results`，不覆盖或删除原件。
- 已接入 OAuth/Graph 验证、Graph 邮件读取及邮箱池发布门槛；GPT 成功分配 Outlook 邮箱时保留账号关联并标记为已分配。
- 已将 Outlook 账号管理页切换到主 API；代理清单与分组复用主 Mongo 资源；邮箱池支持 Outlook 来源筛选和分配状态展示。
- 旧 Outlook 8001 服务未由主启停脚本启动；主服务已提供 Outlook 账号/Graph 管理 API。
- 已补迁移、OAuth/Graph/OTP 读信模拟、敏感字段隔离、导入幂等、冲突邮箱不接管、GPT 成功分配、邮箱换绑，以及 Mongo browser-probe 集成夹具测试。
- 已补统一适配器模式分流、同步 worker 收尾不自锁、停止期间不被迟到进度重开、日志凭据脱敏、接码子邮箱收件人隔离、邮箱池运行 API，以及主任务启动到 Mongo 代理注入的回归测试。
- 已移除无依赖时的生命周期-only disabled fallback；独立构造 `OutlookRegisterTaskService` 也会自动接入统一执行适配器，避免误报“仅完成任务生命周期检查”。
- Vue 已补执行模式、任务轮询、邮箱池分类/子邮箱生成/批量 OAuth/定时检查/导出/OTP 操作。

## 进行中

- 主服务已重启并运行在 `127.0.0.1:8000`；需要在有明确测试账号、可用 Mongo 代理和受控浏览器环境时验证真实注册引擎的浏览器执行、结果入库和 Graph 发布。
- 真实 Microsoft OAuth/Graph、真实 IMAP、真实验证码邮件和外部代理仍需对应环境执行；当前不能把离线模拟当作真实注册交付。

## 已验证的本阶段结果

- 修正 Outlook 注册任务配置/任务集合及 MailCom 迁移集合的 `_id` 索引声明；Mongo 自动维护 `_id` 唯一索引，不能以 `unique=True` 重复创建。新增真实 Mongo 集成回归测试。
- Outlook 统一适配器已接入主任务：模式分流、账号筛选、OAuth/Graph 校验、Mongo 代理传递、邮箱池发布和独立统计均有模拟测试。
- 当前 focused Outlook/Mongo API 测试：47 passed；全后端测试：783 passed、14 skipped。
- 本地 Mongo 集成套件本次结果为 12 passed、1 warning；已补齐 browser-probe 请求默认国家和 JP 代理夹具，Mongo 集成门禁现在全绿。
- 前端本阶段已执行：Vitest 16 文件/119 项通过，`npm run type-check` 和 `npm run build-only` 通过；生产构建保留主 chunk 超过 500 KB 的非阻断提示。
- 主服务重启冒烟：`/api/health`、`/api/mailcom/health`、`/api/outlook/register`、Outlook pool stats/accounts、`/launch`、`/mailcom`、`/outlook-register` 均 200；授权模式空候选任务完成且日志显示真实授权执行器，不再显示“适配器未启用”。当前监听仅有 8000，未启动 3211、8001、5173。

- MailCom Mongo service、内部 `mailcom://account|alias` 句柄、SQLite 只读备份/副本迁移和主服务 DPAPI 前缀重加密已落地。
- 主服务邮箱同步不再主动请求旧 3211；旧 HTTP 句柄仅保留迁移/回滚兼容。
- 新增服务级测试覆盖 MailCom 导入幂等/脱敏、别名同步、Outlook 独立任务状态/代理组/结果入库。
- 前端 Vitest：16 个文件、119 项通过（全量验证使用 10 秒测试超时）；type-check/build-only 通过；生产构建仅有主 chunk 超过 500 KB 的非阻断提示。
- 后端：本次全量 `app/tests/backend` 为 783 passed、14 skipped；Python compileall 和 git diff --check 通过。MailCom 兼容相关测试包含在该结果中。
- 静态托管冒烟：`/api/health` 200，`/launch`、`/mailcom`、`/outlook-register` 200；未知 `/api/tools/payment-links` POST 404。
- 受控本地浏览器执行探针已从主 API 走通：配置 `execution_mode=registration`、`tasks=1`、`concurrent_flows=1` 并注入主 Mongo 代理后，任务提交 1 个浏览器流程，日志出现“浏览器注册执行引擎已启用”，未出现“适配器未启用”；由于探针代理是故意不可连的本地端口，最终按 `browser_launch_fail=1` 结束，Mongo 账号/邮箱池保持 0，证明失败被正确收口而不是假报成功。探针代理已删除，任务已重置。
- 修复 Outlook 任务 reset 后遗留 `proxyGroup`/`proxyCount` 的生命周期元数据；重点测试现为 47 passed，重启主服务后通过 HTTP 验证 reset 会清空旧代理元数据。
- 任务异常日志现在保留脱敏后的可诊断错误明细；密码、Token 等敏感值仍不会进入日志。

## 待处理 / 风险

- 代码已推送到 `codex/outlook-single-service`，现有 PR #5 已更新且 CI 通过；PR 仍保持 OPEN，等待真实外部注册验收。
- 真实浏览器注册引擎尚未在受控测试账号/代理环境运行；当前 Mongo 代理池为空，不能用生产账号或无代理配置宣称注册链路已验收。
- `outlook.com` 消费者账号创建仍沿用仓库已有浏览器引擎；本次没有改动其验证码/反滥用行为，也没有为其新增规避逻辑。
- 真实 Microsoft OAuth/Graph、IMAP、验证码邮件和外部代理仍需对应环境执行；本地受控探针只证明主服务已调用浏览器引擎并正确记录失败，不能替代真实账号成功验收。
- 生产前端主 chunk 约 800 KB；构建成功，本任务不做无关分包改造。
- 后端存在既有 `httpx`/Starlette 弃用提示和一个 coroutine 未 await 警告，不影响当前测试退出码。

## 决策记录

- Outlook 任务使用独立 `outlook_register_tasks`、`outlook_register_logs` 和 `outlook_register_config` 集合，不复用 GPT `runs`。
- `execution_mode=auto` 是默认值：有 Mongo OAuth 候选走授权校验；无候选走已有浏览器注册引擎；需要强制行为时使用 `registration`、`authorized` 或 `both`。
- Outlook 结果通过任务执行适配器写入 `outlook_accounts`；旧 `Results` 仅作为首次迁移/回滚来源。
- MailCom 凭据从旧 SQLite DPAPI 解密后，使用主服务当前 Windows 用户 DPAPI 新前缀重新加密；不直接搬运旧加密 blob。
- MailCom 普通列表、邮箱池列表、任务日志和迁移统计只返回脱敏元数据；凭据仅在需要 IMAP/别名操作时在服务内部解密。
- 不自动删除旧 SQLite；迁移备份只读、可重跑、冲突不覆盖。

## 验收命令

- 后端：`python -m pytest tests/backend`
- MailCom 兼容测试：`python -m pytest ../mailcom-manager/tests`
- Python 编译：`python -m compileall -q backend`
- 前端：`npm run type-check`、`npm test`、`npm run build-only`
- Diff：`git diff --check` 与敏感信息扫描
- Windows 流程：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-autoregister.ps1 -NoBrowser`，检查 `http://127.0.0.1:8000/api/health`、主控制台和停止脚本；不启动 3211/8001。

## 下一步唯一动作

在受控测试环境配置一个非生产 Outlook 测试账号、可用主 Mongo 代理和小任务数，运行 `execution_mode=registration` 的真实浏览器注册；核对任务日志/停止流程、Mongo `outlook_accounts` 与邮箱池写入，以及 `Results` 文件未被运行期修改，完成受控注册验收后更新 PR #5 的验收证据，再决定合并。
