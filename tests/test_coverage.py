import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ledger import (build, coverage_for_file, coverage_report_from_ledger,
                    extract_result_texts, parse_call_records, render_coverage,
                    resolve_session, tool_result_linkage)


class CoverageTests(unittest.TestCase):
    def test_read_file_result_covers_numbered_source_lines(self):
        raw = json.dumps({"content": "1|alpha\n2|beta\n"})
        coverage = coverage_for_file(
            ["alpha\n", "beta\n", "gamma\n"],
            [raw],
        )
        self.assertEqual(coverage["covered_lines"], [1, 2])
        self.assertEqual(coverage["uncovered_lines"], [3])
        self.assertEqual(coverage["covered_count"], 2)
        self.assertEqual(coverage["total_count"], 3)

    def test_resolve_session_rejects_unknown_explicit_id(self):
        import sqlite3
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("create table messages (session_id text, timestamp real)")
        connection.execute("insert into messages values ('known', 1)")
        with self.assertRaises(ValueError):
            resolve_session(connection, "missing")

    def test_fixture_has_tool_result_bodies(self):
        fixture = Path(__file__).with_name("fixtures-session-181656.json")
        payloads = json.loads(fixture.read_text())
        self.assertGreater(len(payloads), 0)
        self.assertTrue(any("19.3%" in text for text in payloads))

    def test_result_texts_ignore_metadata_and_decode_output(self):
        raw = json.dumps({
            "output": "alpha\n",
            "exit_code": 0,
            "error": None,
        })
        self.assertEqual(extract_result_texts(raw), ["alpha\n"])

    def test_coverage_for_file_accepts_result_without_trailing_newline(self):
        raw = json.dumps({"content": "alpha"})
        coverage = coverage_for_file(["alpha\n"], [raw])
        self.assertEqual(coverage["covered_lines"], [1])
        self.assertEqual(coverage["uncovered_lines"], [])


if __name__ == "__main__":
    unittest.main()


class CoverageReportTests(unittest.TestCase):
    def test_coverage_report_reads_corpus_and_reports_partial_lines(self):
        from ledger import coverage_report
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            reports = coverage_report(
                [str(corpus)],
                [json.dumps({"content": "1|alpha\n3|gamma\n"})],
            )
        self.assertEqual(reports[0]["covered_lines"], [1, 3])
        self.assertEqual(reports[0]["uncovered_lines"], [2])
        self.assertEqual(reports[0]["covered_count"], 2)
        self.assertEqual(reports[0]["total_count"], 3)

    def test_render_coverage_exposes_partial_and_ambiguous_lines(self):
        text = render_coverage([{
            "path": "review.md",
            "covered_lines": [1],
            "uncovered_lines": [2, 3],
            "ambiguous_lines": [3],
            "covered_count": 1,
            "total_count": 3,
            "coverage_percent": 33.3333,
            "touched": True,
        }])
        self.assertIn("review.md", text)
        self.assertIn("1/3", text)
        self.assertIn("uncovered: 2, 3", text)
        self.assertIn("ambiguous: 3", text)


class CoverageMatchingTests(unittest.TestCase):
    def test_partial_line_substrings_are_not_counted_as_coverage(self):
        raw = json.dumps({"content": "1|alphabet"})
        coverage = coverage_for_file(["alpha\n"], [raw])
        self.assertEqual(coverage["covered_lines"], [])
        self.assertEqual(coverage["uncovered_lines"], [1])


class ShellPathTests(unittest.TestCase):
    def test_heredoc_body_is_ignored_when_redirect_follows_delimiter(self):
        from ledger import paths_from_command
        command = "cat <<'EOF' > /hoshi/out\nREADME mentions cat > /hoshi/fake <<'EOF'\nEOF"
        self.assertEqual(paths_from_command(command), ['/hoshi/out'])

class CommandOutputTests(unittest.TestCase):
    def test_output_option_is_recorded_as_written_artifact(self):
        from ledger import paths_from_command
        command = "python3 ledger.py --out /hoshi/projects/selfreport-audit/current.md"
        self.assertEqual(
            paths_from_command(command),
            ["/hoshi/projects/selfreport-audit/current.md"],
        )

    def test_output_format_option_is_not_recorded_as_a_file(self):
        from ledger import paths_from_command
        command = "tool --output-format json input.json"
        self.assertEqual(paths_from_command(command), [])


class LedgerClassificationTests(unittest.TestCase):
    def test_write_tools_are_not_reported_as_reads(self):
        rows = [{
            "role": "assistant",
            "tool_calls": json.dumps([{
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "/tmp/out", "content": "x"}),
                }
            }]),
            "content": "",
            "id": 1,
            "timestamp": 1,
        }]
        ledger = build(rows)
        self.assertEqual(ledger["writes"], [("/tmp/out", "path=/tmp/out")])
        self.assertEqual(ledger["reads"], [])

    def test_patch_tools_are_not_reported_as_reads(self):
        rows = [{
            "role": "assistant",
            "tool_calls": json.dumps([{
                "function": {
                    "name": "patch",
                    "arguments": json.dumps({"path": "/tmp/out", "old_string": "a", "new_string": "b"}),
                }
            }]),
            "content": "",
            "id": 1,
            "timestamp": 1,
        }]
        ledger = build(rows)
        self.assertEqual(ledger["writes"], [("/tmp/out", "path=/tmp/out")])
        self.assertEqual(ledger["reads"], [])

    def test_current_output_path_is_excluded_from_artifacts(self):
        rows = [{
            "role": "assistant",
            "tool_calls": json.dumps([{
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({
                        "command": "python3 ledger.py --out /tmp/report.md"
                    }),
                }
            }]),
            "content": "",
            "id": 1,
            "timestamp": 1,
        }]
        ledger = build(rows, exclude_writes=["/tmp/report.md"])
        self.assertEqual(ledger["writes"], [])


class ToolResultLinkageTests(unittest.TestCase):
    def test_parse_call_records_preserves_call_id(self):
        raw = json.dumps([{"id": "call_123", "function": {
            "name": "read_file",
            "arguments": json.dumps({"path": "/tmp/a"}),
        }}])
        self.assertEqual(parse_call_records(raw), [{
            "call_id": "call_123",
            "name": "read_file",
            "args": {"path": "/tmp/a"},
        }])

    def test_tool_result_linkage_reports_missing_and_orphaned_results(self):
        calls = [{"call_id": "call_1"}, {"call_id": "call_2"}]
        results = [{"call_id": "call_1"}, {"call_id": "orphan"}]
        self.assertEqual(tool_result_linkage(calls, results), {
            "matched": 1,
            "missing_call_ids": ["call_2"],
            "orphan_result_ids": ["orphan"],
            "calls_without_ids": 0,
            "results_without_ids": 0,
        })


class ReadEvidenceTests(unittest.TestCase):
    def _rows(self, path="/tmp/review.md", read_result=None, terminal_result=None):
        rows = [{
            "role": "assistant",
            "tool_calls": json.dumps([{
                "id": "call_read",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": path}),
                },
            }]),
            "content": "",
            "id": 1,
            "timestamp": 1,
        }, {
            "role": "assistant",
            "tool_calls": json.dumps([{
                "id": "call_terminal",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": "printf beta"}),
                },
            }]),
            "content": "",
            "id": 2,
            "timestamp": 2,
        }]
        if read_result is not None:
            rows.append({
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_read", "content": read_result, "timestamp": 3,
            })
        if terminal_result is not None:
            rows.append({
                "role": "tool", "tool_name": "terminal",
                "tool_call_id": "call_terminal", "content": terminal_result, "timestamp": 4,
            })
        return rows

    def test_coverage_uses_only_matching_read_file_result(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\n", encoding="utf-8")
            led = build(self._rows(str(corpus),
                json.dumps({"content": "1|alpha"}),
                json.dumps({"content": "2|beta"}),
            ))
            report = coverage_report_from_ledger([str(corpus)], led)[0]

        self.assertEqual(report["covered_lines"], [1])
        self.assertEqual(report["uncovered_lines"], [2])
        self.assertEqual(report["read_call_ids"], ["call_read"])

    def test_coverage_marks_truncated_read_file_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\n", encoding="utf-8")
            led = build(self._rows(str(corpus), json.dumps({
                "content": "1|alpha",
                "truncated": True,
                "next_offset": 2,
                "total_lines": 2,
            })))
            report = coverage_report_from_ledger([str(corpus)], led)[0]

        self.assertTrue(report["truncated"])
        self.assertEqual(report["next_offsets"], [2])

    def test_coverage_recognizes_continuation_after_truncated_read(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            rows = [{
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_read_1",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 1, "limit": 2,
                        }),
                    },
                }]),
                "content": "", "id": 1, "timestamp": 1,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_read_1",
                "content": json.dumps({
                    "content": "1|alpha\n2|beta", "truncated": True, "next_offset": 3,
                }),
                "timestamp": 2,
            }, {
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_read_2",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 3, "limit": 2,
                        }),
                    },
                }]),
                "content": "", "id": 3, "timestamp": 3,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_read_2",
                "content": json.dumps({"content": "3|gamma"}),
                "timestamp": 4,
            }]
            report = coverage_report_from_ledger([str(corpus)], build(rows))[0]

        self.assertEqual(report["continued_read_call_ids"], ["call_read_2"])
        self.assertEqual(report["continuation_chains"], [["call_read_1", "call_read_2"]])
        self.assertEqual(report["uncontinued_truncated_call_ids"], [])

    def test_still_truncated_continuation_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            rows = [{
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_first",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 1, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 1, "timestamp": 1,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_first",
                "content": json.dumps({
                    "content": "1|alpha", "truncated": True, "next_offset": 2,
                }),
                "timestamp": 2,
            }, {
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_second",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 2, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 3, "timestamp": 3,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_second",
                "content": json.dumps({
                    "content": "2|beta", "truncated": True, "next_offset": 3,
                }),
                "timestamp": 4,
            }]
            report = coverage_report_from_ledger([str(corpus)], build(rows))[0]

        self.assertEqual(report["continued_read_call_ids"], ["call_second"])
        self.assertEqual(
            report["truncated_continuation_read_call_ids"], ["call_second"]
        )
        self.assertEqual(report["continuation_chains"], [["call_first", "call_second"]])
        self.assertEqual(report["uncontinued_truncated_call_ids"], ["call_second"])

    def test_continuation_without_stored_result_is_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            rows = [{
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_first",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 1, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 1, "timestamp": 1,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_first",
                "content": json.dumps({
                    "content": "1|alpha", "truncated": True, "next_offset": 2,
                }),
                "timestamp": 2,
            }, {
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_second",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 2, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 3, "timestamp": 3,
            }]
            report = coverage_report_from_ledger([str(corpus)], build(rows))[0]

        self.assertEqual(report["continued_read_call_ids"], [])
        self.assertEqual(report["uncontinued_truncated_call_ids"], ["call_first"])
        self.assertEqual(report["missing_read_call_ids"], ["call_second"])

    def test_parallel_read_calls_are_not_continuations(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\n", encoding="utf-8")
            rows = [{
                "role": "assistant", "tool_calls": json.dumps([
                    {
                        "id": "call_first",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({
                                "path": str(corpus), "offset": 1, "limit": 1,
                            }),
                        },
                    },
                    {
                        "id": "call_parallel",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({
                                "path": str(corpus), "offset": 2, "limit": 1,
                            }),
                        },
                    },
                ]),
                "content": "", "id": 1, "timestamp": 1,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_first",
                "content": json.dumps({
                    "content": "1|alpha", "truncated": True, "next_offset": 2,
                }),
                "timestamp": 2,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_parallel",
                "content": json.dumps({"content": "2|beta"}),
                "timestamp": 3,
            }]
            report = coverage_report_from_ledger([str(corpus)], build(rows))[0]

        self.assertEqual(report["continued_read_call_ids"], [])
        self.assertEqual(report["uncontinued_truncated_call_ids"], ["call_first"])

    def test_coverage_does_not_use_earlier_read_as_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\n", encoding="utf-8")
            rows = [{
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_later",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 3,
                        }),
                    },
                }]),
                "content": "", "id": 1, "timestamp": 1,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_later",
                "content": json.dumps({"content": "3|gamma"}),
                "timestamp": 2,
            }, {
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_first",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 1,
                        }),
                    },
                }]),
                "content": "", "id": 3, "timestamp": 3,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_first",
                "content": json.dumps({
                    "content": "1|alpha", "truncated": True,
                    "next_offset": 3,
                }),
                "timestamp": 4,
            }]
            report = coverage_report_from_ledger([str(corpus)], build(rows))[0]

        self.assertEqual(report["continued_read_call_ids"], [])
        self.assertEqual(report["uncontinued_truncated_call_ids"], ["call_first"])

    def test_coverage_matches_relative_read_path_against_ledger_base(self):
        import ledger
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\n", encoding="utf-8")
            old_base = ledger.BASE
            ledger.BASE = directory
            try:
                rows = [{
                    "role": "assistant", "tool_calls": json.dumps([{
                        "id": "call_relative",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "review.md"}),
                        },
                    }]),
                    "content": "", "id": 1, "timestamp": 1,
                }, {
                    "role": "tool", "tool_name": "read_file",
                    "tool_call_id": "call_relative",
                    "content": json.dumps({"content": "1|alpha"}),
                    "timestamp": 2,
                }]
                report = coverage_report_from_ledger([str(corpus)], build(rows))[0]
            finally:
                ledger.BASE = old_base

        self.assertEqual(report["covered_lines"], [1])

    def test_coverage_does_not_infer_continuation_without_next_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\n", encoding="utf-8")
            rows = [{
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_first",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 1, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 1, "timestamp": 1,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_first",
                "content": json.dumps({"content": "1|alpha", "truncated": True}),
                "timestamp": 2,
            }, {
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_second",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 1, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 3, "timestamp": 3,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_second",
                "content": json.dumps({"content": "1|alpha"}),
                "timestamp": 4,
            }]
            report = coverage_report_from_ledger([str(corpus)], build(rows))[0]

        self.assertEqual(report["continued_read_call_ids"], [])
        self.assertEqual(report["uncontinued_truncated_call_ids"], ["call_first"])

    def test_coverage_reports_read_call_with_missing_result(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\n", encoding="utf-8")
            led = build(self._rows(str(corpus)))
            report = coverage_report_from_ledger([str(corpus)], led)[0]

        self.assertEqual(report["read_call_ids"], [])
        self.assertEqual(report["missing_read_call_ids"], ["call_read"])
        self.assertEqual(report["covered_lines"], [])


    def test_multi_segment_continuation_chain_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "review.md"
            corpus.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            rows = [{
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_first",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 1, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 1, "timestamp": 1,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_first",
                "content": json.dumps({
                    "content": "1|alpha", "truncated": True, "next_offset": 2,
                }),
                "timestamp": 2,
            }, {
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_second",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 2, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 3, "timestamp": 3,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_second",
                "content": json.dumps({
                    "content": "2|beta", "truncated": True, "next_offset": 3,
                }),
                "timestamp": 4,
            }, {
                "role": "assistant", "tool_calls": json.dumps([{
                    "id": "call_third",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": str(corpus), "offset": 3, "limit": 1,
                        }),
                    },
                }]),
                "content": "", "id": 5, "timestamp": 5,
            }, {
                "role": "tool", "tool_name": "read_file",
                "tool_call_id": "call_third",
                "content": json.dumps({"content": "3|gamma"}),
                "timestamp": 6,
            }]
            report = coverage_report_from_ledger([str(corpus)], build(rows))[0]

        self.assertEqual(
            report["continuation_chains"],
            [["call_first", "call_second", "call_third"]],
        )
        self.assertEqual(report["uncontinued_truncated_call_ids"], [])
