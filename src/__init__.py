import os
import re
import time
import random
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from queue import Queue, Empty
from urllib.parse import parse_qs, urlparse, unquote, urlencode
from urllib.request import Request, urlopen

from calibre import random_user_agent
from calibre.ebooks.metadata import check_isbn
from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.sources.base import Source, Option
from calibre.ebooks.BeautifulSoup import BeautifulSoup
from bs4 import Tag

DOUBAN_BOOK_BASE = "https://book.douban.com/"
DOUBAN_SEARCH_JSON_URL = "https://www.douban.com/j/search"
DOUBAN_SEARCH_URL = "https://www.douban.com/search"
DOUBAN_BOOK_URL = 'https://book.douban.com/subject/%s/'
DOUBAN_BOOK_CAT = "1001"
DOUBAN_CONCURRENCY_SIZE = 5  # 并发查询数
DOUBAN_DELAY_RANGE = (0.5, 1.5)
DOUBAN_BOOK_URL_PATTERN = re.compile(".*/subject/(\\d+)/?")
PROVIDER_NAME = "New Douban Books"
PROVIDER_ID = "new_douban"
PROVIDER_VERSION = (3, 0, 0)
PROVIDER_AUTHOR = 'bai0012'


class DoubanBookSearcher:

    def __init__(self, max_workers, douban_delay_enable, douban_login_cookie, douban_debug_enable=False):
        self.book_parser = DoubanBookHtmlParser()
        self.max_workers = max_workers
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='douban_async')
        self.douban_delay_enable = douban_delay_enable
        self.douban_debug_enable = bool(douban_debug_enable)
        self.douban_login_cookie = self.normalize_login_cookie(douban_login_cookie)

    def debug(self, log, message):
        if self.douban_debug_enable:
            log.info(f'[douban-debug] {message}')

    def normalize_login_cookie(self, cookie_text):
        if not cookie_text:
            return None
        cookie_text = str(cookie_text).strip()
        if not cookie_text:
            return None
        cookie_text = self.load_cookie_text(cookie_text)
        netscape_cookie = self.parse_netscape_cookie(cookie_text)
        if netscape_cookie:
            return netscape_cookie
        if cookie_text.lower().startswith('cookie:'):
            cookie_text = cookie_text.split(':', 1)[1].strip()
        return '; '.join(line.strip() for line in cookie_text.splitlines() if line.strip())

    def load_cookie_text(self, cookie_text):
        if '\n' in cookie_text or '\r' in cookie_text:
            return cookie_text
        cookie_path = os.path.expandvars(os.path.expanduser(cookie_text))
        try:
            if os.path.isfile(cookie_path):
                with open(cookie_path, 'r', encoding='utf-8') as cookie_file:
                    return cookie_file.read()
        except OSError:
            pass
        return cookie_text

    def parse_netscape_cookie(self, cookie_text):
        cookies = {}
        now = int(time.time())
        for line in cookie_text.splitlines():
            line = line.strip()
            if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):
                continue
            parts = line.split('\t')
            if len(parts) != 7:
                parts = line.split(None, 6)
            if len(parts) != 7:
                continue
            domain, _, _, _, expires, name, value = parts
            if domain.startswith('#HttpOnly_'):
                domain = domain[len('#HttpOnly_'):]
            domain = domain.lower()
            normalized_domain = domain.lstrip('.')
            if normalized_domain != 'douban.com' and not normalized_domain.endswith('.douban.com'):
                continue
            try:
                expires_at = int(expires)
            except ValueError:
                expires_at = 0
            if expires_at and expires_at < now:
                continue
            cookies[name] = value
        if cookies:
            return '; '.join(f'{name}={value}' for name, value in cookies.items())
        return None

    def calc_url(self, href, log=None):
        if not href:
            return None
        query = urlparse(href).query
        params = parse_qs(query)
        url = params.get('url', [href])[0]
        url = unquote(url)
        if DOUBAN_BOOK_URL_PATTERN.match(url):
            return url
        if log:
            self.debug(log, f'Ignored non-book search result url: {href}')

    def open_url(self, url, log, timeout=30):
        try:
            start_time = time.time()
            self.debug(log, f'Opening url: {url}, timeout={timeout}, cookie_enabled={bool(self.douban_login_cookie)}')
            res = urlopen(Request(url, headers=self.get_headers(), method='GET'), timeout=timeout)
            self.debug(log, 'Response status: {}, url: {}, time={:.0f}ms'.format(
                getattr(res, 'status', None), url, (time.time() - start_time) * 1000
            ))
            return res
        except Exception as e:
            log.info(f'Download failed: {url}, error: {e}')
            return None

    def load_book_urls_new(self, query, log, timeout=30):
        params = {"cat": DOUBAN_BOOK_CAT, "q": query}
        url = DOUBAN_SEARCH_URL + "?" + urlencode(params)
        log.info(f'Load books by search url: {url}')
        res = self.open_url(url, log, timeout=timeout)
        book_urls = []
        if res is not None and res.status in [200, 201]:
            html_content = self.get_res_content(res)
            self.debug(log, f'Search page html length: {len(html_content)}')
            if self.is_prohibited(html_content, log):
                return book_urls
            html = BeautifulSoup(html_content)
            alist = html.select('a.nbg')
            self.debug(log, f'Search result link count: {len(alist)}')
            for link in alist:
                href = link.get('href', '')
                parsed = self.calc_url(href, log)
                if parsed:
                    if len(book_urls) < self.max_workers:
                        book_urls.append(parsed)
            self.debug(log, f'Parsed book urls: {book_urls}')
        elif res is not None:
            self.debug(log, f'Search request returned unexpected status: {res.status}')
        return book_urls

    def search_books(self, query, log, timeout=30):
        if not query:
            self.debug(log, 'Skip empty search query')
            return []
        book_urls = self.load_book_urls_new(query, log, timeout=timeout)
        self.debug(log, f'Search query "{query}" produced {len(book_urls)} book url(s)')
        books = []
        futures = [self.thread_pool.submit(self.load_book, book_url, log, timeout) for book_url in book_urls]
        for future in as_completed(futures):
            try:
                book = future.result()
            except Exception as e:
                log.info(f'Load book task failed: {e}')
                continue
            if self.is_valid_book(book):
                books.append(book)
        self.debug(log, f'Search query "{query}" produced {len(books)} valid book(s)')
        return books

    def load_book(self, url, log, timeout=30):
        book = None
        start_time = time.time()
        if self.douban_delay_enable:
            self.random_sleep(log)
        res = self.open_url(url, log, timeout=timeout)
        if res is not None and res.status in [200, 201]:
            book_detail_content = self.get_res_content(res)
            self.debug(log, f'Book page html length: {len(book_detail_content)}, url: {url}')
            if self.is_prohibited(book_detail_content, log):
                return
            log.info("Downloaded:{} Successful,Time {:.0f}ms".format(url, (time.time() - start_time) * 1000))
            try:
                book = self.book_parser.parse_book(url, book_detail_content)
                if not self.is_valid_book(book):
                    log.info(f"Parse book content error: title not found, url: {url}")
                    self.debug(log, f"Parse book content: {book_detail_content[:2000]}")
                else:
                    self.debug(log, f"Parsed book fields: {sorted(book.keys())}, id={book.get('id')}")
            except Exception as e:
                log.info(f"Parse book content error: {e}, url: {url}")
                self.debug(log, f"Parse book content: {book_detail_content[:2000]}")
        elif res is not None:
            self.debug(log, f'Book request returned unexpected status: {res.status}, url: {url}')
        return book

    def is_valid_book(self, book):
        return book is not None and book.get('title', None)

    def is_prohibited(self, html_content, log):
        prohibited = html_content is not None and '<title>禁止访问</title>' in html_content
        if prohibited:
            html = BeautifulSoup(html_content)
            html_content = html.select_one('div#content')
            log.info(f'Douban网页访问失败：{html_content}')
        return prohibited

    def get_res_content(self, res):
        encoding = res.info().get('Content-Encoding')
        if encoding == 'gzip':
            res_content = gzip.decompress(res.read())
        else:
            res_content = res.read()
        charset = res.headers.get_content_charset() or 'utf-8'
        return res_content.decode(charset, errors='replace')

    def get_headers(self):
        headers = {'User-Agent': random_user_agent(), 'Accept-Encoding': 'gzip, deflate'}
        if self.douban_login_cookie:
            headers['Cookie'] = self.douban_login_cookie
        return headers

    def random_sleep(self, log):
        random_sec = random.uniform(*DOUBAN_DELAY_RANGE)
        log.info("Random sleep time {}s".format(random_sec))
        time.sleep(random_sec)


class DoubanBookHtmlParser:
    def __init__(self):
        self.id_pattern = DOUBAN_BOOK_URL_PATTERN
        self.tag_pattern = re.compile("criteria = '(.+)'")

    def parse_book(self, url, book_content):
        book = {}
        html = BeautifulSoup(book_content)
        if html is None or html.select is None:  # html判空处理
            return None
        title_element = html.select("span[property='v:itemreviewed']")
        book['title'] = self.get_text(title_element)
        share_element = html.select("a[data-url]")
        if len(share_element):
            url = share_element[0].get('data-url')
        book['url'] = url
        id_match = self.id_pattern.match(url)
        if id_match:
            book['id'] = id_match.group(1)
        else:
            return None
        img_element = html.select("a.nbg")
        book['cover'] = ''
        if len(img_element):
            cover = img_element[0].get('href', '')
            if not cover or cover.endswith('update_image'):
                book['cover'] = ''
            else:
                book['cover'] = cover
        rating_element = html.select("strong[property='v:average']")
        book['rating'] = self.get_rating(rating_element)
        elements = html.select("span.pl")
        book['authors'] = []
        book['translators'] = []
        book['publisher'] = ''
        book['publishedDate'] = ''
        book['isbn'] = ''
        book['series'] = ''
        for element in elements:
            text = self.get_text(element)
            parent_ele = element.find_parent()
            if text.startswith("作者"):
                book['authors'].extend([self.get_text(author_element) for author_element in
                                        filter(self.author_filter, parent_ele.select("a"))])
            elif text.startswith("译者"):
                book['translators'].extend([self.get_text(translator_element) for translator_element in
                                            filter(self.author_filter, parent_ele.select("a"))])
            elif text.startswith("出版社"):
                book['publisher'] = self.get_tail(element)
            elif text.startswith("副标题"):
                book['title'] = book['title'] + ':' + self.get_tail(element)
            elif text.startswith("出版年"):
                book['publishedDate'] = self.get_tail(element)
            elif text.startswith("ISBN"):
                book['isbn'] = self.get_tail(element)
            elif text.startswith("丛书"):
                book['series'] = self.get_text(element.find_next_sibling())
        summary_element = html.select("div#link-report div.intro")
        book['description'] = ''
        if len(summary_element):
            book['description'] = str(summary_element[-1])
        book['tags'] = self.get_tags(book_content)
        book['source'] = {
            "id": PROVIDER_ID,
            "description": PROVIDER_NAME,
            "link": DOUBAN_BOOK_BASE
        }
        book['language'] = self.get_book_language(book['title'])
        return book

    def get_book_language(self, title):
        pattern = r'^[a-zA-Z\-_]+$'
        if title and ('英文版' in title or bool(re.match(pattern, title))):
            return 'en_US'
        return 'zh_CN'

    def get_tags(self, book_content):
        tag_match = self.tag_pattern.findall(book_content)
        if len(tag_match):
            return [tag.replace('7:', '') for tag in
                    filter(lambda tag: tag and tag.startswith('7:'), tag_match[0].split('|'))]
        return []

    def get_rating(self, rating_element):
        try:
            return float(self.get_text(rating_element, '0')) / 2
        except ValueError:
            return 0

    def author_filter(self, a_element):
        a_href = a_element.get('href', '')
        return '/author' in a_href or '/search' in a_href

    def get_text(self, element, default_str=''):
        text = default_str
        if isinstance(element, Tag):
            text = element.get_text(strip=True)
        elif element and len(element) and isinstance(element[0], Tag):
            text = element[0].get_text(strip=True)
        return text if text else default_str

    def get_tail(self, element, default_str=''):
        text = default_str
        if isinstance(element, Tag) and element.next_siblings:
            for next_sibling in element.next_siblings:
                if isinstance(next_sibling, str):
                    text += next_sibling.strip()
                elif isinstance(next_sibling, Tag):
                    if not text:
                        text = self.get_text(next_sibling, default_str)
                    break
        return text if text else default_str


class NewDoubanBooks(Source):
    name = 'New Douban Books'  # Name of the plugin
    description = 'Downloads metadata and covers from Douban Books web site.'
    supported_platforms = ['windows', 'osx', 'linux']  # Platforms this plugin will run on
    author = PROVIDER_AUTHOR  # The author of this plugin
    version = PROVIDER_VERSION  # The version number of this plugin
    minimum_calibre_version = (5, 0, 0)
    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset([
        'title', 'authors', 'tags', 'pubdate', 'comments', 'publisher',
        'identifier:isbn', 'rating', 'identifier:' + PROVIDER_ID, 'languages',
        'series'
    ])
    book_searcher = None
    options = (
        # name, type, default, label, default, choices
        # type 'number', 'string', 'bool', 'choices'
        Option(
            'douban_concurrency_size', 'number', DOUBAN_CONCURRENCY_SIZE,
            _('Douban concurrency size:'),
            _('The number of douban concurrency cannot be too high!')
        ),
        Option(
            'add_translator_to_author', 'bool', True,
            _('Add translator to author'),
            _('If selected, translator will be written to metadata as author')
        ),
        Option(
            'douban_delay_enable', 'bool', True,
            _('douban random delay'),
            _('Random delay for a period of time before request')
        ),
        Option(
            'douban_search_with_author', 'bool', True,
            _('search with authors'),
            _('add authors to search keywords')
        ),
        Option(
            'douban_debug_enable', 'bool', False,
            _('douban debug logging'),
            _('Output detailed logs for troubleshooting Douban metadata download')
        ),
        Option(
            'douban_login_cookie', 'string', None,
            _('douban login cookie'),
            _('Browser Cookie header, Netscape cookie text, or Netscape cookie file path after login')
        ),
    )

    def __init__(self, *args, **kwargs):
        Source.__init__(self, *args, **kwargs)
        concurrency_size = self.get_concurrency_size()
        douban_delay_enable = bool(self.prefs.get('douban_delay_enable'))
        self.douban_debug_enable = bool(self.prefs.get('douban_debug_enable'))
        douban_login_cookie = self.prefs.get('douban_login_cookie')
        self.douban_search_with_author = bool(self.prefs.get('douban_search_with_author'))
        self.book_searcher = DoubanBookSearcher(
            concurrency_size, douban_delay_enable, douban_login_cookie, self.douban_debug_enable
        )

    def debug(self, log, message):
        if self.douban_debug_enable:
            log.info(f'[douban-debug] {message}')

    def get_concurrency_size(self):
        try:
            concurrency_size = int(self.prefs.get('douban_concurrency_size') or DOUBAN_CONCURRENCY_SIZE)
        except (TypeError, ValueError):
            concurrency_size = DOUBAN_CONCURRENCY_SIZE
        return max(1, concurrency_size)

    def get_book_url(self, identifiers):  # {{{
        douban_id = identifiers.get(PROVIDER_ID, None)
        if douban_id is None:
            douban_id = identifiers.get('douban', None)
        if douban_id is not None:
            return PROVIDER_ID, douban_id, DOUBAN_BOOK_URL % douban_id

    def download_cover(
            self,
            log,
            result_queue,
            abort,
            title=None,
            authors=None,
            identifiers={},
            timeout=30,
            get_best_cover=False):
        self.debug(log, 'download_cover title={!r}, authors={!r}, identifiers={}, timeout={}, get_best_cover={}'.format(
            title, authors, identifiers, timeout, get_best_cover
        ))
        cached_url = self.get_cached_cover_url(identifiers)
        self.debug(log, f'Initial cached cover url: {cached_url}')
        if cached_url is None:
            log.info('No cached cover found, running identify')
            rq = Queue()
            self.identify(
                log,
                rq,
                abort,
                title=title,
                authors=authors,
                identifiers=identifiers
            )
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(
                key=self.identify_results_keygen(
                    title=title, authors=authors, identifiers=identifiers
                )
            )
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
            self.debug(log, f'Cover url after identify: {cached_url}')
        if cached_url is None:
            log.info('No cover found')
            return
        br = self.browser
        log('Downloading cover from:', cached_url)
        try:
            if self.book_searcher.douban_login_cookie:
                br = br.clone_browser()
                br.set_current_header('Cookie', self.book_searcher.douban_login_cookie)
                self.debug(log, 'Using configured Douban login cookie for cover request')
            br.set_current_header('Referer', DOUBAN_BOOK_BASE)
            cdata = br.open_novisit(cached_url, timeout=timeout).read()
            if cdata:
                self.debug(log, f'Downloaded cover bytes: {len(cdata)}')
                result_queue.put((self, cdata))
        except:
            log.exception('Failed to download cover from:', cached_url)

    def get_cached_cover_url(self, identifiers):  # {{{
        url = None
        db = identifiers.get(PROVIDER_ID, None)
        if db is None:
            isbn = identifiers.get('isbn', None)
            if isbn is not None:
                db = self.cached_isbn_to_identifier(isbn)
        if db is not None:
            url = self.cached_identifier_to_cover_url(db)

        return url

    def identify(
            self,
            log,
            result_queue,
            abort,
            title=None,
            authors=None,  # {{{
            identifiers={},
            timeout=30):
        add_translator_to_author = self.prefs.get(
            'add_translator_to_author')

        isbn = check_isbn(identifiers.get('isbn', None))
        new_douban = self.get_book_url(identifiers)
        self.debug(log, 'identify title={!r}, authors={!r}, identifiers={}, isbn={!r}, timeout={}, cookie_enabled={}'.format(
            title, authors, identifiers, isbn, timeout, bool(self.book_searcher.douban_login_cookie)
        ))
        if new_douban:
            # 如果有new_douban的id，直接精确获取数据
            log.info(f'Load book by {PROVIDER_ID}:{new_douban[1]}')
            book = self.book_searcher.load_book(new_douban[2], log, timeout=timeout)
            books = []
            if self.book_searcher.is_valid_book(book):
                books.append(book)
            self.debug(log, f'Loaded by identifier, valid books: {len(books)}')
        else:
            search_keyword = title
            if self.douban_search_with_author and title and authors:
                authors_str = ','.join(authors)
                search_keyword = f'{title} {authors_str}'
            self.debug(log, f'Primary search keyword: {isbn or search_keyword!r}')
            books = self.book_searcher.search_books(isbn or search_keyword, log, timeout=timeout)
            if not len(books) and title and (isbn or search_keyword != title):
                self.debug(log, f'Fallback search keyword: {title!r}')
                books = self.book_searcher.search_books(title, log, timeout=timeout)  # 用isbn或者title+auther没有数据，用title重新搜一遍
        self.debug(log, f'identify produced {len(books)} book candidate(s)')
        for book in books:
            ans = self.to_metadata(book, add_translator_to_author, log)
            if isinstance(ans, Metadata):
                db = ans.identifiers[PROVIDER_ID]
                if ans.isbn:
                    self.cache_isbn_to_identifier(ans.isbn, db)
                if ans.cover:
                    self.cache_identifier_to_cover_url(db, ans.cover)
                self.clean_downloaded_metadata(ans)
                self.debug(log, f'Queue metadata title={ans.title!r}, identifiers={ans.identifiers}, cover={bool(ans.cover)}')
                result_queue.put(ans)

    def to_metadata(self, book, add_translator_to_author, log):
        if book and book.get('title') and book.get('id'):
            authors = book.get('authors', [])
            if add_translator_to_author:
                authors = authors + book.get('translators', [])
            mi = Metadata(book['title'], authors)
            mi.identifiers = {PROVIDER_ID: book['id']}
            mi.url = book.get('url')
            mi.cover = book.get('cover', None)
            mi.publisher = book.get('publisher', '')
            pubdate = book.get('publishedDate', None)
            if pubdate:
                try:
                    if re.compile('^\\d{4}-\\d+$').match(pubdate):
                        mi.pubdate = datetime.strptime(pubdate, '%Y-%m')
                    elif re.compile('^\\d{4}-\\d+-\\d+$').match(pubdate):
                        mi.pubdate = datetime.strptime(pubdate, '%Y-%m-%d')
                except:
                    log.error('Failed to parse pubdate %r' % pubdate)
            mi.comments = book.get('description', '')
            mi.tags = book.get('tags', [])
            mi.rating = book.get('rating', 0)
            mi.isbn = book.get('isbn', '')
            mi.series = book.get('series', '')
            mi.language = book.get('language', 'zh_CN')
            self.debug(log, f'parsed book: {book}')
            return mi


if __name__ == "__main__":
    # To run these test use: calibre-debug -e ./__init__.py
    from calibre.ebooks.metadata.sources.test import (
        test_identify_plugin, title_test, authors_test
    )

    test_identify_plugin(
        NewDoubanBooks.name, [
            ({
                 'identifiers': {
                     'isbn': '9787111544937'
                 },
                 'title': '深入理解计算机系统（原书第3版）'
             }, [title_test('深入理解计算机系统（原书第3版）', exact=True),
                 authors_test(['randal e.bryant', "david o'hallaron", '贺莲', '龚奕利'])]),
            ({
                 'title': '凤凰架构'
             }, [title_test('凤凰架构:构建可靠的大型分布式系统', exact=True),
                 authors_test(['周志明'])])
        ]
    )
