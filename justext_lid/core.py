# -*- coding: utf-8 -*-

"""
Copyright (c) 2011 Jan Pomikalek

This software is licensed as described in the file LICENSE.rst.
"""

from __future__ import absolute_import
from __future__ import division, print_function, unicode_literals

import re
import lxml.html
import lxml.sax

from lxml.html.clean import Cleaner
from xml.sax.handler import ContentHandler
from .paragraph import Paragraph
from ._compat import unicode, ignored
from .utils import is_blank, get_stoplist, get_stoplists
from ftlangdetect import detect


MAX_LINK_DENSITY_DEFAULT = 0.2
LENGTH_LOW_DEFAULT = 70
LENGTH_HIGH_DEFAULT = 200
STOPWORDS_LOW_DEFAULT = 0.30
STOPWORDS_HIGH_DEFAULT = 0.32
NO_HEADINGS_DEFAULT = False
# Short and near-good headings within MAX_HEADING_DISTANCE characters before
# a good paragraph are classified as good unless --no-headings is specified.
MAX_HEADING_DISTANCE_DEFAULT = 200
PARAGRAPH_TAGS = [
    'body', 'blockquote', 'caption', 'center', 'col', 'colgroup', 'dd',
    'div', 'dl', 'dt', 'fieldset', 'form', 'legend', 'optgroup', 'option',
    'p', 'pre', 'table', 'td', 'textarea', 'tfoot', 'th', 'thead', 'tr',
    'ul', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
]
DEFAULT_ENCODING = 'utf8'
DEFAULT_ENC_ERRORS = 'replace'
CHARSET_META_TAG_PATTERN = re.compile(br"""<meta[^>]+charset=["']?([^'"/>\s]+)""", re.IGNORECASE)

ANY_URL_REGEX = r"""(?i)\b((?:[a-z][\w-]+:(?:/{1,3}|[a-z0-9%])|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'".,<>?«»“”‘’]))"""
ANY_URL_REGEX = re.compile(ANY_URL_REGEX, re.IGNORECASE)
LANGID_TH_DEFAULT = 0.3


class JustextError(Exception):
    "Base class for jusText exceptions."


class JustextInvalidOptions(JustextError):
    pass


def html_to_dom(html, default_encoding=DEFAULT_ENCODING, encoding=None, errors=DEFAULT_ENC_ERRORS):
    """Converts HTML to DOM."""
    if isinstance(html, unicode):
        decoded_html = html
        # encode HTML for case it's XML with encoding declaration
        forced_encoding = encoding if encoding else default_encoding
        html = html.encode(forced_encoding, errors)
    else:
        decoded_html = decode_html(html, default_encoding, encoding, errors)

    try:
        dom = lxml.html.fromstring(decoded_html, parser=lxml.html.HTMLParser())
    except ValueError:
        # Unicode strings with encoding declaration are not supported.
        # for XHTML files with encoding declaration, use the declared encoding
        dom = lxml.html.fromstring(html, parser=lxml.html.HTMLParser())

    return dom


def decode_html(html, default_encoding=DEFAULT_ENCODING, encoding=None, errors=DEFAULT_ENC_ERRORS):
    """
    Converts a `html` containing an HTML page into Unicode.
    Tries to guess character encoding from meta tag.
    """
    if isinstance(html, unicode):
        return html

    if encoding:
        return html.decode(encoding, errors)

    match = CHARSET_META_TAG_PATTERN.search(html)
    if match:
        declared_encoding = match.group(1).decode("ASCII")
        # proceed unknown encoding as if it wasn't found at all
        with ignored(LookupError):
            return html.decode(declared_encoding, errors)

    # unknown encoding
    try:
        # try UTF-8 first
        return html.decode("utf8")
    except UnicodeDecodeError:
        # try lucky with default encoding
        try:
            return html.decode(default_encoding, errors)
        except UnicodeDecodeError as e:
            raise JustextError("Unable to decode the HTML to Unicode: " + unicode(e))


def preprocessor(dom):
    "Removes unwanted parts of DOM."
    options = {
        "processing_instructions": False,
        "remove_unknown_tags": False,
        "safe_attrs_only": False,
        "page_structure": False,
        "annoying_tags": False,
        "frames": False,
        "meta": False,
        "links": False,
        "javascript": False,
        "scripts": True,
        "comments": True,
        "style": True,
        "embedded": True,
        "forms": True,
        "kill_tags": ("head",),
    }
    cleaner = Cleaner(**options)

    return cleaner.clean_html(dom)


class ParagraphMaker(ContentHandler):
    """
    A class for converting a HTML page represented as a DOM object into a list
    of paragraphs.
    """

    @classmethod
    def make_paragraphs(cls, root):
        """Converts DOM into paragraphs."""
        handler = cls()
        lxml.sax.saxify(root, handler)
        return handler.paragraphs

    def __init__(self):
        self.path = PathInfo()
        self.paragraphs = []
        self.paragraph = None
        self.link = False
        self.br = False
        self._start_new_pragraph()

    def _start_new_pragraph(self):
        if self.paragraph and self.paragraph.contains_text():
            self.paragraphs.append(self.paragraph)

        self.paragraph = Paragraph(self.path)

    def startElementNS(self, name, qname, attrs):
        name = name[1]
        self.path.append(name)

        if name in PARAGRAPH_TAGS or (name == "br" and self.br):
            if name == "br":
                # the <br><br> is a paragraph separator and should
                # not be included in the number of tags within the
                # paragraph
                self.paragraph.tags_count -= 1
            self._start_new_pragraph()
        else:
            self.br = bool(name == "br")
            if name == 'a':
                self.link = True
            self.paragraph.tags_count += 1

    def endElementNS(self, name, qname):
        name = name[1]
        self.path.pop()

        if name in PARAGRAPH_TAGS:
            self._start_new_pragraph()
        if name == 'a':
            self.link = False

    def endDocument(self):
        self._start_new_pragraph()

    def characters(self, content):
        if is_blank(content):
            return

        text = self.paragraph.append_text(content)

        if self.link:
            self.paragraph.chars_count_in_links += len(text)
        self.br = False


class PathInfo(object):
    def __init__(self):
        # list of triples (tag name, order, children)
        self._elements = []

    @property
    def dom(self):
        return ".".join(e[0] for e in self._elements)

    @property
    def xpath(self):
        return "/" + "/".join("%s[%d]" % e[:2] for e in self._elements)

    def append(self, tag_name):
        children = self._get_children()
        order = children.get(tag_name, 0) + 1
        children[tag_name] = order

        xpath_part = (tag_name, order, {})
        self._elements.append(xpath_part)

        return self

    def _get_children(self):
        if not self._elements:
            return {}

        return self._elements[-1][2]

    def pop(self):
        self._elements.pop()
        return self


def classify_paragraphs(paragraphs, stoplist=None, length_low=LENGTH_LOW_DEFAULT,
        length_high=LENGTH_HIGH_DEFAULT, stopwords_low=STOPWORDS_LOW_DEFAULT,
        stopwords_high=STOPWORDS_HIGH_DEFAULT, max_link_density=MAX_LINK_DENSITY_DEFAULT,
        no_headings=NO_HEADINGS_DEFAULT, use_langid=True, langs=None, langid_th=LANGID_TH_DEFAULT):
    "Context-free paragraph classification."

    if not use_langid:
        stoplist = frozenset(w.lower() for w in stoplist)
    for paragraph in paragraphs:
        length = len(paragraph)
        if use_langid:
            text_to_detect = re.sub(ANY_URL_REGEX, '', paragraph.text).replace('\n', '')
            lang_id_output = detect(text=text_to_detect, low_memory=False)
            paragraph.lang = lang_id_output['lang']
            langid_score = lang_id_output['score']
        else:
            stopword_density = paragraph.stopwords_density(stoplist)
        link_density = paragraph.links_density()
        paragraph.heading = bool(not no_headings and paragraph.is_heading)

        if link_density > max_link_density:
            paragraph.cf_class = 'bad'
        elif ('\xa9' in paragraph.text) or ('&copy' in paragraph.text):
            paragraph.cf_class = 'bad'
        elif re.search('^select|\.select', paragraph.dom_path):
            paragraph.cf_class = 'bad'
        elif length < length_low:
            if paragraph.chars_count_in_links > 0:
                paragraph.cf_class = 'bad'
            else:
                paragraph.cf_class = 'short'
        #changes for forums
        elif 'blockquote' in paragraph.dom_path:
            paragraph.cf_class = 'bad'

        # Changes for lid
        elif use_langid and paragraph.lang not in langs:
            paragraph.cf_class = 'bad'
        elif use_langid and langid_score >= langid_th:
            if length > length_high:
                paragraph.cf_class = 'good'
            else:
                paragraph.cf_class = 'neargood'
        
        elif not use_langid and stopword_density >= stopwords_high:
            if length > length_high:
                paragraph.cf_class = 'good'
            else:
                paragraph.cf_class = 'neargood'
        elif not use_langid and stopword_density >= stopwords_low:
            paragraph.cf_class = 'neargood'
        else:
            paragraph.cf_class = 'bad'


def _get_neighbour(i, paragraphs, ignore_neargood, inc, boundary):
    while i + inc != boundary:
        i += inc
        c = paragraphs[i].class_type
        if c in ['good', 'bad']:
            return c
        if c == 'neargood' and not ignore_neargood:
            return c
    return 'bad'


def get_prev_neighbour(i, paragraphs, ignore_neargood):
    """
    Return the class of the paragraph at the top end of the short/neargood
    paragraphs block. If ignore_neargood is True, than only 'bad' or 'good'
    can be returned, otherwise 'neargood' can be returned, too.
    """
    return _get_neighbour(i, paragraphs, ignore_neargood, -1, -1)


def get_next_neighbour(i, paragraphs, ignore_neargood):
    """
    Return the class of the paragraph at the bottom end of the short/neargood
    paragraphs block. If ignore_neargood is True, than only 'bad' or 'good'
    can be returned, otherwise 'neargood' can be returned, too.
    """
    return _get_neighbour(i, paragraphs, ignore_neargood, 1, len(paragraphs))


def revise_paragraph_classification(paragraphs, max_heading_distance=MAX_HEADING_DISTANCE_DEFAULT):
    """
    Context-sensitive paragraph classification. Assumes that classify_pragraphs
    has already been called.
    """
    # copy classes
    for paragraph in paragraphs:
        paragraph.class_type = paragraph.cf_class

    # good headings
    for i, paragraph in enumerate(paragraphs):
        if not (paragraph.heading and paragraph.class_type == 'short'):
            continue
        j = i + 1
        distance = 0
        while j < len(paragraphs) and distance <= max_heading_distance:
            if paragraphs[j].class_type == 'good':
                paragraph.class_type = 'neargood'
                break
            distance += len(paragraphs[j].text)
            j += 1

    # classify short
    new_classes = {}
    for i, paragraph in enumerate(paragraphs):
        if paragraph.class_type != 'short':
            continue
        prev_neighbour = get_prev_neighbour(i, paragraphs, ignore_neargood=True)
        next_neighbour = get_next_neighbour(i, paragraphs, ignore_neargood=True)
        neighbours = set((prev_neighbour, next_neighbour))
        if neighbours == set(['good']):
            new_classes[i] = 'good'
        elif neighbours == set(['bad']):
            new_classes[i] = 'bad'
        # it must be set(['good', 'bad'])
        elif (prev_neighbour == 'bad' and get_prev_neighbour(i, paragraphs, ignore_neargood=False) == 'neargood') or \
             (next_neighbour == 'bad' and get_next_neighbour(i, paragraphs, ignore_neargood=False) == 'neargood'):
            new_classes[i] = 'good'
        else:
            new_classes[i] = 'bad'

    for i, c in new_classes.items():
        paragraphs[i].class_type = c

    # revise neargood
    for i, paragraph in enumerate(paragraphs):
        if paragraph.class_type != 'neargood':
            continue
        prev_neighbour = get_prev_neighbour(i, paragraphs, ignore_neargood=True)
        next_neighbour = get_next_neighbour(i, paragraphs, ignore_neargood=True)
        if (prev_neighbour, next_neighbour) == ('bad', 'bad'):
            paragraph.class_type = 'bad'
        else:
            paragraph.class_type = 'good'

    # more good headings
    for i, paragraph in enumerate(paragraphs):
        if not (paragraph.heading and paragraph.class_type == 'bad' and paragraph.cf_class != 'bad'):
            continue
        j = i + 1
        distance = 0
        while j < len(paragraphs) and distance <= max_heading_distance:
            if paragraphs[j].class_type == 'good':
                paragraph.class_type = 'good'
                break
            distance += len(paragraphs[j].text)
            j += 1


def justext(html_text, stoplist=None, length_low=LENGTH_LOW_DEFAULT,
        length_high=LENGTH_HIGH_DEFAULT, stopwords_low=STOPWORDS_LOW_DEFAULT,
        stopwords_high=STOPWORDS_HIGH_DEFAULT, max_link_density=MAX_LINK_DENSITY_DEFAULT,
        max_heading_distance=MAX_HEADING_DISTANCE_DEFAULT, no_headings=NO_HEADINGS_DEFAULT,
        encoding=None, default_encoding=DEFAULT_ENCODING,
        enc_errors=DEFAULT_ENC_ERRORS, preprocessor=preprocessor, use_langid=True,
            langs=None, langid_th=LANGID_TH_DEFAULT):
    """
    Converts an HTML page into a list of classified paragraphs. Each paragraph
    is represented as instance of class ˙˙justext.paragraph.Paragraph˙˙.
    """
    html_text = html_text.replace('<br>',
                                  '<div></div>')  # See http://austral.clientes.ejes.com/noticia_completa.cfm?id=30993919&desde=44523&fecha=44525&tipo=X&canal=&mime=&qidx_cursor=1&page=1&total_pages=10&rpp=25&return_url=%2Fdefault.cfm%3Fdesde%3D44523%26fecha%3D44525%26tipo%3DX%26canal%3D%26mime%3D%26qidx_cursor%3D1
    dom = html_to_dom(html_text, default_encoding, encoding, enc_errors)
#    for item in dom.xpath("//div"):
#        print(item.text)
#        print(item.classes)
#        if "quote" in item.classes:
#            item.getparent().remove(item)
#            print("quote")

    dom = preprocessor(dom)
    paragraphs = ParagraphMaker.make_paragraphs(dom)

#    for para in paragraphs:
#        print(para.xpath)


    classify_paragraphs(paragraphs, stoplist, length_low, length_high,
        stopwords_low, stopwords_high, max_link_density, no_headings, use_langid, langs, langid_th)
    revise_paragraph_classification(paragraphs, max_heading_distance)

    return paragraphs
