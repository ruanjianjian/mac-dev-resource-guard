#!/bin/bash
# 资源看板 one-line installer (one-line-install): download the source, build on this Mac, install.
#   curl -fsSL https://raw.githubusercontent.com/ruanjianjian/mac-dev-resource-guard/main/install.sh | bash
# Run it again to upgrade. RESOURCE_GUARD_REF picks a branch, tag (refs/tags/…) or commit.
set -euo pipefail

REPO="${RESOURCE_GUARD_REPO:-ruanjianjian/mac-dev-resource-guard}"
REF="${RESOURCE_GUARD_REF:-main}"
SOURCE="$HOME/.local/share/dev-resource-guard/source"

fail() { printf '安装未完成：%s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "资源看板只支持 macOS。"
command -v python3 >/dev/null 2>&1 || fail "缺少 Python 3，请先安装 Xcode 命令行工具：xcode-select --install"
if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find swiftc >/dev/null 2>&1; then
  xcode-select --install >/dev/null 2>&1 || true
  fail "缺少 Xcode 命令行工具。系统已弹出安装窗口，装好后再运行这条命令。"
fi

case "$REF" in
  refs/*) path="$REF" ;;
  *) if [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then path="$REF"; else path="refs/heads/$REF"; fi ;;
esac
url="${RESOURCE_GUARD_TARBALL:-https://codeload.github.com/$REPO/tar.gz/$path}"

parent="$(dirname "$SOURCE")"
mkdir -p "$parent"
staging="$(mktemp -d "$parent/.source-new.XXXXXX")"
trap 'rm -rf "$staging"' EXIT
printf '正在下载资源看板（%s）…\n' "$REF"
if ! curl -fsSL --retry 2 "$url" | tar -xz -C "$staging" --strip-components 1; then
  fail "下载失败或源码包不完整（${url}）。已装的版本没有改动，可以稍后重试。"
fi
[ -f "$staging/scripts/install.py" ] || fail "源码包里没有安装脚本（${url}）。已装的版本没有改动。"

# Swap the source only after it is complete, so a failed download never leaves half a tree.
old="$parent/.source-old.$$"
[ -e "$SOURCE" ] && mv "$SOURCE" "$old"
mv "$staging" "$SOURCE"
trap - EXIT
rm -rf "$old"

python3 "$SOURCE/scripts/install.py"
