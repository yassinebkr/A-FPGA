# FPGA target profiles

An FPGA profile is a versioned, board-neutral description of the target used
by future FPGA build and hardware-in-the-loop adapters. It is configuration
and evidence metadata; adapters read it as data rather than run it. Profiles must not
contain API keys, shell commands, arbitrary executable paths, or board-specific
assumptions in shared code.

The schema is implemented by `backend/fpga_profile.py` (`fpga-profile/v1`) and
covers:

- board identity and FPGA vendor/family/part;
- toolchain name, version, backend, and constraint format;
- primary clock and optional reset polarity/synchrony;
- logical HDL-port to physical-pin mappings and electrical attributes;
- programming transport, programmer identity, bitstream format, and
  post-program verification policy;
- an operation timeout and arbitrary non-authoritative metadata.

Profiles are content-addressable. Call `profile.with_hash()` before persisting
one; `profile.hash_matches()` verifies the stored `profile_sha256`. The digest
is calculated over canonical JSON with the digest field omitted, so equivalent
field ordering produces the same value.

No FPGA tool execution is implied by loading or validating a profile. A later
adapter will be responsible for resolving a known toolchain, generating a
bitstream, programming a selected board, and attaching logs and HIL evidence
to the verification manifest. Until that adapter exists, a hardware stage
must remain `inconclusive` when no board is available.
