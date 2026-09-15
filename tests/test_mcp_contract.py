"""Local stdio MCP contract only; no network/Windows/GPU mocked as PASS."""
import asyncio,json,sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
SERVER=Path(__file__).resolve().parents[1]/'plugin/scripts/mcp_server.py'

def test_seven_tools_and_unselected_deny_before_configuration():
    async def exercise():
        async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(SERVER)]))as(r,w):
            async with ClientSession(r,w)as s:
                await s.initialize();t=await s.list_tools()
                assert {x.name for x in t.tools}=={'gpu_status','infer_audio','submit_job','job_status','job_logs','fetch_artifacts','cancel_job'}
                args={'gpu_status':{},'infer_audio':{'input_path':'/nonexistent.wav'},'submit_job':{'spec':{}},'job_status':{'job_id':'none'},'job_logs':{'job_id':'none'},'fetch_artifacts':{'job_id':'none'},'cancel_job':{'job_id':'none'}}
                for name,a in args.items():
                    result=await s.call_tool(name,a)
                    data=result.structuredContent or json.loads(result.content[0].text)
                    assert data['error']['code']=='WINDOWS_GPU_PLUGIN_NOT_SELECTED'
    asyncio.run(exercise())
