import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class CliTests(unittest.TestCase):
    def test_real_inspection(self):
        result=subprocess.run([sys.executable,'-m','datapack.cli','inspect','samples'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['files'])
    def test_invalid_path_is_failure(self):
        result=subprocess.run([sys.executable,'-m','datapack.cli','inspect','missing-directory'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertEqual(result.stdout,'')
