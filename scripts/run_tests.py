#!/usr/bin/env python3
# 隔离模式测试启动器：在 -I 下显式登记仓库根，再运行 unittest 发现。
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
