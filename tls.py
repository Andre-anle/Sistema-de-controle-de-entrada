from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
import ssl
from pathlib import Path

import paths
import segredos
from logs import log

NOME_COMUM = "Etiquetas Hortifruti (autoassinado)"
VALIDADE_DIAS = 365
RENOVAR_ANTES_DIAS = 30


class ErroTLS(Exception):
    pass


def _importar():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:  # pragma: no cover
        raise ErroTLS(
            "A biblioteca 'cryptography' não está instalada; HTTPS indisponível."
        ) from exc
    return x509, hashes, serialization, ec, ExtendedKeyUsageOID, NameOID


def _nomes_alternativos(hosts: list[str]) -> tuple[set[str], set[str]]:
    dns = {"localhost"}
    ips = {"127.0.0.1"}
    try:
        dns.add(socket.gethostname().lower())
    except OSError:
        pass
    for host in hosts:
        host = (host or "").strip().strip("[]").lower()
        if not host:
            continue
        try:
            ips.add(str(ipaddress.ip_address(host)))
        except ValueError:
            dns.add(host)
    return dns, ips


def _senha_chave() -> bytes:
    return segredos.obter_segredo("tls", 32)[0].hex().encode("ascii")


def _certificado_serve(caminho: Path, dns: set[str], ips: set[str]) -> bool:
    x509 = _importar()[0]
    try:
        cert = x509.load_pem_x509_certificate(caminho.read_bytes())
        restante = cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
        if restante < dt.timedelta(days=RENOVAR_ANTES_DIAS):
            return False
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        tem_dns = {n.lower() for n in san.get_values_for_type(x509.DNSName)}
        tem_ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
        return dns <= tem_dns and ips <= tem_ips
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False


def _gerar(cert_path: Path, key_path: Path, dns: set[str], ips: set[str]) -> None:
    x509, hashes, serialization, ec, eku_oid, nome_oid = _importar()
    chave = ec.generate_private_key(ec.SECP256R1())
    agora = dt.datetime.now(dt.timezone.utc)
    nome = x509.Name(
        [
            x509.NameAttribute(nome_oid.ORGANIZATION_NAME, "Hortifruti Natural da Terra"),
            x509.NameAttribute(nome_oid.COMMON_NAME, NOME_COMUM),
        ]
    )
    sans: list = [x509.DNSName(d) for d in sorted(dns)]
    sans += [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    cert = (
        x509.CertificateBuilder()
        .subject_name(nome)
        .issuer_name(nome)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(minutes=5))
        .not_valid_after(agora + dt.timedelta(days=VALIDADE_DIAS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([eku_oid.SERVER_AUTH]), critical=False)
        .sign(chave, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    chave_pem = chave.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(_senha_chave()),
    )
    # grava a chave primeiro: se cair a luz no meio, o certificado antigo segue valendo
    for caminho, dados in ((key_path, chave_pem), (cert_path, cert.public_bytes(serialization.Encoding.PEM))):
        temporario = caminho.with_name(caminho.name + ".tmp")
        temporario.write_bytes(dados)
        os.replace(temporario, caminho)
    log("tls").info("Certificado HTTPS gerado para %s / %s", sorted(dns), sorted(ips))


def garantir_certificado(hosts: list[str]) -> tuple[Path, Path, bytes | None]:
    """Devolve (certificado, chave, senha_da_chave|None), gerando/renovando se preciso."""
    proprio_cert = paths.CERT_DIR / "personalizado.pem"
    proprio_chave = paths.CERT_DIR / "personalizado.key"
    if proprio_cert.is_file() and proprio_chave.is_file():
        return proprio_cert, proprio_chave, None
    cert_path = paths.CERT_DIR / "servidor.pem"
    key_path = paths.CERT_DIR / "servidor.key"
    dns, ips = _nomes_alternativos(hosts)
    if not (cert_path.is_file() and key_path.is_file() and _certificado_serve(cert_path, dns, ips)):
        _gerar(cert_path, key_path, dns, ips)
    return cert_path, key_path, _senha_chave()


def criar_contexto(hosts: list[str]) -> ssl.SSLContext:
    cert, chave, senha = garantir_certificado(hosts)
    contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    contexto.minimum_version = ssl.TLSVersion.TLSv1_2
    contexto.options |= ssl.OP_NO_COMPRESSION | ssl.OP_CIPHER_SERVER_PREFERENCE
    contexto.set_alpn_protocols(["http/1.1"])
    try:
        contexto.load_cert_chain(str(cert), str(chave), password=(lambda: senha) if senha else None)
    except (ssl.SSLError, OSError) as exc:
        raise ErroTLS(f"Não foi possível carregar o certificado HTTPS: {exc}") from exc
    return contexto


def impressao_digital(hosts: list[str] | None = None) -> str:
    """SHA-256 do certificado em uso (para o usuário conferir no aviso do navegador)."""
    x509, hashes, serialization, *_ = _importar()
    cert_path = paths.CERT_DIR / "personalizado.pem"
    if not cert_path.is_file():
        cert_path = paths.CERT_DIR / "servidor.pem"
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return ""
    bruto = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(bruto[i:i + 2] for i in range(0, len(bruto), 2))
