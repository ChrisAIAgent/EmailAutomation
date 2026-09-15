# Email Automation 更新与补丁计划

> **文档状态**：设计规格（未实现）。本次修订仅更新本文件，**未修改任何源码、配置或数据库**。
> **修订日期**：2026-09-03
> **基线版本**：`VERSION` = 1.2.2
> **适用范围**：Inno Setup 安装包（`{autopf}\TAC AISolution\Email Automation`）与便携 Workspace 两种交付形态。

---

## 1. 目标与非目标

### 1.1 目标

1. 客户安装后，后续优化与修复以**补丁**形式交付，无需重装、无需重新授权 Gmail、无需重填模型 Key。
2. 代码补丁**免管理员权限**、可回滚、失败自动恢复，全程保留客户数据。
3. 保留一条明确的**降级路径**：补丁无法覆盖的变更，自动回落到完整安装包升级。

### 1.2 非目标（本计划明确排除）

- **不做二进制差分**（bsdiff / courgette）。可更新代码体量小，差分带来的收益低于其复杂度（见 4.1）。
- **不做静默强制更新**。前期一律"用户确认后执行"。
- **更新器不执行任何业务动作**：不触发 Gmail 同步、不发信、不建 Draft、不批准 Approval、不启用 Automation。
- **前期不做在线更新源、不做遥测、不做自动签名验证**（签名结构与字段先预留，见 6.3）。

---

## 2. 现状基线（已核实的事实）

| 项目 | 现状 | 对更新设计的影响 |
|---|---|---|
| 安装目录 | `{autopf}\TAC AISolution\Email Automation`（Program Files） | **写入需提权**，正式运行时保持只读 |
| 数据目录 | `%LOCALAPPDATA%\TAC AISolution\Email Automation\{config,database,queue,logs,tacwork,run}` | 数据与代码已分离；解析器 `scripts/data-dir.ps1` |
| 运行进程 | backend(uvicorn)、huey consumer、frontend(Next)、TACWork runtime | 更新前必须全部停止（文件被占用） |
| 端口组 | 过渡 `18000-18003`，开发 `28000-28003`，统一 `127.0.0.1` | 健康门禁的检测入口 |
| `backend/app` | 3 MB / 173 文件 | 补丁主体 |
| `scripts` | 1 MB / 35 文件 | 补丁主体（但见 4.4 的自举限制） |
| `runtime/frontend-static` | 4 MB / 22 文件 | 补丁主体（Electron 正式 UI 来源） |
| `runtime/frontend` | 24 MB / 1717 文件（Next standalone） | 仅过渡 Web 入口使用，见 5.2 待决策 |
| `tools/python` | 110 MB | **不可补丁**，属运行时基线 |
| `runtime/python-packages` | 153 MB / 10753 文件 | **不可补丁**，属运行时基线 |
| `runtime/electron` | 367 MB | **不可补丁**，属运行时基线 |
| 版本资产 | 根目录 `VERSION` = 1.2.2 | 补丁校验基线 |
| 完整性资产 | `runtime/runtime-manifest.json`（12568 文件 sha256） | 与补丁存在冲突，见 6.5 |
| Schema 迁移 | `backend/app/migrations.py`，启动时幂等**纯增量**（仅 `ADD COLUMN`） | 补丁加字段自动生效；无 `schema_version` 表 |
| 更新器 | **尚不存在**（全仓无 `electron-updater` / `autoUpdater` 代码） | 需新建 |

### 2.1 可复用的既有能力

| 脚本 | 能力 | 在更新流程中的角色 |
|---|---|---|
| `scripts/stop-stack.ps1` | 按 `run/services.json` 与端口验证身份后停止自有服务，**从不杀外部进程** | 更新前停栈 |
| `scripts/start-stack.ps1` | 启动四服务并做健康门禁：`/api/health.status == "ok"` 且 `.consumer.healthy == $true`，通过后打印 `READY` | 更新后启栈 + 健康门禁 |
| `scripts/verify-runtime.ps1` | 按 `runtime-manifest.json` 逐文件校验 sha256，并校验 `manifest.version == VERSION` | 更新后完整性检查（需改造，见 6.5） |
| `scripts/data-dir.ps1` | 唯一的数据目录解析器，导出 `DataRoot / DataDatabase / DataLogs / DataRun` 等 | 更新器定位可写目录 |
| `desktop/main.cjs` | 第 99 行 spawn `start-stack.ps1`；第 80 行 `staticRoot = runtime/frontend-static`；第 56 行读 `VERSION` | overlay 落点 |
| `EMAIL_AUTOMATION_DATA_DIR` | 已由 `main.cjs` 注入子进程环境 | overlay 直接挂在该数据根下，**无需新增路径解析逻辑** |

---

## 3. 架构：双层更新

### 3.1 Tier 1 —— 代码补丁（主路径，免提权）

- 安装目录**保持只读不变**，补丁落地到用户态目录：
  `%LOCALAPPDATA%\TAC AISolution\Email Automation\patch\<version>\`
- 运行时通过**加载顺序覆盖**（overlay）生效，不做文件替换。
- 特点：**免 UAC**、体积约 8 MB（压缩后约 3 MB）、秒级生效、删除目录即回滚。
- 覆盖绝大多数日常优化与缺陷修复。

### 3.2 Tier 2 —— 完整安装包（兜底，需提权）

- 用于运行时基线变更（Python 解释器、依赖包、Electron、原生组件）。
- 复用现有 Inno Setup 安装包：**静默模式默认保留全部业务数据**（`installer/EmailAutomation.iss` 已实现）。
- 触发方式：客户端检测到 `runtime_rev` 不满足 → 提示下载新安装包 → 用户手动或 `/VERYSILENT` 升级。

### 3.3 分流决策

```
读取 manifest
  ├─ patch_type == "code"
  │    └─ runtime_rev(已安装) >= runtime_rev_required ?
  │         ├─ 是 → Tier 1 overlay 补丁
  │         └─ 否 → 降级 Tier 2（提示下载完整安装包）
  └─ patch_type == "runtime"        → Tier 2（完整安装包）
```

---

## 4. 核心设计决策（一旦定错需返工）

### 4.1 决策一：补丁 = 可更新代码树的**完整快照**，不是增量差异

每个补丁包含 `backend\app\`、`runtime\frontend-static\`（及后续 `scripts\`）的**完整目录**，而非仅本次变更的文件。

| 理由 | 说明 |
|---|---|
| 体积不构成压力 | 三棵树合计约 8 MB，压缩后约 3 MB。差分无实际收益 |
| 消除补丁链 | 任意版本客户可**一步跳到最新**，不存在"必须先打 1.2.3 再打 1.2.4"的顺序依赖 |
| **规避 Python 命名空间包半遮蔽** | 若 overlay 只放 `app` 包的部分文件，Python 会将 `app` 解析为跨多个 `sys.path` 条目的命名空间包，产生难以排查的半遮蔽行为。完整目录使 `import app` 单一来源，行为确定 |
| 回滚绝对正确 | 回滚 = 切回上一个完整目录，不存在"部分回滚"的中间态 |

**被否决方案**：增量 diff（bsdiff）、按文件变更列表打包。

### 4.2 决策二：引入运行时基线 `runtime_rev`

- **定义**：运行时基线的短哈希（建议 12 位），由 `tools/python` 版本 + `runtime/python-packages` 顶层包名与版本的有序清单 + Electron 版本计算 sha256 得到。
- **生成时机**：`scripts/build-runtime.ps1` 构建时写入 `runtime/runtime-manifest.json` 的新增字段 `runtime_rev`。
- **Gate 行为**：补丁 manifest 声明 `runtime_rev_required`；客户端比对不符即降级 Tier 2，**不得尝试强行应用补丁**。

**这是"每次发补丁"能否成立的边界条件**——没有它，补丁系统与运行时变更会静默错配。

### 4.3 决策三：前期**离线补丁导入**，后期再接在线源

- 前期（客户个位数）：开发者生成 `.eapatch` 文件 → 通过邮件/IM 发给客户 → 客户在设置页"导入补丁"。
- **不需要服务器、CDN、域名、证书、签名基础设施**。
- 后期演进：同一套 UI 与流程，仅把"选择本地文件"换成"从 `UPDATE_FEED_URL` 下载"。**下载源抽象成一个函数，UI 与更新状态机不变**。

### 4.4 决策四（补充）：`scripts/` 前期**不进 overlay**

原因：`apply-patch.ps1` 与 `make-patch.ps1` 本身位于 `scripts/`，若 overlay 覆盖 `scripts/`，更新器会在运行中替换自己，产生自举问题与文件占用冲突。

- **前期 overlay 范围**：`backend\app\`、`runtime\frontend-static\`。
- 需要更新 `scripts/` 时，走 Tier 2 完整包；或延至 P2 引入独立的 `updater\` 目录（不参与 overlay）后再放开。

---

## 5. 可更新范围与边界

### 5.1 能力矩阵

| 变更类型 | 补丁 | 完整包 | 说明 |
|---|:---:|:---:|---|
| `backend/app/**` 业务逻辑、缺陷修复、Prompt 调整 | ✅ | ✅ | 主要场景 |
| `runtime/frontend-static/**` 前端 UI | ✅ | ✅ | Electron 正式入口 |
| 新增数据库字段（nullable、纯增量） | ✅ | ✅ | `migrations.py` 启动即生效 |
| `scripts/**` 启动与管理脚本 | ❌（前期） | ✅ | 见 4.4 自举限制 |
| 新增 / 升级 Python 依赖 | ❌ | ✅ | 属运行时基线 |
| Python / Node / Electron 版本变更 | ❌ | ✅ | 属运行时基线 |
| 破坏性数据迁移、数据回填 | ❌ | ✅ | 需人工确认，见 9.3 |

### 5.2 待决策：过渡 Web 入口的前端是否一起打补丁

`runtime/frontend`（Next standalone，24 MB）由 `start-stack.ps1` 拉起，但 Electron 正式 UI 走 `app://email-automation/` → `runtime/frontend-static`。

| 选项 | 补丁体积 | 后果 |
|---|---|---|
| **A（默认建议）只打 `frontend-static`** | 约 8 MB | 过渡 Web 入口（18001）会滞后于补丁版本。需文档标注该入口仅用于诊断 |
| B 两者都打 | 约 32 MB | 两入口版本一致，但补丁体积增加约 4 倍 |

---

## 6. 补丁包规格

### 6.1 文件结构

```
patch-1.2.3.eapatch          （ZIP，扩展名 .eapatch）
├── manifest.json
├── runtime-delta.json       （可选；见 6.5）
├── payload\
│   ├── backend\app\**       （完整目录）
│   └── runtime\frontend-static\**   （完整目录）
└── signature                （预留，前期为空）
```

### 6.2 `manifest.json` 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|:---:|---|
| `schema` | int | ✅ | 固定 `1` |
| `patch_version` | string | ✅ | SemVer，如 `1.2.3` |
| `channel` | string | ✅ | `stable` / `beta` |
| `patch_type` | string | ✅ | `code` / `runtime` |
| `min_base_version` | string | ✅ | 可应用的最低基线版本，如 `1.2.2` |
| `runtime_rev_required` | string | ✅ | 所需运行时基线（见 4.2） |
| `built_at` | string | ✅ | ISO 8601 UTC |
| `db_migration` | bool | ✅ | 是否含数据库迁移；`true` 时**禁用自动回滚**（见 9.3） |
| `requires_stop` | bool | ✅ | 是否需停栈；代码补丁恒为 `true` |
| `size_bytes` | int | ✅ | 补丁包字节数 |
| `sha256` | string | ✅ | 整个 ZIP 的 sha256 |
| `entries` | array | ✅ | `[{path, sha256, bytes}]`，逐文件校验 |
| `excludes_verified` | bool | ✅ | 声明已通过硬排除清单校验（见 6.4） |
| `signature` | object\|null | ✅ | 前期 `null`；后期 `{alg:"ed25519", value:"..."}` |
| `notes_zh` / `notes_en` | string | ⬜ | 更新说明 |

### 6.3 校验与签名（分阶段）

| 阶段 | 校验强度 | 说明 |
|---|---|---|
| **P1（前期）** | ZIP sha256 + 逐文件 sha256 + `excludes_verified` | 离线交付，人工传递，风险可控 |
| **P2+（接入在线源后）** | 追加 **Ed25519 签名**，公钥内置于客户端 | 在线源下**必须**启用；未验签一律拒绝执行 |

> 字段与结构在 P1 就预留，P2 只填值、不改结构。

### 6.4 硬排除清单（安全红线）

`make-patch.ps1` 必须在打包时**强制排除**以下路径，并在打包后断言"结果中不存在"，否则中止：

```
**/.env            **/.env.*          **/*token*         **/*secret*
**/credentials.json                   **/*.db  **/*.db-wal  **/*.db-shm
**/app.db*         **/huey.db*        **/oauth*          **/security.json
**/config/security.json               **/data/**         **/database/**
**/logs/**         **/__pycache__/**  **/.pytest_cache/**
```

**这是安全问题而非整洁问题**：打包机上存在真实凭证与真实客户邮件数据，一旦混入补丁包即构成数据泄露。

### 6.5 关键冲突：`frontend-static` 补丁会破坏 `verify-runtime.ps1`

**已核实的事实**：`runtime/runtime-manifest.json` 覆盖整个 `runtime\` 目录（12568 文件，含 `frontend-static` 全部文件）；`scripts/verify-runtime.ps1` 逐文件比对 sha256，并断言 `manifest.version == VERSION`。

因此**替换 `frontend-static` 后运行 `verify-runtime.ps1` 会报 `hash_mismatch` 而失败**——这是补丁流程中的完整性检查步骤，会导致误判更新失败并触发回滚。

**解决方案（推荐 A）**：

| 方案 | 做法 | 评价 |
|---|---|---|
| **A（推荐）** | 补丁携带 `runtime-delta.json`（仅含被覆盖路径的新 sha256）；`verify-runtime.ps1` 加载 delta 后对这些路径使用新哈希，其余仍按原 manifest | 保护不减弱，改动集中在一个脚本 |
| B | 把 `frontend-static` 从 `runtime-manifest.json` 覆盖范围中移除（改 `build-runtime.ps1`） | 削弱前端产物的完整性保护，不推荐 |

**配套版本约定**：Tier 1 补丁**不得修改安装目录的 `VERSION` 文件**（保持基线版本不变），补丁版本只写入 overlay 的 `patch.json`。这样 `verify-runtime.ps1` 中 `manifest.version == VERSION` 的断言仍然成立。

---

## 7. 更新流程

### 7.1 执行步骤（`scripts/apply-patch.ps1`）

1. 校验 Workspace 完整性（`VERSION`、`runtime-manifest.json`、`stop-stack.ps1`、`start-stack.ps1` 存在）。
2. 解析并校验补丁 `manifest.json`：字段完整、`schema` 兼容、`patch_type` 支持。
3. 校验 `min_base_version` ≤ 当前基线版本；校验 `runtime_rev_required` ≤ 已安装 `runtime_rev`，不符则**降级 Tier 2 并退出（不修改任何文件）**。
4. 校验 ZIP `sha256`；阶段 P2+ 追加签名验证。
5. **前置业务检查**：存在 `running` 的 Run、或 Huey 队列有活跃发送/导入任务时**拒绝更新并提示稍后**，不强制打断。
6. 解压到 `patch\staging\<version>\`（**绝不直接解压到目标目录**）。
7. 逐文件校验 `entries[].sha256`；任一不符即中止并清理 staging。
8. 校验 payload 结构完整（含 `backend\app\__init__.py`、`runtime\frontend-static\index.html`）。
9. **备份**：`database\app.db*` → `database\backups\app.db.bak-<timestamp>`；备份 `config\`（不含密钥明文亦需备份）；保留最近 3 份。
10. 写入 `.update-state.json`（`phase=stopping`）。
11. 调用 `stop-stack.ps1`。
12. **等待进程真正退出并释放文件锁**（轮询，超时 60s；见 7.4 的已知缺口）。
13. 原子提交：`patch\staging\<version>` → `patch\<version>`（目录重命名）。
14. 写入 `patch\active.json` = `{version, path, applied_at, previous}`；备份旧 active 供回滚。
15. 启动 `start-stack.ps1`，捕获输出；**判定成功 = 出现 `READY` 且 `/api/version` 返回的 `patch_version` 等于目标版本**。
16. 运行 `verify-runtime.ps1`（按 6.5 改造后）。
17. 成功 → 写入 `patch\history.log`，清理 staging，**保留最近 2 个历史 overlay**，其余裁剪。
18. 任一环节失败 → 进入 7.3 回滚。

### 7.2 状态文件

`%LOCALAPPDATA%\TAC AISolution\Email Automation\patch\.update-state.json`

```json
{
  "from_version": "1.2.2",
  "to_version": "1.2.3",
  "phase": "health_check",
  "started_at": "2026-09-03T02:10:00Z",
  "backup_path": "...\\database\\backups\\app.db.bak-20260903",
  "previous_active": { "version": "1.2.2", "path": null },
  "status": "running"
}
```

启动时若发现 `status` 为 `running` 或 `failed`，**必须进入恢复检查并提示用户，不得再次覆盖更新**。

### 7.3 回滚规则

- **可自动回滚**：`db_migration == false` 的补丁。回滚 = 恢复 `patch\active.json` 指向上一版本（或清空表示回到基线）→ 启栈 → 健康门禁。
- **禁止自动回滚**：`db_migration == true`。此时只保留现场 + 备份，提示用户人工介入（回滚代码但数据库已变更，可能造成不一致）。
- **数据库不随代码回滚**。数据库恢复只能由用户从 `database\backups\` 手动选择，且必须二次确认。
- 回滚后仍失败 → 停止在"安全停机"状态，输出明确中文错误与日志路径，**绝不留下部分更新的栈在后台运行**。

### 7.4 已知缺口（实施时必须处理）

`scripts/stop-stack.ps1` 使用 `Stop-Process -Force` 后**不等待进程真正退出**，且对外部占用端口的进程只报告不处理。

因此 `apply-patch.ps1` 必须：

1. 轮询确认所有自有 PID 已消失（超时 60 s）；
2. 在重命名前实测目标文件可写（尝试以独占方式打开后关闭）；
3. 若仍被占用，报 `file_locked:<path> pid=<n>` 并中止，**不强行覆盖**。

---

## 8. 代码落点清单（实施时的改动范围）

| 文件 | 改动 | 阶段 |
|---|---|---|
| `backend/app/main.py` | 新增 `GET /api/version`：返回 `base_version`、`patch_version`、`effective_version`、`runtime_rev`、`channel` | P0 |
| `scripts/apply-patch.ps1` | **新建**。7.1 全部流程 | P1 |
| `scripts/make-patch.ps1` | **新建**。生成补丁包 + manifest + 硬排除断言 | P1 |
| `scripts/start-stack.ps1` | 第 60 行 `PYTHONPATH`：**前置 overlay 的 `backend`**（`overlay\backend;backend;python-packages`） | P1 |
| `desktop/main.cjs` | 第 80 行 `staticRoot`：优先 overlay 的 `frontend-static`，不存在则回落安装目录 | P1 |
| `scripts/verify-runtime.ps1` | 支持 `runtime-delta.json`（见 6.5） | P1 |
| `scripts/build-runtime.ps1` | 生成并写入 `runtime_rev`（见 4.2） | P1 |
| 前端设置页 | 「关于 / 导入补丁」区块：当前版本、选择补丁文件、进度、上次结果、回滚入口 | P1 |
| `scripts/data-dir.ps1` | 新增导出 `DataPatch`（overlay 根目录） | P0 |

> overlay 根目录：`%LOCALAPPDATA%\TAC AISolution\Email Automation\patch\`
> 结构：`staging\<version>\`、`\<version>\`、`active.json`、`history.log`、`.update-state.json`

---

## 9. 数据库迁移策略

### 9.1 现状

`backend/app/migrations.py` 在应用启动时执行，幂等且**纯增量**：仅 `ALTER TABLE ... ADD COLUMN <type> NULL`，不删列、不改类型、不删表。

### 9.2 对补丁的意义

- 补丁新增字段 → 客户重启后自动生效，**补丁流程无需额外迁移步骤**。
- 回滚后遗留的 nullable 列不影响旧代码运行（旧代码不读该列）→ **纯增量补丁可安全回滚**。

### 9.3 缺口与规则

- **缺口**：无 `schema_version` 表，无法判断"回滚后的旧代码能否正确读取新数据"。
- **P3 补齐**：新增 `schema_version` 表与有序迁移脚本。
- **过渡期硬性规则**：
  1. 补丁**不得**引入破坏性 DDL；
  2. 补丁**不得**在代码层做一次性数据回填（回填应延至 P3 具备 `schema_version` 后）；
  3. 若确需，manifest 标记 `db_migration: true`，**禁用自动回滚**（见 7.3）。

---

## 10. 护栏与安全边界

1. 更新前存在 `running` Run 或活跃发送/导入任务 → **拒绝更新**。
2. 更新前**必须**备份数据库与配置，保留最近 3 份。
3. 停栈后**必须**确认进程退出且文件可写，不得强行覆盖。
4. 更新后**必须**通过健康门禁（`/api/health` + `/api/version` 版本匹配）才算成功。
5. 补丁包在**校验通过前不得执行其中任何内容**。
6. 远程 manifest **不得**携带可执行脚本指令。
7. 更新失败**不得**自动发送邮件、同步 Gmail、创建 Draft、批准 Approval 或启用 Automation。
8. 更新过程**不修改客户业务数据**，仅允许必要的 schema 迁移。
9. 更新检查失败（离线 / 源不可达）→ 静默跳过，**绝不阻塞应用启动**。
10. 所有更新动作写入 `patch\history.log` 与 `logs\`，保留版本流转审计。
11. 前期：**用户手动确认后才执行**，不做静默更新。

---

## 11. 发布源与运营节奏

### 11.1 发布源（后期）

- 建议：**腾讯云 COS / 阿里云 OSS + CDN**，纯静态托管，无需服务端代码。
- 目录建议：
  ```
  /releases/
  ├─ latest.json                     （按 channel 索引）
  ├─ patch-1.2.3.eapatch
  ├─ Email-Automation-Setup-1.3.0.exe
  └─ signatures/
  ```
- **不建议 GitHub Releases**：国内可达性差。
- 客户端 `UPDATE_FEED_URL` 可配置（`.env` / `config`），便于私有化部署客户指向自有机。

### 11.2 运营节奏

- **基线重切**：每累计约 10 个补丁、或每 3 个月，重新发布一次完整安装包，让新客户拿到较新基线。
- **渠道**：`stable` / `beta`。自用与种子客户走 `beta`，确认无问题后推 `stable`。
- **回滚窗口**：保留最近 2 个历史 overlay。

---

## 12. 实施阶段

| 阶段 | 交付物 | 验收标准 | 前置 |
|---|---|---|---|
| **P0 埋钩子** | `GET /api/version`；`data-dir.ps1` 导出 `DataPatch` | 端点返回正确的 base/patch/runtime_rev；不影响现有功能 | 无 |
| **P0.5 文档定稿** | 本文件确认；6.5 冲突方案选定；5.2 待决策选定 | 用户签字 | 无 |
| **P1 离线补丁闭环** | `make-patch.ps1` + `apply-patch.ps1` + overlay 加载（PYTHONPATH / staticRoot）+ `verify-runtime.ps1` 改造 + `runtime_rev` 生成 | 命令行跑通"生成补丁 → 导入 → 生效 → 回滚"；无前端 UI 亦可验证 | P0 |
| **P2 前端与自动化** | 设置页「关于/导入补丁」；忙碌检测；历史裁剪；签名启用；`UPDATE_FEED_URL` | 客户可在 UI 完成导入与回滚；在线源可选开启 | P1 |
| **P3 产品化** | Tier 2 静默升级（提权方案）；`schema_version` + 有序迁移；渠道与灰度；可选遥测 | 运行时变更可平滑升级；破坏性迁移受控 | P2 |

> **建议先只做 P0 + P0.5**。理由见 12.1。

### 12.1 与"先完善应用"的关系

当前客户尚未铺开，更新系统边际价值有限；而 Tier 1 overlay 是**加载器层改动**（`PYTHONPATH` 前置 + 静态目录覆盖），不触及业务代码，**后补的返工成本很低**。

因此推荐：**先完善应用，同时完成 P0 + P0.5**（合计约 1 小时）。

触发"必须提前做 P1/P2"的信号：

- 已装机客户 > 5；或
- 客户地理分散、无法逐个手动重装；或
- 应用完善周期预计 > 2–3 个月且中途已有客户装机；或
- 合同包含 SLA / 漏洞修复时效承诺。

---

## 13. 测试计划

### 13.1 打包测试

- 生成两个连续版本补丁，核对 `manifest.json` 字段、`sha256`、文件清单完整。
- **断言补丁包内不含**任何 6.4 硬排除清单中的路径（含 `.env`、OAuth、`*.db`）。
- 确认 `entries` 与 payload 实际文件一一对应。
- 确认 `excludes_verified` 为 `true` 才允许发布。

### 13.2 升级测试

在具备以下内容的测试环境执行：Gmail OAuth、Contacts、Campaign、Approval、Knowledge Base、TACWork Session、`.env`、非空 `app.db`。

| 验证项 | 期望 |
|---|---|
| 旧版本启动 → 应用补丁 → 健康门禁 | `/api/version` 返回目标 `patch_version` |
| Gmail 连接 | 仍为已连接，无需重新授权 |
| 业务数据 | Contacts / Campaign / Approval / Draft / Automation 全部保留且数量一致 |
| 新增字段 | 补丁引入的 nullable 列已生效 |
| 模型 Key | 无需重新输入 |
| 写操作 | 更新全过程**无新增发送、无 Run、无 Draft、无 Approval 变更** |

### 13.3 失败与回滚测试（逐项覆盖）

| 故障注入 | 期望 |
|---|---|
| `sha256` 不匹配 | 中止，文件零改动，服务保持运行 |
| ZIP 损坏 | 同上 |
| payload 结构缺失 | 同上 |
| `min_base_version` 不满足 | 拒绝并提示，不修改文件 |
| `runtime_rev` 不满足 | **降级提示 Tier 2**，不应用补丁 |
| 停栈后文件仍被占用 | 报 `file_locked`，中止，服务可恢复 |
| Backend 启动失败 / Consumer 不健康 | 自动回滚上一版本并恢复服务 |
| `/api/version` 版本不匹配 | 判定失败并回滚 |
| 更新进程中途被杀 | 重启后进入恢复检查，不重复覆盖 |
| `db_migration: true` 且失败 | 保留现场 + 备份，提示人工，**禁止自动回滚** |

### 13.4 客户体验验收

- 客户仅需点击一次"导入补丁"并选择文件。
- 不需要重装 Python / Node / 依赖。
- 不需要重新授权 Gmail 或重填模型 Key。
- 失败时显示可理解的中文错误与日志路径。
- 更新完成后 Web 与 TACWork 自动恢复。

---

## 14. 待决策事项

| # | 事项 | 选项 | 建议 |
|---|---|---|---|
| 1 | 过渡 Web 入口前端（`runtime/frontend`，24 MB）是否一起打补丁 | A 只打 `frontend-static`（8 MB） / B 两者都打（32 MB） | **A** |
| 2 | 6.5 中 `verify-runtime.ps1` 冲突的解决方式 | A `runtime-delta.json` / B 缩小 manifest 覆盖范围 | **A** |
| 3 | 签名启用时机 | P1 启用 / **P2 接在线源时启用** | **P2**，P1 先预留字段 |
| 4 | 更新触发方式 | 通知后手动确认 / 启动自动检查+手动确认 / 全自动静默 | **手动确认**（客户产品不宜静默改写代码） |
| 5 | 是否支持完全离线客户 | 需要则另加"离线补丁包 + 手动导入"通道 | 前期默认即为离线导入，天然支持 |
| 6 | P0 是否现在实施 | 现在做 / 随 P1 一起做 | **现在做**（约 1 小时，且是整套系统的前置） |

---

## 附录 A：本版与上一版计划的差异

| 上一版表述 | 本版修订 | 原因 |
|---|---|---|
| 必须保留 `backend/.env`、`backend/app.db`、`backend/data/` | 改为保留 `%LOCALAPPDATA%\TAC AISolution\Email Automation\{config,database,queue,logs,tacwork,run}` | 生产布局已完成数据目录分离（`scripts/data-dir.ps1`） |
| 「仅本机管理员可执行更新」 | 代码补丁**免管理员**；仅 Tier 2 需提权 | 前提从"原地替换安装目录"改为 overlay |
| 「第一版发布完整版本包，不做差分补丁」 | 明确为"可更新代码树的完整快照"，约 8 MB | 上一版指整个 Workspace 全量包（数百 MB），粒度不同 |
| 基线为 `Email-Automation-portable-20260811.zip` / `1.0.0` | 基线 `VERSION` = 1.2.2 | 版本推进 |
| 未涉及 | 新增 4.2 `runtime_rev`、6.5 manifest 冲突、7.4 停栈等待缺口、4.4 更新器自举限制 | 本次代码核对中发现的实施阻塞点 |

## 附录 B：当前状态

本文件仅记录更新与补丁能力的设计规格。**本次修订未实现更新器、未修改启动流程、未修改数据库、未修改配置，也未改变当前客户交付包行为。**
