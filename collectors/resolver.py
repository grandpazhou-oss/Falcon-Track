"""Reject non-public addresses in the resolver used by Scrapy's connector."""
from scrapy.resolver import CachingThreadedResolver
from twisted.internet.error import DNSLookupError
from radar.network_security import is_public_address


class PublicOnlyResolver(CachingThreadedResolver):
    def getHostByName(self, name, timeout=()):
        result = super().getHostByName(name, timeout)
        def require_public(address):
            if not is_public_address(address):
                raise DNSLookupError('non_public_address')
            return address
        result.addCallback(require_public)
        return result
