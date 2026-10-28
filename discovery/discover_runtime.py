"""Read-only discovery of ShadowBot's running OpenCode process parameters.

Windows x64 only. Opens only a verified ShadowBot opencode.exe with query/read
rights, reads its PEB process parameters (not a heap scan), and never prints or
persists credentials. The CLI prints a deliberately narrow, redacted summary.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import PureWindowsPath
import struct
from urllib.parse import urlsplit, urlunsplit


class PROCESSENTRY32W(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", C.c_size_t), ("th32ModuleID", W.DWORD),
                ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD), ("szExeFile", W.WCHAR * 260)]


def kernel_api():
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    k.QueryFullProcessImageNameW.restype = W.BOOL
    k.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
    k.ReadProcessMemory.restype = W.BOOL
    k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    k.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32W)]
    return k


def processes():
    k = kernel_api()
    snap = k.CreateToolhelp32Snapshot(2, 0)
    if snap == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = C.sizeof(entry)
        ok = k.Process32FirstW(snap, C.byref(entry))
        rows = []
        while ok:
            rows.append({"pid": entry.th32ProcessID, "ppid": entry.th32ParentProcessID, "name": entry.szExeFile})
            ok = k.Process32NextW(snap, C.byref(entry))
        return rows
    finally:
        k.CloseHandle(snap)


def read_shadowbot_opencode(pid: int) -> dict:
    """Return private in-memory parameters; callers MUST NOT serialize the result."""
    if C.sizeof(C.c_void_p) != 8:
        raise RuntimeError("Use 64-bit Python; 32-bit PEB layouts are unsupported")
    inventory = {p["pid"]: p for p in processes()}
    entry = inventory.get(pid)
    if not entry or entry["name"].casefold() != "opencode.exe":
        raise RuntimeError("Selected PID is not an opencode.exe process")
    parent = inventory.get(entry["ppid"], {})
    if parent.get("name", "").casefold() != "shadowbot.shell.exe":
        raise RuntimeError("Selected OpenCode is not a direct child of ShadowBot.Shell.exe")
    k = kernel_api()
    handle = k.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        raise C.WinError(C.get_last_error())
    try:
        image_buffer = C.create_unicode_buffer(32768)
        image_length = W.DWORD(len(image_buffer))
        if not k.QueryFullProcessImageNameW(handle, 0, image_buffer, C.byref(image_length)):
            raise C.WinError(C.get_last_error())
        image = PureWindowsPath(image_buffer.value)
        parts = [p.casefold() for p in image.parts]
        if image.name.casefold() != "opencode.exe" or "shadowbot" not in parts:
            raise RuntimeError("OpenCode image is outside the expected ShadowBot installation")

        def read(address, size):
            if not 0 < size <= 1024 * 1024:
                raise RuntimeError("Process-parameter read exceeds the fixed bound")
            buf = C.create_string_buffer(size)
            actual = C.c_size_t()
            if not k.ReadProcessMemory(handle, address, buf, size, C.byref(actual)) or actual.value != size:
                raise C.WinError(C.get_last_error())
            return buf.raw

        nt = C.WinDLL("ntdll")
        nt.NtQueryInformationProcess.argtypes = [W.HANDLE, W.ULONG, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
        nt.NtQueryInformationProcess.restype = W.LONG
        basic = C.create_string_buffer(48)
        returned = W.ULONG()
        status = nt.NtQueryInformationProcess(handle, 0, basic, len(basic), C.byref(returned))
        if status != 0:
            raise RuntimeError(f"NtQueryInformationProcess failed with NTSTATUS 0x{status & 0xffffffff:08x}")
        peb = struct.unpack_from("<Q", basic.raw, 8)[0]
        parameters = struct.unpack("<Q", read(peb + 0x20, 8))[0]
        block = read(parameters, 0x88)
        environment_ptr = struct.unpack_from("<Q", block, 0x80)[0]
        # Read page-sized chunks without crossing an initial page boundary.
        env_bytes = bytearray()
        while len(env_bytes) < 1024 * 1024:
            position = environment_ptr + len(env_bytes)
            size = min(4096 - position % 4096, 1024 * 1024 - len(env_bytes))
            env_bytes.extend(read(position, size))
            end = next((p for p in range(max(0, len(env_bytes) - size - 4), len(env_bytes) - 3, 2)
                        if env_bytes[p:p + 4] == b"\0\0\0\0"), None)
            if end is not None:
                env_bytes = env_bytes[:end]
                break
        else:
            raise RuntimeError("Environment exceeds the fixed 1 MiB bound")
        environment = {}
        for line in env_bytes.decode("utf-16-le").split("\0"):
            if line and not line.startswith("=") and "=" in line:
                key, value = line.split("=", 1)
                if key == "OPENCODE_STUDIO_MCP_URL":
                    environment[key] = value
        return {"pid": pid, "ppid": entry["ppid"], "image": str(image),
                "environment": environment}
    finally:
        k.CloseHandle(handle)


def discover_studio_endpoint(pid: int | None = None) -> dict:
    """Discover the existing Studio HTTP MCP; never launches or mutates ShadowBot.

    Returns {url, headers, source, pid, ppid, image}. Headers are currently empty:
    observed Studio MCP is a distinct loopback server that accepts no-auth MCP.
    OpenCode's SERVER_USERNAME/PASSWORD must never be sent to this endpoint.
    Multiple valid running instances are intentionally ambiguous; select a PID.
    """
    candidates = [pid] if pid is not None else [p["pid"] for p in processes() if p["name"].casefold() == "opencode.exe"]
    found = []
    errors = []
    for candidate in candidates:
        try:
            private = read_shadowbot_opencode(candidate)
            url = private["environment"].get("OPENCODE_STUDIO_MCP_URL", "")
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
                raise RuntimeError("Studio MCP must be an explicit loopback HTTP URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise RuntimeError("Refusing Studio URL with userinfo, query or fragment")
            if not parsed.port or parsed.path != "/api/v1/mcp":
                raise RuntimeError("Unrecognized Studio MCP URL; endpoint contract needs review")
            found.append({"url": url, "headers": {}, "source": "ShadowBot child process environment: OPENCODE_STUDIO_MCP_URL",
                          "pid": candidate, "ppid": private["ppid"], "image": private["image"]})
        except ValueError:
            errors.append(f"PID {candidate}: Invalid Studio endpoint URL")
        except (OSError, RuntimeError) as exc:
            errors.append(f"PID {candidate}: {exc}")
    if len(found) != 1:
        if len(found) > 1:
            raise RuntimeError("Multiple ShadowBot OpenCode instances; specify the intended PID")
        raise RuntimeError("No verified ShadowBot Studio MCP endpoint found. " + "; ".join(errors))
    return found[0]


get_endpoint = discover_studio_endpoint


def safe_url(value):
    try:
        url = urlsplit(str(value))
        if url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return "<non-loopback URL omitted>"
        # Userinfo, query and fragment can carry authentication; never report them.
        host = "[::1]" if url.hostname == "::1" else url.hostname
        return urlunsplit((url.scheme, host + (f":{url.port}" if url.port else ""), url.path, "", ""))
    except (TypeError, ValueError):
        return "<invalid URL omitted>"


def safe_summary(private):
    return {"pid": private["pid"], "ppid": private["ppid"],
            "studio_url": safe_url(private["environment"].get("OPENCODE_STUDIO_MCP_URL", ""))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    candidates = [args.pid] if args.pid else [p["pid"] for p in processes() if p["name"].casefold() == "opencode.exe"]
    reports = []
    for pid in candidates:
        try:
            reports.append(safe_summary(read_shadowbot_opencode(pid)))
        except (OSError, RuntimeError) as exc:
            reports.append({"pid": pid, "error": str(exc)})
    print(json.dumps(reports, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
