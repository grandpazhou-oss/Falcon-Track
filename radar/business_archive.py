"""Lossless single-file snapshot of business evidence, excluding probe-only files."""
from contextlib import closing, contextmanager
import hashlib
import json
import lzma
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

from django.core.management.base import CommandError
from django.db import connection

from .management.commands.backup_data import (
    MAX_ARCHIVE_MEMBERS, MAX_JSON_BYTES, MAX_UNCOMPRESSED_BYTES, raw_source_file, safe_raw_path,
)


def write_compact(zip_path, payload, raw_paths, manifest):
    raw_paths=sorted(raw_paths,key=lambda name:(Path(name).suffix,name))
    with tempfile.TemporaryDirectory(dir=Path(zip_path).parent) as name:
        work=Path(name)
        db=work/'radar.sqlite3'
        with closing(sqlite3.connect(Path(connection.settings_dict['NAME']).resolve().as_uri()+'?mode=ro',uri=True)) as src:
            with closing(sqlite3.connect(db)) as dest:
                src.backup(dest)
                with dest:
                    dest.execute("UPDATE radar_aiconfig SET enabled=0, auto_new=0, search_enabled=0, key_ref='', search_key_ref='', key_source='disabled'")
                    dest.execute("UPDATE radar_source SET enabled=0")
                    dest.execute("DELETE FROM radar_runtimestate")
                    for table in ('django_session','django_admin_log','auth_user_groups','auth_user_user_permissions','auth_user'):
                        dest.execute(f'DELETE FROM {table}')
                dest.execute('VACUUM')
        fixture=work/'radar.json'
        fixture.write_bytes(payload)
        ordered=[('radar.json',fixture),('radar.sqlite3',db),*((name,raw_source_file(name)) for name in raw_paths)]
        if sum(p.stat().st_size for _,p in ordered)>MAX_UNCOMPRESSED_BYTES:
            raise CommandError('业务快照超过 1 GB。')
        assets=[]
        physical={}
        stream=work/'business.xz'
        with lzma.open(stream,'wb',preset=9|lzma.PRESET_EXTREME) as compressed:
            for key,path in ordered:
                data=path.read_bytes()
                sha=hashlib.sha256(data).hexdigest()
                item={'name':key,'size':len(data),'sha256':sha}
                if sha in physical:
                    item['copy_of']=physical[sha]
                else:
                    physical[sha]=key
                    compressed.write(data)
                assets.append(item)
        manifest.update(format=3,compact_members=assets)
        with zipfile.ZipFile(zip_path,'w',compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False),compress_type=zipfile.ZIP_DEFLATED)
            archive.write(stream,'business.xz')


@contextmanager
def opened_backup(path, data_dir):
    with zipfile.ZipFile(path) as archive:
        if 'business.xz' not in archive.namelist():
            yield archive
            return
        if archive.namelist()!=['manifest.json','business.xz'] or archive.getinfo('manifest.json').file_size>2_000_000:
            raise CommandError('紧凑业务归档清单无效。')
        manifest=json.loads(archive.read('manifest.json'))
        assets=manifest.get('compact_members',[])
        if manifest.get('format')!=3 or not 2<=len(assets)<=MAX_ARCHIVE_MEMBERS:
            raise CommandError('紧凑业务归档格式无效。')
        size=sum(a['size'] for a in assets if type(a['size']) is int and a['size']>=0)
        if any(type(a['size']) is not int or a['size']<0 for a in assets) or size>MAX_UNCOMPRESSED_BYTES:
            raise CommandError('紧凑业务归档展开超过限制。')
        with tempfile.TemporaryDirectory(prefix='.compact-restore-', dir=data_dir) as dest:
            root=Path(dest)
            seen={}
            casefold=set()
            with archive.open('business.xz') as member, lzma.open(member,'rb') as compressed:
                for a in assets:
                    key=a['name']
                    if key.casefold() in casefold:
                        raise CommandError('紧凑业务归档路径冲突。')
                    casefold.add(key.casefold())
                    if key not in {'radar.json','radar.sqlite3'}:
                        safe_raw_path(key)
                    elif key=='radar.json' and a['size']>MAX_JSON_BYTES:
                        raise CommandError('业务数据库记录超过限制。')
                    path=root/key
                    path.parent.mkdir(parents=True,exist_ok=True)
                    digest=hashlib.sha256()
                    remaining=a['size']
                    prior=a.get('copy_of')
                    if prior:
                        if prior not in seen or seen[prior]!=a['sha256']:
                            raise CommandError('紧凑业务归档别名无效。')
                        shutil.copyfile(root/prior,path)
                        with path.open('rb') as reader:
                            digest=hashlib.file_digest(reader,'sha256')
                    else:
                        with path.open('xb') as target:
                            while remaining:
                                data=compressed.read(min(remaining,1024*1024))
                                if not data:
                                    raise CommandError('紧凑业务归档已截断。')
                                target.write(data)
                                digest.update(data)
                                remaining-=len(data)
                    if path.stat().st_size!=a['size'] or digest.hexdigest()!=a['sha256']:
                        raise CommandError('紧凑证据 SHA-256 校验失败。')
                    seen[key]=a['sha256']
                if compressed.read(1):
                    raise CommandError('紧凑业务归档包含未声明内容。')
            if not {'radar.json','radar.sqlite3'}.issubset(seen):
                raise CommandError('紧凑业务归档缺少数据库。')
            compat=root/'restore.zip'
            with zipfile.ZipFile(compat,'w') as normalized:
                for key in seen:
                    if key!='radar.sqlite3':
                        normalized.write(root/key,key)
                manifest.update(format=2,raw_sha256={k:v for k,v in seen.items() if k.startswith('raw/')})
                normalized.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
            with zipfile.ZipFile(compat) as normalized:
                yield normalized
