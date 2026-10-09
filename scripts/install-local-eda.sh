#!/bin/sh
set -eu

# Reproducible host-development simulator installation. The entire prefix is
# gitignored and can be removed/rebuilt without touching project state.
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tools_root="$project_root/.tools"
prefix="$tools_root/eda"
source_root="$tools_root/src"
jobs=${EDA_BUILD_JOBS:-2}

VERILATOR_TAG=v5.040
IVERILOG_TAG=v12_0

mkdir -p "$prefix" "$source_root"

checkout_source() {
    name=$1
    url=$2
    tag=$3
    destination="$source_root/$name"
    if [ ! -d "$destination/.git" ]; then
        git clone --depth 1 --branch "$tag" "$url" "$destination"
    fi
    actual=$(git -C "$destination" describe --tags --exact-match 2>/dev/null || true)
    if [ "$actual" != "$tag" ]; then
        echo "$name source is at '$actual', expected '$tag'; remove $destination and retry" >&2
        exit 1
    fi
}

checkout_source verilator https://github.com/verilator/verilator.git "$VERILATOR_TAG"
(
    cd "$source_root/verilator"
    autoconf
    ./configure --prefix="$prefix"
    make -j "$jobs"
    make install
)

checkout_source iverilog https://github.com/steveicarus/iverilog.git "$IVERILOG_TAG"
(
    cd "$source_root/iverilog"
    sh autoconf.sh
    ./configure --prefix="$prefix"
    make -j "$jobs"
    make install
)

PATH="$prefix/bin:$PATH"
export PATH
verilator --version
iverilog -V
