import argparse
import json
import sys
from pathlib import Path

from .records import inspect_bundle, load_rows
from .storage import RevisionStore, save_json, read_json
from .selection import (
    InputError, VerificationFailure, build_model, solve, build_report,
    verify_selection, OPTIMAL, FEASIBLE, UNKNOWN, INFEASIBLE,
)

# 退出码：0 最优/可行；2 输入错误（绝不与无解混用）；3 已证明无解；4 有界但状态未知；5 复核失败
EXIT_INPUT = 2
EXIT_INFEASIBLE = 3
EXIT_UNKNOWN = 4
EXIT_VERIFY = 5


def _load_inputs(candidates, quotas, constraints):
    rows = load_rows(candidates)
    quota_rows = load_rows(quotas)
    constraint_rows = load_rows(constraints)
    return rows, quota_rows, constraint_rows


def _cmd_select(args):
    rows, quotas, constraints = _load_inputs(args.candidates, args.quotas, args.constraints)
    model = build_model(rows, quotas, constraints)
    result = solve(model, mode=args.mode, time_limit=args.time_limit)
    report = build_report(model, result, input_paths={
        'candidates': str(args.candidates),
        'quotas': str(args.quotas),
        'constraints': str(args.constraints),
    })
    # 写出前先跑一次独立校验，拒绝把自相矛盾的结果落盘
    verification = verify_selection(rows, quotas, constraints, report)
    report['verification'] = verification
    if not verification['ok']:
        save_json(args.output, report)
        raise VerificationFailure('独立复核未通过：%s' % verification['mismatches'])
    save_json(args.output, report)
    summary = {
        'status': report['status'],
        'output': str(args.output),
        'selected_groups': report['selected_groups'],
        'objective': report['objective'],
        'bounds': report['bounds'],
        'verification_ok': verification['ok'],
    }
    if report['status'] == INFEASIBLE:
        summary['conflict_core'] = report['conflict']['core']
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if report['status'] == INFEASIBLE:
        return EXIT_INFEASIBLE
    if report['status'] == UNKNOWN:
        return EXIT_UNKNOWN
    return 0


def _cmd_verify(args):
    rows, quotas, constraints = _load_inputs(args.candidates, args.quotas, args.constraints)
    report = read_json(args.selection)
    result = verify_selection(rows, quotas, constraints, report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else EXIT_VERIFY


def main():
    parser = argparse.ArgumentParser(description='数据包内容检查与周批次选样')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('inspect')
    p.add_argument('bundle', type=Path)
    p = commands.add_parser('catalog')
    p.add_argument('database', type=Path)
    p.add_argument('stream')

    p = commands.add_parser('select', help='求解周批次选样并写出 selection.json')
    p.add_argument('candidates', type=Path)
    p.add_argument('quotas', type=Path)
    p.add_argument('constraints', type=Path)
    p.add_argument('-o', '--output', type=Path, required=True)
    p.add_argument('--mode', choices=('auto', 'exact', 'bounded'), default='auto')
    p.add_argument('--time-limit', type=float, default=None,
                   help='bounded 模式的秒级时限；auto 精确模式忽略')

    p = commands.add_parser('verify', help='从原始输入独立复核 selection.json')
    p.add_argument('candidates', type=Path)
    p.add_argument('quotas', type=Path)
    p.add_argument('constraints', type=Path)
    p.add_argument('selection', type=Path)

    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            print(json.dumps(inspect_bundle(args.bundle), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'catalog':
            print(json.dumps(RevisionStore(args.database).latest(args.stream),
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == 'select':
            return _cmd_select(args)
        return _cmd_verify(args)
    except InputError as exc:
        print('输入错误（配置/资料无效，非无解）：%s' % exc, file=sys.stderr)
        return EXIT_INPUT
    except VerificationFailure as exc:
        print('复核失败：%s' % exc, file=sys.stderr)
        return EXIT_VERIFY
    except (ValueError, KeyError, OSError) as exc:
        print('输入资料或目录无效，请检查路径及字段说明：%s' % exc, file=sys.stderr)
        return EXIT_INPUT


if __name__ == '__main__':
    raise SystemExit(main())
