"""Seven explicit-use tools. Startup/listing never touches Windows or GPU."""
import atexit,json,os,sys,threading
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

POLICY=('Use Windows GPU ONLY when explicitly selected by the user for this task. '
        'Installed is not selected. Never set plugin_selected=true based on GPU suitability. '
        'Once selected normal calls need no extra GPU confirmation. Never use batch commands '
        'to bypass separate reboot/security/service/driver/destructive-operation approval.')
mcp=FastMCP('Windows GPU',instructions=POLICY,log_level='WARNING')


_controller=None
_controller_lock=threading.Lock()

def call(name,arguments,plugin_selected):
    if plugin_selected is not True:
        return {'status':'BLOCKED','error':{'code':'WINDOWS_GPU_PLUGIN_NOT_SELECTED','message':'Select Windows GPU for this task before accessing Windows.'}}
    try:
        global _controller
        from zeyu_plugin_client.client import Controller
        with _controller_lock:
            if _controller is None:
                path=Path(os.environ.get('WINDOWS_GPU_CONFIG','~/.config/windows-gpu/client.json')).expanduser()
                _controller=Controller(path,selected=True,require_selected=True)
                atexit.register(_controller.close)
        return _controller.invoke(name,**arguments)
    except (OSError,ValueError,KeyError) as exc:
        return {'status':'FAILED','error':{'code':'WINDOWS_GPU_CONFIGURATION_ERROR','message':type(exc).__name__+'; check the local Windows GPU configuration.'}}

READ=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
WRITE=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=False)
GUARD=' Requires explicit Windows GPU plugin selection; otherwise return without contacting Windows.'

@mcp.tool(description='Check Windows connectivity, SSH, RTX GPU/VRAM and both compute routes.'+GUARD,annotations=READ)
def gpu_status(plugin_selected:bool=False)->dict:
    return call('gpu_status',{},plugin_selected)

@mcp.tool(description='Run one real WAV through the requested persistent GPU model and return a verified Mac file. Use for interactive audio, not benchmark loops.'+GUARD,annotations=WRITE)
def infer_audio(input_path:str,model:str='unet',timeout:float=120,plugin_selected:bool=False)->dict:
    return call('infer_audio',{'file':input_path,'model':model,'timeout':timeout},plugin_selected)

@mcp.tool(description='Submit a batch, training, benchmark or long job using the existing durable worker. Job spec needs project, exact git_commit, environment, command argv, arguments, timeout, artifact_paths and optional resources. No system/security/destructive operations without separate approval.'+GUARD,annotations=WRITE)
def submit_job(spec:dict,idempotency_key:str|None=None,plugin_selected:bool=False)->dict:
    return call('submit_job',{'spec':spec,'idempotency_key':idempotency_key},plugin_selected)

@mcp.tool(description='Read durable job lifecycle and diagnostic metadata.'+GUARD,annotations=READ)
def job_status(job_id:str,plugin_selected:bool=False)->dict:
    return call('job_status',{'job_id':job_id},plugin_selected)

@mcp.tool(description='Read bounded stdout/stderr for a job from the given byte offset.'+GUARD,annotations=READ)
def job_logs(job_id:str,stream:str='stdout',offset:int=0,plugin_selected:bool=False)->dict:
    return call('job_logs',{'job_id':job_id,'stream':stream,'offset':offset},plugin_selected)

@mcp.tool(description='Download a completed job bundle, verify hashes, and return usable Mac paths.'+GUARD,annotations=WRITE)
def fetch_artifacts(job_id:str,plugin_selected:bool=False)->dict:
    return call('fetch_artifacts',{'job_id':job_id},plugin_selected)

@mcp.tool(description='Cancel the specified user-requested job through the durable worker.'+GUARD,annotations=WRITE)
def cancel_job(job_id:str,plugin_selected:bool=False)->dict:
    return call('cancel_job',{'job_id':job_id},plugin_selected)

if __name__=='__main__':mcp.run(transport='stdio')
