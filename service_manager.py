#!/usr/bin/env python3
"""Deploy and administer the current user's persistent FashionStudio selection service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import runpy
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

LABEL = 'com.fashionstudio.selectionworkbench'
DEFAULT_HOME = Path.home() / 'Library/Application Support/FashionStudioSelection'
CODE_NAMES = {'public', 'chrome-helper', 'desktop', 'node_modules'}


def private_dir(p):
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(0o700)
    return p


def settings(home):
    try:
        return json.loads((home/'runtime.json').read_text())
    except (OSError, ValueError):
        return {}


def write_json(p, obj):
    temp = p.with_name(p.name + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    os.replace(temp, p)


def online_backup(src, dst):
    private_dir(dst.parent)
    temp = dst.with_suffix(dst.suffix+'.tmp')
    if temp.exists(): temp.unlink()
    with sqlite3.connect(src) as c, sqlite3.connect(temp) as d:
        c.backup(d, pages=256, sleep=0.05)
        if d.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('迁移数据库完整性校验失败')
    temp.chmod(0o600)
    os.replace(temp, dst)


def copy_file(src, dst):
    if src.resolve() == dst.resolve(): return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if src.stat().st_size != dst.stat().st_size:
        raise RuntimeError('文件复制校验失败: '+str(src))


def migrate(source, home):
    """One-time copy. Source stays intact; an existing runtime DB is never replaced."""
    target = home/'data/selection.sqlite3'
    if target.exists():
        return {'migrated': False, 'reason': '运行库已存在，保留现有员工数据'}
    src_db = source/'data/selection.sqlite3'
    if not src_db.exists(): raise RuntimeError('找不到源数据库')
    project = source.parent.parent
    private_dir(home/'data')
    private_dir(home/'backups')
    original_backup = home/'backups'/('before-migration-'+time.strftime('%Y%m%d-%H%M%S')+'.sqlite3')
    online_backup(src_db, original_backup)
    # Build in a staging directory so a failed migration cannot become the live DB.
    staging = Path(tempfile.mkdtemp(prefix='.migration-', dir=home))
    staged_db = staging/'selection.sqlite3'
    shutil.copy2(original_backup, staged_db)
    copied, missing, references = {}, set(), 0
    def destination(p):
        try: return home/'data'/p.relative_to(source/'data')
        except ValueError: pass
        try:
            relative = p.relative_to(project/'outputs/选款工作台_2026-10-10')
            return home/'exports'/relative
        except ValueError: pass
        try: return home/'project'/p.relative_to(project)
        except ValueError: pass
        return home/'data/imported'/(hashlib.sha256(str(p).encode()).hexdigest()[:16]+'-'+p.name)
    def rewrite(value):
        nonlocal references
        if isinstance(value, dict): return {k:rewrite(v) for k,v in value.items()}
        if isinstance(value, list): return [rewrite(v) for v in value]
        if not isinstance(value, str) or not value.startswith('/') or len(value)>2048 or '\n' in value or '\0' in value: return value
        p = Path(value)
        # URLs and explanatory strings are untouched; only local path values are rebased.
        if not (p.is_file() or p.is_dir() or str(p).startswith(str(project)+'/')): return value
        d = destination(p)
        references += 1
        if p.is_file():
            if value not in copied:
                copy_file(p, d)
                copied[value] = str(d)
        elif not p.exists():
            missing.add(value)
        return str(d)
    try:
        # Copy application uploads and secret pairing state. Never copy WAL/SHM files.
        for p in (source/'data').iterdir():
            if p.name.startswith('selection.sqlite3') or p.name in ('server.lock', 'backups'): continue
            d = home/'data'/p.name
            if p.is_dir(): shutil.copytree(p, d, dirs_exist_ok=True)
            elif p.is_file(): copy_file(p,d)
        with sqlite3.connect(staged_db) as c:
            columns = {'profiles':['data'], 'jobs':['rules'], 'items':['data','review'], 'queue':['url'], 'captures':['data'], 'events':['detail'], 'designs':['data'], 'exports':['data']}
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, fields in columns.items():
                if table not in tables: continue
                for field in fields:
                    for rowid, raw in c.execute(f'SELECT rowid,{field} FROM {table}').fetchall():
                        if raw is None: continue
                        try: obj=json.loads(raw)
                        except (ValueError,TypeError): continue
                        updated=rewrite(obj)
                        if updated != obj:
                            c.execute(f'UPDATE {table} SET {field}=? WHERE rowid=?',(json.dumps(updated,ensure_ascii=False),rowid))
            # Collection does not restart because a deployment or login occurred.
            c.execute("UPDATE jobs SET status='paused' WHERE status='running'")
            c.execute("UPDATE queue SET state='pending',error='部署迁移后保留断点，需人工继续' WHERE state='inflight'")
            c.commit()
            if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise RuntimeError('迁移后校验失败')
        # Future legacy imports and image manifests must also use the stable paths.
        for name in ('all-candidates.json','prepared.json','detail-records.json','verified-list-pages.json','scan-summary.json'):
            p = project/'work/icon-selection-20261009'/name
            if p.is_file():
                d = destination(p)
                d.parent.mkdir(parents=True, exist_ok=True)
                write_json(d, rewrite(json.loads(p.read_text())))
        processed = set()
        while True:
            pending = [(s,d) for s,d in copied.items() if s.endswith('.json') and s not in processed]
            if not pending: break
            for s,d in pending:
                processed.add(s)
                try: obj=json.loads(Path(s).read_text())
                except (OSError, ValueError): continue
                write_json(Path(d), rewrite(obj))
        staged_db.chmod(0o600)
        os.replace(staged_db, target)
        pair=home/'data/collector-key.txt'
        if pair.exists():pair.chmod(0o600)
        manifest={'migrated':True,'time':time.strftime('%Y-%m-%d %H:%M:%S'),'source_database':str(src_db),'target_database':str(target),'backup':str(original_backup),'local_path_references':references,'copied_reference_files':len(copied),'missing_source_files':sorted(missing),'path_map':copied}
        write_json(home/'migration-manifest.json',manifest)
        return {k:v for k,v in manifest.items() if k!='path_map'}
    finally:
        shutil.rmtree(staging,ignore_errors=True)


def deploy(source, home, migrate_data=False, host='0.0.0.0', port=8770):
    source=source.resolve(); home=home.resolve()
    if source == home/'app':raise RuntimeError('部署源必须是编辑工作区，不能用运行目录覆盖自身')
    private_dir(home); private_dir(home/'data'); private_dir(home/'project'); private_dir(home/'exports')
    target=home/'app'; private_dir(target)
    # Do not replace active code. Caller stops only this named LaunchAgent first.
    if service_loaded() or port_live(port):raise RuntimeError('部署前请先停止当前选款后台；不会覆盖运行中的服务')
    for p in source.iterdir():
        if p.is_file() and (p.suffix in ('.py','.mjs','.json','.txt') or p.name=='README.md'):
            copy_file(p,target/p.name)
        elif p.name in CODE_NAMES:
            dst=target/p.name
            if p.is_symlink():
                if dst.is_symlink(): dst.unlink()
                elif dst.exists(): shutil.rmtree(dst)
                dst.symlink_to(p.resolve(),target_is_directory=True)
            elif p.is_dir():
                if dst.exists():shutil.rmtree(dst)
                shutil.copytree(p,dst)
    project=source.parent.parent
    styles=project/'outputs/款式风格识别参考库_v1'
    if styles.is_dir():shutil.copytree(styles,home/'project/outputs/款式风格识别参考库_v1',dirs_exist_ok=True)
    legacy=project/'work/icon-selection-20261009'
    for name in ('all-candidates.json','prepared.json','detail-records.json','verified-list-pages.json','scan-summary.json'):
        if (legacy/name).exists() and not (home/'project/work/icon-selection-20261009'/name).exists():copy_file(legacy/name,home/'project/work/icon-selection-20261009'/name)
    result=migrate(source,home) if migrate_data else {'migrated':False}
    old=settings(home)
    runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies'
    config={**old,'host':host,'port':port,'python':str(Path(sys.executable).resolve()),'node':str(runtime/'node/bin/node'),'data_dir':str(home/'data'),'project_dir':str(home/'project'),'output_dir':str(home/'exports'),'log_dir':str(Path.home()/'Library/Logs/FashionStudioSelection'),'backup_dir':str(home/'backups')}
    if not Path(config['node']).exists():raise RuntimeError('缺少表格导出 Node 运行时')
    write_json(home/'runtime.json',config)
    return {'deployed':True,'home':str(home),'migration':result}


def agent_path():return Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
def domain():return 'gui/'+str(os.getuid())
def target():return domain()+'/'+LABEL

def service_loaded():
    return subprocess.run(['/bin/launchctl','print',target()],capture_output=True).returncode==0

def port_live(port):
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=0.4):return True
    except OSError:return False

def launch(args, check=True):
    result=subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'launchctl 操作失败')
    return result


def install(home, start=False):
    cfg=settings(home)
    if not (home/'app/server.py').is_file() or not cfg:raise RuntimeError('请先部署运行目录')
    log=private_dir(Path(cfg['log_dir']))
    # Early interpreter errors are captured separately; normal logs rotate in Python.
    bootstrap_log=log/'bootstrap.log'
    if bootstrap_log.exists() and bootstrap_log.stat().st_size>1024*1024:
        os.replace(bootstrap_log,log/'bootstrap.previous.log')
    plist={'Label':LABEL,'ProgramArguments':[cfg['python'],'-u',str(home/'app/service_manager.py'),'--home',str(home),'run'],'WorkingDirectory':str(home/'app'),'EnvironmentVariables':{'SELECTION_HOME':str(home),'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1'},'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'ProcessType':'Background','Umask':0o077,'StandardOutPath':str(bootstrap_log),'StandardErrorPath':str(bootstrap_log)}
    p=agent_path();p.parent.mkdir(parents=True,exist_ok=True)
    temp=p.with_suffix('.tmp');temp.write_bytes(plistlib.dumps(plist));temp.chmod(0o600);os.replace(temp,p)
    launch(['enable',target()])
    if start:
        if not service_loaded():launch(['bootstrap',domain(),str(p)])
        else:launch(['kickstart',target()])
    return {'installed':True,'label':LABEL,'autostart':'当前 Mac 用户登录后自动运行','started':start,'plist':str(p)}


def status(home):
    cfg=settings(home);port=int(cfg.get('port',8770));health=None
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=3) as r:health=json.load(r)
    except (OSError,ValueError):pass
    return {'label':LABEL,'loaded':service_loaded(),'home':str(home),'port':port,'health':health}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home',type=Path,default=DEFAULT_HOME)
    sub=p.add_subparsers(dest='command',required=True)
    d=sub.add_parser('deploy');d.add_argument('--source',type=Path,default=Path(__file__).resolve().parent);d.add_argument('--migrate-data',action='store_true');d.add_argument('--host',default='0.0.0.0');d.add_argument('--port',type=int,default=8770)
    i=sub.add_parser('install');i.add_argument('--start',action='store_true')
    for name in ('start','stop','restart','disable','status','backup','run'):sub.add_parser(name)
    a=p.parse_args();home=a.home.expanduser().resolve()
    if a.command=='run':
        os.environ['SELECTION_HOME']=str(home)
        sys.path.insert(0,str(home/'app'))
        from runtime_config import configure_logging
        configure_logging()
        runpy.run_path(str(home/'app/server.py'),run_name='__main__')
        return
    if a.command=='deploy':result=deploy(a.source,home,a.migrate_data,a.host,a.port)
    elif a.command=='install':result=install(home,a.start)
    elif a.command=='start':
        if not service_loaded():launch(['bootstrap',domain(),str(agent_path())])
        else:launch(['kickstart',target()])
        result={'started':True}
    elif a.command=='stop':
        if service_loaded():launch(['bootout',target()])
        result={'stopped':True,'autostart_preserved':True}
    elif a.command=='disable':
        if service_loaded():launch(['bootout',target()])
        launch(['disable',target()]);result={'stopped':True,'autostart':False}
    elif a.command=='restart':
        if service_loaded():launch(['kickstart','-k',target()])
        else:launch(['bootstrap',domain(),str(agent_path())])
        result={'restarted':True}
    elif a.command=='backup':
        sys.path.insert(0,str(home/'app'))
        from runtime_config import backup_database
        result={'backup':str(backup_database(home/'data/selection.sqlite3',home/'backups',force=True))}
    else:result=status(home)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:main()
    except Exception as e:
        print(json.dumps({'error':str(e)},ensure_ascii=False),file=sys.stderr);sys.exit(1)
