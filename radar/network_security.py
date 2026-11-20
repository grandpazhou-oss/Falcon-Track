"""Shared public-address check for direct reads and the Scrapy resolver."""
import ipaddress


def is_public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if getattr(address, 'ipv4_mapped', None) is not None:
        address = address.ipv4_mapped
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_unspecified)
