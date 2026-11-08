# AutoFileSync-DSM 项目说明

本文档面向后续开发、部署和维护，记录项目目标、验证环境、Git 流程与套件基本要求。

## 1. 任务目标

把 `AutoFileSync.sh` 的「源目录 → 目标目录」rsync 镜像同步能力，做成一个 **DSM 7 原生套件**：

- 安装后从 DSM 主菜单打开，在桌面内嵌窗口中管理，不依赖 SSH / crontab / 手工改脚本
- 同步任务**完全可配置**，不是固定几条：支持新增、编辑、删除、启停
- 支持预览（dry-run）与实跑、定时执行、1–3 个任务并发
- 提供删除上限与超限自动回滚、运行历史、独立日志、DSM 桌面通知
- 零 TCP 端口：UI → `api.cgi` → unix socket → 守护进程
- `noarch` 单包，纯 Python 标准库 + 系统自带 rsync

### 非目标（当前版本）

- 远程 rsync（`user@host:path`）
- 邮件 / 短信通知（仅 DSM 桌面通知）
- 长期回收站（回滚快照只在本次运行内使用，结束即清理）
- 超过 3 个任务的并发

## 2. 群晖后台环境信息

以下为当前验证环境（示例值，实际部署以现场为准）：

| 项目 | 值 |
|---|---|
| 主机名 | `HServer` |
| 型号 | `SA6400` |
| 架构 | `x86_64` |
| DSM 版本 | `7.4.0-90075` |
| 存储卷 | `/volume1` – `/volume5` |
| Python | `3.8.15`（DSM 自带） |
| rsync | `/usr/bin/rsync 3.1.2` |
| 权限管理器 | `SimplePermissionManager 1.0.0-10` |
| `spm-exec` | `/usr/local/bin/spm-exec`（setuid root） |
| 套件用户 | `sc-autofilesync`（uid `232991` / gid `245966`） |
| 套件版本 | `1.0.0` |

### 关键路径

| 用途 | 路径 |
|---|---|
| 套件安装目录 | `/var/packages/autofilesync` |
| 持久数据目录 | `/volume1/@appdata/autofilesync` |
| 配置文件 | `/volume1/@appdata/autofilesync/config.json` |
| 运行状态 / 历史 | `/volume1/@appdata/autofilesync/state.json` |
| 服务日志 | `/volume1/@appdata/autofilesync/autofilesync.log` |
| 单次运行日志 | `/volume1/@appdata/autofilesync/logs/<job-id>/` |
| Unix socket | `/volume1/@appdata/autofilesync/http/autofilesync.sock` |
| UI Token | `/volume1/@appdata/autofilesync/token` |
| SPM 权限配置 | `/volume1/@appconf/SimplePermissionManager/permissions.json` |

### 访问方式

```bash
ssh <NAS_USER>@<NAS_IP> -p 22
```

> SSH 密码、DSM 密码、GitHub Token 不写入仓库，也不写入本文档。部署时通过本地密码管理器或环境变量提供。

### 权限模型

- DSM 7 要求第三方套件以 `run-as: package` 运行
- 需要 root 的操作通过 SimplePermissionManager 的 `spm-exec` 提权
- 未授权时自动降级：以 `sc-autofilesync` 运行，附加 `--no-owner --no-group`
- 降级影响：无法保留 uid/gid/ACL，可能无法读取或删除不属于该用户的文件

## 3. Git 推送方式

### 仓库信息

- GitHub：<https://github.com/MeowPerth/AutoFileSync-DSM>
- 默认分支：`main`
- Release：<https://github.com/MeowPerth/AutoFileSync-DSM/releases>
- `dist/` 已在 `.gitignore` 中忽略，SPK 通过 Release 附件发布

### 认证

任选其一：

```bash
# 方式 A：GitHub CLI 设备登录
gh auth login

# 方式 B：环境变量（不要提交到仓库）
export GH_TOKEN=<有 repo 权限的 token>
```

检查：

```bash
gh auth status
```

### 日常提交流程

```bash
cd /root/workspace/SPK/AutoFileSync-DSM

git status
git add -A
git commit -m "feat: ..."
git push origin main
```

### 发布新版本

```bash
# 1) 构建
./build.sh

# 2) 打 tag 并推送
git tag v1.0.1
git push origin v1.0.1

# 3) 创建 Release 并上传 SPK
gh release create v1.0.1 dist/autofilesync_noarch-dsm7_1.0.1.spk \
  --title "AutoFileSync 1.0.1" \
  --notes "修复说明..."
```

> 不要把密码、Token、`config.json`、日志或 `dist/` 提交进仓库。

## 4. 套件基本要求

### 4.1 包格式

- 产物：`dist/autofilesync_noarch-dsm7_<version>.spk`
- `.spk` 为未压缩 tar：`INFO`、`package.tgz`、`scripts/`、`conf/`、`WIZARD_UIFILES/`、图标、LICENSE
- `arch="noarch"`，覆盖全平台
- `os_min_ver="7.0-40120"`
- `startable="yes"`、`reloadui="yes"`
- `dsmappname` 必须与 `dsm-wrapper.js` 中 `Ext.define` 的 AppInstance 类名一致：`MeowPerth.AutoFileSync.AppInstance`
- `conf/privilege` 使用 `run-as: package`，不使用 `run-as: root`

### 4.2 版本号规则

- **首次开发、尚未正式发布**：从 `0.0.1` 开始逐次递增（`0.0.1`、`0.0.2`……）
- **首次正式发布**：才改为 `1.0.0`
- **发布之后**：
  - 修复版本：`1.0.1`
  - 功能版本：`1.1.0`
  - 破坏性变更：`2.0.0`
- 不使用 `1.0.0-0001` 这类构建后缀；`INFO`、SPK 文件名、守护进程版本、界面显示保持一致
- 注意：DSM 不允许降级安装。已装 `1.0.0` 的设备不能直接安装 `0.0.x` 开发版；开发版只用于全新安装或测试机，或在测试机上卸载后安装

当前已发布版本：`1.0.0`，不再带 `-0001`。

### 4.3 安装提示（授权）

- 文件：`WIZARD_UIFILES/install_uifile`、`install_uifile_chs`、`install_uifile_cht`
- 套件中心安装时显示提示页：
  - 安装后请在「权限管理器」（SimplePermissionManager）中授权 AutoFileSync
  - 未授权也可使用，但会以 `sc-autofilesync` 运行，无法保留 uid/gid/ACL，可能无法读取或删除不属于该用户的文件
  - 若未安装权限管理器，请先从套件中心安装
- 应用内未授权时也会显示警告横幅，区分「未安装 SPM」和「未授权」
- 向导仅在套件中心图形界面显示；CLI 安装不会弹向导

### 4.4 卸载提示（数据保留/删除）

- 文件：`WIZARD_UIFILES/uninstall_uifile`、`_chs`、`_cht`
- 套件中心卸载时显示单选：
  - **保留（默认）**：保留 `/volume1/@appdata/autofilesync`
  - **删除**：清除配置、任务、历史与日志，不可恢复
- `postuninst` 仅在 `wizard_delete_data=true` 时删除数据目录，并校验路径必须以 `/autofilesync` 结尾
- CLI `synopkg uninstall` 不显示向导，默认保留数据
- 升级/重装默认保留数据；`preupgrade` 会生成 `config.json.bak-<version>` 快照

### 4.5 升级与数据兼容

- 配置 schema 当前为 `2`
- 新增任务字段使用默认值合并，旧配置可直接升级
- 升级前备份配置；升级后重新注入 UI token
- 配置、状态、日志均位于 `SYNOPKG_PKGVAR`，不放在 `package.tgz` 内

### 4.6 网络与安全

- 不监听任何 TCP 端口
- UI 通过 DSM `api.cgi` 访问 unix socket
- 双重认证：DSM 会话 + 安装期注入的 token（`__AFS_TOKEN__`）
- 路径校验：禁止系统目录、卷根、`@`/`#` 系统目录、目标位于源内
- 目标目录非空需写入 `.autofilesync-target` 确认接管
- 删除上限 + 超限回滚；回滚快照只记录本次删除，成功或回滚后清理

### 4.7 构建与验证

```bash
./build.sh
```

依赖：`bash`、`tar`、`gzip`、`md5sum`、`python3`。

安装验证：

```bash
sudo synopkg install dist/autofilesync_noarch-dsm7_<version>.spk
sudo synopkg status autofilesync
sudo synopkg version autofilesync
```

验收清单：

- [ ] 全新安装显示授权提示
- [ ] 未授权时应用显示降级警告，授权后变为 root
- [ ] 新建任务、预览、实跑正常
- [ ] 定时执行、并发、通知测试正常
- [ ] 历史与日志可查看
- [ ] 卸载选择「保留」后重装，任务仍在
- [ ] 卸载选择「删除」后，`/volume1/@appdata/autofilesync` 被清除
- [ ] 无 TCP 端口监听

## 5. 当前状态

| 项 | 值 |
|---|---|
| 当前版本 | `1.0.0` |
| 仓库 | <https://github.com/MeowPerth/AutoFileSync-DSM> |
| Release | <https://github.com/MeowPerth/AutoFileSync-DSM/releases/tag/v1.0.0> |
| NAS 运行版本 | `1.0.0` |
| 任务数 | 7 |
| 特权模式 | root（spm-exec 授权） |
