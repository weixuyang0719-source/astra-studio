"""Serve only the current, authorized score page from the private profile."""
import re

from PySide6.QtCore import QBuffer, QIODevice, QUrl
from PySide6.QtWebEngineCore import (
    QWebEngineUrlRequestInfo, QWebEngineUrlRequestJob,
    QWebEngineUrlScheme, QWebEngineUrlSchemeHandler,
)


SCHEME = b'astra-score'
SCHEME_NAME = SCHEME.decode('ascii')
PREFIX = SCHEME_NAME + '://page/'
TOKEN_URL = re.compile(re.escape(PREFIX) + r'[0-9a-f]{32}')


def register_scheme():
    if QWebEngineUrlScheme.schemeByName(SCHEME).name():
        return
    scheme = QWebEngineUrlScheme(SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(QWebEngineUrlScheme.Flag.LocalScheme | QWebEngineUrlScheme.Flag.SecureScheme)
    QWebEngineUrlScheme.registerScheme(scheme)


def preview_url(url):
    encoded = url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
    return encoded if TOKEN_URL.fullmatch(encoded) else None


def permits_image_request(info, entry):
    return bool(info.requestMethod() == b'GET'
                and info.resourceType() == QWebEngineUrlRequestInfo.ResourceType.ResourceTypeImage
                and info.firstPartyUrl() == entry
                and preview_url(info.requestUrl()))


class ScorePreviewResources(QWebEngineUrlSchemeHandler):
    def __init__(self, profile, current):
        super().__init__(profile)
        self.current = current

    def requestStarted(self, job):
        uri = preview_url(job.requestUrl())
        if job.requestMethod() != b'GET' or not uri or job.initiator().scheme() != 'file':
            job.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
            return
        page = self.current()
        if page is None or uri != page[0]:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        buffer = QBuffer(job)
        buffer.setData(page[1])
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            return
        job.reply(b'image/png', buffer)
