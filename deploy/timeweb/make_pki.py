#!/usr/bin/env python3
"""Generate local PoC PKI; no network, cloud calls, or secret stdout."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
from pathlib import Path


def command(*argv: str) -> None:
    subprocess.run(
        ["openssl", *argv],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def name(value: str) -> str:
    if len(value) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
        raise ValueError("invalid DNS name")
    return value


def generate(output: Path, runner_name: str, gateway_name: str, days: int) -> None:
    name(runner_name)
    name(gateway_name)
    if runner_name in {gateway_name, "timeweb-gateway"}:
        raise ValueError("runner and gateway must have different DNS identities")
    if not 1 <= days <= 365:
        raise ValueError("certificate lifetime must be 1..365 days")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    old_mask = os.umask(0o077)
    try:
        command(
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(output / "ca.key"),
        )
        command(
            "req",
            "-x509",
            "-new",
            "-sha256",
            "-days",
            str(days),
            "-key",
            str(output / "ca.key"),
            "-subj",
            "/CN=v2c operator PoC CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-out",
            str(output / "ca.crt"),
        )
        leaves = {
            "runner": (runner_name, [runner_name], "serverAuth"),
            "gateway": (gateway_name, [gateway_name, "timeweb-gateway"], "serverAuth"),
            "client": ("v2c-control", [], "clientAuth"),
        }
        for stem, (common, aliases, purpose) in leaves.items():
            key, csr, cert, ext = (
                output / (stem + suffix) for suffix in (".key", ".csr", ".crt", ".ext")
            )
            command(
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-out",
                str(key),
            )
            command(
                "req",
                "-new",
                "-sha256",
                "-key",
                str(key),
                "-subj",
                "/CN=" + common,
                "-out",
                str(csr),
            )
            extensions = (
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage="
                + purpose
                + "\n"
            )
            if aliases:
                extensions += (
                    "subjectAltName=" + ",".join("DNS:" + alias for alias in aliases) + "\n"
                )
            ext.write_text(extensions)
            command(
                "x509",
                "-req",
                "-sha256",
                "-days",
                str(days),
                "-in",
                str(csr),
                "-CA",
                str(output / "ca.crt"),
                "-CAkey",
                str(output / "ca.key"),
                "-set_serial",
                "0x" + secrets.token_hex(16),
                "-extfile",
                str(ext),
                "-out",
                str(cert),
            )
            csr.unlink()
            ext.unlink()
        for path in output.iterdir():
            path.chmod(0o600)
    finally:
        os.umask(old_mask)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runner-name", default="runner.v2c.internal")
    parser.add_argument("--gateway-name", default="timeweb-gateway.v2c.internal")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    generate(args.output, args.runner_name, args.gateway_name, args.days)
    print("PKI created in private directory; copy only required leaf files, keep ca.key offline")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("PKI generation failed; private key contents are not printed")
