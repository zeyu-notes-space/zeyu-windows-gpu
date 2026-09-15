"""Start Windows jobs suspended, contain them, then allow user code to execute.

Closing the non-inheritable Job Object handle kills all contained descendants,
including when the worker process is forcibly terminated. A containment failure
terminates the still-suspended child; user code never runs uncontained.
"""

import ctypes
import os
import shutil
import subprocess
import time
from ctypes import wintypes


class WindowsJobProcess:
    def __init__(self, argv, cwd, env, stdout, stderr):
        if os.name != "nt":
            raise RuntimeError("Windows Job Objects require Windows")
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel = kernel
        self._job = None
        self._process = None
        self.returncode = None

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IOCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class BasicAccounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64), ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64), ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD), ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

        self._accounting_type = BasicAccounting

        class StartupInfo(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD), ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.POINTER(ctypes.c_byte)), ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

        class StartupInfoEx(ctypes.Structure):
            _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.DuplicateHandle.restype = wintypes.BOOL
        kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ProcessInfo)]
        kernel.CreateProcessW.restype = wintypes.BOOL
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD

        if os.path.dirname(argv[0]):
            candidate = argv[0] if os.path.isabs(argv[0]) else os.path.join(str(cwd), argv[0])
            executable = shutil.which(candidate, path=env.get("PATH"))
        else:
            # Python 3.9 shutil.which on Windows implicitly searches the caller's
            # current directory. Search explicit PATH entries ourselves instead.
            executable = None
            for directory in env.get("PATH", "").split(os.pathsep):
                if not directory:
                    continue
                if not os.path.isabs(directory):
                    directory = os.path.join(str(cwd), directory)
                executable = shutil.which(os.path.join(directory, argv[0]))
                if executable:
                    break
        if not executable:
            raise FileNotFoundError("Executable not found: " + argv[0])
        if os.path.splitext(executable)[1].lower() in (".bat", ".cmd"):
            raise ValueError("Batch files require an explicit cmd.exe invocation; implicit shell execution is disabled")
        self._job = kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        duplicates = []
        attributes = None
        info = ProcessInfo()
        try:
            if not kernel.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError("Cannot configure Windows process containment: " + str(ctypes.WinError(ctypes.get_last_error())))
            with open(os.devnull, "rb") as stdin:
                current = kernel.GetCurrentProcess()
                for stream in (stdin, stdout, stderr):
                    duplicate = wintypes.HANDLE()
                    if not kernel.DuplicateHandle(current, msvcrt.get_osfhandle(stream.fileno()), current, ctypes.byref(duplicate), 0, True, 2):
                        raise ctypes.WinError(ctypes.get_last_error())
                    duplicates.append(duplicate)
                size = ctypes.c_size_t()
                kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
                storage = ctypes.create_string_buffer(size.value)
                attributes = ctypes.cast(storage, ctypes.c_void_p)
                if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
                    attributes = None
                    raise ctypes.WinError(ctypes.get_last_error())
                handles = (wintypes.HANDLE * 3)(*[handle.value for handle in duplicates])
                if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x00020002, ctypes.cast(handles, ctypes.c_void_p), ctypes.sizeof(handles), None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
                startup = StartupInfoEx()
                startup.StartupInfo.cb = ctypes.sizeof(startup)
                startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = [handle.value for handle in duplicates]
                startup.lpAttributeList = attributes
                environment = ctypes.create_unicode_buffer("\0".join(key + "=" + value for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0")
                command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
                flags = 0x00000004 | 0x00000200 | 0x00000400 | 0x00080000  # suspended, process group, Unicode, STARTUPINFOEX
                if not kernel.CreateProcessW(executable, command_line, None, None, True, flags, environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
            self._process = info.hProcess
            self.pid = info.dwProcessId
            if not kernel.AssignProcessToJobObject(self._job, self._process):
                error = ctypes.get_last_error()
                kernel.TerminateProcess(self._process, 1)
                kernel.WaitForSingleObject(self._process, 5000)
                raise OSError("Windows process containment failed before execution: " + str(ctypes.WinError(error)))
            if kernel.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            try:
                self.close()
            except Exception:
                # close() releases the kill-on-close handle even if draining
                # accounting fails; retain the original startup diagnostic.
                pass
            raise
        finally:
            if info.hThread:
                kernel.CloseHandle(info.hThread)
            if attributes:
                kernel.DeleteProcThreadAttributeList(attributes)
            for duplicate in duplicates:
                kernel.CloseHandle(duplicate)

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        result = self._kernel.WaitForSingleObject(self._process, 0)
        if result == 0x102:  # WAIT_TIMEOUT
            return None
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not self._kernel.GetExitCodeProcess(self._process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.returncode = code.value
        return self.returncode

    def terminate(self):
        if self._job:
            if not self._kernel.TerminateJobObject(self._job, 1):
                raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, timeout=5):
        result = self._kernel.WaitForSingleObject(self._process, int(timeout * 1000))
        if result == 0x102:
            raise subprocess.TimeoutExpired("contained Windows job", timeout)
        return self.poll()

    def close(self):
        try:
            if self._job:
                self.terminate()
            if self._process:
                if self.returncode is None:
                    self.wait(timeout=5)
                # Job accounting can retain terminated members while process
                # references exist. Release ours before waiting for zero.
                self._kernel.CloseHandle(self._process)
                self._process = None
            if self._job:
                deadline = time.monotonic() + 5
                while True:
                    accounting = self._accounting_type()
                    if not self._kernel.QueryInformationJobObject(self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.ActiveProcesses == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Windows Job Object descendants did not terminate within 5 seconds")
                    time.sleep(0.01)
        finally:
            if self._job:
                self._kernel.CloseHandle(self._job)
                self._job = None
            if self._process:
                self._kernel.CloseHandle(self._process)
                self._process = None
