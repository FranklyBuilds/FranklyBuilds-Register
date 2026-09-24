# 当前任务状态

## 项目主线

构建 Windows 本机账号工作流控制台，使用 Vue 3、FastAPI 和 MongoDB 统一管理邮箱、代理、浏览器任务、账号状态、支付工具和成品。

## 当前任务

将 Outlook 账号/OAuth/Graph 收信和邮箱池融合到主 FastAPI（8000）及主 MongoDB；以幂等、保留原件方式迁移旧 Outlook 数据。

## 任务范围

- 涉及：`app/backend` Outlook 数据模型/API/迁移/邮箱分配、Outlook Graph 收信适配、Vue Outlook 与邮箱池界面、Windows 主服务启停说明及测试。
- 不涉及：扩展第三方平台注册自动化；Outlook 注册器的外部目标行为调整；Mongo 代理集合另建或复制。
- 验收：迁移重复运行不重复导入且不改源文件；OAuth 与 Graph 验证双通过才发布 Outlook 邮箱；成功分配后邮箱从可用池移除但 Outlook 关联保留；普通邮箱列表不返回 Outlook 密钥/令牌；Graph/OAuth/export 模拟测试；手动与 MailCom 兼容；主服务不启动 8001；类型检查、前端测试、后端相关测试、生产构建通过。

## 当前阶段

代码完成；本地自动化验证通过；待提交 Git commit。运行中的主服务/真实 MongoDB 冒烟未执行。

## 已完成

- 已接入 Outlook Mongo store、按规范化邮箱幂等的旧文件导入、只读校验备份与迁移统计；迁移只读取旧 `Results` 文件，不覆盖或删除原件。
- 已接入 OAuth/Graph 验证、Graph 邮件读取及邮箱池发布门槛；GPT 成功分配后保留 Outlook 账号关联并从可用池移除。
- 已将 Outlook 账号管理页切换到主 API；代理清单与分组复用主 Mongo 资源；邮箱池支持 Outlook 来源筛选和分配状态展示。
- 主 Windows 启停脚本不再启动或停止独立 8001 Outlook 服务；主服务生命周期启动迁移。
- 已补迁移、OAuth/Graph/OTP 读信模拟、敏感字段隔离、导入幂等、仅本机导出、冲突邮箱不接管、GPT 成功分配及邮箱换绑测试。
- 前端：类型检查通过；Vitest 16 个文件 / 119 项通过；生产构建通过（存在现有的大 chunk 体积提示）。
- 后端：`pytest -q tests` 通过，765 passed / 13 skipped。另有 Starlette/httpx 弃用提示及一个既有支付测试 coroutine warning。
- Python 编译检查：从 `app` 目录执行 `python -m compileall -q backend` 通过。
- `git diff --check` 通过；本地 8000 健康检查未连通（当时主服务未运行），因此没有真实 MongoDB/Windows 启停冒烟结果。

## 决策记录

- Outlook 凭据存入 Mongo `outlook_accounts`；邮箱资源与普通账号响应只携带来源、内部 Graph URI 和 `outlookAccountId` 关联，不携带 refresh token、client secret 或 Outlook 密码。
- 迁移在主服务启动时执行；按规范化邮箱幂等 upsert；源文件复制为同目录只读备份，迁移统计不含凭据。
- 仅 OAuth refresh 和 Graph `/me` 身份匹配均成功时发布 Outlook 邮箱；OAuth/Graph 失败会将可分配邮箱标为不可用。
- GPT 成功注册消费邮箱时，手动/MailCom 资源维持既有删除行为；Outlook 资源改标 assigned，并保留关联供后续 Graph 收信。
- Outlook 的邮箱注册器外部自动注册任务不在本次范围内；官方启动脚本停止管理其 8001 服务。其遗留 `Results` 文件只用于主服务迁移和回滚；若绕过官方入口手工启动旧注册器，其旧代码仍可能写本地文件。

## 遗留风险

- 未执行真实 Microsoft OAuth/Graph 请求、真实 MongoDB 迁移和 Windows 启停脚本端到端验证；对应流程由 mock/单元测试覆盖。
- 生产构建提示主 JS chunk 超过 500 kB；本任务未做无关的代码分包改造。
- 已创建实现 commit，并创建 PR #3：https://github.com/FranklyBuilds/FranklyBuilds-Register/pull/3。

## 下一步唯一动作

等待 PR #3 检查与合并。
