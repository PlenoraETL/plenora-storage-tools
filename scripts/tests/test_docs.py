from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_docs import anchors, check_document


class DocumentationTests(unittest.TestCase):
    def test_duplicate_headings_and_fenced_comments(self):
        self.assertEqual(anchors('# API\n## API\n```python\n# hidden\n```\n'), {'api', 'api-1'})

    def test_missing_anchor_invalid_example_and_missing_command_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / 'README.md'
            for text in ['# API\n[missing](#unknown)', '```python\nx =\n```',
                         '`python scripts/missing.py`', '[missing](unknown.md)',
                         '[guide][ref]\n[ref]: missing.md']:
                with self.subTest(text=text):
                    document.write_text(text, encoding='utf-8')
                    with self.assertRaises((ValueError, SyntaxError)):
                        check_document(document, root)
            document.write_text('# API\n[here](#api)\n```python\nawait engine.close()\n```', encoding='utf-8')
            check_document(document, root)
