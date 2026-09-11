"""Content acquisition adapters, grouped by transport and routing."""

from .local_file import LocalFileContentFetcher
from .https import HttpsContentFetcherError
from .https import HttpsContentFetcherConfig
from .https import HttpsContentFetcher
from .s3 import S3ContentFetcherError
from .s3 import AnonymousS3ContentFetcherConfig
from .s3 import s3_transport_version
from .s3 import s3_locator
from .s3 import public_s3_url
from .s3 import AnonymousS3ContentFetcher
from .routing import RoutingContentFetcher

__all__ = ['AnonymousS3ContentFetcher', 'AnonymousS3ContentFetcherConfig', 'HttpsContentFetcher', 'HttpsContentFetcherConfig', 'HttpsContentFetcherError', 'LocalFileContentFetcher', 'RoutingContentFetcher', 'S3ContentFetcherError', 'public_s3_url', 's3_locator', 's3_transport_version']
