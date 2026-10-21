#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
# Installed Swift 6.3.3 is compatible with this SDK, not the default 27.0 SDK.
sdk="${WVBAR_SDK:-/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk}"
mkdir -p .tmp .modulecache
TMPDIR="$PWD/.tmp" swiftc -O -sdk "$sdk" -module-cache-path "$PWD/.modulecache" \
  -framework Cocoa -framework WebKit menubar.swift -o .tmp/wvbar.new
mv .tmp/wvbar.new wvbar
