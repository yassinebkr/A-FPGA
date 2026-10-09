"""Executable RTL and Sky130 stages for the frozen matmul qualification."""
from __future__ import annotations
import hashlib, json, os, re, shutil, subprocess, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from matmul_qualification import MATMUL_SPEC, MatmulVector

@dataclass(frozen=True)
class StageResult:
    status: str
    reason: str
    data: dict[str, Any]
    def as_dict(self): return {"status": self.status, "reason": self.reason, "data": self.data}

def _sv32(value): return f"32'h{int(value) & 0xffffffff:08x}"

def build_testbench(vectors: Sequence[MatmulVector], top_module: str) -> str:
    blocks=[]
    for vector in vectors:
        lines=[f"    // {vector.vector_id}", "    a_write=1; b_write=1;"]
        for index,(av,bv) in enumerate(zip(vector.a,vector.b)):
            lines.append(f"    write_index=4'd{index}; a_data=8'h{av & 255:02x}; b_data=8'h{bv & 255:02x}; @(posedge clk); #1;")
        lines += ["    a_write=0; b_write=0; write_index=0; a_data=0; b_data=0;",
                  "    start=1; @(posedge clk); #1; start=0;",
                  "    if (!busy) begin $display(\"FAIL busy not asserted\"); $fatal(1); end",
                  f"    repeat ({MATMUL_SPEC.latency_cycles-1}) begin @(posedge clk); #1; if (done) begin $display(\"FAIL early done\"); $fatal(1); end end",
                  "    @(posedge clk); #1; if (!done) begin $display(\"FAIL missing done\"); $fatal(1); end"]
        for index,expected in enumerate(vector.expected):
            lines.append(f"    read_index=4'd{index}; #1; if (c_data !== {_sv32(expected)}) begin $display(\"FAIL {vector.vector_id} output={index} expected=%h actual=%h\", {_sv32(expected)}, c_data); $fatal(1); end")
        lines += ["    @(posedge clk); #1; if (done) begin $display(\"FAIL done pulse width\"); $fatal(1); end", f"    $display(\"PASS {vector.vector_id}\");"]
        blocks.append("\n".join(lines))
    body="\n".join(blocks)
    return f"""`timescale 1ns/1ps
module matmul_qualification_tb;
 reg clk=0; always #10 clk=~clk;
 reg rst_n=0,a_write=0,b_write=0,start=0; reg [3:0] write_index=0,read_index=0;
 reg signed [7:0] a_data=0,b_data=0; wire busy,done; wire signed [31:0] c_data;
 {top_module} dut(.clk(clk),.rst_n(rst_n),.a_write(a_write),.b_write(b_write),.write_index(write_index),.a_data(a_data),.b_data(b_data),.start(start),.busy(busy),.done(done),.read_index(read_index),.c_data(c_data));
 initial begin repeat(2) @(posedge clk); rst_n=1; @(posedge clk);
{body}
  $display("QUALIFICATION_PASS vectors={len(vectors)}"); $finish; end
 initial begin #2000000; $display("FAIL timeout"); $fatal(1); end
endmodule
"""

def validate_rtl(path: Path, top_module: str):
    if not path.is_file(): raise ValueError(f"RTL file does not exist: {path}")
    if top_module != MATMUL_SPEC.top_module: raise ValueError(f"top module must match frozen spec: {MATMUL_SPEC.top_module}")
    source=path.read_text(encoding="utf-8")
    if not re.search(rf"\bmodule\s+{re.escape(top_module)}\b",source): raise ValueError(f"RTL does not declare frozen top module {top_module}")
    return source,hashlib.sha256(source.encode()).hexdigest()

def run_functional(rtl: Path, top_module: str, vectors, work: Path):
    work.mkdir(parents=True, exist_ok=True)
    _,digest=validate_rtl(rtl,top_module)
    local_bin=Path(__file__).resolve().parents[1]/".tools/eda/bin"
    iv=shutil.which("iverilog") or (str(local_bin/"iverilog") if (local_bin/"iverilog").is_file() else None)
    vvp=shutil.which("vvp") or (str(local_bin/"vvp") if (local_bin/"vvp").is_file() else None)
    if not iv or not vvp: return StageResult("inconclusive","iverilog/vvp unavailable",{"rtl_sha256":digest})
    tb,image=work/"matmul_qualification_tb.sv",work/"matmul_qualification.vvp"; tb.write_text(build_testbench(vectors,top_module),encoding="utf-8")
    try:
        cr=subprocess.run([iv,"-g2012","-s","matmul_qualification_tb","-o",str(image),str(rtl),str(tb)],capture_output=True,text=True,timeout=120)
        clog=(cr.stdout or "")+(cr.stderr or ""); (work/"compile.log").write_text(clog,encoding="utf-8")
        if cr.returncode: return StageResult("failed","RTL/testbench compilation failed",{"rtl_sha256":digest,"log":"compile.log"})
        sr=subprocess.run([vvp,str(image)],capture_output=True,text=True,timeout=180); slog=(sr.stdout or "")+(sr.stderr or ""); (work/"simulation.log").write_text(slog,encoding="utf-8")
        passed=sr.returncode==0 and f"QUALIFICATION_PASS vectors={len(vectors)}" in slog
        return StageResult("passed" if passed else "failed","all frozen vectors and protocol checks passed" if passed else "functional/protocol regression failed",{"rtl_sha256":digest,"simulator":"iverilog","vectors_run":len(vectors) if passed else 0,"compile_log":"compile.log","simulation_log":"simulation.log"})
    except subprocess.TimeoutExpired as exc: return StageResult("failed",f"functional regression timeout: {exc}",{"rtl_sha256":digest})

def run_timing(rtl: Path, top_module: str, work: Path):
    _,digest=validate_rtl(rtl,top_module); sc=Path(__file__).with_name("sc_job.py")
    build_root=Path(os.environ.get("SC_BUILDS", "./build")).resolve()
    build=build_root/"matmul_qualification"/work.parent.name/work.name
    backend_python=Path(__file__).with_name(".venv")/"bin/python"
    python=str(backend_python) if backend_python.is_file() else sys.executable
    cmd=[python,str(sc),"--top",top_module,"--verilog",str(rtl),"--build-dir",str(build),"--target",MATMUL_SPEC.target_flow,"--clock-period-ns",str(MATMUL_SPEC.target_period_ns),"--flow","syn-sta"]
    env={**os.environ,"PYTHONUNBUFFERED":"1"}
    # The supported backend launcher adds locally installed EDA tools to PATH,
    # but this standalone qualification command must be equally reproducible.
    # Add only known existing installation directories and preserve user PATH.
    repo_root=Path(__file__).resolve().parents[1]
    tool_dirs=[repo_root/".tools/eda/bin",Path("/mnt/cold-hdd/OpenSTA/build")]
    existing=[str(path) for path in tool_dirs if path.is_dir()]
    if existing: env["PATH"]=os.pathsep.join(existing+[env.get("PATH","")])
    local_lib=repo_root/".tools/eda/lib"
    if local_lib.is_dir(): env["LD_LIBRARY_PATH"]=os.pathsep.join(
        [str(local_lib),env.get("LD_LIBRARY_PATH","")]).rstrip(os.pathsep)
    try: run=subprocess.run(cmd,capture_output=True,text=True,timeout=1200,env=env)
    except subprocess.TimeoutExpired as exc: return StageResult("inconclusive",f"Sky130 timing timeout: {exc}",{"rtl_sha256":digest})
    log=(run.stdout or "")+(run.stderr or ""); (work/"timing.log").write_text(log,encoding="utf-8"); payload=None
    for line in reversed(log.splitlines()):
        if line.startswith("SCJOB "):
            try: payload=json.loads(line[6:])
            except json.JSONDecodeError: pass
            break
    if run.returncode or not isinstance(payload,dict): return StageResult("inconclusive","Sky130 timing infrastructure did not produce a manifest",{"rtl_sha256":digest,"log":"timing.log"})
    metrics={}
    manifest_path=Path(str(payload.get("manifest_path") or ""))
    if manifest_path.is_file():
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        priority=("write.views","write.gds","route.detailed","route.global","cts.repair_timing","place.detailed","place.global","floorplan.init","synthesis.timing","timing","synthesis","route")
        for name in ("cellarea","cells","registers","peakpower","setupslack","holdslack","setuptns","unconstrained","fmax"):
            nodes=((manifest.get("metric",{}).get(name) or {}).get("node") or {})
            for step in priority:
                try: value=nodes[step]["0"]["value"]
                except (KeyError,TypeError): continue
                if value is not None: metrics[name]=value; break
    def num(name):
        value=metrics.get(name); value=value.get("value") if isinstance(value,dict) else value
        try: return float(str(value).split()[0])
        except (TypeError,ValueError): return None
    slack,fmax=num("setupslack"),num("fmax"); fmax=fmax/1e6 if fmax is not None and fmax>1e5 else fmax
    passed=(slack is not None and slack>=0) or (fmax is not None and fmax>=MATMUL_SPEC.target_fmax_mhz); status="passed" if passed else ("failed" if slack is not None or fmax is not None else "inconclusive")
    return StageResult(status,"50 MHz timing target met" if passed else "50 MHz timing target not met",{"rtl_sha256":digest,"metrics":metrics,"setupslack_ns":slack,"fmax_mhz":fmax,"synthesis_strategy":payload.get("synthesis_strategy"),"log":"timing.log","siliconcompiler_manifest":str(manifest_path) if manifest_path else None,"assurance":"demo_pdk_estimate"})

def create_run_directory(root: Path, digest: str):
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"); run=root/f"matmul3-{stamp}-{digest[:12]}"; run.mkdir(parents=True,exist_ok=False); return run

def write_json(path: Path,payload):
    temp=path.with_suffix(path.suffix+".tmp"); temp.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8"); os.replace(temp,path)
