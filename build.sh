#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

sdk="${WVBAR_SDK:-}"
if [ -z "$sdk" ]; then
  for candidate in \
    "/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk" \
    "$(xcrun --sdk macosx --show-sdk-path 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -d "$candidate" ]; then sdk="$candidate"; break; fi
  done
fi
if [ -z "$sdk" ] || [ ! -d "$sdk" ]; then
  echo "找不到 macOS SDK；请安装 Xcode Command Line Tools，或设置 WVBAR_SDK 指向 SDK 路径。"
  exit 1
fi
echo "使用 SDK: $sdk"

mkdir -p .tmp .modulecache
if ! TMPDIR="$PWD/.tmp" swiftc -O -sdk "$sdk" -module-cache-path "$PWD/.modulecache" \
  -framework Cocoa -framework WebKit menubar.swift -o .tmp/wvbar.new; then
  echo
  echo "编译失败。可尝试：WVBAR_SDK=/path/to/MacOSX*.sdk bash build.sh"
  exit 1
fi
mv .tmp/wvbar.new wvbar
echo "已生成 ./wvbar"
