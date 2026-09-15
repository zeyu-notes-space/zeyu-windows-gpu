import json,os,sys,time,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime/windows'))
sys.path.insert(0,str(ROOT/'compute-fabric'))
from supervisor import recover_dead_lease
from zeyu_fabric.gpu_lease import GpuLease

def test_live_lease_is_not_recovered(tmp_path):
    p=tmp_path/'gpu.lock';lease=GpuLease(p,{'component':'TEST'})
    lease.acquire()
    try: assert recover_dead_lease(p)['recovered'] is False
    finally:lease.release()

def test_dead_child_lease_recovers_without_expiry(tmp_path):
    p=tmp_path/'gpu.lock'
    code='from zeyu_fabric.gpu_lease import GpuLease; import sys,os; GpuLease(sys.argv[1],{"component":"TEST"}).acquire(); os._exit(2)'
    e=os.environ.copy();e['PYTHONPATH']=str(ROOT/'compute-fabric')
    r=subprocess.run([sys.executable,'-c',code,str(p)],env=e)
    assert r.returncode==2
    assert recover_dead_lease(p)['recovered'] is True
    l=GpuLease(p,{'component':'NEW'}).acquire();l.release()


def test_persistent_crash_quarantine_survives_new_supervisor(tmp_path):
    import importlib.util
    from pathlib import Path
    source=ROOT/'runtime/windows/supervisor.py'
    spec=importlib.util.spec_from_file_location('quarantined_supervisor',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    state=tmp_path/'state';state.mkdir();(state/'bento.blocked').write_text('{}')
    result=module.supervise({'name':'bento','state_dir':str(state),'log_dir':str(tmp_path/'logs')})
    assert result==0
    assert (state/'bento.blocked').exists()
