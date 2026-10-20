#!/bin/sh
# Fixed release + upstream SHA-256; keep dependencies outside the source tree.
set -eu
task_deps="${1:-$HOME/build/ai-gateway-deps}"
mkdir -p "$task_deps"
task_archive="$task_deps/boost_1_88_0.tar.bz2"
if [ ! -f "$task_archive" ]; then
  curl --fail --location --retry 2 --connect-timeout 15 \
    https://archives.boost.io/release/1.88.0/source/boost_1_88_0.tar.bz2 -o "$task_archive"
fi
echo "46d9d2c06637b219270877c9e16155cbd015b6dc84349af064c088e9b5b12f7b  $task_archive" | sha256sum --check
if [ ! -d "$task_deps/boost_1_88_0" ]; then
  tar -xjf "$task_archive" -C "$task_deps"
fi
echo "Boost source: $task_deps/boost_1_88_0"
