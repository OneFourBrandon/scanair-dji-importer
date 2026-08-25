"""Windows DPAPI protection for persisted importer credentials."""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes

ENCRYPTED_PREFIX = "dpapi:v1:"
_ENTROPY = b"ScanAirDJIImporter:desktop-session:v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialProtectionError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _data_blob(payload: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(payload)
    blob = _DataBlob(
        len(payload),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def _crypt(payload: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise CredentialProtectionError("Persistent importer sign-in requires Windows DPAPI.")
    input_blob, input_buffer = _data_blob(payload)
    entropy_blob, entropy_buffer = _data_blob(_ENTROPY)
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.restype = wintypes.BOOL
    function.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    description = "ScanAir DJI Importer session" if protect else None
    result = function(
        ctypes.byref(input_blob),
        description,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    # Keep input buffers alive until the native call has completed.
    _ = input_buffer, entropy_buffer
    if not result:
        error = ctypes.get_last_error()
        raise CredentialProtectionError(f"Windows could not protect the importer session (error {error}).")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def protect_secret(secret: str) -> str:
    if not secret:
        return ""
    encrypted = _crypt(secret.encode("utf-8"), protect=True)
    return f"{ENCRYPTED_PREFIX}{base64.b64encode(encrypted).decode('ascii')}"


def unprotect_secret(value: str) -> str:
    if not value.startswith(ENCRYPTED_PREFIX):
        raise CredentialProtectionError("Importer credential format is not recognized.")
    encoded = value[len(ENCRYPTED_PREFIX):]
    try:
        encrypted = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialProtectionError("Importer credential data is corrupted.") from exc
    try:
        return _crypt(encrypted, protect=False).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialProtectionError("Importer credential data is corrupted.") from exc
