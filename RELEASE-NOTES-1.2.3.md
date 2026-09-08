# Email Automation v1.2.3 发布说明

- 版本：**1.2.3**
- 发布来源：Git 精确标签 `v1.2.3`（构建与交付只以该 tag 为准）
- 日期：2026-09-07
- 安装包：`Email-Automation-Setup-1.2.3.exe`（构建产物，位于 `dist/`）
- 性质：**未签名内部候选包，仅供内部试装验收，不得直接交付客户**

---

## 一、本版范围

v1.2.3 是稳定性收敛版本，**不引入新业务功能**。范围限于三类：

1. **Gmail 可靠性**：修复真实账号被 demo 层劫持、配额错误重试失效、脏游标导致增量同步失败。
2. **启动与打包**：修复启动脚本环境变量重复键崩溃、前端产物缺失时的静默失败、打包清单自描述导致的哈希陈旧、构建闸门只查存在性不查一致性。
3. **诊断稳定性**：手动诊断改为严格只读、诊断与业务配置分离、请求链路可追踪、调度器心跳可观测。

前置说明：2026-09-03 曾在装机副本上修过一批问题但**未回灌源码**，导致每次重建安装包都会把缺陷重新打进去。本版已将该批修复全部回灌至源码工作区，这是部署反复踩坑的主导原因。

---

## 二、已修复问题

### 2.1 Gmail 可靠性

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | 已连接真实 Gmail 账号仍只导入 0 线程，`historyId=1000` | `get_transport_for_account` 先查缓存再校验证书，demo 层 `InMemoryGmailTransport` 一旦缓存就永久劫持真实账号 | 先校验真实凭证可用再决定是否复用缓存；OAuth 回调成功后主动 `clear_transport_cache(account.id)` |
| 2 | 增量同步报 `cursor_expired`，用户无从判断该做什么 | 同步异常被统一归为 `GMAIL_SYNC_FAILED` | 区分 `initial_import_required`，返回 409 `INITIAL_IMPORT_REQUIRED` 并提示执行全量导入 |
| 3 | 首轮全量导入撞配额后不再重试，直接失败 | Gmail 每分钟配额错误返回 **HTTP 403**（不是 429），原重试分支只认 429/5xx | 按响应体 `rateLimitExceeded` / `usageLimits` 识别，403 与 429 一并纳入，退避 65 秒（配额窗口 60 秒） |
| 4 | 脏游标（如 demo 哨兵 `1000`）触发无效增量同步 | 游标合法性未校验 | 新增 `_looks_like_gmail_history_id()`，显式拒绝哨兵值 `1000`，非法游标直接抛 `initial_import_required` 强制全量 |
| 5 | 扫描任务在导入未完成时提前退出 | 任务轮询未等待扫描真正结束 | `models.py` 增加 `scan_completed` 列，`tasks.py` 改为 `while not run.scan_completed` |

### 2.2 启动与打包

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 6 | `Start-Process` 抛 `ArgumentException`，堆栈起不来 | PowerShell 5.1 用大小写敏感字典复制环境变量，`http_proxy`/`HTTP_PROXY` 这类重复键直接崩 | 启动时统一折叠所有大小写重复的环境变量（`Path` 优先保留） |
| 7 | 缺少 `.next-prod` 时前端静默失败，排查成本高 | 独立产物目录不存在仍尝试启动 | 检测到无 `.next-prod` 且存在静态产物时自动跳过并给出明确原因 |
| 8 | 便携包清单哈希校验必然失败 | 清单在枚举时把自身也算进去，写完即陈旧 | 清单生成排除自身 |
| 9 | 构建闸门放过坏清单 | 只做 `Test-Path` 存在性判断，文件存在但哈希陈旧照样通过 | 升级为存在性 + SHA-256 逐条比对，不一致即 `Fail` |
| 10 | 运行时与数据目录口径不一 | 构建期运行时生成/校验口径分散 | `build-runtime.ps1`、`verify-runtime.ps1` 补齐校验与报告 |
| 11 | 停止脚本误杀/漏杀 | 进程归属匹配过宽 | `stop-stack.ps1` 的 `Test-WorkspaceProcess` 收紧为路径前缀 + 正则双重匹配 |

### 2.3 诊断稳定性

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 12 | 手动诊断会写数据库 / 写 OAuth / 自动修复，用户不敢点 | 诊断复用了业务配置 | 新增 `get_diagnostic_settings()`，诊断探针独立且**严格只读**：不建探测文件、不写库、不写 OAuth、不自动修复 |
| 13 | 无法判断"现在跑的到底是哪一套" | 健康检查缺少实例身份信息 | `main.py` 增加 `request_context_middleware`（`X-Request-ID`），`/api/health` 返回 `instance.install_root` 与 `instance.data_root` |
| 14 | 调度器是否活着不可观测 | 无心跳 | `scheduler/runner.py` 写心跳文件 |
| 15 | 诊断结论会泄漏凭证 | 日志脱敏不彻底 | 诊断输出统一走 `redact_for_log` |
| 16 | 前端无法直观查看诊断 | 无入口 | Dashboard 增加 Diagnostics 标签页；`api.ts` 增加 `unknown` 整体状态 |
| 17 | "发送安全口径"误报 | 空白名单被判为错误 | 空 `RESTRICTED_RECIPIENT_ALLOWLIST` 不再判错（仅在配置了白名单时才限制） |

---

## 三、验证结果（全部实测）

| 检查项 | 结果 |
|---|---|
| 后端测试（隔离数据目录） | **308 passed**，337.67s，exit 0 |
| 前端 TypeScript | 0 error |
| Electron `main.cjs` / `preload.cjs` 语法 | 通过 |
| 进程归属离线校验 | **PASS**，14 项断言，未启动/停止任何服务 |
| `git diff --check` | exit 0（仅 CRLF 归一化告警） |
| 版本号一致性 | 7 处版本源全部为 `1.2.3` |
| `git status --porcelain` | 发布提交后为空 |

---

## 四、已知限制

1. **`portable-diagnose.ps1:96` 端口检查仍用 8000/3000，而真实端口组是 18000–18003** → 端口冲突检测会出**假绿灯**，而 `port_conflict` 正是客户机真实故障之一。本版未修（不在本轮清单），已记录待办。
2. `backend/app/services/sync.py` 带 UTF-8 BOM：裸 `ast.parse` 会报 U+FEFF（既有现象），`import` 不受影响。
3. 数据目录迁移产生的备份文件（`*.legacy-archived-*` / `*.preshell-*`）仍在数据目录内，确认稳定后可手动清理，不会自动删除。
4. 本版**不含**自动更新/补丁能力。仓库中的 `UPDATE-PLAN.md` 是一份**尚未实现**的前瞻规划（含 §5.2、§14 两处待决策项与 P0.5 签字门禁），不作为本版交付内容。
5. 真实发送仍由 `ENABLE_REAL_SEND` 控制（默认 false）；白名单由 `app/policy/engine.py` 强制。

---

## 五、支持范围

- **操作系统**：仅 Windows 10 x64 与 Windows 11 x64。不支持 32 位、不支持 ARM、不支持 macOS/Linux。
- **数据隔离**：正式版只读写 `%LOCALAPPDATA%\TAC AISolution\Email Automation\`；开发模式为同路径 `…\Email Automation Dev`，端口组 28000–28003，与正式版 18000–18003 互不影响。
- **本版不做**：不迁移、不清空、不导入、不读取开发模式或客户生产环境的业务数据；不修改既有客户 OAuth / Token / 邮件库 / Automation 状态。

---

## 六、签名状态

> **本安装包未签名。** 安装时 Windows SmartScreen 会提示"未知发布者"，需手动确认继续。
>
> 本包定位为 **v1.2.3 内部试装候选包**，仅用于 Windows 10 x64 / Windows 11 x64 双平台内部验收。
>
> **在完成双平台验收并使用组织代码签名证书签名、核验 Authenticode 发布者/时间戳/签名有效性之前，不得向客户发布。**
>
> 签名后需重新计算 SHA-256 并做最终安装冒烟测试，产物清单为：签名安装包 + SHA-256 + 本发布说明 + 客户安装指南 + 诊断指南 + 已知限制说明。
