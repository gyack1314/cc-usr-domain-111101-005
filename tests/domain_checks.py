from datapack.domain import matches,group_costs,coverage
def check(case):
    case.assertTrue(matches({'all':[{'tag':'rain'},{'not':{'tag':'day'}}]},{'rain','night'}))
    with case.assertRaises(ValueError):matches({'exec':'x'},set())
    case.assertEqual(group_costs([{'group_id':'a','bytes':3},{'group_id':'a','bytes':5}]),{'a':8})
    case.assertEqual(coverage([{'scene_id':'a','tags':['x']},{'scene_id':'a','tags':['x']}],{'tag':'x'}),1)
