"""Boot-task supervision using Fabric's OS process containment, not a serving engine."""
import json,os,sys,time,uuid
from pathlib import Path
import psutil
from zeyu_fabric.runner import spawn


def atomic(path,value):
    path=Path(path);temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,path)


def recover_dead_lease(path):
    """Never use expiry: acquire OS lock, then prove owner PID cannot be alive."""
    path=Path(path)
    if not path.exists():return {'recovered':False,'reason':'NO_LEASE'}
    with path.open('r+b') as f:
        try:
            f.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:return {'recovered':False,'reason':'LOCK_HELD'}
        # The OS releases this lock on file close, including all early returns.
        try:
            f.seek(0);lines=f.read().decode('utf-8').splitlines();owner=json.loads(lines[1])
        except (ValueError,IndexError,UnicodeError):return {'recovered':False,'reason':'INVALID_MARKER'}
        if owner.get('state')!='HELD':return {'recovered':False,'reason':'NOT_HELD'}
        pid=owner.get('pid')
        if type(pid)is not int or pid<=0:return {'recovered':False,'reason':'INVALID_PID'}
        acquired=owner.get('acquired_at_unix_ns')
        before_boot=False
        if psutil.pid_exists(pid):
            try:before_boot=isinstance(acquired,int) and acquired<psutil.boot_time()*1e9
            except (OSError,psutil.Error):pass
        if psutil.pid_exists(pid) and not before_boot:
            return {'recovered':False,'reason':'OWNER_MAY_BE_ALIVE'}
        owner.update(state='RELEASED',recovery='OS_LOCK_FREE_AND_OWNER_DEAD_OR_PREVIOUS_BOOT',recovered_at_unix_ns=time.time_ns())
        f.seek(0);f.truncate();f.write(('0\n'+json.dumps(owner)+'\n').encode());f.flush();os.fsync(f.fileno())
        return {'recovered':True,'previous_pid':pid,'reason':owner['recovery']}


def supervise(config):
    name=config['name'];root=Path(config['state_dir']);root.mkdir(parents=True,exist_ok=True)
    logs=Path(config['log_dir']);logs.mkdir(parents=True,exist_ok=True)
    stop=root/(name+'.stop');blocked=root/(name+'.blocked');state=root/(name+'.json');restarts=[];generation=0
    if blocked.exists():return 0  # Persist crash-loop quarantine across Task Scheduler/boot restarts.
    env=os.environ.copy();env.update(config.get('env',{}))
    if config.get('path_prefix'):env['PATH']=os.pathsep.join(config['path_prefix'])+os.pathsep+env.get('PATH','')
    env['PYTHONUNBUFFERED']='1';env['PYTHONIOENCODING']='utf-8'
    while not stop.exists():
        generation+=1
        recovery=recover_dead_lease(config['gpu_lease_path'])
        stamp=str(time.time_ns());process=None
        record={'name':name,'supervisor_pid':os.getpid(),'generation':generation,'started_ns':time.time_ns(),'state':'STARTING','lease_recovery':recovery}
        with (logs/(name+'-'+stamp+'.stdout.log')).open('ab',buffering=0)as out,(logs/(name+'-'+stamp+'.stderr.log')).open('ab',buffering=0)as err:
            try:
                process=spawn(config['command'],config['cwd'],env,out,err)
                record.update(state='RUNNING',pid=process.pid,create_time=psutil.Process(process.pid).create_time(),stdout=out.name,stderr=err.name);atomic(state,record)
                while process.poll()is None and not stop.exists():time.sleep(.25)
                if stop.exists() and process.poll()is None:process.terminate()
                code=process.wait(timeout=15)
                # close drains all descendants before making any lease recoverable.
                process.close();process=None
                record.update(state='STOPPED' if stop.exists() else 'EXITED',exit_code=code,ended_ns=time.time_ns());atomic(state,record)
            except BaseException as exc:
                err.write((repr(exc)+'\n').encode());record.update(state='SUPERVISOR_ERROR',error=repr(exc),ended_ns=time.time_ns());atomic(state,record)
                if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
            finally:
                if process is not None:process.close()
        recover_dead_lease(config['gpu_lease_path'])
        if stop.exists():return 0
        now=time.monotonic();restarts=[x for x in restarts if now-x<600];restarts.append(now)
        if len(restarts)>=10:
            record.update(state='CRASH_LOOP_BLOCKED');atomic(state,record);atomic(blocked,record);return 0
        deadline=now+5
        while time.monotonic()<deadline and not stop.exists():time.sleep(.25)
    return 0

if __name__=='__main__':
    raise SystemExit(supervise(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))))
