"""Create the private panel login in Windows Credential Manager; never print passwords."""

import argparse
import ctypes
import hashlib
import json
import os
import secrets
from ctypes import wintypes
from pathlib import Path

TARGET = "Codex/LongforAutoCheckin/PreReleasePanel"


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def _api():
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is required")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api


def read_login():
    api = _api()
    pointer = ctypes.POINTER(Credential)()
    if not api.CredReadW(TARGET, 1, 0, ctypes.byref(pointer)):
        raise OSError(ctypes.get_last_error(), "Panel credential is not available")
    try:
        item = pointer.contents
        return item.UserName, ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-16-le")
    finally:
        api.CredFree(pointer)


def create_hash(destination: Path):
    try:
        username, password = read_login()
    except OSError as exc:
        if exc.errno != 1168:
            raise
        username, password = "operator", secrets.token_urlsafe(24)
        blob = password.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        item = Credential(Type=1, TargetName=TARGET, UserName=username, Persist=2,
                          Comment="Private prerelease auxiliary panel",
                          CredentialBlobSize=len(blob), CredentialBlob=buffer)
        if not _api().CredWriteW(ctypes.byref(item), 0):
            raise OSError(ctypes.get_last_error(), "Could not save panel credential")
    salt = secrets.token_bytes(32)
    value = {"username": username, "salt": salt.hex(), "iterations": 600000,
             "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000).hex()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value), encoding="utf-8")
    print(json.dumps({"saved": True, "username": username, "credential_target": TARGET}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-hash", type=Path)
    parser.add_argument("--copy-password", action="store_true")
    args = parser.parse_args()
    if args.write_hash:
        create_hash(args.write_hash)
    elif args.copy_password:
        import tkinter
        username, password = read_login()
        window = tkinter.Tk()
        window.withdraw()
        window.clipboard_clear()
        window.clipboard_append(password)
        window.update()
        print(f"Copied panel password for {username}; close this window with Ctrl+C after pasting.")
        window.mainloop()
    else:
        parser.error("choose --write-hash or --copy-password")


if __name__ == "__main__":
    main()
