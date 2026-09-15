import hashlib
import http.server
import io
import json
from pathlib import Path
import threading
import time
import urllib.error

import pytest


HERE = Path(__file__).resolve().parents[2] / "plugin" / "scripts"
import sys
sys.path.insert(0, str(HERE))

from zeyu_plugin_client import Controller, PluginConfig, ToolError, load_config
from zeyu_plugin_client.bento import BentoClient
from zeyu_plugin_client.tunnel import SSHTunnel
import zeyu_plugin_client.tunnel as tunnel_module


TOKEN = "t" * 64

def infer_operation(output=b"RIFFfixture"):
    sha=hashlib.sha256(output).hexdigest()
    return {"ok":True,"operation":"infer_audio","run_id":"11111111-1111-4111-8111-111111111111",
            "model":{"alias":"unet"},"checkpoint":"checkpoint-sha","git_commit":"a"*40,
            "parameters":{"input_sha256":hashlib.sha256(b"RIFFinput").hexdigest(),"input_bytes":len(b"RIFFinput")},
            "artifact":{"sha256":sha,"bytes":len(output)},"artifact_sha256":sha}



def write_config(tmp_path, *, bento_port=19876, fabric_port=18765, **changes):
    token = tmp_path / "token"
    token.write_text(TOKEN, encoding="utf-8")
    token.chmod(0o600)
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host zeyu-test\n  HostName 127.0.0.1\n", encoding="utf-8")
    value = {
        "ssh_config": str(ssh_config),
        "ssh_host": "zeyu-test",
        "bento_url": f"http://127.0.0.1:{bento_port}",
        "fabric_url": f"http://127.0.0.1:{fabric_port}",
        "token_file": str(token),
        "artifact_root": str(tmp_path / "artifacts"),
        "known_projects": ["test"],
        "known_environments": ["test"],
        "offline_timeout_seconds": 8,
        "request_timeout_seconds": 2,
    }
    value.update(changes)
    path = tmp_path / "client.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class FakeProcess:
    def __init__(self, command):
        self.command = command
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakePopen:
    def __init__(self):
        self.calls = []
        self.processes = []

    def __call__(self, command, **kwargs):
        process = FakeProcess(command)
        self.calls.append((command, kwargs))
        self.processes.append(process)
        return process


def controller(tmp_path, **config_changes):
    path = write_config(tmp_path, **config_changes)
    config = load_config(path)
    popen = FakePopen()
    value = Controller(config, popen=popen, probe_tunnel=False)
    return value, popen


def test_config_keeps_init_local_and_validates_loopback(tmp_path):
    value = load_config(write_config(tmp_path))
    assert value.bento_url == "http://127.0.0.1:19876"
    assert value.fabric_url == "http://127.0.0.1:18765"
    assert value.known_projects == ("test",)
    with pytest.raises(ToolError, match="127.0.0.1"):
        load_config(write_config(tmp_path, bento_url="http://192.0.2.4:19876"))


def test_controller_does_not_start_tunnel_until_selected_tool_call(tmp_path):
    config = load_config(write_config(tmp_path))
    popen = FakePopen()
    value = Controller(config, require_selected=True, selected=False, popen=popen)
    assert popen.calls == []
    with pytest.raises(ToolError) as caught:
        value.gpu_status()
    assert caught.value.code == "PLUGIN_NOT_SELECTED"
    assert popen.calls == []


def test_first_authorized_call_starts_one_private_dual_forward(tmp_path):
    value, popen = controller(tmp_path)
    value._bento_client = type("Health", (), {"gpu_health": lambda self, timeout=None: {"online": True}})()
    value._fabric_client = type("FabricHealth", (), {"json": lambda self, path: {"status": "ok", "path": path}})()
    result = value.gpu_status()
    assert result["status"] == "COMPLETED"
    assert result["health"]["fabric"]["health"]["status"] == "ok"
    assert result["health"]["fabric"]["workers"]["path"] == "/v1/workers"
    assert len(popen.calls) == 1
    command, kwargs = popen.calls[0]
    assert command[:4] == ["ssh", "-N", "-T", "-F"]
    assert str(tmp_path / "ssh_config") in command
    assert "-o" in command and "StrictHostKeyChecking=yes" in command
    assert "-o" in command and "ExitOnForwardFailure=yes" in command
    assert "127.0.0.1:19876:127.0.0.1:9876" in command
    assert "127.0.0.1:18765:127.0.0.1:8765" in command
    assert kwargs["shell"] is False


def test_tunnel_rejects_an_occupied_forward_before_starting_ssh(tmp_path, monkeypatch):
    config = load_config(write_config(tmp_path))
    popen = FakePopen()

    class OccupiedSocket:
        def setsockopt(self, *args):
            pass

        def bind(self, address):
            if address == ("127.0.0.1", 19876):
                raise OSError("address already in use")

        def close(self):
            pass

    monkeypatch.setattr(tunnel_module.socket, "socket", lambda *_args: OccupiedSocket())
    tunnel = SSHTunnel(config, popen=popen)
    with pytest.raises(ToolError) as caught:
        tunnel.ensure()
    assert caught.value.code == "LOCAL_TUNNEL_BUSY"
    assert popen.calls == []


def test_gpu_status_has_an_absolute_offline_deadline_and_parallel_branches(tmp_path):
    value, _ = controller(tmp_path, offline_timeout_seconds=0.2)
    value._tunnel.ensure = lambda: None

    class SlowBento:
        def gpu_health(self, timeout=None):
            time.sleep(1)

    class SlowFabric:
        def json(self, path):
            time.sleep(1)

    value._bento_client = SlowBento()
    value._fabric_client = SlowFabric()
    started = time.monotonic()
    result = value.invoke("gpu_status")
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert result["ok"] is False
    assert result["error"]["code"] == "WINDOWS_GPU_OFFLINE"


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    requests = []
    release_count = 0
    submitted = []

    def log_message(self, *_):
        pass

    def _send_json(self, value, status=200, headers=None):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        FixtureHandler.requests.append(("GET", self.path, b""))
        if self.path == "/gpu_health":
            self._send_json({"hardware": {"name": "local-fixture"}, "runtime": {"kind": "fixture"}})
            return
        if self.path.startswith("/v1/jobs/") and "/logs?stream=stdout&offset=0" in self.path:
            self._send_json({"text": "hello\n", "next_offset": 6, "state": "COMPLETED"})
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        FixtureHandler.requests.append(("POST", self.path, body))
        if self.path == "/infer_audio":
            metadata = infer_operation()
            output = b"RIFFfixture"
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(output)))
            self.send_header("X-ZeYu-Operation", json.dumps(metadata, separators=(",", ":")))
            self.end_headers()
            self.wfile.write(output)
            return
        if self.path == "/release_model":
            FixtureHandler.release_count += 1
            self._send_json({"status": "released"})
            return
        if self.path == "/v1/jobs":
            FixtureHandler.submitted.append((self.headers.get("Idempotency-Key"), json.loads(body)))
            self._send_json({"job_id": "22222222-2222-4222-8222-222222222222", "state": "QUEUED"})
            return
        self._send_json({"error": "not found"}, 404)


class Server:
    def __init__(self, handler):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def test_infer_audio_writes_file_and_preserves_operation_metadata(tmp_path):
    try:
        server = Server(FixtureHandler)
    except PermissionError:
        pytest.skip("local sandbox does not permit loopback fixture binding")
    try:
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFFinput")
        value, _ = controller(tmp_path, bento_url=f"http://127.0.0.1:{server.port}")
        value._tunnel.ensure = lambda: None
        result = value.infer_audio(source, model="unet", run_id="11111111-1111-4111-8111-111111111111")
        assert result["status"] == "COMPLETED"
        output = Path(result["output_path"])
        assert output.read_bytes() == b"RIFFfixture"
        assert json.loads(output.with_suffix(".operation.json").read_text()) == result["operation"]
        manifest=json.loads((output.parent/"manifest.json").read_text())
        assert manifest["metadata"] == result["metadata"]
        assert manifest["artifacts"]["output"]["sha256"] == result["output_sha256"]
        assert result["metadata"]["run_id"] == "11111111-1111-4111-8111-111111111111"
        assert result["metadata"]["artifact_hashes"]["output"] == hashlib.sha256(b"RIFFfixture").hexdigest()
        assert any(path == "/infer_audio" for method, path, _ in FixtureHandler.requests)
    finally:
        server.close()


def test_inference_transport_failure_is_unknown_and_not_retried(tmp_path):
    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFFinput")
    value, _ = controller(tmp_path, bento_url="http://127.0.0.1:1", request_timeout_seconds=0.2)
    value._tunnel.ensure = lambda: None
    with pytest.raises(ToolError) as caught:
        value.infer_audio(source, model="tiger", run_id="33333333-3333-4333-8333-333333333333")
    assert caught.value.code == "OUTCOME_UNKNOWN"
    assert caught.value.envelope["metadata"]["status"] == "UNKNOWN"
    assert caught.value.envelope["metadata"]["error"]["outcome_unknown"] is True


def test_bento_connectivity_failure_uses_stable_offline_code(tmp_path):
    config = load_config(write_config(tmp_path, bento_port=1, request_timeout_seconds=0.2))
    client = BentoClient(config)
    with pytest.raises(ToolError) as caught:
        client.gpu_health(timeout=0.1)
    assert caught.value.code == "WINDOWS_GPU_OFFLINE"


def test_bento_multipart_and_file_output_are_verified_without_gpu(tmp_path):
    config = load_config(write_config(tmp_path))
    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFFinput")
    output = tmp_path / "artifacts" / "run" / "unet.wav"
    client = BentoClient(config)
    seen = {}

    def response(method, path, *, body, content_type, timeout, run_id):
        seen.update(method=method, path=path, body=body, content_type=content_type, run_id=run_id)
        return b"RIFFoutput", {"content-type": "audio/wav", "x-zeyu-operation": json.dumps(infer_operation(b"RIFFoutput"))}

    client._request = response
    result = client.infer_audio(source, output, model="unet", run_id="11111111-1111-4111-8111-111111111111")
    assert output.read_bytes() == b"RIFFoutput"
    assert b'name="model"' in seen["body"] and b"unet" in seen["body"]
    assert b'name="run_id"' in seen["body"]
    assert seen["path"] == "/infer_audio"
    assert result["output_sha256"] == hashlib.sha256(b"RIFFoutput").hexdigest()


def test_bento_gpu_health_uses_post_empty_json(tmp_path):
    config = load_config(write_config(tmp_path))
    client = BentoClient(config)
    calls = []

    def response(method, path, *, body=None, content_type=None, timeout=None, run_id=None):
        calls.append((method, path, body, content_type))
        return b'{"gpu": {"name": "fixture"}}', {"content-type": "application/json"}

    client._request = response
    result = client.gpu_health(timeout=1)
    assert result["gpu"]["name"] == "fixture"
    assert calls == [("POST", "/gpu_health", b"{}", "application/json")]


def test_bento_structured_service_error_code_is_preserved(tmp_path):
    config = load_config(write_config(tmp_path))
    client = BentoClient(config)

    class ErrorOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "busy",
                {},
                io.BytesIO(json.dumps({"error": {"code": "SERVICE_TIMEOUT", "message": "busy"}}).encode()),
            )

    # The fixture uses the standard HTTPError body contract; no GPU or server is faked.
    client.opener = ErrorOpener()
    with pytest.raises(ToolError) as caught:
        client.gpu_health(timeout=1)
    assert caught.value.code == "SERVICE_TIMEOUT"


def test_batch_releases_model_then_uses_fabric_idempotency(tmp_path, monkeypatch):
    try:
        bento_server = Server(FixtureHandler)
        fabric_server = Server(FixtureHandler)
    except PermissionError:
        pytest.skip("local sandbox does not permit loopback fixture binding")
    try:
        value, _ = controller(tmp_path, bento_url=f"http://127.0.0.1:{bento_server.port}",
                              fabric_url=f"http://127.0.0.1:{fabric_server.port}")
        value._tunnel.ensure = lambda: None
        result = value.submit_job({
            "project": "test", "environment": "test", "git_commit": "a" * 40,
            "command": ["{python}", "-c", "print(1)"], "arguments": [],
            "timeout": 30, "artifact_paths": [],
        }, idempotency_key="batch-key")
        assert result["job_id"] == "22222222-2222-4222-8222-222222222222"
        assert result["run_id"] == result["job_id"]
        assert FixtureHandler.release_count >= 1
        assert FixtureHandler.submitted[-1][0] == "batch-key"
    finally:
        bento_server.close()
        fabric_server.close()


def test_fetch_artifacts_rejects_destination_outside_artifact_root(tmp_path):
    value, _ = controller(tmp_path)
    with pytest.raises(ToolError, match="artifact_root"):
        value.fetch_artifacts("22222222-2222-4222-8222-222222222222", output=tmp_path / "outside")


class FakeBento:
    def __init__(self):
        self.released = 0

    def release_model(self, timeout=None):
        self.released += 1
        return {"status": "released"}


class FakeFabric:
    def __init__(self, artifact_dir):
        self.artifact_dir = artifact_dir
        self.keys = []

    def submit(self, spec, key):
        self.keys.append(key)
        return {"job_id": "22222222-2222-4222-8222-222222222222", "state": "QUEUED"}

    def status(self, job_id):
        return {"job_id": job_id, "state": "COMPLETED", "runtime": {"fixture": True}}

    def logs(self, job_id, stream, follow, offset, output):
        output.write("fixture log\n")
        return offset + len("fixture log\n".encode())

    def artifacts(self, job_id, output):
        target = Path(output) / job_id
        target.mkdir(parents=True)
        (target / "inventory.json").write_text(json.dumps({"files": [{"path": "artifacts/out.txt", "sha256": "a" * 64}]}), encoding="utf-8")
        (target / "manifest.json").write_text(json.dumps({"state":"COMPLETED","git_commit":"a"*40,"host":{"gpu":"fixture"},"runtime":{"kind":"fixture"}}))
        return {"job_id": job_id, "directory": str(target), "verified": True}

    def json(self, path, body):
        return {"job_id": path.split("/")[3], "state": "CANCELLED"}


def test_all_batch_tools_use_fabric_and_surface_unified_metadata(tmp_path):
    value, _ = controller(tmp_path)
    value._tunnel.ensure = lambda: None
    bento = FakeBento()
    value._bento_client = bento
    value._fabric_client = FakeFabric(tmp_path / "artifacts")
    spec = {
        "project": "test", "environment": "test", "git_commit": "a" * 40,
        "command": ["{python}", "-c", "print(1)"], "arguments": [], "timeout": 30, "artifact_paths": [],
    }
    submitted = value.submit_job(spec, idempotency_key="fixture-key")
    assert submitted["metadata"]["run_id"] == submitted["job_id"]
    assert bento.released == 1
    assert value.job_status(submitted["job_id"])["status"] == "COMPLETED"
    assert value.job_logs(submitted["job_id"])["text"] == "fixture log\n"
    fetched = value.fetch_artifacts(submitted["job_id"])
    assert fetched["metadata"]["artifact_hashes"]["artifacts/out.txt"] == "a" * 64
    assert value.cancel_job(submitted["job_id"])["status"] == "CANCELLED"

@pytest.mark.parametrize('code,retries', [('MODEL_BUSY',2),('GPU_BUSY',1),('OUTCOME_UNKNOWN',1)])
def test_model_switch_only_retries_explicit_preexecution_rejection(tmp_path,code,retries):
    value,_=controller(tmp_path);value._tunnel.ensure=lambda:None
    source=tmp_path/'input.wav';source.write_bytes(b'fixture')
    class Reject(FakeBento):
        calls=0
        def infer_audio(self,*args,**kwargs):
            self.calls+=1
            raise ToolError(code,'rejected')
    client=Reject();value._bento_client=client
    with pytest.raises(ToolError):value.infer_audio(source,model='tiger')
    assert client.calls==retries
    assert client.released==retries-1


def test_tunnel_rebinds_time_wait_but_rejects_live_listener(tmp_path):
    import socket
    listener=socket.socket()
    listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    listener.bind(('127.0.0.1',0));port=listener.getsockname()[1];listener.listen()
    client=socket.create_connection(('127.0.0.1',port));accepted,_=listener.accept()
    tunnel=SSHTunnel(load_config(write_config(tmp_path)),probe=False)
    with pytest.raises(ToolError):tunnel._assert_ports_free((port,))
    accepted.shutdown(socket.SHUT_WR);client.recv(1);client.close();accepted.close();listener.close()
    tunnel._assert_ports_free((port,))


@pytest.mark.parametrize("case",["missing","malformed","run_id","model","input","artifact_hash","top_hash","bytes","input_bytes","nan"])
def test_bento_rejects_unbound_artifact_before_publication(tmp_path,case):
    source=tmp_path/"input.wav";source.write_bytes(b"RIFFinput")
    output=tmp_path/"output.wav";client=BentoClient(load_config(write_config(tmp_path)))
    op=infer_operation()
    if case=="run_id":op["run_id"]="22222222-2222-4222-8222-222222222222"
    if case=="model":op["model"]["alias"]="tiger"
    if case=="input":op["parameters"]["input_sha256"]="a"*64
    if case=="artifact_hash":op["artifact"]["sha256"]="a"*64
    if case=="top_hash":op["artifact_sha256"]="a"*64
    if case=="bytes":op["artifact"]["bytes"]=999
    if case=="input_bytes":op["parameters"]["input_bytes"]=999
    if case=="nan":op["timing"]={"total_ms":float('nan')}
    header=None if case=="missing" else "{" if case=="malformed" else json.dumps(op)
    client._request=lambda *args,**kwargs:(b"RIFFfixture",{"content-type":"audio/wav","x-zeyu-operation":header})
    with pytest.raises(ToolError) as caught:
        client.infer_audio(source,output,model="unet",run_id="11111111-1111-4111-8111-111111111111")
    assert caught.value.code=="INVALID_RESPONSE"
    assert not output.exists() and not output.with_suffix(".operation.json").exists()


def test_failed_batch_metadata_preserves_failure_hardware_and_parameters(tmp_path):
    from zeyu_plugin_client.client import _metadata
    failure={"code":"WORKER_INTERRUPTED","message":"not rerun"}
    value=_metadata(run_id="run",model="batch",status="FAILED",backend={"failure":failure,"host":{"gpu":"fixture"},"parameters":["--iterations","3"]})
    assert value["error"]==failure
    assert value["hardware"]=={"gpu":"fixture"}
    assert value["parameters"]["arguments"]==["--iterations","3"]


def test_unknown_submission_retains_retry_identity_without_retry(tmp_path):
    value,_=controller(tmp_path);value._tunnel.ensure=lambda:None;value._bento_client=FakeBento()
    class OfflineError(Exception):pass
    class Disconnected:
        calls=0
        def submit(self,spec,key):
            self.calls+=1;raise OfflineError('reply lost after request')
    remote=Disconnected();value._fabric_client=remote
    spec={"project":"test","environment":"test","git_commit":"a"*40,"command":["{python}","x.py"],"arguments":[],"timeout":30,"artifact_paths":[]}
    result=value.invoke('submit_job',spec=spec,idempotency_key='stable-retry-key')
    assert result['status']=='UNKNOWN' and result['error']['code']=='OUTCOME_UNKNOWN'
    assert result['error']['idempotency_key']=='stable-retry-key'
    assert result['metadata']['parameters']['idempotency_key']=='stable-retry-key'
    assert remote.calls==1
