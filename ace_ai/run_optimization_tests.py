"""Offline regression runner. Each module uses a fresh process and temporary DB."""
from __future__ import annotations
import argparse, os, pathlib, subprocess, sys, tempfile, uuid, json
HERE = pathlib.Path(__file__).resolve().parent
DEFAULT = ['test_triangle_formation_round2','test_triangle_primary_round1','test_triangle_basis','test_triangle_chart_facts','test_member_pattern','test_optimization_1008','test_audit_needs_extra','test_audit_score_extra','test_audit_local_only_extra','test_audit_triangle_extra','test_triangle_state','test_user_triangle_direction','test_kline_debug','test_discord_access','test_fix_1008','test_perf_regressions','test_review_regressions','test_routing_corpus','test_closed_quotes','test_support_score_fix','test_near_zone_limit','test_needs_1008']
def worker(module, temp):
    import platform
    platform.uname()
    os.environ.update(DISCORD_AI_MARKET_CACHE_DB=str(temp/'market.sqlite3'), DISCORD_AI_TRI_STATE_DB='0', DISCORD_AI_NEEDS_PARSE='0', MPLBACKEND='Agg', TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp))
    tempfile.tempdir=str(temp)
    # Inherit Windows sandbox ACL, while all databases remain under this test directory.
    def tempdir(suffix=None,prefix=None,dir=None):
        p=pathlib.Path(dir or temp)/(str(prefix or 'tmp')+uuid.uuid4().hex+str(suffix or ''))
        p.mkdir(mode=0o777); return str(p)
    tempfile.mkdtemp=tempdir
    import socket, threading
    pair=threading.local(); origpair=socket.socketpair; origconnect=socket.socket.connect
    def blocked(*a,**k): raise RuntimeError('Offline tests forbid external network/API')
    def socketpair(*a,**k):
        pair.active=True
        try: return origpair(*a,**k)
        finally: pair.active=False
    def connect(sock,address):
        if getattr(pair,'active',False) and isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'): return origconnect(sock,address)
        return blocked()
    socket.socketpair=socketpair; socket.socket.connect=connect
    socket.socket.connect_ex=blocked; socket.create_connection=blocked; socket.getaddrinfo=blocked
    os.chdir(HERE); sys.path.insert(0,str(HERE)); sys.dont_write_bytecode=True
    if module=='ROUTING':
        import routing_harness as h
        harness=h.Harness(); failed=[]
        try:
            for index,case in enumerate(h.load_corpus()):
                got,problems=h.check_case(harness,case,user=f"case{index}")
                if problems: failed.append({'question':case['question'],'problems':problems})
        finally: harness.close()
        print(json.dumps({'cases':len(h.load_corpus()),'failures':failed},ensure_ascii=False)); return int(bool(failed))
    import unittest
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(module))
    (temp/'result.json').write_text(json.dumps({'module':module,'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped)},ensure_ascii=False),encoding='utf-8')
    return int(not result.wasSuccessful())
def main():
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(); parser.add_argument('--all',action='store_true'); parser.add_argument('--routing',action='store_true'); parser.add_argument('--worker'); parser.add_argument('--temp'); parser.add_argument('--modules',nargs='*')
    args=parser.parse_args()
    if args.worker: return worker(args.worker,pathlib.Path(args.temp))
    modules=args.modules or (['ROUTING'] if args.routing else sorted(p.stem for p in HERE.glob('test_*.py')) if args.all else DEFAULT)
    modules=[m for m in modules if m=='ROUTING' or (HERE/(m+'.py')).exists()]
    summary=[]
    def inherited_tempdir(suffix=None,prefix=None,dir=None):
        parent=pathlib.Path(dir or os.environ.get('ACE_TEST_TEMP_ROOT') or tempfile.gettempdir())
        out=parent/(str(prefix or 'tmp')+uuid.uuid4().hex+str(suffix or ''))
        out.mkdir(mode=0o777); return str(out)
    tempfile.mkdtemp=inherited_tempdir
    with tempfile.TemporaryDirectory(prefix='ace_offline_tests_') as base:
        for m in modules:
            folder=pathlib.Path(base)/m; folder.mkdir(mode=0o777)
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
            try:
                proc=subprocess.run([sys.executable,str(pathlib.Path(__file__).resolve()),'--worker',m,'--temp',str(folder)],env=env,cwd=HERE,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=240)
                print(proc.stdout,end=''); print(proc.stderr,end='')
                item=json.loads((folder/'result.json').read_text(encoding='utf-8')) if (folder/'result.json').exists() else {'module':m}
                item['returncode']=proc.returncode
            except subprocess.TimeoutExpired: item={'module':m,'returncode':124,'error':'240-second timeout'}
            summary.append(item)
    print('TEST_SUMMARY',json.dumps(summary,ensure_ascii=False))
    return int(any(x['returncode'] for x in summary))
if __name__=='__main__': raise SystemExit(main())
