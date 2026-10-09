from module_index import build_module_index


def test_module_index_is_digest_bound_and_location_preserving():
    project = {
        "digest": "a" * 64, "top_module": "top",
        "files": [
            {"path": "rtl/top.sv", "role": "rtl", "sha256": "b" * 64,
             "content": "// module fake;\nmodule top;\nendmodule\n"},
            {"path": "rtl/alu.sv", "role": "rtl", "sha256": "c" * 64,
             "content": "/* module fake2; */\nmodule alu; endmodule\n"},
        ],
    }
    result = build_module_index(project)
    assert result["schema"] == "module-index/v1"
    assert result["hardware_project_digest"] == project["digest"]
    assert [(m["name"], m["file"], m["line"]) for m in result["modules"]] == [
        ("top", "rtl/top.sv", 2), ("alu", "rtl/alu.sv", 2)]
    assert result["completeness"] == "declarations_only"


def test_module_index_reports_duplicate_declarations():
    result = build_module_index({"digest": "d", "top_module": "x", "files": [
        {"path": "a.v", "role": "rtl", "content": "module x; endmodule", "sha256": "1"},
        {"path": "b.v", "role": "rtl", "content": "module x; endmodule", "sha256": "2"},
    ]})
    assert result["duplicate_modules"] == ["x"]
