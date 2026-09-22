# 当前任务状态

## 项目主线

构建一个面向 Windows 本机运行的账号工作流控制台，使用 Vue 3、FastAPI 和 MongoDB，统一管理邮箱、代理、浏览器任务、账号状态、支付工具和成品。

## 当前任务

将 `E:\code\FB-Outlook-register\OutlookRegisterPlus` 的 Outlook 注册后端完整纳入主项目，并由主项目启动脚本统一管理。

## 验收标准

- [x] 根目录存在项目级 `AGENTS.md`
- [x] 规范覆盖开发、任务执行、AI 上下文和完成定义
- [x] 存在可持续更新的任务状态文件
- [x] 已将 commit 和 PR 要求写入项目规范
- [x] 上游 Outlook 注册核心、API、配置模板和许可证纳入 `app/outlook_register/`
- [x] 主项目启动/停止脚本管理 8001 Outlook 注册服务
- [x] Vue 控制台兼容上游嵌套状态，并暴露 OAuth2、辅助邮箱和代理配置
- [x] 前端类型检查通过，16 个测试文件 / 119 个测试通过
- [x] Outlook 后端 Python 编译通过，8001 `/api/health` 与 `/api/register` 冒烟通过
- [ ] 在具备真实代理、Chromium、验证码和 OAuth2 环境下执行完整端到端注册验证

## 已完成

- 已拉取远程仓库到当前工作区。
- 已确认项目主线和主要技术栈。
- 已新增项目级开发规范。
- 已初始化本任务状态文件。
- 已核对 `FB-Outlook-register` 上游结构与 API 契约。
- 已完成 Outlook 后端代码和主项目启动链路集成。

## 进行中

确认真实 Outlook、代理、验证码和 OAuth2 环境下的注册任务。

## 待处理

- 随后续开发任务更新当前目标、验收标准和验证命令。
- 重要架构或范围决策写入“决策记录”。

## 决策记录

- 规范只写入本项目根目录，不修改任何全局配置。
- 使用 `AGENTS.md` 作为项目级规则入口，使用 `docs/task-state.md` 作为外部工作记忆。
- 独立任务完成后必须提交 commit；需要合并的任务必须创建或更新对应 PR。
- Outlook 注册后端使用独立 8001 端口，主 FastAPI 保持 8000，避免路由和依赖冲突。
- 当前 Vue 页面复用上游注册 API，不复制上游独立 Web 前端；上游 API 的池、结果和接码接口仍随后端保留。

## 下一步

已执行 `npm run type-check`、`npm test`、Python 编译和 8001 API 冒烟；下一步唯一动作是运行一次真实注册链路。
