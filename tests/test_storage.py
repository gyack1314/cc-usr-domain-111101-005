import json
import tempfile
import unittest
from pathlib import Path
from datapack.storage import RevisionStore,Conflict,atomic_write,canonical,confined,read_json
from datapack.records import index_rows,load_rows,inspect_bundle
ROOT=Path(__file__).resolve().parents[1]

class StorageTests(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(canonical({'b':1,'a':2}),canonical({'a':2,'b':1}))
        with self.assertRaises(ValueError):canonical({'x':float('nan')})
    def test_revision_reopen_and_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'c.sqlite3';s=RevisionStore(path)
            self.assertEqual(s.put('x',0,{'v':1}),1)
            self.assertEqual(RevisionStore(path).put('x',0,{'v':1}),1)
            with self.assertRaises(Conflict):s.put('x',0,{'v':2})
            self.assertEqual(s.put('x',1,{'v':2}),2)
            self.assertEqual(s.get('x',1)['value'],{'v':1})
            self.assertEqual(s.latest('x')['revision'],2)
    def test_atomic_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.json';atomic_write(p,b'{"a":1}');self.assertEqual(read_json(p),{'a':1})
            atomic_write(p,b'{"a":1,"a":2}')
            with self.assertRaises(ValueError):read_json(p)
            self.assertEqual(list(Path(d).glob('.pending-*')),[])
    def test_path(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):confined(d,'../x')
            self.assertEqual(confined(d,'a').parent,Path(d).resolve())
    def test_duplicate_vs_conflict(self):
        r=index_rows([{'id':'a','v':1},{'id':'a','v':1},{'id':'a','v':2}],'id')
        self.assertEqual(r['duplicates'],['a']);self.assertEqual(r['conflicts'],['a'])
    def test_source_bundle(self):
        result=inspect_bundle(ROOT/'samples')
        self.assertGreater(len(result['files']),1)
        self.assertTrue(all(f['rows'] for f in result['files']))
    def test_csv_shape(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.csv';p.write_text('a,b\n1,2,3\n')
            with self.assertRaises(ValueError):load_rows(p)
if __name__=='__main__':unittest.main()
