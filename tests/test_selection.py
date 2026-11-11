import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from datapack.records import load_rows
from datapack.selection import (
    InputError, VerificationFailure, build_model, solve, build_report,
    verify_selection, enumerate_feasible, extract_conflict, validate_predicate,
    _reference_optimal, OPTIMAL, FEASIBLE, UNKNOWN, INFEASIBLE, EXACT_GROUP_LIMIT,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts' / 'cli_runner.py'


def sample_inputs():
    return (load_rows(ROOT / 'samples/candidates.json'),
            load_rows(ROOT / 'samples/quotas.json'),
            load_rows(ROOT / 'samples/constraints.json'))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class ExactOptimalityTests(unittest.TestCase):
    def test_sample_matches_independent_bruteforce(self):
        rows, quotas, constraints = sample_inputs()
        model = build_model(rows, quotas, constraints)
        result = solve(model, mode='exact')
        self.assertEqual(result['status'], OPTIMAL)
        report = build_report(model, result)
        verification = verify_selection(rows, quotas, constraints, report)
        self.assertTrue(verification['ok'], verification['mismatches'])
        # 独立穷举（itertools 全组合，与主 DFS 代码路径完全分离）给出同一最优
        _, combo, ref_value, ref_bytes = _reference_optimal(model)
        sel_value = result['objective']['distinct_scene_value']
        sel_bytes = result['objective']['bytes']
        self.assertEqual((ref_value, ref_bytes), (sel_value, sel_bytes))
        self.assertEqual([model['eligible_gids'][i] for i in combo],
                         report['selected_groups'])

    def test_bruteforce_agreement_across_random_cases(self):
        # 多组随机小实例：主求解器与独立穷举在三元目标上必须完全一致
        import random
        rng = random.Random(20261111)
        for case in range(40):
            n_groups = rng.randint(1, 9)
            rows = []
            for g in range(n_groups):
                for _ in range(rng.randint(1, 3)):
                    rows.append({
                        'scene_id': 's%02d_%02d' % (g, len(rows)),
                        'group_id': 'g%02d' % g,
                        'bytes': rng.randint(50, 300),
                        'value': rng.randint(0, 9),
                        'city': rng.choice(['东城', '西城', '北城']),
                        'tags': rng.sample(['rain', 'night', 'rare', 'normal'],
                                           rng.randint(1, 2)),
                        'eligible': rng.random() > 0.12,
                    })
            quotas = [{'id': 'q_rain', 'predicate': {'tag': 'rain'},
                       'min': rng.randint(0, 2)}]
            constraints = [{'id': 'budget', 'kind': 'budget',
                            'bytes': rng.randint(200, 1500)}]
            if n_groups >= 2:
                constraints.append({'id': 'ex', 'kind': 'exclusive',
                                    'groups': ['g00', 'g01']})
            model = build_model(rows, quotas, constraints)
            result = solve(model, mode='exact')
            ref = _reference_optimal(model)
            if ref is None:
                self.assertEqual(result['status'], INFEASIBLE)
                continue
            self.assertEqual(result['status'], OPTIMAL)
            _, combo, ref_value, ref_bytes = ref
            self.assertEqual(result['objective']['distinct_scene_value'], ref_value)
            self.assertEqual(result['objective']['bytes'], ref_bytes)
            self.assertEqual([model['eligible_gids'][i] for i in result['solution']],
                             [model['eligible_gids'][i] for i in combo])


class DeterminismTests(unittest.TestCase):
    def test_input_permutation_does_not_change_choice(self):
        import random
        rows, quotas, constraints = sample_inputs()
        baseline = None
        for seed in range(12):
            shuffled = rows[:]
            random.Random(seed).shuffle(shuffled)
            model = build_model(shuffled, quotas, constraints)
            result = solve(model, mode='exact')
            gids = tuple(model['eligible_gids'][i] for i in result['solution'])
            baseline = gids if baseline is None else baseline
            self.assertEqual(gids, baseline)

    def test_explicit_tie_breaks_to_smallest_group_list(self):
        # b/c 完全对称且互斥，预算只够两组，决胜应稳定落在编号更小的 b
        rows = [
            {'scene_id': 's1', 'group_id': 'a', 'bytes': 100, 'value': 5,
             'city': '东城', 'tags': ['rain'], 'eligible': True},
            {'scene_id': 's2', 'group_id': 'b', 'bytes': 100, 'value': 5,
             'city': '西城', 'tags': ['rain'], 'eligible': True},
            {'scene_id': 's3', 'group_id': 'c', 'bytes': 100, 'value': 5,
             'city': '北城', 'tags': ['rain'], 'eligible': True},
        ]
        quotas = [{'id': 'q', 'predicate': {'tag': 'rain'}, 'min': 2}]
        constraints = [{'id': 'budget', 'kind': 'budget', 'bytes': 200},
                       {'id': 'ex', 'kind': 'exclusive', 'groups': ['b', 'c']}]
        model = build_model(rows, quotas, constraints)
        result = solve(model, mode='exact')
        self.assertEqual([model['eligible_gids'][i] for i in result['solution']],
                         ['a', 'b'])


class GroupIntegrityAndDedupTests(unittest.TestCase):
    def test_ineligible_record_disqualifies_whole_group(self):
        rows, quotas, constraints = sample_inputs()
        model = build_model(rows, quotas, constraints)
        self.assertEqual(model['ineligible_gids'], ['g08'])
        result = solve(model, mode='exact')
        chosen = {model['eligible_gids'][i] for i in result['solution']}
        self.assertNotIn('g08', chosen)

    def test_required_ineligible_group_is_infeasible_not_input_error(self):
        rows, quotas, constraints = sample_inputs()
        constraints = constraints + [{'id': 'req8', 'kind': 'required',
                                      'groups': ['g08']}]
        model = build_model(rows, quotas, constraints)
        result = solve(model, mode='exact')
        self.assertEqual(result['status'], INFEASIBLE)
        self.assertIn('req8', result['conflict']['core'])

    def test_duplicate_scene_counted_once_and_bytes_actual_sum(self):
        rows = [
            {'scene_id': 's1', 'group_id': 'g1', 'bytes': 100, 'value': 7,
             'city': '东城', 'tags': ['rain'], 'eligible': True},
            {'scene_id': 's1', 'group_id': 'g1', 'bytes': 100, 'value': 7,
             'city': '东城', 'tags': ['rain'], 'eligible': True},
            {'scene_id': 's2', 'group_id': 'g2', 'bytes': 50, 'value': 3,
             'city': '东城', 'tags': ['night'], 'eligible': True},
        ]
        quotas = [{'id': 'q', 'predicate': {'tag': 'rain'}, 'min': 1}]
        constraints = [{'id': 'budget', 'kind': 'budget', 'bytes': 1000}]
        model = build_model(rows, quotas, constraints)
        result = solve(model, mode='exact')
        self.assertEqual(result['objective']['distinct_scene_value'], 10)
        # 预算按实际字节和：g1 两条记录共 200 字节，不按去重行数估算
        self.assertEqual(result['objective']['bytes'], 250)

    def test_conflicting_duplicate_scene_is_input_error(self):
        rows = [
            {'scene_id': 's1', 'group_id': 'g1', 'bytes': 100, 'value': 7,
             'city': '东城', 'tags': ['rain'], 'eligible': True},
            {'scene_id': 's1', 'group_id': 'g1', 'bytes': 100, 'value': 8,
             'city': '东城', 'tags': ['rain'], 'eligible': True},
        ]
        with self.assertRaises(InputError):
            build_model(rows, [], [])


class InputErrorTests(unittest.TestCase):
    def _build(self, rows=None, quotas=None, constraints=None):
        r, q, c = sample_inputs()
        return build_model(rows if rows is not None else r,
                           quotas if quotas is not None else q,
                           constraints if constraints is not None else c)

    def test_unknown_row_field_is_input_error(self):
        r, q, c = sample_inputs()
        r[0]['hack'] = True
        with self.assertRaises(InputError):
            self._build(rows=r)

    def test_unknown_group_reference_is_input_error(self):
        _, q, _ = sample_inputs()
        with self.assertRaises(InputError):
            self._build(quotas=q, constraints=[
                {'id': 'r', 'kind': 'required', 'groups': ['ghost']}])

    def test_unknown_city_reference_is_input_error(self):
        with self.assertRaises(InputError):
            self._build(quotas=[{'id': 'c', 'city': '火星', 'max': 1}])

    def test_unknown_constraint_kind_is_input_error(self):
        with self.assertRaises(InputError):
            self._build(constraints=[{'id': 'x', 'kind': 'eval', 'code': '1'}])

    def test_duplicate_ids_are_input_error(self):
        with self.assertRaises(InputError):
            self._build(quotas=[
                {'id': 'dup', 'predicate': {'tag': 'rain'}, 'min': 1},
                {'id': 'dup', 'predicate': {'tag': 'night'}, 'min': 1}])

    def test_unsafe_predicate_rejected(self):
        for bad in ({'exec': 'x'}, {'all': []}, {'tag': 5},
                    {'not': {'import': 'os'}}, {'any': [{'tag': 'rain'}], 'x': 1}):
            with self.assertRaises(InputError):
                validate_predicate(bad)

    def test_cli_unknown_field_returns_exit_2_not_infeasible(self):
        with tempfile.TemporaryDirectory() as d:
            r, q, c = sample_inputs()
            r[0]['hack'] = True
            rp, qp, cp, op = (Path(d) / n for n in
                              ('rows.json', 'q.json', 'c.json', 'out.json'))
            write_json(rp, r)
            write_json(qp, q)
            write_json(cp, c)
            proc = subprocess.run(
                [sys.executable, '-I', str(RUNNER), 'select',
                 str(rp), str(qp), str(cp), '-o', str(op)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('输入错误', proc.stderr)
            self.assertFalse(op.exists())


class InfeasibilityProofTests(unittest.TestCase):
    def test_tight_budget_names_exact_affected_quota(self):
        rows, quotas, _ = sample_inputs()
        constraints = [
            {'id': 'budget', 'kind': 'budget', 'bytes': 1},
            {'id': 'required', 'kind': 'required', 'groups': ['g00']},
        ]
        model = build_model(rows, quotas, constraints)
        result = solve(model, mode='exact')
        self.assertEqual(result['status'], INFEASIBLE)
        core = result['conflict']['core']
        # 核必含预算，且逐约束给出确切成本/占用，而不是只打印“预算不足”
        self.assertIn('budget', core)
        diag = {d['id']: d for d in result['conflict']['diagnostics']}
        self.assertTrue(all(diag[cid]['impact'] for cid in core))
        if 'required' in diag:
            self.assertIn('超过', diag['required']['impact'])
        # 证明可独立复核：整体无解，删核中任一约束即可行
        verification = verify_selection(
            rows, quotas, constraints, build_report(model, result))
        self.assertTrue(verification['ok'], verification['mismatches'])

    def test_budget_cut_names_exact_affected_coverage_quota(self):
        # 预算放得下必选组但凑不齐任一最低覆盖配额时，冲突核须点名确切配额，
        # 并给出该配额的最低可行成本与预算的对比，而不是只打印“预算不足”。
        rows, quotas, _ = sample_inputs()
        constraints = [
            {'id': 'budget', 'kind': 'budget', 'bytes': 300},
            {'id': 'required', 'kind': 'required', 'groups': ['g00']},
            {'id': 'exclusive', 'kind': 'exclusive', 'groups': ['g01', 'g02']},
        ]
        model = build_model(rows, quotas, constraints)
        result = solve(model, mode='exact')
        self.assertEqual(result['status'], INFEASIBLE)
        diag = {d['id']: d for d in result['conflict']['diagnostics']}
        coverage_in_core = [cid for cid in result['conflict']['core']
                            if diag[cid]['kind'].startswith('quota:')]
        self.assertTrue(coverage_in_core)
        for cid in coverage_in_core:
            self.assertIn('无法容纳', diag[cid]['impact'])
            self.assertIn('最低组合成本', diag[cid]['impact'])
        verification = verify_selection(
            rows, quotas, constraints, build_report(model, result))
        self.assertTrue(verification['ok'], verification['mismatches'])

    def test_mus_is_inclusion_minimal(self):
        rows, quotas, _ = sample_inputs()
        constraints = [
            {'id': 'budget', 'kind': 'budget', 'bytes': 1},
            {'id': 'required', 'kind': 'required', 'groups': ['g00']},
            {'id': 'exclusive', 'kind': 'exclusive', 'groups': ['g01', 'g02']},
        ]
        model = build_model(rows, quotas, constraints)
        all_ids = frozenset(q['id'] for q in model['quotas']) | \
            frozenset(c['id'] for c in model['constraints'])
        self.assertEqual(enumerate_feasible(model, all_ids, True)['status'],
                         INFEASIBLE)
        conflict = extract_conflict(model, all_ids)
        core = conflict['core']
        self.assertTrue(core)
        for cid in core:
            probe = enumerate_feasible(model, frozenset(core) - {cid}, True)
            self.assertEqual(probe['status'], FEASIBLE)


class BoundedModeTests(unittest.TestCase):
    def _large_model(self):
        rows, tags = [], [['rain', 'rare'], ['night', 'normal'],
                          ['rain', 'normal'], ['night', 'rare']]
        cities = ['东城', '西城', '北城']
        for g in range(28):
            for k in range(3):
                rows.append({'scene_id': 's%03d' % (g * 3 + k), 'group_id': 'g%02d' % g,
                             'bytes': 100 + g * 7, 'value': (g + k) % 7,
                             'city': cities[(g + k) % 3], 'tags': tags[(g + k) % 4],
                             'eligible': True})
        quotas = [
            {'id': 'rr', 'predicate': {'all': [{'tag': 'rain'}, {'tag': 'rare'}]},
             'min': 8},
            {'id': 'night', 'predicate': {'tag': 'night'}, 'min': 10},
            {'id': 'cap', 'city': '东城', 'max': 12}]
        constraints = [{'id': 'budget', 'kind': 'budget', 'bytes': 4000},
                       {'id': 'ex', 'kind': 'exclusive', 'groups': ['g01', 'g02']}]
        return build_model(rows, quotas, constraints)

    def test_small_input_is_always_exact_in_auto(self):
        rows, quotas, constraints = sample_inputs()
        model = build_model(rows, quotas, constraints)
        self.assertLessEqual(len(model['eligible_gids']), EXACT_GROUP_LIMIT)
        result = solve(model)  # auto
        self.assertEqual(result['status'], OPTIMAL)

    def test_interrupted_never_called_infeasible_and_keeps_bounds(self):
        model = self._large_model()
        result = solve(model, mode='bounded', time_limit=0.3)
        self.assertIn(result['status'], (FEASIBLE, UNKNOWN))
        self.assertNotIn(result['status'], (INFEASIBLE, OPTIMAL))
        if result['status'] == FEASIBLE:
            b = result['bounds']
            self.assertLessEqual(b['lower_value'], b['upper_value'])
            self.assertLessEqual(b['upper_value'],
                                 b['global_relaxed_upper_value'])
            report = build_report(model, result)
            verification = verify_selection(
                model['rows'],
                [q for q in _raw_quotas(model)],
                [c for c in model['constraints']], report)
            self.assertTrue(verification['ok'], verification['mismatches'])

    def test_bounded_with_generous_limit_matches_exact(self):
        rows, quotas, constraints = sample_inputs()
        model = build_model(rows, quotas, constraints)
        exact = solve(model, mode='exact')
        bounded = solve(model, mode='bounded', time_limit=30)
        self.assertEqual(bounded['status'], OPTIMAL)
        self.assertEqual(bounded['solution'], exact['solution'])


def _raw_quotas(model):
    # 从编译模型还原校验所需的原始配额结构
    for q in model['quotas']:
        if q['kind'] == 'predicate':
            yield {'id': q['id'], 'predicate': q['predicate'], 'min': q['min']}
        else:
            yield {'id': q['id'], 'city': q['city'], 'max': q['max']}


class ReportAndTraceabilityTests(unittest.TestCase):
    def test_selection_json_traces_to_source_rows(self):
        rows, quotas, constraints = sample_inputs()
        model = build_model(rows, quotas, constraints)
        report = build_report(model, solve(model, mode='exact'))
        for scene in report['selected_scenes']:
            original = rows[scene['source_row_index']]
            self.assertEqual(original['scene_id'], scene['scene_id'])
            self.assertEqual(original['group_id'], scene['group_id'])
            self.assertEqual(original['bytes'], scene['bytes'])
        # 每条约束都有逐项占用/覆盖
        ids = {entry['id'] for entry in report['constraint_report']}
        self.assertEqual(ids,
                         {q['id'] for q in quotas} | {c['id'] for c in constraints})
        self.assertTrue(all(e['satisfied'] for e in report['constraint_report']))

    def test_tampered_report_is_rejected(self):
        rows, quotas, constraints = sample_inputs()
        model = build_model(rows, quotas, constraints)
        report = build_report(model, solve(model, mode='exact'))
        report['selected_groups'] = ['g02']  # 违反互斥/必选的篡改
        report['selected_scenes'] = []
        verification = verify_selection(rows, quotas, constraints, report)
        self.assertFalse(verification['ok'])
        self.assertTrue(verification['mismatches'])

    def test_report_is_canonical_and_rewrite_stable(self):
        from datapack.storage import save_json, read_json, canonical
        rows, quotas, constraints = sample_inputs()
        model = build_model(rows, quotas, constraints)
        report = build_report(model, solve(model, mode='exact'))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'selection.json'
            save_json(p, report)
            again = Path(d) / 'selection2.json'
            save_json(again, read_json(p))
            self.assertEqual(p.read_bytes(), again.read_bytes())
            self.assertEqual(canonical(report), p.read_bytes().strip())


if __name__ == '__main__':
    unittest.main()
