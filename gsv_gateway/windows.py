"""Windows process handles and fail-closed ownership. No process starts at import."""

import asyncio
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LaunchSpec:
    executable: str
    arguments: list[str]
    cwd: str
    environment: dict | None = None


def require_windows():
    if os.name != "nt":
        raise RuntimeError("This operation requires Windows")


def creation_time(handle):
    import win32process

    return win32process.GetProcessTimes(handle)["CreationTime"].isoformat()


def current_creation_time():
    import win32api

    return creation_time(win32api.GetCurrentProcess())


class Mutex:
    def __init__(self, name):
        require_windows()
        import win32api
        import win32event
        from .security import pipe_security

        self.handle = win32event.CreateMutex(pipe_security(), True, "Global\\GSVGateway-" + name)
        if win32api.GetLastError() == 183:
            self.handle.Close()
            self.handle = None
            raise RuntimeError("Instance or backend already reserved (Global mutex)")

    def close(self):
        if self.handle is not None:
            import win32event

            win32event.ReleaseMutex(self.handle)
            self.handle.Close()
            self.handle = None


class ManagedProcess:
    """Create suspended, assign a kill-on-close Job, then allow user code to execute.

    Pipes use blocking OS reads/writes on bounded worker threads. Killing the Job
    unblocks pipe operations; consumers must join their pumps before releasing it.
    """

    def __init__(self, spec: LaunchSpec):
        require_windows()
        import msvcrt
        import pywintypes
        import win32api
        import win32con
        import win32job
        import win32pipe
        import win32process

        self.handle = self.job = None
        self.stdin = self.stdout = self.stderr = None
        self.pid = None
        handles = []
        thread = None
        try:
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            attributes = pywintypes.SECURITY_ATTRIBUTES()
            attributes.bInheritHandle = True
            stdin_read, stdin_write = win32pipe.CreatePipe(attributes, 0)
            stdout_read, stdout_write = win32pipe.CreatePipe(attributes, 0)
            stderr_read, stderr_write = win32pipe.CreatePipe(attributes, 0)
            handles.extend([stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write])
            for handle in (stdin_write, stdout_read, stderr_read):
                win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
            startup = win32process.STARTUPINFO()
            startup.dwFlags |= win32process.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = stdin_read, stdout_write, stderr_write
            # STARTUPINFO handle inheritance is serialized by the interpreter's
            # process creation lock so concurrent spawns cannot inherit these pipes.
            with subprocess._waitpid_lock if hasattr(subprocess, "_waitpid_lock") else _spawn_lock:
                self.handle, thread, self.pid, _ = win32process.CreateProcess(
                    spec.executable,
                    subprocess.list2cmdline([spec.executable, *spec.arguments]),
                    None,
                    None,
                    True,
                    win32process.CREATE_SUSPENDED | win32process.CREATE_NO_WINDOW,
                    spec.environment,
                    spec.cwd,
                    startup,
                )
            win32job.AssignProcessToJobObject(self.job, self.handle)
            self.created = creation_time(self.handle)
            for handle in (stdin_read, stdout_write, stderr_write):
                handle.Close()
                handles.remove(handle)
            self.stdin = os.fdopen(
                msvcrt.open_osfhandle(stdin_write.Detach(), os.O_WRONLY | os.O_BINARY), "wb", buffering=0
            )
            handles.remove(stdin_write)
            self.stdout = os.fdopen(
                msvcrt.open_osfhandle(stdout_read.Detach(), os.O_RDONLY | os.O_BINARY), "rb", buffering=0
            )
            handles.remove(stdout_read)
            self.stderr = os.fdopen(
                msvcrt.open_osfhandle(stderr_read.Detach(), os.O_RDONLY | os.O_BINARY), "rb", buffering=0
            )
            handles.remove(stderr_read)
            win32process.ResumeThread(thread)
        except BaseException:
            if self.handle:
                win32api.TerminateProcess(self.handle, 1)
            self.close()
            raise
        finally:
            if thread:
                thread.Close()
            for handle in handles:
                handle.Close()

    @property
    def returncode(self):
        import win32process
        import win32event

        return (
            None
            if win32event.WaitForSingleObject(self.handle, 0) == 258
            else win32process.GetExitCodeProcess(self.handle)
        )

    async def read(self, which="stdout", size=8192):
        return await asyncio.to_thread(getattr(self, which).read, size)

    async def write(self, data):
        view = memoryview(data)
        while view:
            n = await asyncio.to_thread(self.stdin.write, view)
            if not n:
                raise BrokenPipeError()
            view = view[n:]

    def end_input(self):
        if self.stdin and not self.stdin.closed:
            self.stdin.close()

    async def wait(self):
        import win32event

        while await asyncio.to_thread(win32event.WaitForSingleObject, self.handle, 200) == 258:
            pass
        return self.returncode

    async def wait_tree(self, timeout=30):
        await wait_job_empty(self.job, timeout)
        return self.returncode

    def terminate(self):
        if self.job:
            import win32job

            win32job.TerminateJobObject(self.job, 1)

    def close(self):
        if self.job:
            self.job.Close()
            self.job = None
        for stream in [self.stdin, self.stdout, self.stderr]:
            if stream and not stream.closed:
                stream.close()
        if self.handle:
            self.handle.Close()
            self.handle = None


_spawn_lock = threading.Lock()


def process_matches(pid, created):
    try:
        import win32api
        import win32con

        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.SYNCHRONIZE, False, pid
        )
        try:
            import win32event

            return creation_time(handle) == created and win32event.WaitForSingleObject(handle, 0) == 258
        finally:
            handle.Close()
    except Exception:
        return False


def startup_instance_matches(state, nonce, job):
    """Verify the live interpreter belongs to our suspended-created launch tree.

    Windows venv python.exe can redirect to a child interpreter with its own PID.
    Never substitute PID equality for actual Job membership and creation time.
    """
    if state.get("nonce") != nonce:
        return False
    try:
        import win32api
        import win32con
        import win32event
        import win32job

        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.SYNCHRONIZE, False, state["pid"]
        )
        try:
            return (
                creation_time(handle) == state["created"]
                and win32event.WaitForSingleObject(handle, 0) == 258
                and win32job.IsProcessInJob(handle, job)
            )
        finally:
            handle.Close()
    except Exception:
        return False


def job_has_processes(job):
    import win32job

    return bool(
        win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)[
            "ActiveProcesses"
        ]
    )


def detached_gateway(arguments: list[str], cwd: Path):
    import sys
    import win32process
    import win32job

    job = win32job.CreateJobObject(None, "")
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    startup = win32process.STARTUPINFO()
    handle = thread = None
    try:
        handle, thread, pid, _ = win32process.CreateProcess(
            sys.executable,
            subprocess.list2cmdline([sys.executable, *arguments]),
            None,
            None,
            False,
            win32process.CREATE_NO_WINDOW
            | win32process.CREATE_NEW_PROCESS_GROUP
            | win32process.CREATE_SUSPENDED,
            None,
            str(cwd),
            startup,
        )
        win32job.AssignProcessToJobObject(job, handle)
        win32process.ResumeThread(thread)
        return handle, pid, creation_time(handle), job
    except BaseException:
        if handle:
            import win32api
            import win32event

            win32api.TerminateProcess(handle, 1)
            win32event.WaitForSingleObject(handle, 10000)
            handle.Close()
        job.Close()
        raise
    finally:
        if thread:
            thread.Close()


def release_startup_job(job):
    import win32job

    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] &= ~win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)


async def wait_job_empty(job, timeout):
    import win32job

    deadline = time.monotonic() + timeout
    while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)[
        "ActiveProcesses"
    ]:
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned process tree has not exited")
        await asyncio.sleep(0.05)
