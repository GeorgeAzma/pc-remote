"""Self-signed TLS for the LAN.

Browsers only expose WebCodecs (hardware H.264 decode, the low-latency
stream) in a secure context: https or localhost. We mint a local CA once
and a server certificate for every LAN IP / hostname of this PC. Accept the
browser warning once, or install the CA (served at /ca.crt) on the phone to
make the warning go away for good.
"""
import datetime
import ipaddress
import os
import socket
import ssl

import paths

DIR = os.path.join(paths.DATA, ".certs")
CA_CERT, CA_KEY = os.path.join(DIR, "ca.crt"), os.path.join(DIR, "ca.key")
CERT, KEY = os.path.join(DIR, "server.crt"), os.path.join(DIR, "server.key")


def local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # the address of the interface that routes outward (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ips)


def _names() -> list[str]:
    host = socket.gethostname()
    return ["localhost", host, host.lower() + ".local"]


def _load_or_make_ca(x509, rsa, hashes, serialization, NameOID):
    if os.path.exists(CA_CERT) and os.path.exists(CA_KEY):
        with open(CA_KEY, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), None)
        with open(CA_CERT, "rb") as f:
            return key, x509.load_pem_x509_certificate(f.read())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"PC Remote CA ({socket.gethostname()})")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    with open(CA_KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(CA_CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def _cert_covers(x509, ips, names) -> bool:
    try:
        with open(CERT, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have_ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
        have_dns = set(san.get_values_for_type(x509.DNSName))
        fresh = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) > datetime.timedelta(days=30)
        return set(ips) <= have_ips and set(names) <= have_dns and fresh
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False


def ensure() -> bool:
    """Create/refresh the certificates. Returns False if `cryptography` is
    missing (the server then runs plain HTTP only)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        return False
    os.makedirs(DIR, exist_ok=True)
    ips, names = local_ips(), _names()
    if os.path.exists(KEY) and os.path.exists(CA_CERT) and _cert_covers(x509, ips, names):
        return True
    ca_key, ca = _load_or_make_ca(x509, rsa, hashes, serialization, NameOID)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, socket.gethostname())]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=800))  # iOS rejects > 825 days
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    with open(KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
        f.write(ca.public_bytes(serialization.Encoding.PEM))  # full chain
    return True


def context() -> ssl.SSLContext | None:
    if not ensure():
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx
