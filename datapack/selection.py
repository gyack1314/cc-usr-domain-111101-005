# 周批次选样求解层
#
# 在 datapack.domain 的安全集合谓词、组成本与去重覆盖原语之上，实现：
#   1. 输入校验与候选索引建模（未知字段/未知引用一律 InputError，绝不判成无解）
#   2. 组不可拆、场景去重的分支限界精确求解，以及带时限的有界求解
#   3. 无解时基于完整枚举证明的包含极小冲突核（MUS）提取
#   4. 独立结果校验（含独立穷举对照），供复核人员重复核验
#
# 状态语义（硬约束要求，不得混用）：
#   optimal   —— 已完整枚举证明的全局最优
#   feasible  —— 时限中断，但拿到可行解；附带可核对的上下界，不声称最优
#   unknown   —— 时限中断且未找到可行解；不等于无解
#   infeasible—— 完整枚举证明无解，并给出包含极小冲突核
import itertools
import time

from .domain import matches, group_costs, coverage
from .storage import fingerprint

# 状态常量
OPTIMAL = 'optimal'
FEASIBLE = 'feasible'
UNKNOWN = 'unknown'
INFEASIBLE = 'infeasible'

# auto 模式下，合格组不超过该数量即做完整精确枚举
EXACT_GROUP_LIMIT = 22
# 独立校验器做穷举对照的规模上限
VERIFY_BRUTEFORCE_LIMIT = 20

_ROW_KEYS = ('scene_id', 'group_id', 'bytes', 'value', 'city', 'tags', 'eligible')
_QUOTA_PREDICATE_KEYS = frozenset(('id', 'predicate', 'min'))
_QUOTA_CITY_KEYS = frozenset(('id', 'city', 'max'))
_CONSTRAINT_KEYS = {
    'budget': frozenset(('id', 'kind', 'bytes')),
    'required': frozenset(('id', 'kind', 'groups')),
    'exclusive': frozenset(('id', 'kind', 'groups')),
}


class InputError(ValueError):
    """输入资料或配置本身有误（区别于业务上的无解）。"""


class VerificationFailure(Exception):
    """selection.json 与原始输入独立复核不一致。"""


def _is_int(value, nonnegative=False, positive=False):
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    if positive and value <= 0:
        return False
    if nonnegative and value < 0:
        return False
    return True


def validate_predicate(expr, depth=0):
    """配额表达式结构校验：只允许 tag/all/any/not，绝不执行任意 Python 表达式。"""
    if depth > 16 or not isinstance(expr, dict) or len(expr) != 1:
        raise InputError('配额表达式结构无效')
    key, value = next(iter(expr.items()))
    if key == 'tag':
        if not isinstance(value, str) or not value:
            raise InputError('标签须为非空字符串')
        return
    if key == 'not':
        validate_predicate(value, depth + 1)
        return
    if key not in ('all', 'any') or not isinstance(value, list) or not value:
        raise InputError('集合运算仅允许非空 all/any')
    for child in value:
        validate_predicate(child, depth + 1)


def _validate_rows(rows):
    if not isinstance(rows, list) or not rows:
        raise InputError('候选记录须为非空对象数组')
    seen = {}
    clean = []
    for pos, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InputError('第%d条候选记录不是对象' % pos)
        unknown = set(row) - set(_ROW_KEYS)
        if unknown:
            raise InputError('候选记录含未知字段：%s' % sorted(unknown))
        for key in ('scene_id', 'group_id', 'city'):
            if not isinstance(row.get(key), str) or not row[key]:
                raise InputError('第%d条记录字段 %s 须为非空字符串' % (pos, key))
        if not _is_int(row.get('bytes'), positive=True):
            raise InputError('第%d条记录 bytes 须为正整数' % pos)
        if not _is_int(row.get('value'), nonnegative=True):
            raise InputError('第%d条记录 value 须为非负整数' % pos)
        if not isinstance(row.get('eligible'), bool):
            raise InputError('第%d条记录 eligible 须为布尔值' % pos)
        tags = row.get('tags')
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t for t in tags):
            raise InputError('第%d条记录 tags 须为非空字符串数组' % pos)
        if len(tags) != len(set(tags)):
            raise InputError('第%d条记录标签重复：%s' % (pos, row['scene_id']))
        sid = row['scene_id']
        if sid in seen:
            if fingerprint(seen[sid]) != fingerprint(row):
                raise InputError('scene_id 内容冲突：%s' % sid)
            # 完全重复的记录保留原行号用于追溯，建模时再去重
        else:
            seen[sid] = row
            clean.append(row)
    return clean


def _validate_quotas(quotas, known_cities):
    if not isinstance(quotas, list):
        raise InputError('配额文件须为对象数组')
    normalized = []
    for pos, quota in enumerate(quotas):
        if not isinstance(quota, dict) or 'id' not in quota:
            raise InputError('第%d条配额缺少 id' % pos)
        keys = frozenset(quota)
        if 'predicate' in quota:
            if keys != _QUOTA_PREDICATE_KEYS:
                raise InputError('标签配额 %s 字段非法：%s' % (quota.get('id'), sorted(keys)))
            validate_predicate(quota['predicate'])
            if not _is_int(quota['min'], nonnegative=True):
                raise InputError('配额 %s 的 min 须为非负整数' % quota['id'])
            normalized.append({'id': quota['id'], 'kind': 'predicate',
                               'predicate': quota['predicate'], 'min': quota['min']})
        elif 'city' in quota:
            if keys != _QUOTA_CITY_KEYS:
                raise InputError('城市配额 %s 字段非法：%s' % (quota.get('id'), sorted(keys)))
            if not isinstance(quota['city'], str) or not quota['city']:
                raise InputError('城市配额 %s 的 city 须为非空字符串' % quota.get('id'))
            if quota['city'] not in known_cities:
                raise InputError('城市配额 %s 引用了候选中不存在的城市：%s'
                                 % (quota['id'], quota['city']))
            if not _is_int(quota['max'], nonnegative=True):
                raise InputError('配额 %s 的 max 须为非负整数' % quota['id'])
            normalized.append({'id': quota['id'], 'kind': 'city',
                               'city': quota['city'], 'max': quota['max']})
        else:
            raise InputError('配额 %s 必须声明 predicate 或 city' % quota.get('id'))
    return normalized


def _validate_constraints(constraints):
    if not isinstance(constraints, list):
        raise InputError('约束文件须为对象数组')
    normalized = []
    for pos, constraint in enumerate(constraints):
        if not isinstance(constraint, dict) or 'id' not in constraint:
            raise InputError('第%d条约束缺少 id' % pos)
        kind = constraint.get('kind')
        if kind not in _CONSTRAINT_KEYS:
            raise InputError('约束 %s 的 kind 未知：%r' % (constraint.get('id'), kind))
        if frozenset(constraint) != _CONSTRAINT_KEYS[kind]:
            raise InputError('约束 %s 字段非法：%s' % (constraint['id'], sorted(constraint)))
        if not isinstance(constraint['id'], str) or not constraint['id']:
            raise InputError('约束 id 须为非空字符串')
        if kind == 'budget':
            if not _is_int(constraint['bytes'], positive=True):
                raise InputError('预算 %s 的 bytes 须为正整数' % constraint['id'])
            normalized.append({'id': constraint['id'], 'kind': 'budget',
                               'bytes': constraint['bytes']})
        else:
            groups = constraint['groups']
            if not isinstance(groups, list) or any(not isinstance(g, str) or not g for g in groups):
                raise InputError('约束 %s 的 groups 须为非空字符串数组' % constraint['id'])
            if len(groups) != len(set(groups)):
                raise InputError('约束 %s 的 groups 存在重复项' % constraint['id'])
            if kind == 'exclusive' and len(groups) < 2:
                raise InputError('互斥约束 %s 至少包含两个组' % constraint['id'])
            normalized.append({'id': constraint['id'], 'kind': kind, 'groups': list(groups)})
    return normalized


def build_model(rows, quotas, constraints):
    """把原始候选、配额、约束校验并编译为位掩码索引模型。"""
    unique_rows = _validate_rows(rows)
    cities = sorted({r['city'] for r in unique_rows})
    city_index = {city: i for i, city in enumerate(cities)}
    quotas = _validate_quotas(quotas, set(cities))
    constraints = _validate_constraints(constraints)

    ids = [q['id'] for q in quotas] + [c['id'] for c in constraints]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise InputError('配额与约束的 id 必须全部唯一且非空')

    # 场景索引：按首次出现顺序编号，重复 scene_id 只建一次
    scene_ids, scene_pos = [], {}
    for row in unique_rows:
        if row['scene_id'] not in scene_pos:
            scene_pos[row['scene_id']] = len(scene_ids)
            scene_ids.append(row['scene_id'])
    n = len(scene_ids)
    scene_value = [0] * n
    scene_city = [0] * n
    scene_tags = [None] * n
    source_rows = {sid: [] for sid in scene_ids}
    for pos, row in enumerate(rows):
        sid = row['scene_id']
        source_rows[sid].append(pos)
        i = scene_pos[sid]
        scene_value[i] = row['value']
        scene_city[i] = city_index[row['city']]
        scene_tags[i] = frozenset(row['tags'])

    # 组索引与实际字节（含重复记录的字节，预算按真实下载量计）
    raw_costs = group_costs(rows)
    all_groups, group_ineligible = {}, set()
    for row in rows:
        all_groups.setdefault(row['group_id'], []).append(scene_pos[row['scene_id']])
        if not row['eligible']:
            group_ineligible.add(row['group_id'])
    for gid in all_groups:
        all_groups[gid] = sorted(set(all_groups[gid]))

    eligible_gids = sorted(g for g in all_groups if g not in group_ineligible)
    gid_to_idx = {gid: i for i, gid in enumerate(eligible_gids)}
    m = len(eligible_gids)
    gmask, gbytes = [], []
    for gid in eligible_gids:
        mask = 0
        for i in all_groups[gid]:
            mask |= 1 << i
        gmask.append(mask)
        gbytes.append(raw_costs[gid])

    # 预算/必选/互斥
    budget = None
    required_idx = set()
    required_gids = []
    forbid_mask = [0] * m
    for constraint in constraints:
        kind = constraint['kind']
        if kind == 'budget':
            budget = constraint['bytes']
        elif kind == 'required':
            for gid in constraint['groups']:
                if gid not in all_groups:
                    raise InputError('必选约束 %s 引用了不存在的组：%s' % (constraint['id'], gid))
                required_gids.append(gid)
                if gid in gid_to_idx:
                    required_idx.add(gid_to_idx[gid])
        else:
            idxs = []
            for gid in constraint['groups']:
                if gid not in all_groups:
                    raise InputError('互斥约束 %s 引用了不存在的组：%s' % (constraint['id'], gid))
                if gid in gid_to_idx:
                    idxs.append(gid_to_idx[gid])
            for a, b in itertools.combinations(sorted(set(idxs)), 2):
                forbid_mask[a] |= 1 << b
                forbid_mask[b] |= 1 << a

    # 配额位掩码
    compiled_quotas = []
    for quota in quotas:
        if quota['kind'] == 'predicate':
            mask = 0
            for i, sid in enumerate(scene_ids):
                if matches(quota['predicate'], set(scene_tags[i])):
                    mask |= 1 << i
            compiled_quotas.append({'id': quota['id'], 'kind': 'predicate',
                                    'predicate': quota['predicate'],
                                    'min': quota['min'], 'mask': mask})
        else:
            compiled_quotas.append({'id': quota['id'], 'kind': 'city',
                                    'city': quota['city'], 'max': quota['max'],
                                    'city_index': city_index[quota['city']]})
    min_quotas = [q for q in compiled_quotas if q['kind'] == 'predicate']
    city_quotas = [q for q in compiled_quotas if q['kind'] == 'city']

    # 后缀场景集合与价值上界，供分支限界与可核对界使用
    suffix_mask = [0] * (m + 1)
    for d in range(m - 1, -1, -1):
        suffix_mask[d] = suffix_mask[d + 1] | gmask[d]

    def mask_value(mask):
        total = 0
        while mask:
            low = mask & -mask
            total += scene_value[low.bit_length() - 1]
            mask ^= low
        return total

    global_mask = suffix_mask[0]

    return {
        'rows': rows,
        'unique_rows': unique_rows,
        'scene_ids': scene_ids,
        'scene_pos': scene_pos,
        'scene_value': scene_value,
        'scene_city': scene_city,
        'scene_tags': scene_tags,
        'source_rows': source_rows,
        'cities': cities,
        'all_groups': all_groups,
        'eligible_gids': eligible_gids,
        'ineligible_gids': sorted(group_ineligible),
        'gid_to_idx': gid_to_idx,
        'gmask': gmask,
        'gbytes': gbytes,
        'budget': budget,
        'required_idx': required_idx,
        'required_gids': required_gids,
        'forbid_mask': forbid_mask,
        'quotas': compiled_quotas,
        'min_quotas': min_quotas,
        'city_quotas': city_quotas,
        'constraints': constraints,
        'suffix_mask': suffix_mask,
        'global_mask': global_mask,
        'global_value': mask_value(global_mask),
        'mask_value': mask_value,
        'fingerprints': {
            'candidates': fingerprint(rows),
            'quotas': fingerprint(quotas),
            'constraints': fingerprint(constraints),
        },
    }


# --------------------------------------------------------------------------- #
# 枚举/求解
# --------------------------------------------------------------------------- #

def _active_sets(model, active_ids):
    """把生效约束 id 集合翻译成枚举器可用的开关。"""
    by_id = {c['id']: c for c in model['constraints']}
    budget = None
    required, exclusive = set(), []
    required_blocked = []  # 必选约束引用了含无资格记录的组，任何方案都无法满足
    for cid in active_ids:
        c = by_id.get(cid)
        if c is None:
            continue
        if c['kind'] == 'budget':
            budget = c['bytes']
        elif c['kind'] == 'required':
            for gid in c['groups']:
                if gid in model['gid_to_idx']:
                    required.add(model['gid_to_idx'][gid])
                elif gid in model['ineligible_gids']:
                    required_blocked.append((cid, gid))
        else:
            for a, b in itertools.combinations(c['groups'], 2):
                ia, ib = model['gid_to_idx'].get(a), model['gid_to_idx'].get(b)
                if ia is not None and ib is not None:
                    exclusive.append((ia, ib))
    forbid = [0] * len(model['gmask'])
    for ia, ib in exclusive:
        forbid[ia] |= 1 << ib
        forbid[ib] |= 1 << ia
    min_quotas = [q for q in model['min_quotas'] if q['id'] in active_ids]
    city_quotas = [q for q in model['city_quotas'] if q['id'] in active_ids]
    return budget, required, forbid, min_quotas, city_quotas, required_blocked


def _leaf_feasible(model, scene_mask, city_masks, min_quotas, city_quotas):
    for q in min_quotas:
        if (scene_mask & q['mask']).bit_count() < q['min']:
            return False
    for q in city_quotas:
        if city_masks[q['city_index']].bit_count() > q['max']:
            return False
    return True


def enumerate_feasible(model, active_ids, first_only=True, deadline=None):
    """在生效约束集合上做深度枚举。

    first_only=True 时找到第一个可行组合即返回（用于 MUS 的可行性判定）；
    否则做带分支限界的完整枚举，按 (独立场景价值↓, 字节↓, 组编号列表) 决胜。
    每个合格组按编号顺序先“纳入”后“排除”递归，因此同分下第一个最优解即
    组编号排序列表的字典序最小者，与输入记录顺序无关。
    返回字典含 status/solution/stats/interrupted/frontier_upper_value。
    """
    budget, required, forbid, min_quotas, city_quotas, required_blocked = \
        _active_sets(model, active_ids)
    gmask, gbytes = model['gmask'], model['gbytes']
    suffix_mask = model['suffix_mask']
    mask_value = model['mask_value']
    m = len(gmask)
    limit = 10**30 if budget is None else budget
    # 必选组含无资格记录：任何选样都无法满足，整体可行域为空（仍为完整证明的结论）
    if required_blocked:
        return {'status': INFEASIBLE, 'solution': None,
                'stats': {'combinations': 1 << m, 'nodes_visited': 0,
                          'pruned_budget': 0, 'pruned_city': 0, 'pruned_exclusive': 0,
                          'pruned_required': 0, 'pruned_coverage': 0, 'pruned_objective': 0,
                          'required_blocked': required_blocked},
                'interrupted': False, 'frontier_upper_value': None}

    stats = {'combinations': 1 << m, 'nodes_visited': 0,
             'pruned_budget': 0, 'pruned_city': 0, 'pruned_exclusive': 0,
             'pruned_required': 0, 'pruned_coverage': 0, 'pruned_objective': 0}
    best = None  # (value, bytes, selected_tuple)
    # frontier_ubs 收集“仍有未探索排除分支”的节点上界：
    # 这些分支子树尚未进入，其节点上界是对该子树最优值的可靠上界。
    state = {'interrupted': False, 'frontier_ub': 0}

    def dfs(d, scene_mask, value, used, city_masks, blocked, selected, node_ub):
        nonlocal best
        stats['nodes_visited'] += 1
        if deadline is not None and stats['nodes_visited'] % 256 == 0 \
                and time.monotonic() >= deadline:
            # 中止时把当前节点上界也计入（其子树尚未探索完）；不把“没找完”当成无解
            state['interrupted'] = True
            state['frontier_ub'] = max(state['frontier_ub'], node_ub)
            return
        if d == m:
            if _leaf_feasible(model, scene_mask, city_masks, min_quotas, city_quotas):
                if first_only:
                    best = (value, used, selected)
                    return True
                if best is None or (-value, used, selected) < \
                        (-best[0], best[1], best[2]):
                    best = (value, used, selected)
            return False

        # 目标分支限界：价值上界严格低于现任解才剪枝；可能追平时必须保留，
        # 因为该分支可能以更少字节或更小的组编号列表在次级决胜中胜出。
        if not first_only and best is not None and node_ub < best[0]:
            stats['pruned_objective'] += 1
            return False
        # 覆盖下界：即便吃掉后缀全部场景仍不够，则剪枝
        for q in min_quotas:
            if ((scene_mask | suffix_mask[d]) & q['mask']).bit_count() < q['min']:
                stats['pruned_coverage'] += 1
                return False

        # 纳入分支优先（字典序决胜的关键）
        can_include = True
        if blocked & (1 << d):
            stats['pruned_exclusive'] += 1
            can_include = False
        elif used + gbytes[d] > limit:
            stats['pruned_budget'] += 1
            can_include = False
        include_ub = node_ub
        if can_include:
            added = gmask[d] & ~scene_mask
            new_mask = scene_mask | gmask[d]
            new_cities = city_masks
            if added:
                new_cities = list(city_masks)
                bits = added
                while bits:
                    low = bits & -bits
                    si = low.bit_length() - 1
                    new_cities[model['scene_city'][si]] |= 1 << si
                    bits ^= low
                city_ok = all(new_cities[q['city_index']].bit_count() <= q['max']
                              for q in city_quotas)
                new_cities = tuple(new_cities)
            else:
                city_ok = True
            if city_ok:
                include_ub = value + mask_value(added) + \
                    mask_value(suffix_mask[d + 1] & ~new_mask)
                if dfs(d + 1, new_mask, value + mask_value(added), used + gbytes[d],
                       new_cities, blocked | forbid[d], selected + (d,), include_ub):
                    return True
            else:
                stats['pruned_city'] += 1
        if state['interrupted']:
            return False

        # 排除分支（必选组不得排除）。该兄弟子树尚未探索，登记其可靠上界。
        if d in required:
            stats['pruned_required'] += 1
            return False
        exclude_ub = value + mask_value(suffix_mask[d + 1] & ~scene_mask)
        state['frontier_ub'] = max(state['frontier_ub'], exclude_ub)
        if dfs(d + 1, scene_mask, value, used, city_masks, blocked,
               selected, exclude_ub):
            return True
        return False

    dfs(0, 0, 0, 0, (0,) * len(model['cities']), 0, (),
        mask_value(suffix_mask[0]))

    if state['interrupted']:
        return {'status': FEASIBLE if best is not None else UNKNOWN,
                'solution': best[2] if best else None,
                'stats': stats, 'interrupted': True,
                'frontier_upper_value': max(state['frontier_ub'], best[0] if best else 0)}
    if best is None:
        return {'status': INFEASIBLE, 'solution': None, 'stats': stats,
                'interrupted': False, 'frontier_upper_value': None}
    return {'status': OPTIMAL if not first_only else FEASIBLE,
            'solution': best[2], 'stats': stats,
            'interrupted': False, 'frontier_upper_value': None}


def _greedy_incumbent(model):
    """有界模式的启动可行解：先必选组，再按单位字节新增价值贪心，最后补覆盖。"""
    m = len(model['gmask'])
    selected, scene_mask, used, blocked = set(), 0, 0, 0
    city_masks = [0] * len(model['cities'])

    def can_add(i):
        if i in selected or blocked & (1 << i):
            return False
        if used + model['gbytes'][i] > model['budget']:
            return False
        added = model['gmask'][i] & ~scene_mask
        bits = added
        tentative = list(city_masks)
        while bits:
            low = bits & -bits
            si = low.bit_length() - 1
            tentative[model['scene_city'][si]] |= 1 << si
            bits ^= low
        return all(tentative[q['city_index']].bit_count() <= q['max']
                   for q in model['city_quotas'])

    def add(i):
        nonlocal scene_mask, used, blocked
        added = model['gmask'][i] & ~scene_mask
        scene_mask |= model['gmask'][i]
        used += model['gbytes'][i]
        blocked |= model['forbid_mask'][i]
        selected.add(i)
        bits = added
        while bits:
            low = bits & -bits
            si = low.bit_length() - 1
            city_masks[model['scene_city'][si]] |= 1 << si
            bits ^= low

    for i in sorted(model['required_idx']):
        if not can_add(i):
            return None
        add(i)

    # 第一优先：先把最低覆盖配额补齐（预算紧张时不能先按价值把预算花光）。
    # 每轮选对未满足配额贡献场景最多、字节最省、编号最小的组。
    for _ in range(m):
        unsatisfied = [q for q in model['min_quotas']
                       if (scene_mask & q['mask']).bit_count() < q['min']]
        if not unsatisfied:
            break
        best_i, best_key = None, None
        for i in range(m):
            if not can_add(i):
                continue
            helped = sum((model['gmask'][i] & ~scene_mask & q['mask']).bit_count()
                         for q in unsatisfied)
            if helped == 0:
                continue
            key = (-helped, model['gbytes'][i], model['eligible_gids'][i])
            if best_key is None or key < best_key:
                best_key, best_i = key, i
        if best_i is None:
            return None
        add(best_i)

    # 第二优先：覆盖已达标后，在预算与城市余量内按单位字节新增价值贪心提升目标。
    def density(i):
        added = model['gmask'][i] & ~scene_mask
        return (-model['mask_value'](added) / model['gbytes'][i], model['eligible_gids'][i])

    for i in sorted((i for i in range(m) if i not in selected), key=density):
        if can_add(i):
            add(i)

    if not _leaf_feasible(model, scene_mask, tuple(city_masks),
                         model['min_quotas'], model['city_quotas']):
        return None
    return tuple(sorted(selected))


def solve(model, mode='auto', time_limit=None):
    """求解入口。mode: exact / bounded / auto。"""
    m = len(model['gmask'])
    if mode == 'auto':
        mode = 'exact' if m <= EXACT_GROUP_LIMIT else 'bounded'
    if mode not in ('exact', 'bounded'):
        raise InputError('未知求解模式：%r' % mode)
    if time_limit is not None and (not _is_int_like(time_limit) or time_limit <= 0):
        raise InputError('time_limit 须为正数秒')
    deadline = None
    if mode == 'bounded':
        deadline = time.monotonic() + (5.0 if time_limit is None else float(time_limit))

    all_ids = [q['id'] for q in model['quotas']] + [c['id'] for c in model['constraints']]
    greedy = _greedy_incumbent(model) if mode == 'bounded' else None
    result = enumerate_feasible(model, frozenset(all_ids), first_only=False, deadline=deadline)

    if result['interrupted']:
        if result['solution'] is None:
            result['solution'] = greedy
        if result['solution'] is not None:
            selected = result['solution']
            scene_mask = 0
            for i in selected:
                scene_mask |= model['gmask'][i]
            result['status'] = FEASIBLE
            result['objective'] = {
                'distinct_scene_value': model['mask_value'](scene_mask),
                'bytes': sum(model['gbytes'][i] for i in selected),
            }
            result['bounds'] = {
                'lower_value': result['objective']['distinct_scene_value'],
                'upper_value': min(result['frontier_upper_value'], model['global_value']),
                'global_relaxed_upper_value': model['global_value'],
            }
        else:
            result['status'] = UNKNOWN
            result['objective'] = None
            result['bounds'] = {
                'lower_value': None,
                'upper_value': model['global_value'],
                'global_relaxed_upper_value': model['global_value'],
            }
        result['mode'] = mode
        return result

    if result['status'] == INFEASIBLE:
        result['conflict'] = extract_conflict(model, frozenset(all_ids))
        result['mode'] = mode
        return result

    selected = result['solution']
    scene_mask = 0
    for i in selected:
        scene_mask |= model['gmask'][i]
    result['status'] = OPTIMAL
    result['objective'] = {
        'distinct_scene_value': model['mask_value'](scene_mask),
        'bytes': sum(model['gbytes'][i] for i in selected),
    }
    result['mode'] = mode
    return result


def _is_int_like(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# 无解证明：包含极小冲突核（MUS）
# --------------------------------------------------------------------------- #

def extract_conflict(model, all_ids):
    """删除法提取包含极小冲突核。

    前置条件：all_ids 整体已被完整枚举证明无解。
    核中每条约束都附带“删除它即可行”的见证，核外约束被证明与无解无关。
    """
    all_ids = list(all_ids)
    first = enumerate_feasible(model, frozenset(all_ids), first_only=True)
    if first['status'] != INFEASIBLE:
        raise AssertionError('冲突核提取要求整体无解，但找到了可行解')
    core = list(all_ids)
    removal_tests = []
    for cid in list(core):
        trial = frozenset(core) - {cid}
        probe = enumerate_feasible(model, trial, first_only=True)
        removal_tests.append({'removed': cid, 'feasible': probe['status'] == FEASIBLE,
                              'witness_groups': [model['eligible_gids'][i]
                                                 for i in (probe['solution'] or ())],
                              'nodes_visited': probe['stats']['nodes_visited']})
        if probe['status'] == INFEASIBLE:
            core.remove(cid)
    # 终核复验：对每个成员再做一次“终核减该成员”的可行性见证，保证可独立复核
    member_witnesses = []
    for cid in core:
        probe = enumerate_feasible(model, frozenset(core) - {cid}, first_only=True)
        member_witnesses.append({
            'constraint_id': cid,
            'feasible_without_it': probe['status'] == FEASIBLE,
            'witness_groups': [model['eligible_gids'][i] for i in (probe['solution'] or ())],
            'nodes_visited': probe['stats']['nodes_visited'],
        })
    return {
        'core': list(core),
        'proof': {
            'method': 'deletion-based inclusion-minimal unsat core',
            'full_set_nodes_visited': first['stats']['nodes_visited'],
            'full_set_combinations': first['stats']['combinations'],
            'removal_trace': removal_tests,
            'member_witnesses': member_witnesses,
            'repeatable': '对核中任一约束取得可行性见证，对整体做完整枚举无解，'
                          '即可重复核验该核包含极小',
        },
        'diagnostics': _conflict_diagnostics(model, frozenset(core)),
    }


def _min_cost_to_reach_quota(model, quota):
    """单配额诊断：满足该最低覆盖所需的最少字节（忽略其他约束，仅保留资格）。"""
    target = quota['min']
    mask = quota['mask']
    gmask, gbytes = model['gmask'], model['gbytes']
    m = len(gmask)
    best = [None]

    def dfs(d, scene_mask, cost):
        if best[0] is not None and cost >= best[0]:
            return
        if (scene_mask & mask).bit_count() >= target:
            best[0] = cost
            return
        if d == m:
            return
        dfs(d + 1, scene_mask | gmask[d], cost + gbytes[d])
        dfs(d + 1, scene_mask, cost)

    dfs(0, 0, 0)
    return best[0]


def _conflict_diagnostics(model, core):
    """逐条说明核内约束的确切要求，以及预算削减后确切受影响的配额。"""
    by_id = {c['id']: c for c in model['constraints']}
    q_by_id = {q['id']: q for q in model['quotas']}
    budget_cid = next((cid for cid in core if by_id.get(cid, {}).get('kind') == 'budget'), None)
    budget = by_id[budget_cid]['bytes'] if budget_cid else None
    items = []
    for cid in core:
        if cid in by_id:
            c = by_id[cid]
            item = {'id': cid, 'kind': c['kind'], 'requirement': None, 'impact': None}
            if c['kind'] == 'budget':
                item['requirement'] = '下载预算上限 %d 字节' % c['bytes']
                item['impact'] = '所有入选组实际字节之和不得超过该值'
            elif c['kind'] == 'required':
                costs = []
                total = 0
                blocked_gids = []
                for gid in c['groups']:
                    cost = sum(r['bytes'] for r in model['rows'] if r['group_id'] == gid)
                    total += cost
                    costs.append('%s=%d字节' % (gid, cost))
                    if gid in model['ineligible_gids']:
                        blocked_gids.append(gid)
                item['requirement'] = '必选组 %s' % c['groups']
                if blocked_gids:
                    item['impact'] = '必选组实际成本：%s；组 %s 含无资格记录，整组不可选，约束无法满足' % (
                        '，'.join(costs), blocked_gids)
                else:
                    item['impact'] = '必选组实际成本：%s' % '，'.join(costs)
                    if budget is not None:
                        item['impact'] += '；合计%d字节%s预算%d字节' % (
                            total, '超过' if total > budget else '未超过', budget)
            else:
                item['requirement'] = '互斥组 %s 至多选一' % c['groups']
                item['impact'] = '同一次选样不能同时出现其中两个组'
            items.append(item)
        else:
            q = q_by_id[cid]
            item = {'id': cid, 'kind': 'quota:' + q['kind'], 'requirement': None, 'impact': None}
            if q['kind'] == 'predicate':
                item['requirement'] = '独立场景至少 %d 个' % q['min']
                min_cost = _min_cost_to_reach_quota(model, q)
                item['impact'] = '满足该配额的最低组合成本 %s 字节' % (
                    '不存在（合格候选不足）' if min_cost is None else min_cost)
                if budget is not None:
                    item['impact'] += '；预算 %d 字节%s' % (
                        budget, '无法容纳' if min_cost is None or min_cost > budget else '可容纳')
            else:
                item['requirement'] = '城市 %s 独立场景至多 %d 个' % (q['city'], q['max'])
                item['impact'] = '该城市场景去重后计数不得超限'
            items.append(item)
    return items


# --------------------------------------------------------------------------- #
# 报告与解释
# --------------------------------------------------------------------------- #

def _selection_facts(model, selected_idx):
    """由入选组索引计算全部可核对事实。"""
    selected_idx = tuple(selected_idx)
    scene_mask, used = 0, 0
    for i in selected_idx:
        scene_mask |= model['gmask'][i]
        used += model['gbytes'][i]
    city_masks = {}
    bits = scene_mask
    while bits:
        low = bits & -bits
        si = low.bit_length() - 1
        ci = model['scene_city'][si]
        city_masks[ci] = city_masks.get(ci, 0) | (1 << si)
        bits ^= low
    return scene_mask, used, city_masks


def explain_selection(model, selected_idx):
    """逐组解释：成本、新增独立场景与价值、对各配额的覆盖贡献、城市。"""
    scene_mask, used, _ = _selection_facts(model, selected_idx)
    explanations = []
    covered = 0
    for i in selected_idx:
        gid = model['eligible_gids'][i]
        added = model['gmask'][i] & ~covered
        added_scenes = [model['scene_ids'][b] for b in _bits(added)]
        contributions = []
        for q in model['min_quotas']:
            contributions.append({'quota_id': q['id'],
                                  'new_scenes': (added & q['mask']).bit_count()})
        city_counts = {}
        bits = added
        while bits:
            low = bits & -bits
            si = low.bit_length() - 1
            city = model['cities'][model['scene_city'][si]]
            city_counts[city] = city_counts.get(city, 0) + 1
            bits ^= low
        explanations.append({
            'group_id': gid,
            'bytes': model['gbytes'][i],
            'new_distinct_scenes': added_scenes,
            'new_distinct_scene_value': model['mask_value'](added),
            'quota_contributions': contributions,
            'cities': city_counts,
        })
        covered |= model['gmask'][i]
    return explanations, used, scene_mask


def _bits(mask):
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def constraint_report(model, selected_idx):
    """逐约束覆盖与预算占用。"""
    scene_mask, used, city_masks = _selection_facts(model, selected_idx)
    report = []
    for q in model['quotas']:
        if q['kind'] == 'predicate':
            actual = (scene_mask & q['mask']).bit_count()
            report.append({'id': q['id'], 'kind': 'min_coverage',
                           'predicate': q['predicate'], 'required_min': q['min'],
                           'actual_distinct_scenes': actual,
                           'slack': actual - q['min'],
                           'satisfied': actual >= q['min']})
        else:
            actual = city_masks.get(q['city_index'], 0).bit_count()
            report.append({'id': q['id'], 'kind': 'city_max', 'city': q['city'],
                           'allowed_max': q['max'], 'actual_distinct_scenes': actual,
                           'slack': q['max'] - actual, 'satisfied': actual <= q['max']})
    for c in model['constraints']:
        if c['kind'] == 'budget':
            report.append({'id': c['id'], 'kind': 'budget', 'bytes_limit': c['bytes'],
                           'bytes_used': used, 'slack_bytes': c['bytes'] - used,
                           'satisfied': used <= c['bytes']})
        elif c['kind'] == 'required':
            chosen = {model['eligible_gids'][i] for i in selected_idx}
            missing = [g for g in c['groups'] if g not in chosen]
            report.append({'id': c['id'], 'kind': 'required', 'groups': c['groups'],
                           'missing': missing, 'satisfied': not missing})
        else:
            chosen = {model['eligible_gids'][i] for i in selected_idx}
            hits = [g for g in c['groups'] if g in chosen]
            report.append({'id': c['id'], 'kind': 'exclusive', 'groups': c['groups'],
                           'selected_from_set': hits,
                           'satisfied': len(hits) <= 1})
    return report


def build_report(model, result, input_paths=None):
    """组装可追到原记录的 selection 报告。"""
    selected_idx = result.get('solution') or ()
    selected_gids = [model['eligible_gids'][i] for i in selected_idx]
    explanations, used, scene_mask = explain_selection(model, selected_idx)
    selected_scenes = []
    for i in selected_idx:
        gid = model['eligible_gids'][i]
        for pos, row in enumerate(model['rows']):
            if row['group_id'] == gid:
                selected_scenes.append({
                    'scene_id': row['scene_id'], 'group_id': gid,
                    'source_row_index': pos, 'bytes': row['bytes'],
                    'value': row['value'], 'city': row['city'], 'tags': row['tags'],
                })
    report = {
        'schema_version': 1,
        'status': result['status'],
        'mode': result.get('mode'),
        'inputs': {
            'paths': input_paths or {},
            'fingerprints': model['fingerprints'],
            'candidate_groups_total': len(model['all_groups']),
            'eligible_groups': len(model['eligible_gids']),
            'ineligible_groups': model['ineligible_gids'],
        },
        'objective': result.get('objective'),
        'bounds': result.get('bounds'),
        'selected_groups': selected_gids,
        'tie_break_rule': '价值与字节均相同时，取组编号排序后列表字典序最小者',
        'selected_scenes': selected_scenes,
        'group_rationale': explanations,
        'constraint_report': constraint_report(model, selected_idx),
        'search': result['stats'],
    }
    if result['status'] == INFEASIBLE:
        report['conflict'] = result['conflict']
    return report


# --------------------------------------------------------------------------- #
# 独立校验
# --------------------------------------------------------------------------- #

def _reference_optimal(model):
    """独立穷举对照：itertools 全组合，显式三元目标比较，与主求解器代码路径分离。"""
    gids = model['eligible_gids']
    limit = 10**30 if model['budget'] is None else model['budget']
    best = None
    for size in range(len(gids) + 1):
        for combo in itertools.combinations(range(len(gids)), size):
            # 必选组必须全部出现（参考实现独立强制，与主求解器代码路径分离）
            if not set(model['required_idx']) <= set(combo):
                continue
            scene_mask, used, blocked = 0, 0, 0
            city_masks = {}
            ok = True
            for i in combo:
                if blocked & (1 << i):
                    ok = False
                    break
                if used + model['gbytes'][i] > limit:
                    ok = False
                    break
                added = model['gmask'][i] & ~scene_mask
                scene_mask |= model['gmask'][i]
                used += model['gbytes'][i]
                blocked |= model['forbid_mask'][i]
                for si in _bits(added):
                    ci = model['scene_city'][si]
                    city_masks[ci] = city_masks.get(ci, 0) | (1 << si)
            if not ok:
                continue
            if not _leaf_feasible(model, scene_mask,
                                  tuple(city_masks.get(i, 0) for i in range(len(model['cities']))),
                                  model['min_quotas'], model['city_quotas']):
                continue
            value = model['mask_value'](scene_mask)
            key = (-value, used, [gids[i] for i in combo])
            if best is None or key < best[0]:
                best = (key, combo, value, used)
    return best


def verify_selection(rows, quotas, constraints, report):
    """从原始输入独立复核 selection 报告，返回逐项检查结论。"""
    model = build_model(rows, quotas, constraints)
    checks, mismatches = [], []

    def check(name, ok, detail):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            mismatches.append('%s：%s' % (name, detail))

    check('输入指纹一致',
          report.get('inputs', {}).get('fingerprints') == model['fingerprints'],
          '报告=%r 现算=%r' % (report.get('inputs', {}).get('fingerprints'),
                              model['fingerprints']))

    selected_gids = report.get('selected_groups', [])
    unknown = [g for g in selected_gids if g not in model['all_groups']]
    check('入选组均存在', not unknown, '未知组=%s' % unknown)
    bad_eligibility = [g for g in selected_gids if g in model['ineligible_gids']]
    check('入选组全部合格（组内无无资格记录）', not bad_eligibility,
          '含无资格记录的组=%s' % bad_eligibility)
    check('入选组列表按编号排序且无重复',
          selected_gids == sorted(set(selected_gids)), selected_gids)

    selected_idx = tuple(model['gid_to_idx'][g] for g in selected_gids
                         if g in model['gid_to_idx'])
    scene_mask, used, city_masks = _selection_facts(model, selected_idx)
    value = model['mask_value'](scene_mask)

    # 用 domain 原语独立重算覆盖与组成本
    chosen_rows = [r for r in rows if r['group_id'] in set(selected_gids) and r['eligible']]
    check('字节按入选组实际字节和计算（非去重行数估算）',
          report.get('objective') is None or used == report['objective'].get('bytes'),
          '重算=%d 报告=%r' % (used, None if report.get('objective') is None
                               else report['objective'].get('bytes')))
    check('独立场景价值去重计数',
          report.get('objective') is None or value == report['objective'].get('distinct_scene_value'),
          '重算=%d 报告=%r' % (value, None if report.get('objective') is None
                               else report['objective'].get('distinct_scene_value')))

    status = report.get('status')
    if status == INFEASIBLE:
        # 无解时不存在应满足约束的选样；逐约束检查替换为冲突核的完整证明复核
        for entry in constraint_report(model, selected_idx):
            check('报告记录的约束状态 %s' % entry['id'],
                  entry['id'] in {q['id'] for q in model['quotas']} |
                  {c['id'] for c in model['constraints']}, entry['kind'])
        conflict = report.get('conflict') or {}
        core = conflict.get('core') or []
        all_ids = frozenset(q['id'] for q in model['quotas']) | \
            frozenset(c['id'] for c in model['constraints'])
        check('冲突核是约束全集的子集', frozenset(core) <= all_ids, core)
        check('冲突核非空', len(core) > 0, core)
        full = enumerate_feasible(model, all_ids, first_only=True)
        check('完整枚举确认整体无解', full['status'] == INFEASIBLE, full['stats'])
        minimal = True
        for cid in core:
            probe = enumerate_feasible(model, frozenset(core) - {cid}, first_only=True)
            if probe['status'] != FEASIBLE:
                minimal = False
                check('核成员 %s 删除后应可行' % cid, False, probe['status'])
        check('冲突核包含极小（删任一成员即可行）', minimal, core)
        return {'ok': not mismatches, 'status': status, 'checks': checks,
                'mismatches': mismatches}

    # 可行/最优/未知但携带选样时，逐约束必须全部满足
    for entry in constraint_report(model, selected_idx):
        check('约束满足 %s' % entry['id'], entry['satisfied'], entry)
    # 覆盖数字与 domain.coverage 原语交叉核对
    for q in model['min_quotas']:
        primitive = coverage(chosen_rows, q['predicate'])
        actual = (scene_mask & q['mask']).bit_count()
        check('覆盖原语交叉核对 %s' % q['id'], primitive == actual,
              'coverage()=%d 位掩码=%d' % (primitive, actual))

    if status == OPTIMAL:
        if len(model['eligible_gids']) <= VERIFY_BRUTEFORCE_LIMIT:
            ref = _reference_optimal(model)
            if ref is None:
                check('独立穷举存在最优解', False, '穷举未找到任何可行解')
            else:
                _, combo, ref_value, ref_bytes = ref
                ref_gids = [model['eligible_gids'][i] for i in combo]
                same = (ref_value == value and ref_bytes == used and ref_gids == selected_gids)
                check('独立穷举最优一致（价值→字节→组列表）', same,
                      '穷举=(价值%d,字节%d,%s) 报告=(价值%d,字节%d,%s)'
                      % (ref_value, ref_bytes, ref_gids, value, used, selected_gids))
        else:
            check('最优状态规模超出穷举阈值，仅做界检查', True,
                  '合格组=%d>%d' % (len(model['eligible_gids']), VERIFY_BRUTEFORCE_LIMIT))
    elif status == FEASIBLE:
        bounds = report.get('bounds') or {}
        ok = (bounds.get('lower_value') == value
              and bounds.get('upper_value', -1) >= value
              and bounds.get('upper_value', 10**99) <= model['global_value'] + 10**-9
              and bounds.get('global_relaxed_upper_value') == model['global_value'])
        check('有界解上下界可核对且未声称最优', ok, bounds)
    elif status == UNKNOWN:
        check('unknown 未携带伪可行解', not selected_gids, selected_gids)
        bounds = report.get('bounds') or {}
        check('unknown 上界不超过全局松弛上界',
              bounds.get('upper_value') == model['global_value'], bounds)
    else:
        check('状态字段合法', False, '未知状态=%r' % status)

    return {'ok': not mismatches, 'status': status, 'checks': checks, 'mismatches': mismatches}
