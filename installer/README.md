# Windows 安装包

基于当前便携运行时制作，包含项目所需的 Python、Node、TACWork Runtime 与离线依赖，
并携带现有 TACWork / TAC 正式品牌 Logo 资源。安装包**不携带** `.env`、OAuth Token、
数据库或业务数据；客户首次启动后通过 Web 页面完成配置、模型配置与 Gmail OAuth 授权。

## 构建前置

- **Inno Setup 6**（提供 `ISCC.exe`）。无则：`winget install JRSoftware.InnoSetup`
- **ImageMagick**（提供 `magick.exe`，用于把 Logo 转换为安装器 `.ico`）。无则：
  `winget install ImageMagick.ImageMagick`

`scripts/build-installer.ps1` 会自动探测这两个工具；缺任一将**终止构建**，不会伪造成功。

## 构建

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1
```

显式指定版本（同步写入 .iss、安装包文件名、Payload `version.txt`）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -Version "1.0.0"
```

也可直接指定编译器：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 `
  -InnoCompiler "C:\Users\Administrats\AppData\Local\Programs\Inno Setup 6\ISCC.exe"
```

输出：`dist\Email-Automation-Setup-1.0.0.exe`

构建流程（`build-installer.ps1`）：定位 ISCC / magick → 复用 `portable-package.ps1`
的排除 / 完整性 / 安全规则暂存 Payload → 二次完整性校验 → 由正式 Logo 生成
`installer/assets/EmailAutomation.ico` → 对 Payload 做第二轮密钥扫描 → 把版本号注入
`.iss` → 调用 ISCC 编译 → 打印构建报告并清理暂存目录。任一前置缺失、完整性失败、
密钥泄漏或编译失败均终止构建。

## 快捷方式

安装后在「开始菜单 / Email Automation」与（可选）桌面提供：

- **启动 Email Automation** → `start-stack.bat`（首次会自动 bootstrap 再启动）
- **停止 Email Automation** → `stop-stack.bat`
- **打开 Email Automation** → 默认浏览器打开 `http://127.0.0.1:3000`
- **健康检查** → `scripts/agent-health.ps1`

安装完成页的「启动」为可勾选项（默认勾选），由 `start-stack.bat` 引导首启。

## 首次启动流程

1. 用户点击「启动 Email Automation」。
2. `start-stack.bat` 的守卫检测：若 `backend/.venv` 或 `frontend/node_modules` 缺失，
   先调用 `portable-bootstrap.ps1` 用随包 Python 建虚拟环境、从离线缓存安装依赖；
   初始化失败会明确报错退出，不会进入「启动却缺运行时」的半残状态。已就绪则跳过。
3. 启动 Backend、Huey Consumer、Frontend、TACWork，等待健康检查。
4. 浏览器打开 `http://127.0.0.1:3000` 显示配置向导：
   AI Provider / Base URL / Model / API Key、APP_ENCRYPTION_KEY、SECRET_KEY、Gmail OAuth。
   未完成配置前**不会**自动同步 Gmail、生成 Draft、创建 / 批准 Approval、发送邮件或
   启用 Automation。配置完成后重启服务进入正常运营 Dashboard。

## 业务安全不变量

安装 / 升级全流程不触发 Gmail 同步、不生成 Draft、不创建 / 批准 Approval、不发送邮件、
不启用 Automation；`real_send` 仍由原有服务端策略控制。TACWork `approval=auto` 不会绕过
邮件安全策略。

## 升级与卸载

- **覆盖安装**：保留 `backend/.env`、`backend/app.db`、`backend/data`、`data`、OAuth 与
  业务配置；安装前自动停止当前运行的服务；安装后不自动执行 Gmail 同步，由用户重新启动。
- **卸载**：默认只删除程序文件，**保留**业务数据、配置与日志；额外提供
  「是否删除业务数据与日志」卸载选项（默认不勾选），勾选后**二次确认并显示准确目录**
  才删除。绝不在默认卸载中删除数据库 / OAuth / 配置文件。

## 品牌资源与已知缺口

- 安装器、快捷方式与 Web 页面统一使用现有正式 Logo（`TACWork-Logo-Black.PNG`、
  `frontend/public/tac-logo.png`）。
- 当前工作区**未发现** `TACWork-Logo-White` 文件，因此：不伪造白色 Logo，也不将其作为
  构建阻塞；安装器图标由正式黑色 Logo 经 ImageMagick 转换为多尺寸 `.ico`。
