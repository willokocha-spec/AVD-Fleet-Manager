"""Convert an unencrypted PuTTY PPK (RSA) to an OpenSSH private key.

Temporary helper for publishing; does not print private material.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def read_mpint(data: bytes, offset: int) -> tuple[int, int]:
    length = int.from_bytes(data[offset : offset + 4], "big")
    offset += 4
    value = int.from_bytes(data[offset : offset + length], "big")
    return value, offset + length


def read_string(data: bytes, offset: int) -> tuple[bytes, int]:
    length = int.from_bytes(data[offset : offset + 4], "big")
    offset += 4
    return data[offset : offset + length], offset + length


def main() -> int:
    ppk_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    lines = ppk_path.read_text(encoding="ascii").splitlines()
    fields: dict[str, str] = {}
    public_lines: list[str] = []
    private_lines: list[str] = []
    section = ""
    for line in lines:
        if line.startswith("PuTTY-User-Key-File") or line.startswith("Encryption:") or line.startswith("Comment:"):
            fields[line.split(":", 1)[0]] = line.split(":", 1)[1].strip()
        elif line.startswith("Public-Lines:"):
            section = "public"
        elif line.startswith("Private-Lines:"):
            section = "private"
        elif line.startswith("Private-MAC:"):
            section = ""
        elif section == "public":
            public_lines.append(line.strip())
        elif section == "private":
            private_lines.append(line.strip())
    if not any(key.startswith("PuTTY-User-Key-File") for key in fields):
        raise SystemExit("not a PuTTY key file")
    key_version = next(key for key in fields if key.startswith("PuTTY-User-Key-File"))
    if fields[key_version] != "ssh-rsa":
        raise SystemExit("not an RSA PPK")
    if fields.get("Encryption") != "none":
        raise SystemExit("PPK is encrypted; conversion would need a passphrase")
    public_blob = base64.b64decode("".join(public_lines))
    kind, offset = read_string(public_blob, 0)
    if kind != b"ssh-rsa":
        raise SystemExit(f"unsupported public key type: {kind!r}")
    e, offset = read_mpint(public_blob, offset)
    n, offset = read_mpint(public_blob, offset)
    private_blob = base64.b64decode("".join(private_lines))
    d, offset = read_mpint(private_blob, 0)
    p, offset = read_mpint(private_blob, offset)
    q, offset = read_mpint(private_blob, offset)
    _iqmp, offset = read_mpint(private_blob, offset)
    numbers = rsa.RSAPrivateNumbers(
        p=p,
        q=q,
        d=d,
        dmp1=d % (p - 1),
        dmq1=d % (q - 1),
        iqmp=pow(q, -1, p),
        public_numbers=rsa.RSAPublicNumbers(e=e, n=n),
    )
    key = numbers.private_key()
    out_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_ssh = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    print(public_ssh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
