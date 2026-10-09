import json
import subprocess
import sys
from pathlib import Path

import pytest

from matmul_qualification import MATMUL_SPEC, CandidateEvidence, MatmulGoldenModel, QualificationStatus, evaluate_run, matmul_reference, qualification_vectors
from matmul_rtl_runner import run_functional

GOOD_RTL = r"""
module systolic_mm_4x4(
 input clk,input rst_n,input a_write,input b_write,input [3:0] write_index,
 input signed [7:0] a_data,input signed [7:0] b_data,input start,
 output reg busy,output reg done,input [3:0] read_index,output signed [31:0] c_data);
 reg signed [7:0] am[0:15], bm[0:15]; reg signed [31:0] cm[0:15];
 integer i,j,k; integer acc; reg [4:0] count;
 assign c_data=cm[read_index];
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin busy<=0; done<=0; count<=0; for(i=0;i<16;i=i+1) begin am[i]<=0;bm[i]<=0;cm[i]<=0;end end
  else begin
   done<=0;
   if(!busy) begin
    if(a_write) am[write_index]<=a_data;
    if(b_write) bm[write_index]<=b_data;
    if(start) begin
     for(i=0;i<4;i=i+1) for(j=0;j<4;j=j+1) begin
      acc=0; for(k=0;k<4;k=k+1) acc=acc+$signed(am[i*4+k])*$signed(bm[k*4+j]);
      cm[i*4+j]<=acc;
     end
     busy<=1; count<=10;
    end
   end else if(count==1) begin busy<=0; done<=1; count<=0; end
   else count<=count-1;
  end
 end
endmodule
"""

def test_reference_signed_extremes():
    result=matmul_reference([-128]*16,[-128]*16)
    assert result==(65536,)*16
    assert matmul_reference([-1]*16,[-1]*16)==(4,)*16

def test_vectors_and_cycle_contract_are_deterministic():
    first=qualification_vectors(seed=3,count=12); second=qualification_vectors(seed=3,count=12)
    assert first==second
    model=MatmulGoldenModel(); samples=model.load_and_run(first[0])
    started=next(s for s in samples if s.start_accepted); done=next(s for s in samples if s.done)
    assert done.cycle-started.cycle==MATMUL_SPEC.latency_cycles

def test_actual_rtl_regression_passes(tmp_path):
    rtl=tmp_path/'candidate.sv'; rtl.write_text(GOOD_RTL)
    result=run_functional(rtl,MATMUL_SPEC.top_module,qualification_vectors(count=8),tmp_path/'run')
    assert result.status=='passed', result
    assert result.data['vectors_run']==8

def test_wrong_rtl_fails(tmp_path):
    rtl=tmp_path/'candidate.sv'; rtl.write_text(GOOD_RTL.replace('cm[i*4+j]<=acc;','cm[i*4+j]<=0;'))
    result=run_functional(rtl,MATMUL_SPEC.top_module,qualification_vectors(count=8),tmp_path/'run')
    assert result.status=='failed'

def test_cli_dry_run_and_invalid_input(tmp_path):
    rtl=tmp_path/'candidate.sv'; rtl.write_text(GOOD_RTL)
    script=Path(__file__).parents[2]/'scripts/run-matmul-qualification.py'
    dry=subprocess.run([sys.executable,str(script),'--rtl',str(rtl),'--dry-run','--json'],capture_output=True,text=True)
    assert dry.returncode==0
    assert json.loads(dry.stdout)['status']=='preflight'
    invalid=subprocess.run([sys.executable,str(script),'--rtl',str(rtl),'--top','wrong'],capture_output=True,text=True)
    assert invalid.returncode==3


def test_cli_functional_run_persists_inconclusive_manifest_without_timing(tmp_path):
    rtl=tmp_path/'candidate.sv'; rtl.write_text(GOOD_RTL)
    output=tmp_path/'qualifications'
    script=Path(__file__).parents[2]/'scripts/run-matmul-qualification.py'
    run=subprocess.run([sys.executable,str(script),'--rtl',str(rtl),'--vectors','8',
                        '--output-root',str(output),'--json'],capture_output=True,text=True)
    assert run.returncode==2, run.stdout+run.stderr
    payload=json.loads(run.stdout)
    assert payload['status']=='inconclusive'
    assert payload['functional']['status']=='passed'
    manifest=Path(payload['manifest'])
    assert manifest.is_file()
    stored=json.loads(manifest.read_text())
    assert stored['rtl_sha256']==payload['rtl_sha256']
    assert stored['timing'] is None


def test_evidence_reducer_preserves_best_and_reports_bounded_gap():
    report=evaluate_run(candidates=(
        CandidateEvidence(1,True,True,46.41,-1.55,'accumulator carry chain',98041),
        CandidateEvidence(2,True,True,44.19,-2.63,'accumulator carry chain',82311),
    ),usage={'model_rounds':6,'rtl_revisions':2,'screen_invocations':3,
             'full_implementation_runs':1,'tokens':90000,'tool_calls':10,'wall_seconds':300},
      final_implementation_passed=True,count=8)
    assert report.status is QualificationStatus.FAILED
    assert report.best_revision==1
    assert report.best_fmax_mhz==46.41
    assert report.remaining_gap_mhz==pytest.approx(3.59)
    assert 'Retained best revision 1' in report.bounded_failure

def test_evidence_reducer_enforces_budget_and_final_implementation():
    candidate=(CandidateEvidence(1,True,True,55.0,1.0,None,100),)
    over=evaluate_run(candidates=candidate,usage={'tokens':120001},final_implementation_passed=True,count=8)
    assert over.status is QualificationStatus.FAILED
    assert over.budget_violations
    incomplete=evaluate_run(candidates=candidate,usage={},final_implementation_passed=False,count=8)
    assert incomplete.status is QualificationStatus.INCONCLUSIVE
