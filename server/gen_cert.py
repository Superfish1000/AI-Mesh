#!/usr/bin/env python3
"""
AI Mesh Certificate Tool

Usage:
  python gen_cert.py                                        # self-signed (default)
  python gen_cert.py --letsencrypt --domain X --email Y    # Let's Encrypt via certbot
  python gen_cert.py --provided --cert /path/cert.pem --key /path/key.pem  # validate existing

Self-signed certs work for localhost and LAN. For internet-facing deployments use
--letsencrypt (requires certbot installed and port 80 accessible).
"""

import argparse
import ipaddress
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).parent
CERT_FILE  = SERVER_DIR / "cert.pem"
KEY_FILE   = SERVER_DIR / "key.pem"


def gen_self_signed():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("Missing dependency: pip install cryptography")
        sys.exit(1)

    print("Generating self-signed certificate (10-year validity)...")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "AI Mesh Server"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AI Mesh"),
    ])

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                x509.IPAddress(ipaddress.IPv6Address("::1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    print(f"\n✓ Certificate : {CERT_FILE}")
    print(f"✓ Private key : {KEY_FILE}")
    print("\nStart server with:")
    print("  uvicorn server:app --ssl-certfile cert.pem --ssl-keyfile key.pem --port 8443")
    print("\nNote: browsers will warn 'Not secure' for self-signed certs.")
    print("For production use --letsencrypt with a real domain.")


def gen_letsencrypt(domain: str, email: str):
    if shutil.which("certbot") is None:
        print("certbot not found. Install it first:")
        print("  pip install certbot   OR   apt install certbot")
        sys.exit(1)

    print(f"Requesting Let's Encrypt certificate for {domain}...")
    print("Note: port 80 must be open and reachable from the internet.\n")

    result = subprocess.run(
        [
            "certbot", "certonly", "--standalone",
            "--non-interactive", "--agree-tos",
            "--email", email,
            "-d", domain,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("certbot failed:")
        print(result.stderr)
        sys.exit(1)

    le_live = Path(f"/etc/letsencrypt/live/{domain}")
    if not le_live.exists():
        # Windows / non-standard path
        print(f"Cert directory not found at {le_live}")
        print("Locate your certbot live directory and copy fullchain.pem → cert.pem, privkey.pem → key.pem")
        sys.exit(1)

    shutil.copy(le_live / "fullchain.pem", CERT_FILE)
    shutil.copy(le_live / "privkey.pem",   KEY_FILE)

    print(f"\n✓ Certificate installed → {CERT_FILE}")
    print(f"✓ Private key installed → {KEY_FILE}")
    print("\nStart server with:")
    print("  uvicorn server:app --ssl-certfile cert.pem --ssl-keyfile key.pem --port 443")
    print("\nAuto-renewal (add to cron or Task Scheduler):")
    print(f"  certbot renew --quiet")
    print(f"  cp /etc/letsencrypt/live/{domain}/fullchain.pem {CERT_FILE}")
    print(f"  cp /etc/letsencrypt/live/{domain}/privkey.pem {KEY_FILE}")


def use_provided(cert: str, key: str):
    cert_path = Path(cert)
    key_path  = Path(key)

    if not cert_path.exists():
        print(f"Cert file not found: {cert_path}")
        sys.exit(1)
    if not key_path.exists():
        print(f"Key file not found: {key_path}")
        sys.exit(1)

    shutil.copy(cert_path, CERT_FILE)
    shutil.copy(key_path,  KEY_FILE)

    print(f"✓ Cert copied → {CERT_FILE}")
    print(f"✓ Key copied  → {KEY_FILE}")
    print("\nStart server with:")
    print("  uvicorn server:app --ssl-certfile cert.pem --ssl-keyfile key.pem --port 8443")


def main():
    parser = argparse.ArgumentParser(description="AI Mesh Certificate Tool")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--letsencrypt", action="store_true", help="Use Let's Encrypt (requires certbot + public domain)")
    group.add_argument("--provided",    action="store_true", help="Copy an existing cert/key pair")
    parser.add_argument("--domain", help="Domain name (required for --letsencrypt)")
    parser.add_argument("--email",  help="Contact email (required for --letsencrypt)")
    parser.add_argument("--cert",   help="Path to existing cert.pem (required for --provided)")
    parser.add_argument("--key",    help="Path to existing key.pem (required for --provided)")
    args = parser.parse_args()

    if args.letsencrypt:
        if not args.domain or not args.email:
            parser.error("--letsencrypt requires --domain and --email")
        gen_letsencrypt(args.domain, args.email)
    elif args.provided:
        if not args.cert or not args.key:
            parser.error("--provided requires --cert and --key")
        use_provided(args.cert, args.key)
    else:
        gen_self_signed()


if __name__ == "__main__":
    main()
