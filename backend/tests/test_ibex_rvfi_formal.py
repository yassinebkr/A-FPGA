import hashlib

import pytest

from ibex_rvfi_formal import instrument_converted_rtl


def test_instrumentation_is_explicit_and_does_not_replace_rtl():
    source = "module helper; endmodule\nmodule ibex_core(input clk_i);\nendmodule\n"
    result = instrument_converted_rtl(source)
    assert result.startswith("module helper; endmodule\nmodule ibex_core")
    assert "assert(rvfi_ixl == 2'd1);" in result
    assert result.count("endmodule") == 2
    assert hashlib.sha256(result.encode()).hexdigest() != hashlib.sha256(source.encode()).hexdigest()


def test_instrumentation_fails_closed_on_wrong_or_reused_artifact():
    with pytest.raises(ValueError, match="ibex_core"):
        instrument_converted_rtl("module other; endmodule\n")
    once = instrument_converted_rtl("module ibex_core;\nendmodule\n")
    with pytest.raises(ValueError, match="already instrumented"):
        instrument_converted_rtl(once)
