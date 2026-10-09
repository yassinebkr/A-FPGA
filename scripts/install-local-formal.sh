#!/bin/sh
set -eu

# Reproducible, project-local formal toolchain.  The default build is the
# smallest useful property-checking stack: Yosys + SymbiYosys + Z3.  EQY and
# Boolector are optional because their upstream source trees/build systems are
# not consistently available on every host (and EQY is not required by SBY).
# Nothing is installed system-wide; all outputs live under the gitignored
# .tools prefix used by start.sh.
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tools_root="$project_root/.tools"
prefix="$tools_root/eda"
source_root="$tools_root/src"
jobs=${EDA_BUILD_JOBS:-2}
install_optional=${FORMAL_OPTIONAL_TOOLS:-0}

# Tags are deliberately explicit.  Update them together with this script and
# review the resulting versions; do not silently follow a moving branch.
YOSYS_TAG=${YOSYS_TAG:-v0.50}
SBY_TAG=${SBY_TAG:-yosys-0.44}
Z3_TAG=${Z3_TAG:-z3-4.13.4}
BOOLECTOR_TAG=${BOOLECTOR_TAG:-3.2.3}
EQY_TAG=${EQY_TAG:-yosys-0.44}

mkdir -p "$prefix/bin" "$source_root"
if [ ! -f "$prefix/lib/pkgconfig/tcl.pc" ]; then
    echo "Tcl runtime missing; run ./scripts/install-local-timing-runtime.sh first" >&2
    exit 1
fi

# Tcl is installed by install-local-timing-runtime.sh and is required by the
# SiliconCompiler Yosys Tcl entrypoint.
PATH="$prefix/bin:$PATH"
PKG_CONFIG_PATH="$prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH PKG_CONFIG_PATH LD_LIBRARY_PATH

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
        return 1
    fi
}

echo "== Yosys $YOSYS_TAG =="
yosys_source_name="yosys-${YOSYS_TAG#yosys-}"
checkout_source "$yosys_source_name" https://github.com/YosysHQ/yosys.git "$YOSYS_TAG"
# Yosys 0.50 requires cxxopts; SiliconCompiler ASIC synthesis also requires
# the ABC revision pinned by the Yosys repository.
git -C "$source_root/$yosys_source_name" submodule update --init --depth 1 libs/cxxopts abc
(
    cd "$source_root/$yosys_source_name"
    # SiliconCompiler runs ASIC synthesis through Yosys Tcl scripts, so Tcl is
    # mandatory. Use the pinned project-local runtime; ABC is initialized from Yosys's pinned submodule revision for ASIC mapping.
    make config-gcc
    printf '%s\n' 'ENABLE_TCL := 1' 'ENABLE_ABC := 1' >> Makefile.conf
    make -j "$jobs"
    make PREFIX="$prefix" install
)

echo "== SymbiYosys $SBY_TAG =="
checkout_source sby https://github.com/YosysHQ/sby.git "$SBY_TAG"
(
    cd "$source_root/sby"
    make PREFIX="$prefix" install
)

echo "== Z3 $Z3_TAG =="
checkout_source z3 https://github.com/Z3Prover/z3.git "$Z3_TAG"
(
    cd "$source_root/z3"
    # Z3's source build generator is stable across the pinned release series.
    # Keep its generated build directory in .tools so the source tree remains
    # inspectable and the install can be removed atomically with the prefix.
    python3 scripts/mk_make.py --prefix="$prefix"
    make -C build -j "$jobs"
    make -C build install
)

PATH="$project_root/backend/.venv/bin:$prefix/bin:$PATH"
export PATH

# EQY is useful for equivalence but not needed for property checking.  Some
# releases publish no matching tag or require extra Python dependencies.  Try
# it only when requested and leave an explicit, non-fatal limitation message.
if [ "$install_optional" = "1" ]; then
    echo "== optional EQY $EQY_TAG =="
    if checkout_source eqy https://github.com/YosysHQ/eqy.git "$EQY_TAG"; then
        if (cd "$source_root/eqy" && make PREFIX="$prefix" install); then
            :
        elif [ -f "$source_root/eqy/eqy.py" ]; then
            install -m 0755 "$source_root/eqy/eqy.py" "$prefix/bin/eqy"
        else
            echo "EQY unavailable: pinned source has no supported install target" >&2
        fi
    else
        echo "EQY unavailable: tag $EQY_TAG is not published by upstream; continuing" >&2
    fi

    echo "== optional Boolector $BOOLECTOR_TAG =="
    if checkout_source boolector https://github.com/boolector/boolector.git "$BOOLECTOR_TAG"; then
        if (
            cd "$source_root/boolector"
            ./configure.sh --prefix="$prefix"
            make -j "$jobs"
            make install
        ); then
            :
        else
            echo "Boolector unavailable: source build failed on this host; continuing" >&2
        fi
    else
        echo "Boolector unavailable: pinned source/tag is not available; continuing" >&2
    fi
fi

echo "Formal toolchain installed under $prefix"
for tool in yosys sby z3 eqy boolector; do
    if command -v "$tool" >/dev/null 2>&1; then
        "$tool" --version 2>&1 | head -n 1
    else
        echo "$tool: unavailable (not part of the required property stack)"
    fi
done
