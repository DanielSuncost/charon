#!/bin/sh
# Build and sign the Charon Desktop helper (ai.charon.desktop).
#
# The grants macOS gives this helper are pinned to its signing identity, so
# it must be signed with a stable certificate (a Developer ID), never
# ad hoc: an ad-hoc build loses every grant when it is rebuilt.
#
#   CHARON_DESKTOP_SIGN_IDENTITY  codesign identity (name or SHA-1); required
#   CHARON_DESKTOP_OUT            output dir (default: <repo>/build/desktop)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=${CHARON_DESKTOP_OUT:-$repo/build/desktop}
identity=${CHARON_DESKTOP_SIGN_IDENTITY:?set CHARON_DESKTOP_SIGN_IDENTITY to a Developer ID codesigning identity}
app="$out/CharonDesktop.app"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$here/Info.plist" "$app/Contents/Info.plist"
swiftc -O -swift-version 5 -o "$app/Contents/MacOS/CharonDesktop" "$here/Sources/main.swift"
# Hardened runtime, no entitlements: the helper needs none of the
# exceptions (no JIT, no unsigned libraries, no debugger attach).
codesign --force --timestamp --options runtime --sign "$identity" "$app"
codesign --verify --strict --verbose=2 "$app"
echo "$app"
