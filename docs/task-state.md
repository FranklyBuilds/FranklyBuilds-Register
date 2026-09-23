# 当前任务状态

## 项目主线

构建一个面向 Windows 本机运行的账号工作流控制台，使用 Vue 3、FastAPI 和 MongoDB，统一管理邮箱、代理、浏览器任务、账号状态、支付工具和成品。

## 当前任务

修复 GitHub Actions 中安装包分析测试因归档证据未被 Git 跟踪而失败的问题，并通过新 PR 合入主分支。

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
- [x] 定位 GitHub Actions 失败原因为 `artifacts/reverse/...` 被 `.gitignore` 整体排除
- [x] 仅纳入测试和只读分析接口所需的清单、报告与 MoMo 源文件，不提交安装器和 payload 压缩包
- [x] 本地完整验证通过：后端 755 passed / 13 skipped，MailCom 14 passed，前端 119 passed，类型检查和生产构建通过
- [ ] 在具备真实代理、Chromium、验证码和 OAuth2 环境下执行完整端到端注册验证
- [ ] GitHub Actions 新 PR 检查通过

## 已完成

- 已拉取远程仓库到当前工作区。
- 已确认项目主线和主要技术栈。
- 已新增项目级开发规范。
- 已初始化本任务状态文件。
- 已核对 `FB-Outlook-register` 上游结构与 API 契约。
- 已完成 Outlook 后端代码和主项目启动链路集成。

## 进行中

提交修复分支并等待 GitHub Actions 验证。

## 待处理

- 随后续开发任务更新当前目标、验收标准和验证命令。
- 重要架构或范围决策写入“决策记录”。

## 决策记录

- 规范只写入本项目根目录，不修改任何全局配置。
- 使用 `AGENTS.md` 作为项目级规则入口，使用 `docs/task-state.md` 作为外部工作记忆。
- 独立任务完成后必须提交 commit；需要合并的任务必须创建或更新对应 PR。
- Outlook 注册后端使用独立 8001 端口，主 FastAPI 保持 8000，避免路由和依赖冲突。
- 当前 Vue 页面复用上游注册 API，不复制上游独立 Web 前端；上游 API 的池、结果和接码接口仍随后端保留。
- 安装包分析接口继续使用 `artifacts/reverse/GPT-Register-Tool-Setup-v2026.08.05` 作为只读证据源；Git 只跟踪必要清单、报告和 MoMo 源码，忽略原始 EXE、ZIP 和其余 payload。清单中的机器绝对路径在载入时归一为相对/文件名形式。

## 下一步

推送 `codex/fix-ci-installation-evidence` 并创建修复 PR，等待 GitHub Actions 结果。
