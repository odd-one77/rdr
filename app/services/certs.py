"""Self-signed HTTPS for the LAN, so phone browsers allow camera access.

Creates a local certificate authority ("RDR Local CA") once, then issues a
server certificate for this machine's hostname and LAN addresses. Phones
install the CA certificate once (the /trust page explains how) and every
later server certificate is trusted automatically.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path

from ..config import DATA_DIR, ensure_dirs

CERT_DIR = DATA_DIR / "certs"
CA_KEY, CA_CRT = CERT_DIR / "rdr-ca.key", CERT_DIR / "rdr-ca.crt"
SRV_KEY, SRV_CRT = CERT_DIR / "server.key", CERT_DIR / "server.crt"


def available() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _lan_ips() -> list[str]:
    from ..main import lan_addresses
    return lan_addresses()


def _write_key(path: Path, key) -> None:
    from cryptography.hazmat.primitives import serialization
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def _load_ca():
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(CA_KEY.read_bytes(), password=None)
    crt = x509.load_pem_x509_certificate(CA_CRT.read_bytes())
    return key, crt


def ensure_ca() -> None:
    if CA_KEY.exists() and CA_CRT.exists():
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ensure_dirs(); CERT_DIR.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RDR Local CA"), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "RDR")])
    now = dt.datetime.now(dt.timezone.utc)
    crt = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(CA_KEY, key)
    CA_CRT.write_bytes(crt.public_bytes(serialization.Encoding.PEM))


def issue_server_cert(extra_hosts: list[str] | None = None) -> dict:
    """(Re)issue the server certificate for the current hostname and LAN IPs."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ensure_ca()
    ca_key, ca_crt = _load_ca()
    hostname = socket.gethostname()
    names: list = [x509.DNSName("localhost"), x509.DNSName(hostname), x509.DNSName(f"{hostname}.local"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    ips = _lan_ips()
    for ip in ips:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    for h in extra_hosts or []:
        h = h.strip()
        if not h:
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            names.append(x509.DNSName(h))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    crt = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_crt.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=820))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(SRV_KEY, key)
    SRV_CRT.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
    return {"hostname": hostname, "ips": ips, "expires": (now + dt.timedelta(days=820)).date().isoformat()}


def status() -> dict:
    info = {"available": available(), "ca": CA_CRT.exists(), "server": SRV_CRT.exists(), "hosts": [], "expires": None}
    if info["server"] and info["available"]:
        try:
            from cryptography import x509
            crt = x509.load_pem_x509_certificate(SRV_CRT.read_bytes())
            san = crt.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            info["hosts"] = [str(v) for v in san.get_values_for_type(x509.DNSName)] + [str(v) for v in san.get_values_for_type(x509.IPAddress)]
            info["expires"] = crt.not_valid_after_utc.date().isoformat()
            from cryptography.hazmat.primitives import hashes as _h
            info["fingerprint"] = crt.fingerprint(_h.SHA256()).hex()   # what a site node pins to trust this hub
        except Exception:  # noqa: BLE001
            pass
    return info


def ready() -> bool:
    return SRV_KEY.exists() and SRV_CRT.exists()
