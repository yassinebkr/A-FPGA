#!/bin/sh
set -eu

# Install KLayout into the user's nix profile — the same precedent as OpenROAD
# (see AGENTS.md, "EDA tool setup on this machine"). KLayout is not built from
# source into the project-local .tools prefix like yosys/verilator/iverilog:
# its Qt-based build is far heavier than the rest of the toolchain, and the
# nix binary cache provides a pinned, reproducible user-local install with no
# root access.
#
# Why it is required: SiliconCompiler's `pnr` flow fails closed without
# yosys + openroad + klayout (sc_job.pick_flow), so the post-P&R layout view
# (Phase G4: DEF-derived, digest-bound) can only be produced live when
# klayout is on PATH.
#
# Nix needs the new-style profile commands; this machine's user profile lives
# under ~/.local/state/nix/profiles/profile (symlinked as ~/.nix-profile).

NIX_BIN=${NIX_BIN:-/nix/var/nix/profiles/per-user/root/profile-1-link/bin/nix}
NIXPKGS_ATTR=${NIXPKGS_ATTR:-nixpkgs#klayout}

if [ ! -x "$NIX_BIN" ]; then
    echo "nix not found at $NIX_BIN; set NIX_BIN to your nix binary" >&2
    exit 1
fi

# The agent/desktop shell can carry a polluted LD_LIBRARY_PATH (editor
# extensions ship their own libstdc++); run nix with a clean loader path.
unset LD_LIBRARY_PATH
"$NIX_BIN" --extra-experimental-features 'nix-command flakes' profile add "$NIXPKGS_ATTR"

PROFILE_BIN="${HOME}/.local/state/nix/profiles/profile/bin"
echo "installed:"
"$PROFILE_BIN/klayout" -v
echo
echo "klayout is available at $PROFILE_BIN/klayout; ~/.nix-profile/bin is on PATH via ~/.bashrc"
