"""Small Win32 process group: assign suspended cmd before any user code runs.

Only the input/output pipe handles are inherited. The job handle stays exclusively
in the owner so abrupt owner termination also kills the entire process tree.
"""

import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import time


SIZE_T = C.c_size_t
HANDLE = W.HANDLE


class BasicLimits(C.Structure):
    _fields_ = [("per_process", C.c_int64), ("per_job", C.c_int64),
                ("flags", W.DWORD), ("minimum", SIZE_T), ("maximum", SIZE_T),
                ("active_limit", W.DWORD), ("affinity", SIZE_T),
                ("priority", W.DWORD), ("scheduling", W.DWORD)]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", C.c_uint64 * 6),
                ("process_memory", SIZE_T), ("job_memory", SIZE_T),
                ("peak_process", SIZE_T), ("peak_job", SIZE_T)]


class Accounting(C.Structure):
    _fields_ = [("user", C.c_int64), ("kernel", C.c_int64),
                ("period_user", C.c_int64), ("period_kernel", C.c_int64),
                ("faults", W.DWORD), ("total", W.DWORD),
                ("active", W.DWORD), ("terminated", W.DWORD)]


class StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
                ("title", W.LPWSTR), ("x", W.DWORD), ("y", W.DWORD),
                ("xsize", W.DWORD), ("ysize", W.DWORD), ("xchars", W.DWORD),
                ("ychars", W.DWORD), ("fill", W.DWORD), ("flags", W.DWORD),
                ("show", W.WORD), ("reserved_size", W.WORD), ("reserved2", C.c_void_p),
                ("stdin", HANDLE), ("stdout", HANDLE), ("stderr", HANDLE)]


class StartupInfoEx(C.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE),
                ("pid", W.DWORD), ("tid", W.DWORD)]


def _api():
    if os.name != "nt":
        raise OSError("Background execution requires Windows.")
    api = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], HANDLE),
        "OpenJobObjectW": ([W.DWORD, W.BOOL, W.LPCWSTR], HANDLE),
        "SetInformationJobObject": ([HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        "AssignProcessToJobObject": ([HANDLE, HANDLE], W.BOOL),
        "TerminateJobObject": ([HANDLE, W.UINT], W.BOOL),
        "CloseHandle": ([HANDLE], W.BOOL),
        "InitializeProcThreadAttributeList": ([C.c_void_p, W.DWORD, W.DWORD, C.POINTER(SIZE_T)], W.BOOL),
        "UpdateProcThreadAttribute": ([C.c_void_p, W.DWORD, SIZE_T, C.c_void_p, SIZE_T, C.c_void_p, C.c_void_p], W.BOOL),
        "DeleteProcThreadAttributeList": ([C.c_void_p], None),
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL,
                            W.DWORD, C.c_void_p, W.LPCWSTR, C.c_void_p, C.POINTER(ProcessInfo)], W.BOOL),
        "ResumeThread": ([HANDLE], W.DWORD),
        "TerminateProcess": ([HANDLE, W.UINT], W.BOOL),
        "GetExitCodeProcess": ([HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "WaitForSingleObject": ([HANDLE, W.DWORD], W.DWORD),
        "GetSystemDirectoryW": ([W.LPWSTR, W.UINT], W.UINT),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


class WindowsJob:
    def __init__(self, name=None):
        self.api = _api()
        self.job = self.process = None
        self.output_fd = None
        self.pid = None
        C.set_last_error(0)
        self.job = self.api.CreateJobObjectW(None, name)
        existed = C.get_last_error() == 183
        self._check(self.job)
        try:
            if name and existed:
                raise OSError("The operation job already exists.")
            limits = ExtendedLimits()
            # No BREAKAWAY flags: descendants cannot leave this group.
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self._check(self.api.SetInformationJobObject(self.job, 9, C.byref(limits), C.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(result):
        if not result:
            raise C.WinError(C.get_last_error())
        return result

    def launch(self, command, cwd, output_file=None):
        import msvcrt

        if output_file is None:
            read_fd, write_fd = os.pipe()
        else:
            read_fd, write_fd = None, os.dup(output_file.fileno())
        input_fd, input_write = os.pipe()
        os.close(input_write)  # EOF on stdin: the service cannot answer interactive prompts.
        attributes = None
        initialized = False
        info = ProcessInfo()
        try:
            handles = (HANDLE * 2)(msvcrt.get_osfhandle(input_fd), msvcrt.get_osfhandle(write_fd))
            os.set_inheritable(input_fd, True)
            os.set_inheritable(write_fd, True)
            size = SIZE_T()
            self.api.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
            attributes = C.create_string_buffer(size.value)
            self._check(self.api.InitializeProcThreadAttributeList(attributes, 1, 0, C.byref(size)))
            initialized = True
            self._check(self.api.UpdateProcThreadAttribute(attributes, 0, 0x20002,
                                                           handles, C.sizeof(handles), None, None))
            startup = StartupInfoEx()
            startup.startup.cb = C.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin = handles[0]
            startup.startup.stdout = startup.startup.stderr = handles[1]
            startup.attributes = C.cast(attributes, C.c_void_p)
            system = C.create_unicode_buffer(32768)
            length = self.api.GetSystemDirectoryW(system, len(system))
            self._check(length and length < len(system))
            executable = system.value + "\\cmd.exe"
            # /s /c strips this outer pair; the caller's command is kept intact.
            line = C.create_unicode_buffer(subprocess.list2cmdline([executable]) + ' /d /s /c "' + command + '"')
            self._check(self.api.CreateProcessW(executable, line, None, None, True,
                0x4 | 0x80000 | 0x08000000, None, str(cwd), C.byref(startup), C.byref(info)))
            self.process, self.pid = info.process, info.pid
            try:
                self._check(self.api.AssignProcessToJobObject(self.job, self.process))
                if self.api.ResumeThread(info.thread) == 0xFFFFFFFF:
                    raise C.WinError(C.get_last_error())
            except BaseException:
                # Assignment can fail, so kill the suspended process explicitly as well.
                self.api.TerminateProcess(self.process, 1)
                self.api.WaitForSingleObject(self.process, 0xFFFFFFFF)
                raise
            self.output_fd, read_fd = read_fd, None
        finally:
            if info.thread:
                self.api.CloseHandle(info.thread)
            if initialized:
                self.api.DeleteProcThreadAttributeList(attributes)
            for descriptor in (read_fd, write_fd, input_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def active_count(self):
        accounting = Accounting()
        self._check(self.api.QueryInformationJobObject(self.job, 1, C.byref(accounting), C.sizeof(accounting), None))
        return accounting.active

    def exit_code(self):
        if self.api.WaitForSingleObject(self.process, 0xFFFFFFFF) != 0:
            raise C.WinError(C.get_last_error())
        code = W.DWORD()
        self._check(self.api.GetExitCodeProcess(self.process, C.byref(code)))
        return code.value

    def terminate(self):
        self._check(self.api.TerminateJobObject(self.job, 1))

    def poll_exit_code(self):
        status = self.api.WaitForSingleObject(self.process, 0)
        if status == 0x102:
            return None
        if status != 0:
            raise C.WinError(C.get_last_error())
        code = W.DWORD()
        self._check(self.api.GetExitCodeProcess(self.process, C.byref(code)))
        return code.value

    def detach(self):
        # The application must survive closing the job handle.
        limits = ExtendedLimits()
        self._check(self.api.SetInformationJobObject(self.job, 9, C.byref(limits), C.sizeof(limits)))
        self.api.CloseHandle(self.job)
        self.job = None
        self.api.CloseHandle(self.process)
        self.process = None

    def close(self):
        try:
            if self.job and self.process:
                self.terminate()
                deadline = time.monotonic() + 30
                while self.active_count():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Process group termination was not confirmed.")
                    time.sleep(0.01)
                self.api.WaitForSingleObject(self.process, 0xFFFFFFFF)
        finally:
            if self.job:
                self.api.CloseHandle(self.job)
                self.job = None
            if self.process:
                self.api.CloseHandle(self.process)
                self.process = None
            if self.output_fd is not None:
                os.close(self.output_fd)
                self.output_fd = None


def stop_previous_job(name, timeout=30):
    """After obtaining runtime ownership, ensure an old named group is gone."""
    api = _api()
    handle = api.OpenJobObjectW(0x4 | 0x8, False, name)  # QUERY | TERMINATE
    if not handle:
        if C.get_last_error() == 2:  # ERROR_FILE_NOT_FOUND: last group reference is gone.
            return
        raise C.WinError(C.get_last_error())
    try:
        WindowsJob._check(api.TerminateJobObject(handle, 1))
        deadline = time.monotonic() + timeout
        accounting = Accounting()
        while True:
            WindowsJob._check(api.QueryInformationJobObject(handle, 1, C.byref(accounting), C.sizeof(accounting), None))
            if accounting.active == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Previous operation processes have not stopped.")
            time.sleep(0.02)
    finally:
        api.CloseHandle(handle)
