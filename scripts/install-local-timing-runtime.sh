#!/bin/sh
set -eu

# Reproducible Tcl runtime for the existing source-built OpenSTA binary.
# OpenSTA is linked against libtcl8.6.so; Debian hosts may not retain that
# runtime after an OS upgrade. Install it project-locally without sudo.
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tools_root="$project_root/.tools"
prefix="$tools_root/eda"
source_root="$tools_root/src"
jobs=${EDA_BUILD_JOBS:-2}
TCL_TAG=${TCL_TAG:-core-8-6-16}
source_dir="$source_root/tcl-${TCL_TAG#core-}"

mkdir -p "$prefix" "$source_root"
if [ ! -d "$source_dir/.git" ]; then
    git clone --depth 1 --branch "$TCL_TAG" https://github.com/tcltk/tcl.git "$source_dir"
fi
actual=$(git -C "$source_dir" describe --tags --exact-match 2>/dev/null || true)
if [ "$actual" != "$TCL_TAG" ]; then
    echo "Tcl source is at '$actual', expected '$TCL_TAG'" >&2
    exit 1
fi

(
    cd "$source_dir/unix"
    ./configure --prefix="$prefix" --enable-threads
    make -j "$jobs"
    make install
)

LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
TCL_LIBRARY="$prefix/lib/tcl8.6"
export LD_LIBRARY_PATH TCL_LIBRARY

echo "Tcl runtime installed under $prefix"
"$prefix/bin/tclsh8.6" <<'EOF'
puts "Tcl [info patchlevel]"
EOF
