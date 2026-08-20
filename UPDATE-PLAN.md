# Email Automation 客户端一键更新与回滚计划

## Summary

为便携版增加“版本化发布 + 一键更新器”能力。客户现有的 `.env`、OAuth、数据库、联系人、Campaign、Approval、知识库和 TACWork Session 全部保留；更新器只替换应用源码、前端构建产物、启动脚本、依赖运行时和 TACWork Runtime。

更新来源使用受控的私有 HTTPS 下载地址。

## Implementation Changes

### 1. 版本与发布清单

新增统一版本文件，例如：

```text
release-manifest.json
```

包含产品版本、发布时间、最低更新器版本、更新包地址、SHA-256、包大小、是否需要重启和是否需要数据库迁移。

版本号采用 SemVer：

- `MAJOR`：不兼容升级
- `MINOR`：新功能
- `PATCH`：Bug 修复和小优化

Workspace、ZIP 包和运行状态读取同一个版本号，避免 Web、TACWork 和启动器版本不一致。

### 2. 客户端更新器

新增：

```text
scripts/update.ps1
update.bat
```

更新器流程固定为：

1. 检查 Workspace 是否完整。
2. 检查服务状态。
3. 调用 `stop-stack.bat` 停止 Backend、Consumer、Frontend、TACWork 和 OpenCode。
4. 创建带时间戳的完整备份。
5. 读取当前版本和远程 `release-manifest.json`。
6. 比较版本，已是最新则退出且不修改文件。
7. 下载更新包到 Workspace 外部临时目录。
8. 校验 SHA-256；失败立即终止。
9. 解压到临时目录，不直接覆盖当前运行目录。
10. 校验新包必需文件和版本。
11. 只替换应用文件。
12. 保留客户本地数据和配置。
13. 执行数据库迁移；迁移失败立即回滚。
14. 执行 Python、Frontend、TACWork 和脚本完整性检查。
15. 启动统一服务并运行健康检查。
16. 全部通过后写入更新成功记录。
17. 失败则恢复备份、重新启动旧版本并报告原因。

### 3. 必须保留的客户内容

更新器不得删除或覆盖：

```text
backend/.env
backend/app.db
backend/data/
backend/data/huey.db
backend/data/oauth*
backend/data/tokens*
logs/
```

同时保留 Gmail OAuth、加密 Token、Contacts、Campaign、Approval、Draft、Automation、Knowledge Base、AuditLog、TACWork Session 和用户自定义配置。

允许重建的缓存目录：

```text
backend/.venv
frontend/node_modules
frontend/.next*
backend/.pytest_cache
```

更新器不得通过删除整个 Workspace 再解压替代更新。

### 4. 原子更新与回滚

使用 `.update-staging/`、`.update-backup/` 和 `.update-state.json` 管理临时文件、备份和更新状态。

更新状态至少记录：

```json
{
  "from_version": "1.0.0",
  "to_version": "1.1.0",
  "phase": "health_check",
  "started_at": "...",
  "backup_path": "...",
  "status": "running"
}
```

启动时检测到上一次更新处于 `running` 或 `failed`，必须进入恢复检查，不得再次覆盖更新。

每次更新前备份 `.env`、数据库、`backend/data`、TACWork Session 数据和当前版本清单。默认保留最近 3 个备份，并至少保留一个可回滚版本。

### 5. 数据库迁移兼容

新增统一迁移入口：

```text
scripts/migrate-update.ps1
```

要求：迁移幂等；迁移前自动备份数据库；失败返回非零退出码；不得清理业务数据；迁移版本独立记录；旧版本数据必须能被新版本读取；新版本无法启动时恢复旧版本和旧数据库备份。

### 6. 私有发布服务

维护私有 HTTPS 发布目录：

```text
/releases/
├─ release-manifest.json
├─ Email-Automation-1.1.0-full.zip
├─ Email-Automation-1.1.0-patch.zip
└─ signatures/
```

第一版建议发布完整版本包，而不是二进制差分补丁，以确保运行时和依赖完整。下载必须支持 HTTPS、SHA-256 校验、失败重试、超时和临时文件下载；下载失败不得影响当前版本。

### 7. Web 与 TACWork 更新入口

在 Email Automation Web 的 Agent/Settings 区域增加当前版本、最新版本、检查更新、下载并更新、更新进度、上次更新结果和回滚入口。

更新期间显示停止服务、备份、安装、迁移、启动和健康检查进度。更新按钮必须经过管理员确认；TACWork 可以解释状态，但不能绕过备份、校验、迁移和回滚流程。

### 8. 更新权限与安全边界

- 仅本机管理员可执行更新。
- 更新脚本必须校验 Workspace 路径。
- 远程 manifest 不能直接执行脚本。
- 下载完成并校验前不得执行更新包内容。
- 更新前必须停止所有项目服务。
- 更新完成后必须通过统一健康检查。
- 更新失败不得自动发送邮件、同步 Gmail、创建 Draft、批准 Approval 或启用 Automation。
- 更新过程中不修改客户业务数据，只允许执行必要的数据库 schema 迁移。

## Test Plan

### 基础发布测试

- 生成两个版本包并核对版本号、manifest、SHA-256 和文件清单。
- 确认 `.env`、OAuth、数据库和客户数据不进入发布包。
- 确认 Python、Node、TACWork Runtime 和离线依赖完整。

### 升级测试

在测试 Workspace 中准备 Gmail OAuth、Contacts、Campaign、Knowledge Base、Approval、TACWork Session、`.env` 和数据库，然后执行旧版本启动、更新、健康检查、Gmail 连接、业务数据、Session 和本地时区验证。

必须确认更新过程中没有新增发送、业务 Run 或其他运营写操作。

### 失败回滚测试

覆盖下载失败、SHA-256 不匹配、压缩包损坏、必需文件缺失、数据库迁移失败、Backend 启动失败、Consumer 不健康、TACWork 不健康、Frontend 非 200 和更新进程中断。

每种失败都必须保留旧版本、配置和业务数据，恢复服务并输出明确错误。

### 客户体验验收

- 客户只需点击一次“检查更新”或运行 `update.bat`。
- 不需要重新安装 Python、Node 或依赖。
- 不需要重新授权 Gmail 或重新输入模型 Key。
- 更新失败时显示可理解的中文错误。
- 更新完成后 Web 和 TACWork 自动恢复。

## Assumptions and Defaults

- 默认采用“一键更新器”。
- 更新源使用私有 HTTPS 地址。
- 默认更新源码、前端、启动脚本、依赖运行时和 TACWork Runtime。
- 默认保留客户配置、业务数据、OAuth 和 TACWork Session。
- 第一版发布完整更新包，不实现二进制差分补丁。
- 更新器只处理软件更新，不执行 Gmail 同步、邮件发送、Approval 操作或 Automation 操作。
- 现有 `portable-package.ps1` 继续用于首次交付包；后续新增 `release-package.ps1` 用于版本发布包。
- 当前 `Email-Automation-portable-20260811.zip` 作为基线版本；正式实施更新器前补充正式版本号，例如 `1.0.0`。

## Current Status

本文件仅记录后续更新能力建设计划。本次未实现更新器、未修改启动流程、未修改数据库、未修改配置，也未改变当前客户 ZIP 包行为。
