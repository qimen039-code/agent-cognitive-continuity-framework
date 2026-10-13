from __future__ import annotations

import importlib
import unittest
from pathlib import Path


class PythonInlineWriteAnalysisTests(unittest.TestCase):
    def analyze(self, command: str):
        try:
            module = importlib.import_module("python_inline_write_analysis")
        except ModuleNotFoundError:
            self.fail("python_inline_write_analysis module is missing")
        analyzer = getattr(module, "analyze_python_inline_write_command", None)
        self.assertTrue(callable(analyzer), "inline write analyzer is missing")
        return analyzer(command)

    def operation_binding(self, command: str):
        module = importlib.import_module("python_inline_write_analysis")
        binder = getattr(module, "python_inline_write_operation_binding", None)
        self.assertTrue(callable(binder), "inline write operation binder is missing")
        return binder(command)

    def test_literal_open_write_returns_exact_target_and_actual_payload(self) -> None:
        result = self.analyze(
            r"""python -c "open('report.txt','w',encoding='utf-8').write('bad\ufffd')" """
        )

        self.assertTrue(result.recognized_inline)
        self.assertEqual("parsed", result.parse_status)
        self.assertTrue(result.write_intent)
        self.assertEqual(("report.txt",), result.exact_targets)
        self.assertFalse(result.unresolved_target)
        self.assertTrue(result.static_write_only)
        self.assertEqual(("bad\ufffd",), result.literal_text_payloads)
        self.assertTrue(result.contains_unicode_replacement_character)

    def test_path_write_text_is_recognized_without_marking_plain_python(self) -> None:
        write = self.analyze(
            """py -c "from pathlib import Path; Path('report.txt').write_text('bad�', encoding='utf-8')" """
        )
        joined = self.analyze(
            """python -c "from pathlib import Path; Path('reports', 'daily.txt').write_text('ok')" """
        )
        plain = self.analyze("""python -c "print('bad�')" """)
        read = self.analyze("""python -c "print(open('report.txt').read())" """)

        self.assertTrue(write.write_intent)
        self.assertEqual(("report.txt",), write.exact_targets)
        self.assertFalse(write.static_write_only)
        self.assertTrue(write.contains_unicode_replacement_character)
        self.assertEqual(
            (str(Path("reports", "daily.txt")),),
            joined.exact_targets,
        )
        self.assertFalse(plain.write_intent)
        self.assertFalse(read.write_intent)

    def test_dynamic_target_preserves_write_intent_but_is_unresolved(self) -> None:
        result = self.analyze(
            r"""python -c "open(name,'w').write('bad\ufffd')" """
        )
        partly_dynamic_path = self.analyze(
            """python -c "from pathlib import Path; Path('reports', name).write_text('bad�')" """
        )
        byte_path = self.analyze(
            r"""python -c "open(b'report.txt','wb').write(b'data')" """
        )

        self.assertTrue(result.write_intent)
        self.assertEqual((), result.exact_targets)
        self.assertTrue(result.unresolved_target)
        self.assertTrue(result.contains_unicode_replacement_character)
        self.assertTrue(partly_dynamic_path.write_intent)
        self.assertEqual((), partly_dynamic_path.exact_targets)
        self.assertTrue(partly_dynamic_path.unresolved_target)
        self.assertTrue(byte_path.write_intent)
        self.assertEqual((), byte_path.exact_targets)
        self.assertTrue(byte_path.unresolved_target)

    def test_partial_write_script_is_detected_but_not_classified_as_write_only(
        self,
    ) -> None:
        extra_python = self.analyze(
            """python -c "open('report.txt','w').write('ok'); import os; """
            """os.replace('a','b')" """
        )
        extra_shell = self.analyze(
            """python -c "open('report.txt','w').write('ok')"; unknown-tool"""
        )
        dynamic_opener = self.analyze(
            """python -c "open('report.txt','w',opener=make_opener()).write('ok')" """
        )

        for result in (extra_python, extra_shell, dynamic_opener):
            with self.subTest(result=result):
                self.assertTrue(result.write_intent)
                self.assertFalse(result.static_write_only)

    def test_operation_binding_allows_payload_repair_but_preserves_write_mode(self) -> None:
        damaged = self.operation_binding(
            r"""python -c "open('report.txt','w',encoding='utf-8').write('bad\ufffd')" """
        )
        repaired = self.operation_binding(
            """python -c "open('report.txt','w',encoding='utf-8').write('clean')" """
        )
        append_mode = self.operation_binding(
            """python -c "open('report.txt','a',encoding='utf-8').write('clean')" """
        )

        self.assertIsNotNone(damaged)
        self.assertEqual(damaged, repaired)
        self.assertNotEqual(damaged, append_mode)

    def test_operation_binding_fails_closed_for_compound_shell_context(self) -> None:
        self.assertIsNone(
            self.operation_binding(
                """python -c "open('report.txt','w').write('clean')"; unknown-tool"""
            )
        )

    def test_unrecognized_or_invalid_inline_source_does_not_invent_write_intent(self) -> None:
        script = self.analyze("python script.py")
        invalid = self.analyze("""python -c "open(" """)

        self.assertFalse(script.recognized_inline)
        self.assertFalse(script.write_intent)
        self.assertEqual("not_applicable", script.parse_status)
        self.assertTrue(invalid.recognized_inline)
        self.assertFalse(invalid.write_intent)
        self.assertEqual("syntax_error", invalid.parse_status)


if __name__ == "__main__":
    unittest.main()
