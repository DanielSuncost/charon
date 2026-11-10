#!/bin/sh
# Compile the kernel and the capture core with their tests into a scratch binary and run it.
# Needs no grants, keychain or signing.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp "$here/Tests/KernelTests.swift" "$tmp/main.swift"   # top-level code must live in main.swift
swiftc -swift-version 5 -o "$tmp/kernel-tests" "$here"/Sources/Kernel/*.swift \
    "$here"/Sources/Capture/CaptureCore.swift "$tmp/main.swift"
"$tmp/kernel-tests"
