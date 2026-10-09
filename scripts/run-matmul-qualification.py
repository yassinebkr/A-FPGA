#!/usr/bin/env python3
"""Reproducible matmul3 qualification gate.

Preflight is free. ``--rtl`` runs the independent Icarus regression. Sky130
synthesis/STA runs only when ``--timing`` is explicitly supplied. This command
never invokes an LLM.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
SCRIPT_DIR=Path(__file__).resolve().parent
REPO_ROOT=SCRIPT_DIR.parent
BACKEND_DIR=REPO_ROOT/"backend"
sys.path.insert(0,str(BACKEND_DIR))
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT/".env")
    load_dotenv(BACKEND_DIR/".env")
except ImportError:
    pass
from matmul_qualification import MATMUL_SPEC, golden_model_provenance, qualification_vectors, vector_set_digest, preflight
from matmul_rtl_runner import create_run_directory, run_functional, run_timing, validate_rtl, write_json

EXIT_PASS=0; EXIT_FAIL=1; EXIT_INCONCLUSIVE=2; EXIT_INPUT=3; EXIT_EXECUTION=4

def parser():
    p=argparse.ArgumentParser(description="Frozen 4x4 INT8 matmul Sky130 qualification")
    p.add_argument("--rtl",type=Path,help="candidate RTL implementing the frozen interface")
    p.add_argument("--top",default=MATMUL_SPEC.top_module)
    p.add_argument("--timing",action="store_true",help="explicitly run bounded Sky130 synthesis+STA")
    p.add_argument("--trials",type=int,default=1,help="repeat the complete candidate evaluation")
    p.add_argument("--dry-run",action="store_true",help="validate inputs and print plan without simulation/EDA")
    p.add_argument("--seed",type=int,default=3); p.add_argument("--vectors",type=int,default=40)
    p.add_argument("--output-root",type=Path,default=REPO_ROOT/"backend/data/qualifications/matmul3")
    p.add_argument("--json",action="store_true",dest="as_json")
    return p

def emit(payload,as_json):
    if as_json: print(json.dumps(payload,indent=2,sort_keys=True))
    else:
        print(f"matmul3 qualification: {payload['status']}")
        print(payload.get('reason',''))
        if payload.get('manifest'): print(f"manifest: {payload['manifest']}")

def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.vectors <= 0: raise ValueError("--vectors must be positive")
        if args.trials <= 0: raise ValueError("--trials must be positive")
        vectors=qualification_vectors(seed=args.seed,count=args.vectors)
        if args.rtl is None:
            report=preflight(seed=args.seed,count=args.vectors).as_dict(); emit(report,args.as_json); return EXIT_PASS
        rtl=args.rtl.resolve(); source,digest=validate_rtl(rtl,args.top)
        plan={"schema":"matmul3-qualification-run/v1","status":"preflight" if args.dry_run else "running",
              "reason":"dry-run validated frozen spec, corpus, RTL and execution plan" if args.dry_run else "",
              "created_at":datetime.now(timezone.utc).isoformat(),"spec":MATMUL_SPEC.canonical(),
              "spec_sha256":MATMUL_SPEC.digest,"golden_model":golden_model_provenance(),
              "corpus_sha256":vector_set_digest(vectors),"vectors":len(vectors),
              "rtl":str(rtl),"rtl_sha256":digest,"top_module":args.top,"timing_requested":args.timing,"trials_requested":args.trials,
              "assurance":"Sky130 demo-PDK engineering evidence; not Tiny Tapeout/foundry signoff"}
        if args.dry_run: emit(plan,args.as_json); return EXIT_PASS
        run_dir=create_run_directory(args.output_root.resolve(),digest)
        write_json(run_dir/"spec.json",MATMUL_SPEC.canonical())
        write_json(run_dir/"corpus.json",[v.canonical() for v in vectors])
        trials=[]
        for number in range(1,args.trials+1):
            trial_dir=run_dir/f"trial-{number:03d}"; trial_dir.mkdir()
            functional=run_functional(rtl,args.top,vectors,trial_dir)
            timing=run_timing(rtl,args.top,trial_dir) if functional.status=="passed" and args.timing else None
            trial={"trial":number,"functional":functional.as_dict(),"timing":timing.as_dict() if timing else None}
            write_json(trial_dir/"manifest.json",trial); trials.append(trial)
            if functional.status!="passed" or (args.timing and (timing is None or timing.status!="passed")): break
        functionals=[trial["functional"]["status"] for trial in trials]
        timings=[trial["timing"]["status"] for trial in trials if trial["timing"]]
        if "failed" in functionals: status,reason,code="failed","functional/protocol regression failed",EXIT_FAIL
        elif "inconclusive" in functionals: status,reason,code="inconclusive","functional infrastructure unavailable",EXIT_INCONCLUSIVE
        elif not args.timing: status,reason,code="inconclusive","functional regression passed; timing evidence was not requested",EXIT_INCONCLUSIVE
        elif "failed" in timings: status,reason,code="failed","50 MHz timing target not met",EXIT_FAIL
        elif "inconclusive" in timings or len(timings)!=args.trials: status,reason,code="inconclusive","Sky130 timing evidence unavailable",EXIT_INCONCLUSIVE
        else: status,reason,code="passed",f"all {args.trials} trial(s) passed functional, protocol, and 50 MHz Sky130 timing criteria",EXIT_PASS
        manifest={**plan,"status":status,"reason":reason,"trials_completed":len(trials),"trials":trials,
                  "functional":trials[-1]["functional"],"timing":trials[-1]["timing"]}
        write_json(run_dir/"manifest.json",manifest)
        result={**manifest,"manifest":str(run_dir/"manifest.json")}; emit(result,args.as_json); return code
    except (ValueError,OSError,json.JSONDecodeError) as exc:
        emit({"status":"invalid","reason":str(exc)},args.as_json); return EXIT_INPUT
    except KeyboardInterrupt:
        emit({"status":"cancelled","reason":"qualification cancelled"},args.as_json); return EXIT_EXECUTION
    except Exception as exc:
        emit({"status":"error","reason":f"{type(exc).__name__}: {exc}"},args.as_json); return EXIT_EXECUTION

if __name__=="__main__": raise SystemExit(main())
