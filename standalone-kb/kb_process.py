"""Subprocess ownership: suspended Windows job assignment or a POSIX process group."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import signal
import subprocess

import psutil


class BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


def windows_kernel():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    declarations = {
        'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'OpenThread': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'ResumeThread': ([wintypes.HANDLE], wintypes.DWORD),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in declarations.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


class ProcessScope:
    def __init__(self):
        self.job = None
        self.process = None
        if os.name == 'nt':
            self.kernel = windows_kernel()
            self.job = self.kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x8
            limits.BasicLimitInformation.ActiveProcessLimit = 32
            if not self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                self.kernel.CloseHandle(self.job)
                self.job = None
                raise ctypes.WinError(ctypes.get_last_error())

    def start(self, argv, **options):
        if self.job is None:
            self.process = subprocess.Popen(argv, start_new_session=True, **options)
            return self.process
        self.process = subprocess.Popen(argv, creationflags=0x00000004, **options)
        handle = self.kernel.OpenProcess(0x100 | 0x1 | 0x1000, False, self.process.pid)
        try:
            if not handle or not self.kernel.AssignProcessToJobObject(self.job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            threads = psutil.Process(self.process.pid).threads()
            thread = self.kernel.OpenThread(0x2, False, threads[0].id)
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.kernel.CloseHandle(thread)
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=2)
            raise
        finally:
            if handle:
                self.kernel.CloseHandle(handle)
        return self.process

    def _job_processes(self):
        buffer = ctypes.create_string_buffer(8 + 32 * ctypes.sizeof(ctypes.c_size_t))
        if not self.kernel.QueryInformationJobObject(self.job, 3, buffer, len(buffer), None):
            raise ctypes.WinError(ctypes.get_last_error())
        count = ctypes.c_uint32.from_buffer(buffer, 4).value
        pids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
        processes = []
        for pid in pids:
            try:
                processes.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                continue
        return processes

    def close(self):
        if self.job is not None:
            try:
                processes = self._job_processes()
                if not self.kernel.TerminateJobObject(self.job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                psutil.wait_procs(processes, timeout=2)
            finally:
                self.kernel.CloseHandle(self.job)
                self.job = None
        elif self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process is not None:
            self.process.wait(timeout=2)
