import os, socket, subprocess, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
original=subprocess.Popen
attempts=[]
def guarded(args,*a,**kw):
    command=list(args) if not isinstance(args,str) else [args]
    program=Path(str(command[0])).name
    safe=(program=='bash' and command[1:]==['-lc','sleep 2']) or (program=='git' and not any(str(x) in ['push','fetch','pull','ls-remote','clone'] for x in command[1:]))
    if not safe:
        attempts.append(program);raise RuntimeError('offline suite attempted unmocked operational subprocess')
    return original(args,*a,**kw)
subprocess.Popen=guarded
def offline(*a,**kw):raise OSError('offline suite network boundary')
socket.create_connection=offline
socket.getaddrinfo=offline
suite=unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent),pattern='test_*.py')
result=unittest.TextTestRunner(verbosity=2).run(suite)
if attempts:raise SystemExit('unmocked operational subprocess attempted; names withheld')
raise SystemExit(0 if result.wasSuccessful() else 1)
