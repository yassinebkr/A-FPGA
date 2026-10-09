#!/usr/bin/env python3
"""Run one paid, bounded Kimi repair completion for the saved matmul3 RTL."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backend/data/evidence/artifacts/sha256/c2/c2ec673bb182d6f33386b95ab963e9143f98c6c5ccd0f45493a648945cbf3478"
OUT_ROOT = ROOT / "backend/data/qualifications/matmul3/focused-repair"

FAILURE = """The simulation has a deterministic row shift. For random matrices,
result row 0 contains expected row 1, result row 1 contains expected row 2,
result row 2 contains expected row 3, and result row 3 is correct. The same
pattern repeats on a second frame, a back-to-back frame, and after reset.
The frozen independent testbench reports 56 mismatches. Lint is clean."""


def extract_verilog(text: str) -> str:
    tagged = re.search(r"<verilog>\s*(.*?)\s*</verilog>", text, re.S | re.I)
    if tagged:
        code = tagged.group(1)
    else:
        fenced = re.search(r"```(?:systemverilog|verilog|sv)?\s*(.*?)```", text, re.S | re.I)
        if not fenced:
            raise RuntimeError("Kimi response did not contain a Verilog block")
        code = fenced.group(1)
    code = code.strip() + "\n"
    if "module systolic_mm_4x4" not in code or "endmodule" not in code:
        raise RuntimeError("Kimi response did not contain the required complete module")
    return code


def main() -> int:
    # Process environment wins; repository root is the canonical project config.
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend/.env", override=False)
    key = os.getenv("KIMI_API_KEY")
    if not key:
        raise RuntimeError("KIMI_API_KEY is not configured")

    source = SOURCE.read_text()
    prompt = f"""You are repairing one already-approved RTL candidate. Do not redesign
the architecture, change the interface, alter latency/II, write a testbench, or
call tools. Diagnose only the row/tag/valid pipeline alignment defect described
below and return one minimally corrected complete module.

Frozen requirements: module systolic_mm_4x4; signed int8 4x4 multiply to exact
signed int32; asynchronous active-low reset; combinational indexed result read;
done exactly 10 cycles after start; II=10 including back-to-back acceptance.
Keep the three arithmetic stages and bounded one-multiply/one-adder path depths.

Observed independent-simulation failure:
{FAILURE}

Return a concise diagnosis followed by the complete corrected RTL between exact
<verilog> and </verilog> tags. Make only the minimum control/tag alignment fix.

CURRENT LINT-CLEAN RTL (sha256 c2ec673bb182d6f33386b95ab963e9143f98c6c5ccd0f45493a648945cbf3478):
{source}"""

    client = OpenAI(
        api_key=key,
        base_url=os.getenv("KIMI_BASE_URL", "https://api.kimi.com/coding/v1"),
        timeout=900.0,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=os.getenv("KIMI_MODEL", "k3"),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8000,
        temperature=1,
    )
    text = response.choices[0].message.content or ""
    code = extract_verilog(text)
    digest = hashlib.sha256(code.encode()).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUT_ROOT / f"{stamp}-{digest[:12]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "systolic_mm_4x4.sv").write_text(code)
    usage = response.usage.model_dump() if response.usage else None
    record = {
        "schema": "focused-kimi-matmul-repair/v1",
        "provider": "kimi-direct",
        "model": os.getenv("KIMI_MODEL", "k3"),
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "candidate_sha256": digest,
        "usage": usage,
        "failure_summary": FAILURE,
        "response": text,
    }
    (run_dir / "model-response.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"run_dir": str(run_dir), "rtl": str(run_dir / "systolic_mm_4x4.sv"),
                      "candidate_sha256": digest, "usage": usage}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
