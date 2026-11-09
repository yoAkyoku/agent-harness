"""Write the certificate authorities this computer trusts to a PEM file, for `docker build --secret id=ca,...`.

Security software that inspects HTTPS signs connections with its own authority, which the host trusts
but a fresh container does not. Exporting the host's trusted roots (read-only; nothing is installed or
changed) lets the test image's build download its dependencies; the file is only mounted during the
build and never stored in the image.

    python scripts/export_host_ca.py host-ca.pem
"""
from pathlib import Path
import ssl
import sys


def main() -> None:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "host-ca.pem")
    context = ssl.create_default_context()
    context.load_default_certs()
    stores = ["ROOT", "CA"] if sys.platform == "win32" else []
    certificates = {certificate for store in stores
                    for certificate, encoding, _ in ssl.enum_certificates(store)  # type: ignore[attr-defined]
                    if encoding == "x509_asn"}
    certificates.update(certificate for certificate in context.get_ca_certs(binary_form=True))  # type: ignore[union-attr]
    destination.write_text("".join(ssl.DER_cert_to_PEM_cert(certificate) for certificate in sorted(certificates)),
                           encoding="ascii")
    print(f"Wrote {len(certificates)} certificates to {destination}")


if __name__ == "__main__":
    main()
