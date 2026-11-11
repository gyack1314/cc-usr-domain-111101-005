#!/usr/bin/env python3
# 隔离模式启动器：仅负责把仓库根加入 sys.path 后转交 datapack.cli。
# 数据文件一律通过命令行参数传入，启动器与样本目录分离，避免同目录植入模块风险。
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datapack.cli import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
