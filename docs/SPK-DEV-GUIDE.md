# 群晖 DSM 7 套件开发通用指南（模板）

> 适用于把脚本、服务或 Web 工具封装成 Synology DSM 7 原生 SPK。
> 文中 `<PKG_NAME>`、`<NAS_HOST>`、`<VERSION>` 等均为占位符，按项目替换。
> 目标：一套流程可复用于不同套件，不绑定具体项目。

---

## 1. 任务目标（项目立项模板）

把现有能力封装成 DSM 原生套件，通常要满足：

- 从 DSM 主菜单打开，有独立图标和桌面窗口
- 配置、运行、日志都在图形界面完成，不依赖 SSH / crontab
- 数据持久化到 `SYNOPKG_PKGVAR`，升级/重装不丢
- 需要 root 的操作走权限管理器，不用 `run-as: root`
- 安装、升级、卸载都有明确提示和数据策略
- 产物可重复构建，版本号可追踪，能通过 GitHub Release 分发

**非目标**要提前写清楚，避免范围膨胀，例如：远程执行、多租户、集群、长期归档等。

---

## 2. 群晖后台环境信息

### 2.1 采集命令

在新设备上先记录以下信息，写入项目文档或部署记录：

```bash
hostname
cat /proc/sys/kernel/syno_hw_version
uname -m
cat /etc.defaults/VERSION
python3 --version
/usr/bin/rsync --version | head -1
ls -d /volume* 2>/dev/null
id sc-<PKG_NAME> 2>/dev/null
/usr/syno/bin/synopkg version <PKG_NAME> 2>/dev/null
/usr/syno/bin/synopkg version SimplePermissionManager 2>/dev/null
ls -l /usr/local/bin/spm-exec 2>/dev/null
```

### 2.2 记录表（示例）

| 项目 | 值 |
|---|---|
| 主机名 | `<NAS_HOST>` |
| 型号 | `<MODEL>` |
| 架构 | `x86_64` / `armv8` / … |
| DSM 版本 | `<DSM_VERSION>`，例如 `7.4.0-90075` |
| 存储卷 | `/volume1` … |
| Python | `<PYTHON_VERSION>`（DSM 自带） |
| rsync | `/usr/bin/rsync <VERSION>` |
| 权限管理器 | `SimplePermissionManager <VERSION>` |
| `spm-exec` | `/usr/local/bin/spm-exec`（setuid root） |
| 套件用户 | `sc-<PKG_NAME>`（uid/gid） |

### 2.3 关键路径

| 用途 | 路径 |
|---|---|
| 套件安装目录 | `/var/packages/<PKG_NAME>` |
| 持久数据目录 | `/volume1/@appdata/<PKG_NAME>` |
| 配置文件 | `<PKG_VAR>/config.json` |
| 状态/历史 | `<PKG_VAR>/state.json` |
| 服务日志 | `<PKG_VAR>/<PKG_NAME>.log` |
| 单次运行日志 | `<PKG_VAR>/logs/` |
| SPM 权限配置 | `/volume1/@appconf/SimplePermissionManager/permissions.json` |
| SPM 可执行文件 | `/usr/local/bin/spm-exec` |

> SSH 密码、DSM 密码、GitHub Token 不写入仓库，也不写入文档。

---

## 3. 仓库结构（建议）

```
<repo>/
├── build.sh                     # 一键打包
├── README.md
├── LICENSE
├── .gitignore                   # dist/、__pycache__、*.pyc、*.spk
├── docs/
│   ├── PROJECT.md               # 本项目说明
│   └── SPK-DEV-GUIDE.md         # 本通用指南
├── spk/
│   ├── INFO                     # 可由 build.sh 生成
│   ├── conf/privilege
│   ├── scripts/
│   │   ├── installer            # 公共函数 + 钩子
│   │   ├── preinst / postinst
│   │   ├── preuninst / postuninst
│   │   ├── preupgrade / postupgrade
│   │   └── start-stop-status
│   ├── WIZARD_UIFILES/
│   │   ├── install_uifile
│   │   ├── install_uifile_chs
│   │   ├── install_uifile_cht
│   │   ├── uninstall_uifile
│   │   ├── uninstall_uifile_chs
│   │   └── uninstall_uifile_cht
│   └── var-default-config.json
└── src/
    ├── bin/<daemon>.py
    └── ui/                      # index.html、api.cgi、config、dsm-wrapper.js、images
```

---

## 4. 套件基本要求

### 4.1 .spk 结构

`.spk` 是**未压缩 tar**：

```
INFO
package.tgz
scripts/
conf/
WIZARD_UIFILES/
PACKAGE_ICON.PNG
PACKAGE_ICON_256.PNG
LICENSE
```

### 4.2 INFO 模板

```
package="<PKG_NAME>"
version="<VERSION>"
description="English description"
description_chs="中文说明"
description_cht="繁體說明"
arch="noarch"
os_min_ver="7.0-40120"
displayname="<DISPLAY_NAME>"
dsmuidir="ui"
dsmappname="<VENDOR>.<PKG>.AppInstance"
maintainer="<MAINTAINER>"
maintainer_url="https://github.com/<USER>/<REPO>"
distributor="<MAINTAINER>"
distributor_url="https://github.com/<USER>/<REPO>"
startable="yes"
reloadui="yes"
changelog="<VERSION>: ..."
checksum="<package.tgz md5>"
```

要求：

- 只能 `key="value"`，不能有空行或注释，否则 DSM 拒绝安装
- `arch="noarch"` 适合纯脚本/Python；含二进制时用具体架构
- `dsmappname` 必须与 `dsm-wrapper.js` 中 `Ext.define` 的 AppInstance 类名完全一致
- `startable="yes"` 才会在套件中心出现启动/停止

### 4.3 package.tgz

`package.tgz` 是 gzip 压缩包，内容按需：

```
bin/
ui/
var/          # 首装默认配置，安装时同步到 PKG_VAR
```

构建时固定成员顺序，便于 checksum 稳定。

### 4.4 权限（DSM 7）

第三方套件**不能**使用 `run-as: root`（会返回 `invalid package privilege content`，code 319）。

`conf/privilege` 模板：

```json
{
  "defaults": { "run-as": "package" },
  "username": "sc-<PKG_NAME>"
}
```

需要 root 时：

- 通过 SimplePermissionManager 的 `spm-exec` 提权
- 应用启动时检测 `spm-exec` 是否可用、是否已授权
- 未授权时降级运行，并在 UI 明确提示影响

### 4.5 脚本钩子

| 钩子 | 时机 | 用途 |
|---|---|---|
| `preinst` | 安装前 | 环境检查 |
| `postinst` | 安装后 | 建目录、生成 token、注入 UI、启动 |
| `preuninst` | 卸载前 | 停止服务 |
| `postuninst` | 卸载后 | 按用户选择删除/保留数据 |
| `preupgrade` | 升级前 | 备份配置 |
| `postupgrade` | 升级后 | 恢复/迁移、重新注入 token |
| `start-stop-status` | 启停 | 启动/停止守护进程、返回状态 |

常用环境变量：

```
SYNOPKG_PKGNAME
SYNOPKG_PKGDEST
SYNOPKG_PKGVAR
SYNOPKG_PKGVER
SYNOPKG_TEMP_LOGFILE
```

### 4.6 UI 与桌面应用

- `ui/config` 注册 ExtJS AppInstance / AppWindow
- `dsmappname` 与类名一致
- 页面放 `ui/index.html`，通过 `api.cgi` 调后端
- 不想开 TCP 端口时：UI → `api.cgi` → unix socket → 守护进程
- 需要 i18n 通知时，配置 `"texts": "texts"` 与 `"preloadTexts"`

---

## 5. 版本号规范

- **首次开发、尚未发布**：从 `0.0.1` 开始，逐次递增（`0.0.1`、`0.0.2`……）
- **首次正式发布**：才改为 `1.0.0`
- **发布之后**：
  - 修复：`1.0.1`
  - 功能：`1.1.0`
  - 破坏性：`2.0.0`
- **不要使用** `1.0.0-0001` 这类构建后缀；`INFO`、SPK 文件名、守护进程、界面显示保持一致
- 开发版 `0.0.x` 只用于全新安装或测试机
- DSM **不允许降级**：已装 `1.0.0` 的设备不能直接装 `0.0.x`，需卸载或换测试机
- 发布前检查：
  - `INFO` 的 `version`
  - `build.sh` 的 `VERSION`
  - 守护进程/程序内版本常量
  - SPK 文件名
  - 安装后 `synopkg version`

---

## 6. 安装提示（授权提醒）

### 6.1 文件

```
WIZARD_UIFILES/install_uifile        # 默认（英文）
WIZARD_UIFILES/install_uifile_chs    # 简体中文
WIZARD_UIFILES/install_uifile_cht    # 繁体中文
```

套件中心安装时会先显示一页提示；**CLI 安装不会弹向导**。

### 6.2 JSON 模板

```json
[
  {
    "step_title": "<DISPLAY_NAME> 安装提示",
    "items": [
      {
        "desc": "安装完成后，请在 <b>「权限管理器」（SimplePermissionManager）</b> 中授权 <b><DISPLAY_NAME></b>。<br><br>未授权也可以使用，但会以 <b>sc-<PKG_NAME></b> 身份运行：无法保留 uid/gid/ACL，且可能无法读取或删除不属于该用户的文件。<br><br>如果尚未安装权限管理器，请先从套件中心安装 SimplePermissionManager。"
      }
    ]
  }
]
```

### 6.3 应用内提示

- 检测 `spm-exec` 是否可用、是否授权
- 未授权时显示警告横幅，并区分：
  - 没装权限管理器 → 提示先安装
  - 装了但没授权 → 提示去授权
- 说明降级影响，不要只写“请授权”

---

## 7. 卸载提示（保留/删除数据）

### 7.1 文件

```
WIZARD_UIFILES/uninstall_uifile
WIZARD_UIFILES/uninstall_uifile_chs
WIZARD_UIFILES/uninstall_uifile_cht
```

### 7.2 JSON 模板（单选框，默认保留）

```json
[
  {
    "step_title": "卸载 <DISPLAY_NAME>",
    "items": [
      {
        "type": "singleselect",
        "desc": "请选择卸载时如何处理配置、任务、历史与日志：",
        "subitems": [
          {
            "key": "wizard_keep_data",
            "desc": "<b>保留</b>：保留全部数据，重新安装后可继续使用（默认）。",
            "defaultValue": true
          },
          {
            "key": "wizard_delete_data",
            "desc": "<b style=\"color: red\">删除</b>：删除全部配置、任务、历史与日志，不可恢复。",
            "defaultValue": false
          }
        ]
      }
    ]
  }
]
```

### 7.3 postuninst 处理

```sh
postuninst ()
{
    log_step postuninst

    case "${wizard_delete_data:-}" in
        true|TRUE|1)
            if [ -n "${SYNOPKG_PKGVAR:-}" ] && [ -d "${SYNOPKG_PKGVAR}" ]; then
                case "${SYNOPKG_PKGVAR}" in
                    */<PKG_NAME>)
                        rm -rf -- "${SYNOPKG_PKGVAR}"
                        ;;
                    *)
                        install_log "Refuse to delete unexpected path: ${SYNOPKG_PKGVAR}"
                        ;;
                esac
            fi
            ;;
        *)
            install_log "keep package data: ${SYNOPKG_PKGVAR:-unknown}"
            ;;
    esac

    call_func service_postuninst
    exit 0
}
```

要点：

- 默认保留：不选删除时不要动 `SYNOPKG_PKGVAR`
- 删除前校验路径必须以 `/<PKG_NAME>` 结尾，避免空变量误删
- CLI `synopkg uninstall` 不走向导，默认保留
- 升级/重装保留数据；`preupgrade` 可备份配置

---

## 8. Git / GitHub CLI 流程

### 8.1 认证

```bash
# 设备登录
gh auth login

# 让 git 使用 gh 的凭据（否则 git push 可能报 could not read Username）
gh auth setup-git

gh auth status
```

或用环境变量：

```bash
export GH_TOKEN=<有 repo 权限的 token>
```

> Token 不要写入仓库、脚本或文档。

### 8.2 初始化与推送

```bash
cd <repo>
git init -b main
git add -A
git commit -m "init: <项目> 0.0.1"

# 新建远程仓库并推送
gh repo create <USER>/<REPO> --public --source=. --remote=origin --push

# 已有仓库
git remote add origin https://github.com/<USER>/<REPO>.git
git push -u origin main
```

### 8.3 发布 Release

```bash
./build.sh

git tag v1.0.0
git push origin v1.0.0

gh release create v1.0.0 dist/<PKG_NAME>_<ARCH>-dsm7_1.0.0.spk \
  --title "<DISPLAY_NAME> 1.0.0" \
  --notes "首个发布版本。"
```

`.gitignore` 建议：

```
dist/
__pycache__/
*.pyc
.DS_Store
*.spk
```

---

## 9. 构建与验收

### 9.1 构建

```bash
./build.sh
# 产物：dist/<PKG_NAME>_<ARCH>-dsm7_<VERSION>.spk
```

依赖：`bash`、`tar`、`gzip`、`md5sum`、`python3`。

### 9.2 安装验证

```bash
sudo synopkg install dist/<...>.spk
sudo synopkg version <PKG_NAME>
sudo synopkg status <PKG_NAME>
```

### 9.3 验收清单

- [ ] 全新安装显示授权提示
- [ ] 未授权时应用提示降级影响；授权后 `spm-exec` 正常
- [ ] 核心功能可用
- [ ] 升级后配置保留
- [ ] 卸载选择「保留」后重装，数据仍在
- [ ] 卸载选择「删除」后，`/volume1/@appdata/<PKG_NAME>` 被清除
- [ ] 无意外 TCP 端口监听
- [ ] 版本号一致，无 `-0001` 后缀
- [ ] Release 附件可下载安装

---

## 10. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| `invalid package privilege content` (319) | DSM 7 不允许第三方 `run-as: root`，改用 `package` + SPM |
| 桌面图标打不开 / 类找不到 | `dsmappname` 与 `dsm-wrapper.js` 的类名不一致 |
| 安装失败，INFO 解析错误 | INFO 有空行、注释或非 `key="value"` 行 |
| `cannot downgrade to older version` | 目标版本低于已安装版本；卸载或换测试机 |
| 卸载向导不出现 | 只有套件中心图形卸载会显示；CLI 不走向导 |
| 卸载后重装配置没了 | 选择了「删除」，或手动删了 `@appdata/<PKG_NAME>` |
| `git push` 报 could not read Username | 执行 `gh auth setup-git`，或配置 token |
| 通知显示“不可用” | 通知 className/文本未注册；DSM 桌面通知需要正确的 i18n/mail key |
| 升级后页面 token 失效 | 升级钩子需重新注入 token / 重建 UI |

---

## 11. 最小交付清单

- [ ] `build.sh` 可重复构建
- [ ] `INFO` + `package.tgz` + `scripts/` + `conf/privilege`
- [ ] 安装/卸载向导
- [ ] 版本号规范（0.0.1 → 1.0.0）
- [ ] README（功能、安装、截图、构建）
- [ ] LICENSE
- [ ] `.gitignore`
- [ ] GitHub 仓库 + Release SPK
