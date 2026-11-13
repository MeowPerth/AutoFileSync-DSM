#!/usr/bin/env bash
# AutoFileSync SPK 一键打包：产物 dist/autofilesync_noarch-dsm7_<ver>.spk
# .spk = 未压缩 tar{ INFO, package.tgz, scripts/, conf/, PACKAGE_ICON*.PNG, LICENSE }
# 只需 bash / tar / gzip / md5sum / python3，无需 Synology SDK 或 spksrc。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PKG_NAME="autofilesync"
VERSION="1.0.3"
ARCH="noarch"
SPK_FILE="${ROOT}/dist/${PKG_NAME}_${ARCH}-dsm7_${VERSION}.spk"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
mkdir -p "$STAGE/bin" "$STAGE/ui/images" "$STAGE/var" \
         "$WORK/scripts" "$WORK/conf" "$ROOT/dist"

echo "==> 自检：Python 语法 / JSON 文件 / 占位符 / 图标"
python3 - <<'PY'
import json
for path in ("src/bin/autofilesync-daemon.py", "src/ui/api.cgi"):
    compile(open(path, encoding="utf-8").read(), path, "exec")
for path in ("src/ui/config", "spk/var-default-config.json", "spk/conf/privilege"):
    json.load(open(path, encoding="utf-8"))
print("   Python / JSON OK")
PY
grep -q "__AFS_TOKEN__" src/ui/index.html \
  || { echo "ERROR: src/ui/index.html 缺少 __AFS_TOKEN__ 占位符（installer 无法注入 token）"; exit 1; }
for icon in 16 24 32 48 64 72 256; do
  [ -f "src/ui/images/icon_${icon}.png" ] || { echo "ERROR: 缺少图标 src/ui/images/icon_${icon}.png"; exit 1; }
done
for icon in PACKAGE_ICON.PNG PACKAGE_ICON_256.PNG; do
  [ -f "$icon" ] || { echo "ERROR: 缺少套件图标 $icon"; exit 1; }
done

echo "==> 组装 package.tgz 内容"
install -m 755 "$ROOT/src/bin/autofilesync-daemon.py" "$STAGE/bin/autofilesync-daemon.py"
install -m 755 "$ROOT/src/bin/afs-rollback.py"        "$STAGE/bin/afs-rollback.py"
install -m 755 "$ROOT/src/bin/afs-fs.py"               "$STAGE/bin/afs-fs.py"
install -m 755 "$ROOT/src/ui/api.cgi"                 "$STAGE/ui/api.cgi"
install -m 644 "$ROOT/src/ui/index.html"              "$STAGE/ui/index.html"
install -m 644 "$ROOT/src/ui/config"                  "$STAGE/ui/config"
install -m 644 "$ROOT/src/ui/dsm-wrapper.js"          "$STAGE/ui/dsm-wrapper.js"
install -m 644 "$ROOT/src/ui/images/"icon_*.png       "$STAGE/ui/images/"
mkdir -p "$STAGE/ui/texts"
cp -a "$ROOT/src/ui/texts/." "$STAGE/ui/texts/"
install -m 644 "$ROOT/spk/var-default-config.json"    "$STAGE/var/config.json"

echo "==> scripts / conf / 图标 / LICENSE"
for f in installer service-setup start-stop-status \
         preinst postinst preuninst postuninst preupgrade postupgrade; do
  [ -f "$ROOT/spk/scripts/$f" ] || { echo "缺少 spk/scripts/$f"; exit 1; }
  install -m 755 "$ROOT/spk/scripts/$f" "$WORK/scripts/$f"
done
install -m 644 "$ROOT/spk/conf/privilege" "$WORK/conf/privilege"
install -m 644 "$ROOT/PACKAGE_ICON.PNG"     "$WORK/PACKAGE_ICON.PNG"       # DSM6/7 小图标
install -m 644 "$ROOT/PACKAGE_ICON_256.PNG" "$WORK/PACKAGE_ICON_256.PNG"
install -m 644 "$ROOT/LICENSE" "$WORK/LICENSE"
cp -a "$ROOT/spk/WIZARD_UIFILES" "$WORK/WIZARD_UIFILES"

echo "==> 生成 package.tgz（gzip，固定成员顺序）"
( cd "$STAGE" && tar czf "$WORK/package.tgz" bin ui var )

CHECKSUM="$(md5sum "$WORK/package.tgz" | cut -d' ' -f1)"
echo "==> package.tgz md5 = $CHECKSUM"

# INFO 只允许 key="value" 行，不能有任何注释/空行——DSM 的 spk 解析器会因此拒绝安装
# （dsmappname 必须等于 dsm-wrapper.js 中 Ext.define 的 AppInstance 类名）
cat > "$WORK/INFO" <<EOF
package="$PKG_NAME"
version="$VERSION"
description="Mirror multiple folders with rsync on a schedule: preview/real runs, 1-3 concurrent jobs, excludes, delete limits with rollback, run history, logs and DSM desktop notifications."
description_chs="用 rsync 按计划镜像同步多个文件夹：支持预览/实跑、1–3 个任务并行、排除规则、删除上限与超限回滚，带运行历史、日志和 DSM 桌面通知。"
description_cht="用 rsync 按計劃鏡像同步多個資料夾：支援預覽/實跑、1–3 個任務並行、排除規則、刪除上限與超限回滾，帶執行歷史、日誌和 DSM 桌面通知。"
arch="$ARCH"
os_min_ver="7.0-40120"
displayname="AutoFileSync"
dsmuidir="ui"
dsmappname="MeowPerth.AutoFileSync.AppInstance"
maintainer="MeowPerth"
maintainer_url="https://github.com/MeowPerth/AutoFileSync-DSM"
distributor="MeowPerth"
distributor_url="https://github.com/MeowPerth/AutoFileSync-DSM"
helpurl="https://github.com/MeowPerth/AutoFileSync-DSM#readme"
support_url="https://github.com/MeowPerth/AutoFileSync-DSM/issues"
startable="yes"
reloadui="yes"
changelog="$VERSION: 同步引擎（任务 / 调度 / 护栏 / 历史 / 通知）"
checksum="$CHECKSUM"
EOF
# 自检：INFO 必须全部是 key="value" 行
if grep -Evq '^[A-Za-z_]+=".*"$' "$WORK/INFO"; then
  echo "ERROR: INFO 含非法行："; grep -Env '^[A-Za-z_]+=".*"$' "$WORK/INFO"; exit 1
fi

echo "==> 打包 .spk（未压缩 tar）"
rm -f "$SPK_FILE"
( cd "$WORK" && tar cpf "$SPK_FILE" INFO package.tgz scripts conf WIZARD_UIFILES \
    PACKAGE_ICON.PNG PACKAGE_ICON_256.PNG LICENSE )

echo "==> 产物：$SPK_FILE ($(du -h "$SPK_FILE" | cut -f1))"
tar tf "$SPK_FILE"
echo "==> package.tgz 内容："
tar xfO "$SPK_FILE" package.tgz | tar tzf -
