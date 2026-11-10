import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')

def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def atomic_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.pending-')
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

def save_json(path,value):
    atomic_write(path,canonical(value)+b'\n')

def read_json(path):
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ValueError('JSON键重复：'+key)
            out[key]=value
        return out
    def nonfinite(value):raise ValueError('JSON数值不能为'+value)
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream,object_pairs_hook=pairs,parse_constant=nonfinite)

def confined(root,name):
    root=Path(root).resolve();raw=Path(name)
    if raw.is_absolute():raise ValueError('只允许相对路径')
    result=(root/raw).resolve()
    if not result.is_relative_to(root):raise ValueError('路径超出资料目录')
    return result

class Conflict(ValueError):pass

class RevisionStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS revisions (stream TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL,body BLOB NOT NULL,PRIMARY KEY(stream,revision))')
    @contextlib.contextmanager
    def connection(self):
        conn=sqlite3.connect(self.path,timeout=10)
        try:
            with conn:yield conn
        finally:conn.close()
    def latest(self,stream):
        with self.connection() as conn:
            row=conn.execute('SELECT revision,digest,body FROM revisions WHERE stream=? ORDER BY revision DESC LIMIT 1',(stream,)).fetchone()
        return None if row is None else {'revision':row[0],'digest':row[1],'value':json.loads(row[2])}
    def put(self,stream,expected,value):
        if not isinstance(expected,int) or expected<0 or not stream:raise ValueError('版本或流名称无效')
        data=canonical(value);sha=hashlib.sha256(data).hexdigest()
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT revision,digest FROM revisions WHERE stream=? ORDER BY revision DESC LIMIT 1',(stream,)).fetchone()
            current=row[0] if row else 0
            if row and row[1]==sha:return current
            if current!=expected:raise Conflict('预期版本不匹配')
            conn.execute('INSERT INTO revisions VALUES(?,?,?,?)',(stream,current+1,sha,data))
        return current+1
    def get(self,stream,revision):
        with self.connection() as conn:
            row=conn.execute('SELECT digest,body FROM revisions WHERE stream=? AND revision=?',(stream,revision)).fetchone()
        if row is None:raise KeyError('版本不存在')
        return {'digest':row[0],'value':json.loads(row[1])}
