# -*- coding: utf-8 -*-

# Copyright 2014-2025 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Common classes and constants used by extractor modules."""

import os
import re
import ssl
import time
import netrc
import queue
import random
import getpass
import logging
import requests
import threading
from datetime import datetime
from xml.etree import ElementTree
from requests.adapters import HTTPAdapter
from .message import Message
from .. import config, output, text, util, cache, exception
urllib3 = requests.packages.urllib3


class Extractor():

    category = ""
    subcategory = ""
    basecategory = ""
    categorytransfer = False
    directory_fmt = ("{category}",)
    filename_fmt = "{filename}.{extension}"
    archive_fmt = ""
    status = 0
    root = ""
    cookies_domain = ""
    cookies_index = 0
    referer = True
    ciphers = None
    tls12 = True
    browser = None
    useragent = util.USERAGENT_FIREFOX
    request_interval = 0.0
    request_interval_min = 0.0
    request_interval_429 = 60.0
    request_timestamp = 0.0

    def __init__(self, match):
        self.log = logging.getLogger(self.category)
        self.url = match.string
        self.match = match
        self.groups = match.groups()
        self.kwdict = {}

        if self.category in CATEGORY_MAP:
            catsub = f"{self.category}:{self.subcategory}"
            if catsub in CATEGORY_MAP:
                self.category, self.subcategory = CATEGORY_MAP[catsub]
            else:
                self.category = CATEGORY_MAP[self.category]

        self._cfgpath = ("extractor", self.category, self.subcategory)
        self._parentdir = ""

    @classmethod
    def from_url(cls, url):
        if isinstance(cls.pattern, str):
            cls.pattern = util.re_compile(cls.pattern)
        match = cls.pattern.match(url)
        return cls(match) if match else None

    def __iter__(self):
        self.initialize()
        return self.items()

    def initialize(self):
        self._init_options()
        self._init_session()
        self._init_cookies()
        self._init()
        self.initialize = util.noop

    def finalize(self):
        pass

    def items(self):
        yield Message.Version, 1

    def skip(self, num):
        return 0

    def config(self, key, default=None):
        return config.interpolate(self._cfgpath, key, default)

    def config2(self, key, key2, default=None, sentinel=util.SENTINEL):
        value = self.config(key, sentinel)
        if value is not sentinel:
            return value
        return self.config(key2, default)

    def config_deprecated(self, key, deprecated, default=None,
                          sentinel=util.SENTINEL, history=set()):
        value = self.config(deprecated, sentinel)
        if value is not sentinel:
            if deprecated not in history:
                history.add(deprecated)
                self.log.warning("'%s' is deprecated. Use '%s' instead.",
                                 deprecated, key)
            default = value

        value = self.config(key, sentinel)
        if value is not sentinel:
            return value
        return default

    def config_accumulate(self, key):
        return config.accumulate(self._cfgpath, key)

    def config_instance(self, key, default=None):
        return default

    def _config_shared(self, key, default=None):
        return config.interpolate_common(
            ("extractor",), self._cfgpath, key, default)

    def _config_shared_accumulate(self, key):
        first = True
        extr = ("extractor",)

        for path in self._cfgpath:
            if first:
                first = False
                values = config.accumulate(extr + path, key)
            elif conf := config.get(extr, path[0]):
                values[:0] = config.accumulate(
                    (self.subcategory,), key, conf=conf)

        return values

    def request(self, url, method="GET", session=None, fatal=True,
                retries=None, retry_codes=None, interval=True,
                encoding=None, notfound=None, **kwargs):
        if session is None:
            session = self.session
        if retries is None:
            retries = self._retries
        if retry_codes is None:
            retry_codes = self._retry_codes
        if "proxies" not in kwargs:
            kwargs["proxies"] = self._proxies
        if "timeout" not in kwargs:
            kwargs["timeout"] = self._timeout
        if "verify" not in kwargs:
            kwargs["verify"] = self._verify

        if "json" in kwargs:
            if (json := kwargs["json"]) is not None:
                kwargs["data"] = util.json_dumps(json).encode()
                del kwargs["json"]
                if headers := kwargs.get("headers"):
                    headers["Content-Type"] = "application/json"
                else:
                    kwargs["headers"] = {"Content-Type": "application/json"}

        response = challenge = None
        tries = 1

        if self._interval and interval:
            seconds = (self._interval() -
                       (time.time() - Extractor.request_timestamp))
            if seconds > 0.0:
                self.sleep(seconds, "request")

        while True:
            try:
                response = session.request(method, url, **kwargs)
            except requests.exceptions.ConnectionError as exc:
                try:
                    reason = exc.args[0].reason
                    cls = reason.__class__.__name__
                    pre, _, err = str(reason.args[-1]).partition(":")
                    msg = f" {cls}: {(err or pre).lstrip()}"
                except Exception:
                    msg = exc
                code = 0
            except (requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                msg = exc
                code = 0
            except (requests.exceptions.RequestException) as exc:
                msg = exc
                break
            else:
                code = response.status_code
                if self._write_pages:
                    self._dump_response(response)
                if (
                    code < 400 or
                    code < 500 and (
                        not fatal and code != 429 or fatal is None) or
                    fatal is ...
                ):
                    if encoding:
                        response.encoding = encoding
                    return response
                if notfound and code == 404:
                    self.status |= exception.NotFoundError.code
                    raise exception.NotFoundError(notfound)

                msg = f"'{code} {response.reason}' for '{response.url}'"

                challenge = util.detect_challenge(response)
                if challenge is not None:
                    self.log.warning(challenge)

                if code == 429 and self._handle_429(response):
                    continue
                elif code == 429 and self._interval_429:
                    pass
                elif code not in retry_codes and code < 500:
                    break

            finally:
                Extractor.request_timestamp = time.time()

            self.log.debug("%s (%s/%s)", msg, tries, retries+1)
            if tries > retries:
                break

            seconds = tries
            if self._interval:
                s = self._interval()
                if seconds < s:
                    seconds = s
            if code == 429 and self._interval_429:
                s = self._interval_429()
                if seconds < s:
                    seconds = s
                self.wait(seconds=seconds, reason="429 Too Many Requests")
            else:
                self.sleep(seconds, "retry")
            tries += 1

        if not fatal or fatal is ...:
            self.log.warning(msg)
            return util.NullResponse(url, msg)

        if challenge is None:
            exc = exception.HttpError(msg, response)
        else:
            exc = exception.ChallengeError(challenge, response)
        self.status |= exc.code
        raise exc

    def request_location(self, url, **kwargs):
        kwargs.setdefault("method", "HEAD")
        kwargs.setdefault("allow_redirects", False)
        return self.request(url, **kwargs).headers.get("location", "")

    def request_json(self, url, **kwargs):
        response = self.request(url, **kwargs)

        try:
            return util.json_loads(response.text)
        except Exception as exc:
            fatal = kwargs.get("fatal", True)
            if not fatal or fatal is ...:
                if challenge := util.detect_challenge(response):
                    self.log.warning(challenge)
                else:
                    self.log.warning("%s: %s", exc.__class__.__name__, exc)
                return {}
            raise

    def request_xml(self, url, xmlns=True, **kwargs):
        response = self.request(url, **kwargs)

        if xmlns:
            text = response.text
        else:
            text = response.text.replace(" xmlns=", " ns=")

        parser = ElementTree.XMLParser()
        try:
            parser.feed(text)
            return parser.close()
        except Exception as exc:
            fatal = kwargs.get("fatal", True)
            if not fatal or fatal is ...:
                if challenge := util.detect_challenge(response):
                    self.log.warning(challenge)
                else:
                    self.log.warning("%s: %s", exc.__class__.__name__, exc)
                return ElementTree.Element("")
            raise

    _handle_429 = util.false

    def wait(self, seconds=None, until=None, adjust=1.0,
             reason="rate limit"):
        now = time.time()

        if seconds:
            seconds = float(seconds)
            until = now + seconds
        elif until:
            if isinstance(until, datetime):
                # convert to UTC timestamp
                until = util.datetime_to_timestamp(until)
            else:
                until = float(until)
            seconds = until - now
        else:
            raise ValueError("Either 'seconds' or 'until' is required")

        seconds += adjust
        if seconds <= 0.0:
            return

        if reason:
            t = datetime.fromtimestamp(until).time()
            isotime = f"{t.hour:02}:{t.minute:02}:{t.second:02}"
            self.log.info("Waiting until %s (%s)", isotime, reason)
        time.sleep(seconds)

    def sleep(self, seconds, reason):
        self.log.debug("Sleeping %.2f seconds (%s)",
                       seconds, reason)
        time.sleep(seconds)

    def input(self, prompt, echo=True):
        self._check_input_allowed(prompt)

        if echo:
            try:
                return input(prompt)
            except (EOFError, OSError):
                return None
        else:
            return getpass.getpass(prompt)

    def _check_input_allowed(self, prompt=""):
        input = self.config("input")
        if input is None:
            input = output.TTY_STDIN
        if not input:
            raise exception.AbortExtraction(
                f"User input required ({prompt.strip(' :')})")

    def _get_auth_info(self):
        """Return authentication information as (username, password) tuple"""
        username = self.config("username")
        password = None

        if username:
            password = self.config("password")
            if not password:
                self._check_input_allowed("password")
                password = util.LazyPrompt()

        elif self.config("netrc", False):
            try:
                info = netrc.netrc().authenticators(self.category)
                username, _, password = info
            except (OSError, netrc.NetrcParseError) as exc:
                self.log.error("netrc: %s", exc)
            except TypeError:
                self.log.warning("netrc: No authentication info")

        return username, password

    def _init(self):
        pass

    def _init_options(self):
        self._write_pages = self.config("write-pages", False)
        self._retry_codes = self.config("retry-codes")
        self._retries = self.config("retries", 4)
        self._timeout = self.config("timeout", 30)
        self._verify = self.config("verify", True)
        self._proxies = util.build_proxy_map(self.config("proxy"), self.log)
        self._interval = util.build_duration_func(
            self.config("sleep-request", self.request_interval),
            self.request_interval_min,
        )
        self._interval_429 = util.build_duration_func(
            self.config("sleep-429", self.request_interval_429),
        )

        if self._retries < 0:
            self._retries = float("inf")
        if not self._retry_codes:
            self._retry_codes = ()

    def _init_session(self):
        self.session = session = requests.Session()
        headers = session.headers
        headers.clear()
        ssl_options = ssl_ciphers = 0

        # .netrc Authorization headers are alwsays disabled
        session.trust_env = True if self.config("proxy-env", True) else False

        browser = self.config("browser")
        if browser is None:
            browser = self.browser
        if browser and isinstance(browser, str):
            browser, _, platform = browser.lower().partition(":")

            if not platform or platform == "auto":
                platform = ("Windows NT 10.0; Win64; x64"
                            if util.WINDOWS else "X11; Linux x86_64")
            elif platform == "windows":
                platform = "Windows NT 10.0; Win64; x64"
            elif platform == "linux":
                platform = "X11; Linux x86_64"
            elif platform == "macos":
                platform = "Macintosh; Intel Mac OS X 15.5"

            if browser == "chrome":
                if platform.startswith("Macintosh"):
                    platform = platform.replace(".", "_")
            else:
                browser = "firefox"

            for key, value in HEADERS[browser]:
                if value and "{}" in value:
                    headers[key] = value.replace("{}", platform)
                else:
                    headers[key] = value

            ssl_options |= (ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 |
                            ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1)
            ssl_ciphers = CIPHERS[browser]
        else:
            headers["User-Agent"] = self.useragent
            headers["Accept"] = "*/*"
            headers["Accept-Language"] = "en-US,en;q=0.5"

            ssl_ciphers = self.ciphers
            if ssl_ciphers is not None and ssl_ciphers in CIPHERS:
                ssl_ciphers = CIPHERS[ssl_ciphers]

        if BROTLI:
            headers["Accept-Encoding"] = "gzip, deflate, br"
        else:
            headers["Accept-Encoding"] = "gzip, deflate"
        if ZSTD:
            headers["Accept-Encoding"] += ", zstd"

        if referer := self.config("referer", self.referer):
            if isinstance(referer, str):
                headers["Referer"] = referer
            elif self.root:
                headers["Referer"] = self.root + "/"

        custom_ua = self.config("user-agent")
        if custom_ua is None or custom_ua == "auto":
            pass
        elif custom_ua == "browser":
            headers["User-Agent"] = _browser_useragent()
        elif self.useragent is Extractor.useragent and not self.browser or \
                custom_ua is not config.get(("extractor",), "user-agent"):
            headers["User-Agent"] = custom_ua

        if custom_headers := self.config("headers"):
            if isinstance(custom_headers, str):
                if custom_headers in HEADERS:
                    custom_headers = HEADERS[custom_headers]
                else:
                    self.log.error("Invalid 'headers' value '%s'",
                                   custom_headers)
                    custom_headers = ()
            headers.update(custom_headers)

        if custom_ciphers := self.config("ciphers"):
            if isinstance(custom_ciphers, list):
                ssl_ciphers = ":".join(custom_ciphers)
            elif custom_ciphers in CIPHERS:
                ssl_ciphers = CIPHERS[custom_ciphers]
            else:
                ssl_ciphers = custom_ciphers

        if source_address := self.config("source-address"):
            if isinstance(source_address, str):
                source_address = (source_address, 0)
            else:
                source_address = (source_address[0], source_address[1])

        tls12 = self.config("tls12")
        if tls12 is None:
            tls12 = self.tls12
        if not tls12:
            ssl_options |= ssl.OP_NO_TLSv1_2
            self.log.debug("TLS 1.2 disabled.")

        if self.config("truststore"):
            try:
                from truststore import SSLContext as ssl_ctx
            except ImportError as exc:
                self.log.error("%s: %s", exc.__class__.__name__, exc)
                ssl_ctx = None
        else:
            ssl_ctx = None

        adapter = _build_requests_adapter(
            ssl_options, ssl_ciphers, ssl_ctx, source_address)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    def _init_cookies(self):
        """Populate the session's cookiejar"""
        self.cookies = self.session.cookies
        self.cookies_file = None
        if self.cookies_domain is None:
            return

        if cookies := self.config("cookies"):
            if select := self.config("cookies-select"):
                if select == "rotate":
                    cookies = cookies[self.cookies_index % len(cookies)]
                    Extractor.cookies_index += 1
                else:
                    cookies = random.choice(cookies)
            self.cookies_load(cookies)

    def cookies_load(self, cookies_source):
        if isinstance(cookies_source, dict):
            self.cookies_update_dict(cookies_source, self.cookies_domain)

        elif isinstance(cookies_source, str):
            path = util.expand_path(cookies_source)
            try:
                with open(path) as fp:
                    cookies = util.cookiestxt_load(fp)
            except Exception as exc:
                self.log.warning("cookies: Failed to load '%s' (%s: %s)",
                                 cookies_source, exc.__class__.__name__, exc)
            else:
                self.log.debug("cookies: Loading cookies from '%s'",
                               cookies_source)
                set_cookie = self.cookies.set_cookie
                for cookie in cookies:
                    set_cookie(cookie)
                self.cookies_file = path

        elif isinstance(cookies_source, (list, tuple)):
            key = tuple(cookies_source)
            cookies = CACHE_COOKIES.get(key)

            if cookies is None:
                from ..cookies import load_cookies
                try:
                    cookies = load_cookies(cookies_source)
                except Exception as exc:
                    self.log.warning("cookies: %s", exc)
                    cookies = ()
                else:
                    CACHE_COOKIES[key] = cookies
            else:
                self.log.debug("cookies: Using cached cookies from %s", key)

            set_cookie = self.cookies.set_cookie
            for cookie in cookies:
                set_cookie(cookie)

        else:
            self.log.error(
                "cookies: Expected 'dict', 'list', or 'str' value for "
                "'cookies' option, got '%s' instead (%r)",
                cookies_source.__class__.__name__, cookies_source)

    def cookies_store(self):
        """Store the session's cookies in a cookies.txt file"""
        export = self.config("cookies-update", True)
        if not export:
            return

        if isinstance(export, str):
            path = util.expand_path(export)
        else:
            path = self.cookies_file
            if not path:
                return

        path_tmp = path + ".tmp"
        try:
            with open(path_tmp, "w") as fp:
                util.cookiestxt_store(fp, self.cookies)
            os.replace(path_tmp, path)
        except OSError as exc:
            self.log.error("cookies: Failed to write to '%s' "
                           "(%s: %s)", path, exc.__class__.__name__, exc)

    def cookies_update(self, cookies, domain=""):
        """Update the session's cookiejar with 'cookies'"""
        if isinstance(cookies, dict):
            self.cookies_update_dict(cookies, domain or self.cookies_domain)
        else:
            set_cookie = self.cookies.set_cookie
            try:
                cookies = iter(cookies)
            except TypeError:
                set_cookie(cookies)
            else:
                for cookie in cookies:
                    set_cookie(cookie)

    def cookies_update_dict(self, cookiedict, domain):
        """Update cookiejar with name-value pairs from a dict"""
        set_cookie = self.cookies.set
        for name, value in cookiedict.items():
            set_cookie(name, value, domain=domain)

    def cookies_check(self, cookies_names, domain=None, subdomains=False):
        """Check if all 'cookies_names' are in the session's cookiejar"""
        if not self.cookies:
            return False

        if domain is None:
            domain = self.cookies_domain
        names = set(cookies_names)
        now = time.time()

        for cookie in self.cookies:
            if cookie.name not in names:
                continue

            if not domain or cookie.domain == domain:
                pass
            elif not subdomains or not cookie.domain.endswith(domain):
                continue

            if cookie.expires:
                diff = int(cookie.expires - now)

                if diff <= 0:
                    self.log.warning(
                        "cookies: %s/%s expired at %s",
                        cookie.domain.lstrip("."), cookie.name,
                        datetime.fromtimestamp(cookie.expires))
                    continue

                elif diff <= 86400:
                    hours = diff // 3600
                    self.log.warning(
                        "cookies: %s/%s will expire in less than %s hour%s",
                        cookie.domain.lstrip("."), cookie.name,
                        hours + 1, "s" if hours else "")

            names.discard(cookie.name)
            if not names:
                return True
        return False

    def _extract_jsonld(self, page):
        return util.json_loads(text.extr(
            page, '<script type="application/ld+json">', "</script>"))

    def _extract_nextdata(self, page):
        return util.json_loads(text.extr(
            page, ' id="__NEXT_DATA__" type="application/json">', "</script>"))

    def _cache(self, func, maxage, keyarg=None):
        #  return cache.DatabaseCacheDecorator(func, maxage, keyarg)
        return cache.DatabaseCacheDecorator(func, keyarg, maxage)

    def _cache_memory(self, func, maxage=None, keyarg=None):
        return cache.Memcache()

    def _get_date_min_max(self, dmin=None, dmax=None):
        """Retrieve and parse 'date-min' and 'date-max' config values"""
        def get(key, default):
            ts = self.config(key, default)
            if isinstance(ts, str):
                try:
                    ts = int(datetime.strptime(ts, fmt).timestamp())
                except ValueError as exc:
                    self.log.warning("Unable to parse '%s': %s", key, exc)
                    ts = default
            return ts
        fmt = self.config("date-format", "%Y-%m-%dT%H:%M:%S")
        return get("date-min", dmin), get("date-max", dmax)

    @classmethod
    def _dump(cls, obj):
        util.dump_json(obj, ensure_ascii=False, indent=2)

    def _dump_response(self, response, history=True):
        """Write the response content to a .txt file in the current directory.

        The file name is derived from the response url,
        replacing special characters with "_"
        """
        if history:
            for resp in response.history:
                self._dump_response(resp, False)

        if hasattr(Extractor, "_dump_index"):
            Extractor._dump_index += 1
        else:
            Extractor._dump_index = 1
            Extractor._dump_sanitize = util.re_compile(
                r"[\\\\|/<>:\"?*&=#]+").sub

        fname = (f"{Extractor._dump_index:>02}_"
                 f"{Extractor._dump_sanitize('_', response.url)}")

        if util.WINDOWS:
            path = os.path.abspath(fname)[:255]
        else:
            path = fname[:251]

        try:
            with open(path + ".txt", 'wb') as fp:
                util.dump_response(
                    response, fp,
                    headers=(self._write_pages in ("all", "ALL")),
                    hide_auth=(self._write_pages != "ALL")
                )
            self.log.info("Writing '%s' response to '%s'",
                          response.url, path + ".txt")
        except Exception as e:
            self.log.warning("Failed to dump HTTP request (%s: %s)",
                             e.__class__.__name__, e)


class GalleryExtractor(Extractor):

    subcategory = "gallery"
    filename_fmt = "{category}_{gallery_id}_{num:>03}.{extension}"
    directory_fmt = ("{category}", "{gallery_id} {title}")
    archive_fmt = "{gallery_id}_{num}"
    enum = "num"

    def __init__(self, match, url=None):
        Extractor.__init__(self, match)

        if url is None and (path := self.groups[0]) and path[0] == "/":
            self.page_url = f"{self.root}{path}"
        else:
            self.page_url = url

    def items(self):
        self.login()

        if self.page_url:
            page = self.request(
                self.page_url, notfound=self.subcategory).text
        else:
            page = None

        data = self.metadata(page)
        imgs = self.images(page)
        assets = self.assets(page)

        if "count" in data:
            if self.config("page-reverse"):
                images = util.enumerate_reversed(imgs, 1, data["count"])
            else:
                images = zip(
                    range(1, data["count"]+1),
                    imgs,
                )
        else:
            enum = enumerate
            try:
                data["count"] = len(imgs)
            except TypeError:
                pass
            else:
                if self.config("page-reverse"):
                    enum = util.enumerate_reversed
            images = enum(imgs, 1)

        yield Message.Directory, data
        enum_key = self.enum

        if assets:
            for asset in assets:
                url = asset["url"]
                asset.update(data)
                asset[enum_key] = 0
                if "extension" not in asset:
                    text.nameext_from_url(url, asset)
                yield Message.Url, url, asset

        for data[enum_key], (url, imgdata) in images:
            if imgdata:
                data.update(imgdata)
                if "extension" not in imgdata:
                    text.nameext_from_url(url, data)
            else:
                text.nameext_from_url(url, data)
            yield Message.Url, url, data

    def login(self):
        """Login and set necessary cookies"""

    def metadata(self, page):
        """Return a dict with general metadata"""

    def images(self, page):
        """Return a list or iterable of all (image-url, metadata)-tuples"""

    def assets(self, page):
        """Return an iterable of additional gallery assets

        Each asset must be a 'dict' containing at least 'url' and 'type'
        """


class ChapterExtractor(GalleryExtractor):

    subcategory = "chapter"
    directory_fmt = (
        "{category}", "{manga}",
        "{volume:?v/ />02}c{chapter:>03}{chapter_minor:?//}{title:?: //}")
    filename_fmt = (
        "{manga}_c{chapter:>03}{chapter_minor:?//}_{page:>03}.{extension}")
    archive_fmt = (
        "{manga}_{chapter}{chapter_minor}_{page}")
    enum = "page"


class MangaExtractor(Extractor):

    subcategory = "manga"
    categorytransfer = True
    chapterclass = None
    reverse = True

    def __init__(self, match, url=None):
        Extractor.__init__(self, match)

        if url is None and (path := self.groups[0]) and path[0] == "/":
            self.page_url = f"{self.root}{path}"
        else:
            self.page_url = url

        if self.config("chapter-reverse", False):
            self.reverse = not self.reverse

    def items(self):
        self.login()

        if self.page_url:
            page = self.request(self.page_url, notfound=self.subcategory).text
        else:
            page = None

        chapters = self.chapters(page)
        if self.reverse:
            chapters.reverse()

        for chapter, data in chapters:
            data["_extractor"] = self.chapterclass
            yield Message.Queue, chapter, data

    def login(self):
        """Login and set necessary cookies"""

    def chapters(self, page):
        """Return a list of all (chapter-url, metadata)-tuples"""


class Dispatch():
    subcategory = "user"
    cookies_domain = None
    finalize = Extractor.finalize
    skip = Extractor.skip

    def __iter__(self):
        return self.items()

    def initialize(self):
        pass

    def _dispatch_extractors(self, extractor_data, default=(), alt=None):
        extractors = {
            data[0].subcategory: data
            for data in extractor_data
        }

        if alt is not None:
            for sub, sub_alt in alt:
                extractors[sub_alt] = extractors[sub]

        include = self.config("include", default) or ()
        if include == "all":
            include = extractors
        elif isinstance(include, str):
            include = include.replace(" ", "").split(",")

        results = [(Message.Version, 1)]
        for category in include:
            try:
                extr, url = extractors[category]
            except KeyError:
                self.log.warning("Invalid include '%s'", category)
            else:
                results.append((Message.Queue, url, {"_extractor": extr}))
        return iter(results)


class AsynchronousMixin():
    """Run info extraction in a separate thread"""

    def __iter__(self):
        self.initialize()

        messages = queue.Queue(5)
        thread = threading.Thread(
            target=self.async_items,
            args=(messages,),
            daemon=True,
        )

        thread.start()
        while True:
            msg = messages.get()
            if msg is None:
                thread.join()
                return
            if isinstance(msg, Exception):
                thread.join()
                raise msg
            yield msg
            messages.task_done()

    def async_items(self, messages):
        try:
            for msg in self.items():
                messages.put(msg)
        except Exception as exc:
            messages.put(exc)
        messages.put(None)


class BaseExtractor(Extractor):
    instances = ()

    def __init__(self, match):
        if not self.category:
            self.groups = match.groups()
            self.match = match
            self._init_category()
        Extractor.__init__(self, match)

    def _init_category(self):
        for index, group in enumerate(self.groups):
            if group is not None:
                if index:
                    self.category, self.root, info = self.instances[index-1]
                    if not self.root:
                        self.root = text.root_from_url(self.match[0])
                    self.config_instance = info.get
                else:
                    self.root = group
                    self.category = group.partition("://")[2]
                break

    @classmethod
    def update(cls, instances):
        if extra_instances := config.get(("extractor",), cls.basecategory):
            for category, info in extra_instances.items():
                if isinstance(info, dict) and "root" in info:
                    instances[category] = info

        pattern_list = []
        instance_list = cls.instances = []
        for category, info in instances.items():
            if root := info["root"]:
                root = root.rstrip("/")
            instance_list.append((category, root, info))

            pattern = info.get("pattern")
            if not pattern:
                pattern = re.escape(root[root.index(":") + 3:])
            pattern_list.append(pattern + "()")

        return (
            r"(?:" + cls.basecategory + r":(https?://[^/?#]+)|"
            r"(?:https?://)?(?:" + "|".join(pattern_list) + r"))"
        )


class RequestsAdapter(HTTPAdapter):

    def __init__(self, ssl_context=None, source_address=None):
        self.ssl_context = ssl_context
        self.source_address = source_address
        HTTPAdapter.__init__(self)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        kwargs["source_address"] = self.source_address
        return HTTPAdapter.init_poolmanager(self, *args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        kwargs["source_address"] = self.source_address
        return HTTPAdapter.proxy_manager_for(self, *args, **kwargs)


def _build_requests_adapter(
        ssl_options, ssl_ciphers, ssl_ctx, source_address):

    key = (ssl_options, ssl_ciphers, ssl_ctx, source_address)
    try:
        return CACHE_ADAPTERS[key]
    except KeyError:
        pass

    if ssl_options or ssl_ciphers or ssl_ctx:
        if ssl_ctx is None:
            ssl_context = urllib3.connection.create_urllib3_context(
                options=ssl_options or None, ciphers=ssl_ciphers)
            if not requests.__version__ < "2.32":
                # https://github.com/psf/requests/pull/6731
                ssl_context.load_verify_locations(requests.certs.where())
        else:
            ssl_ctx_orig = urllib3.util.ssl_.SSLContext
            try:
                urllib3.util.ssl_.SSLContext = ssl_ctx
                ssl_context = urllib3.connection.create_urllib3_context(
                    options=ssl_options or None, ciphers=ssl_ciphers)
            finally:
                urllib3.util.ssl_.SSLContext = ssl_ctx_orig
        ssl_context.check_hostname = False
    else:
        ssl_context = None

    adapter = CACHE_ADAPTERS[key] = RequestsAdapter(
        ssl_context, source_address)
    return adapter


@cache.cache(maxage=86400)
def _browser_useragent():
    """Get User-Agent header from default browser"""
    import webbrowser
    import socket

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    host, port = server.getsockname()
    webbrowser.open(f"http://{host}:{port}/user-agent")

    client = server.accept()[0]
    server.close()

    for line in client.recv(1024).split(b"\r\n"):
        key, _, value = line.partition(b":")
        if key.strip().lower() == b"user-agent":
            useragent = value.strip()
            break
    else:
        useragent = b""

    client.send(b"HTTP/1.1 200 OK\r\n\r\n" + useragent)
    client.close()

    return useragent.decode()


CACHE_ADAPTERS = {}
CACHE_COOKIES = {}
CATEGORY_MAP = ()


HEADERS_FIREFOX_140 = (
    ("User-Agent", "Mozilla/5.0 ({}; rv:140.0) Gecko/20100101 Firefox/140.0"),
    ("Accept", "text/html,application/xhtml+xml,"
               "application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "en-US,en;q=0.5"),
    ("Accept-Encoding", None),
    ("Connection", "keep-alive"),
    ("Content-Type", None),
    ("Content-Length", None),
    ("Referer", None),
    ("Origin", None),
    ("Cookie", None),
    ("Sec-Fetch-Dest", "empty"),
    ("Sec-Fetch-Mode", "cors"),
    ("Sec-Fetch-Site", "same-origin"),
    ("TE", "trailers"),
)
HEADERS_FIREFOX_128 = (
    ("User-Agent", "Mozilla/5.0 ({}; rv:128.0) Gecko/20100101 Firefox/128.0"),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8"),
    ("Accept-Language", "en-US,en;q=0.5"),
    ("Accept-Encoding", None),
    ("Referer", None),
    ("Connection", "keep-alive"),
    ("Upgrade-Insecure-Requests", "1"),
    ("Cookie", None),
    ("Sec-Fetch-Dest", "empty"),
    ("Sec-Fetch-Mode", "no-cors"),
    ("Sec-Fetch-Site", "same-origin"),
    ("TE", "trailers"),
)
HEADERS_CHROMIUM_138 = (
    ("Connection", "keep-alive"),
    ("sec-ch-ua", '"Not)A;Brand";v="8", "Chromium";v="138"'),
    ("sec-ch-ua-mobile", "?0"),
    ("sec-ch-ua-platform", '"Linux"'),
    ("Upgrade-Insecure-Requests", "1"),
    ("User-Agent", "Mozilla/5.0 ({}) AppleWebKit/537.36 (KHTML, "
                   "like Gecko) Chrome/138.0.0.0 Safari/537.36"),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    ("Referer", None),
    ("Sec-Fetch-Site", "same-origin"),
    ("Sec-Fetch-Mode", "no-cors"),
    #  ("Sec-Fetch-User", "?1"),
    ("Sec-Fetch-Dest", "empty"),
    ("Accept-Encoding", None),
    ("Accept-Language", "en-US,en;q=0.9"),
)
HEADERS_CHROMIUM_111 = (
    ("Connection", "keep-alive"),
    ("Upgrade-Insecure-Requests", "1"),
    ("User-Agent", "Mozilla/5.0 ({}) AppleWebKit/537.36 (KHTML, "
                   "like Gecko) Chrome/111.0.0.0 Safari/537.36"),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    ("Referer", None),
    ("Sec-Fetch-Site", "same-origin"),
    ("Sec-Fetch-Mode", "no-cors"),
    ("Sec-Fetch-Dest", "empty"),
    ("Accept-Encoding", None),
    ("Accept-Language", "en-US,en;q=0.9"),
    ("cookie", None),
    ("content-length", None),
)
HEADERS = {
    "firefox"    : HEADERS_FIREFOX_140,
    "firefox/140": HEADERS_FIREFOX_140,
    "firefox/128": HEADERS_FIREFOX_128,
    "chrome"     : HEADERS_CHROMIUM_138,
    "chrome/138" : HEADERS_CHROMIUM_138,
    "chrome/111" : HEADERS_CHROMIUM_111,
}

CIPHERS_FIREFOX = (
    "TLS_AES_128_GCM_SHA256:"
    "TLS_CHACHA20_POLY1305_SHA256:"
    "TLS_AES_256_GCM_SHA384:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-CHACHA20-POLY1305:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:"
    "ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-AES256-SHA:"
    "ECDHE-ECDSA-AES128-SHA:"
    "ECDHE-RSA-AES128-SHA:"
    "ECDHE-RSA-AES256-SHA:"
    "AES128-GCM-SHA256:"
    "AES256-GCM-SHA384:"
    "AES128-SHA:"
    "AES256-SHA"
)
CIPHERS_CHROMIUM = (
    "TLS_AES_128_GCM_SHA256:"
    "TLS_AES_256_GCM_SHA384:"
    "TLS_CHACHA20_POLY1305_SHA256:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:"
    "ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-AES128-SHA:"
    "ECDHE-RSA-AES256-SHA:"
    "AES128-GCM-SHA256:"
    "AES256-GCM-SHA384:"
    "AES128-SHA:"
    "AES256-SHA"
)
CIPHERS = {
    "firefox"    : CIPHERS_FIREFOX,
    "firefox/140": CIPHERS_FIREFOX,
    "firefox/128": CIPHERS_FIREFOX,
    "chrome"     : CIPHERS_CHROMIUM,
    "chrome/138" : CIPHERS_CHROMIUM,
    "chrome/111" : CIPHERS_CHROMIUM,
}


# disable Basic Authorization header injection from .netrc data
try:
    requests.sessions.get_netrc_auth = lambda _: None
except Exception:
    pass

# detect brotli support
try:
    BROTLI = urllib3.response.brotli is not None
except AttributeError:
    BROTLI = False

# detect zstandard support
try:
    ZSTD = urllib3.response.HAS_ZSTD
except AttributeError:
    ZSTD = False

# set (urllib3) warnings filter
action = config.get((), "warnings", "default")
if action:
    try:
        import warnings
        warnings.simplefilter(action, urllib3.exceptions.HTTPWarning)
    except Exception:
        pass
del action
