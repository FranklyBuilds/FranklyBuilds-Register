# 当前任务状态

## 项目主线

构建 Windows 本机账号工作流控制台，使用 Vue 3、FastAPI 和 MongoDB 统一管理邮箱、代理、浏览器任务、账号状态、支付工具和成品。

## 当前任务

在不改变 Outlook 外部注册流程和反滥用行为的前提下，完成 Outlook 任务管理、MailCom Hub 主服务化、统一启动/停止与旧数据迁移回滚闭环。

## 任务范围

- 涉及：`app/backend` Outlook 独立任务状态/配置/日志/结果写入、主 Mongo 代理分组接入、MailCom 账号/别名/IMAP/OTP/服务器推送服务化、SQLite 幂等迁移与 DPAPI 重新保护、Vue 控制台、Windows 启停与生产静态前端服务、相关测试和文档。
- 不涉及：新增或改造第三方平台注册自动化、验证码/反滥用规避策略、外部目标行为；已有 Outlook 注册引擎仅作为可插拔执行适配器接入，不扩大其行为。
- 当前日期：2026-09-24。
- 验收：Outlook 任务与 GPT 任务完全隔离；配置可读/写/校验；启动/停止/进度/日志/失败统计独立持久化；注册结果不依赖旧 `Results` 文件并写入 Mongo；代理来自主 Mongo 且支持分组；MailCom SQLite 重复迁移不重复建档、不覆盖源文件，凭据解密后重新使用主服务 DPAPI 保护；普通 API/日志/统计不返回凭据；主服务可提供生产 Vue 控制台且启停流程不依赖 3211/8001；前后端检查与测试通过。

## 当前阶段

代码完成，交付前验证已完成；真实外部服务验收按环境条件保留为部署后操作。

## 已完成

- 已新增独立 Outlook 注册任务 Mongo 服务：配置校验/脱敏、独立任务状态、启动/停止/重置、日志和失败统计；API 不再读取 GPT `runs`。
- Outlook 页面已增加独立任务控制、配置保存、代理组/代理计数和日志展示；前端 gateway 已补齐任务接口。
- 默认执行适配器保持关闭，避免把第三方平台注册自动化或反滥用行为隐式纳入主服务；适配器接收主 Mongo 代理候选，不回退到本地 7890。
- 已接入 Outlook Mongo store、按规范化邮箱幂等的旧 `Results` 文件导入、只读校验备份与迁移统计；迁移只读取旧 `Results`，不覆盖或删除原件。
- 已接入 OAuth/Graph 验证、Graph 邮件读取及邮箱池发布门槛；GPT 成功分配 Outlook 邮箱时保留账号关联并标记为已分配。
- 已将 Outlook 账号管理页切换到主 API；代理清单与分组复用主 Mongo 资源；邮箱池支持 Outlook 来源筛选和分配状态展示。
- 旧 Outlook 8001 服务未由主启停脚本启动；主服务已提供 Outlook 账号/Graph 管理 API。
- 已补迁移、OAuth/Graph/OTP 读信模拟、敏感字段隔离、导入幂等、冲突邮箱不接管、GPT 成功分配及邮箱换绑测试。
- 前端和后端既有测试在上一阶段通过；本阶段已重新执行完整验证。

## 进行中

- 全量验证和 Windows 本机端到端冒烟；真实 Microsoft OAuth/Graph、真实 MongoDB、真实 IMAP 仍需在具备对应外部服务的环境执行。

## 已验证的本阶段结果

- MailCom Mongo service、内部 `mailcom://account|alias` 句柄、SQLite 只读备份/副本迁移和主服务 DPAPI 前缀重加密已落地。
- 主服务邮箱同步不再主动请求旧 3211；旧 HTTP 句柄仅保留迁移/回滚兼容。
- 新增服务级测试覆盖 MailCom 导入幂等/脱敏、别名同步、Outlook 独立任务状态/代理组/结果入库。
- 前端 Vitest：16 个文件、119 项通过；type-check/build-only 通过；生产构建仅有主 chunk 超过 500 KB 的非阻断提示。
- 后端：767 passed、13 skipped；MailCom 兼容测试 14 passed；Python compileall、PowerShell 启动/停止脚本解析和 git diff --check 通过。
- 静态托管冒烟：`/api/health` 200，`/launch`、`/mailcom`、`/outlook-register` 200；未知 `/api/tools/payment-links` POST 404。

## 待处理 / 风险

- 尚未在当前环境执行真实 Microsoft OAuth/Graph、真实 MongoDB、真实 IMAP 和真实 Windows 进程启停；已完成离线模拟与静态托管冒烟，部署时需按验收命令执行。
- 生产前端主 chunk 约 800 KB；构建成功，本任务不做无关分包改造。
- 真实 Outlook 注册执行适配器未启用；当前交付的是独立任务管理、Mongo 结果接收和可插拔适配器边界，不扩展第三方平台注册/反滥用流程。
- 后端测试仍有一个既有测试夹具的 coroutine 未 await 警告，不影响 767 项通过结果。

## 决策记录

- Outlook 任务使用独立 `outlook_register_tasks`、`outlook_register_logs` 和 `outlook_register_config` 集合，不复用 GPT `runs`。
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

提交当前融合变更的 Git commit，并在需要合并时创建 PR；部署环境随后按真实 Mongo、OAuth/Graph、IMAP 和 Windows 启停验收命令复核。
