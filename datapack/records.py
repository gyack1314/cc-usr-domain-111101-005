import csv
import io
import json
from pathlib import Path
from .storage import fingerprint,confined,read_json

def load_rows(path):
    path=Path(path)
    if path.suffix=='.jsonl':
        result=[]
        with path.open(encoding='utf-8') as stream:
            for line_no,line in enumerate(stream,1):
                if not line.strip():continue
                try:row=json.loads(line)
                except ValueError:raise ValueError('JSONL第'+str(line_no)+'行解析失败') from None
                if not isinstance(row,dict):raise ValueError('JSONL记录必须为对象')
                result.append(row)
        return result
    if path.suffix=='.csv':
        with path.open(encoding='utf-8',newline='') as stream:
            reader=csv.DictReader(stream)
            if reader.fieldnames is None or len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('CSV字段重复或缺失')
            rows=list(reader)
            if any(None in r or any(v is None for v in r.values()) for r in rows):raise ValueError('CSV列数不一致')
            return rows
    value=read_json(path)
    if not isinstance(value,list) or any(not isinstance(r,dict) for r in value):raise ValueError('记录文件应是对象数组')
    return value

def index_rows(rows,key):
    index={};duplicates=[];conflicts=[]
    for row in rows:
        if key not in row or row[key] is None:raise ValueError('记录缺主键')
        id=row[key]
        if id in index:
            (duplicates if fingerprint(index[id])==fingerprint(row) else conflicts).append(id)
        else:index[id]=row
    return {'rows':index,'duplicates':duplicates,'conflicts':conflicts}

def inspect_bundle(root):
    root=Path(root);config=read_json(root/'bundle.json');files=[]
    for item in config['files']:
        path=confined(root,item['path']);rows=load_rows(path)
        if item.get('key'):index_rows(rows,item['key'])
        files.append({'path':item['path'],'rows':len(rows),'sha256':fingerprint(rows)})
    return {'name':config['name'],'files':files,'bundle_digest':fingerprint(files)}
