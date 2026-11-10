def matches(expr,tags,depth=0):
    if depth>16 or not isinstance(expr,dict) or len(expr)!=1:raise ValueError('配额表达式结构无效')
    key,value=next(iter(expr.items()))
    if key=='tag':
        if not isinstance(value,str):raise ValueError('标签须为字符串')
        return value in tags
    if key=='not':return not matches(value,tags,depth+1)
    if key not in {'all','any'} or not isinstance(value,list) or not value:raise ValueError('集合运算无效')
    values=[matches(x,tags,depth+1) for x in value]
    return all(values) if key=='all' else any(values)
def group_costs(rows):
    groups={}
    for row in rows:
        if not isinstance(row['bytes'],int) or isinstance(row['bytes'],bool) or row['bytes']<=0:raise ValueError('体积须为正整数字节')
        groups[row['group_id']]=groups.get(row['group_id'],0)+row['bytes']
    return groups
def coverage(rows,predicate):return len({r['scene_id'] for r in rows if matches(predicate,set(r['tags']))})
