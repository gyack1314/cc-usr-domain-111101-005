import argparse
import json
import sys
from pathlib import Path
from .records import inspect_bundle
from .storage import RevisionStore

def main():
    parser=argparse.ArgumentParser(description='数据包内容检查')
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('inspect');p.add_argument('bundle',type=Path)
    p=commands.add_parser('catalog');p.add_argument('database',type=Path);p.add_argument('stream')
    args=parser.parse_args()
    try:
        value=inspect_bundle(args.bundle) if args.command=='inspect' else RevisionStore(args.database).latest(args.stream)
        print(json.dumps(value,ensure_ascii=False,indent=2));return 0
    except (ValueError,KeyError,OSError):
        print('输入资料或目录无效，请检查路径及字段说明',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
