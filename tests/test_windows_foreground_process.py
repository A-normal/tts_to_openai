import asyncio
import os
import sys

import pytest

from gsv_gateway.windows import (
    LaunchSpec,
    ManagedProcess,
    creation_time,
    current_creation_time,
    job_has_processes,
    startup_instance_matches,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows process handles and Job Objects")
async def test_native_suspended_job_pipes_in_foreground(tmp_path, monkeypatch):
    import win32job
    import win32process

    # The workspace prohibits background processes. Exercise the same suspended
    # creation/Job/pipes implementation with only its window-hiding flag removed.
    monkeypatch.setattr(win32process, "CREATE_NO_WINDOW", 0)
    script = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.buffer.write(b'diagnostic')"
    process = ManagedProcess(LaunchSpec(sys.executable, ["-c", script], str(tmp_path)))
    stdout = asyncio.create_task(process.read())
    stderr = asyncio.create_task(process.read("stderr"))
    try:
        assert creation_time(process.handle) == process.created
        assert process.pid in win32job.QueryInformationJobObject(
            process.job, win32job.JobObjectBasicProcessIdList
        )
        await process.write(b"foreground-test")
        process.end_input()
        assert await asyncio.wait_for(stdout, 5) == b"foreground-test"
        assert await asyncio.wait_for(stderr, 5) == b"diagnostic"
        assert await asyncio.wait_for(process.wait(), 5) == 0
        assert await process.wait_tree(5) == 0
    finally:
        process.terminate()
        await asyncio.gather(stdout, stderr, return_exceptions=True)
        await process.wait()
        process.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows venv redirector and Job membership")
async def test_startup_accepts_actual_interpreter_only_in_owned_job(tmp_path, monkeypatch):
    import win32api
    import win32con
    import win32process

    monkeypatch.setattr(win32process, "CREATE_NO_WINDOW", 0)
    script = "import os,sys; print(os.getpid(), flush=True); sys.stdin.buffer.read()"
    process = ManagedProcess(LaunchSpec(sys.executable, ["-c", script], str(tmp_path)))
    try:
        # A finite foreground helper uses the exact venv redirector and Job
        # implementation, without running a gateway or GSV service.
        line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5)
        actual_pid = int(line)
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, actual_pid)
        try:
            state = {"pid": actual_pid, "created": creation_time(handle), "nonce": "test-nonce"}
        finally:
            handle.Close()
        assert job_has_processes(process.job)
        assert startup_instance_matches(state, "test-nonce", process.job)
        assert not startup_instance_matches(state, "wrong-nonce", process.job)
        assert not startup_instance_matches({**state, "created": "stale"}, "test-nonce", process.job)
        unrelated = {"pid": os.getpid(), "created": current_creation_time(), "nonce": "test-nonce"}
        assert not startup_instance_matches(unrelated, "test-nonce", process.job)
        process.end_input()
        assert await asyncio.wait_for(process.wait(), 5) == 0
        assert await process.wait_tree(5) == 0
        assert not job_has_processes(process.job)
        assert not startup_instance_matches(state, "test-nonce", process.job)
    finally:
        process.terminate()
        process.end_input()
        await process.wait_tree(5)
        process.close()
