"""Isolated OP HTML image discovery; caller must surface omitted/untrusted counts."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import re
from trusted_images import target
class OpImages(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.links=[];self.images=[];self.omitted=0;self.ignored=0
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='a':self.links.append(a.get('href'))
  if tag!='img':return
  classes=(a.get('class') or '').lower().split();src=a.get('data-orig-src') or a.get('data-src') or a.get('src') or ''
  if any(c in ('emoji','avatar','icon','site-icon','tracking-pixel') or c.startswith('emoji-') for c in classes):self.ignored+=1;return
  if not src:self.omitted+=1;return
  if '/emoji/' in src.lower() or '/avatar/' in src.lower():self.ignored+=1;return
  try:w=int(a.get('width') or 0);h=int(a.get('height') or 0)
  except ValueError:w=h=0
  if (w and w<160) or (h and h<120):self.ignored+=1;return
  candidate=src
  if self.links and self.links[-1] and src:
   image_url=urlsplit(urljoin('https://linux.do/',src))
   link_url=urlsplit(urljoin('https://linux.do/',self.links[-1]))
   if (image_url.scheme in ('http','https') and link_url.scheme in ('http','https')
       and image_url.hostname and image_url.hostname==link_url.hostname
       and re.search(r'\.(?:jpe?g|png|gif|webp|avif)$',link_url.path,re.I)):
    candidate=self.links[-1]
  url=urljoin('https://linux.do/',candidate)
  try:target(url)
  except ValueError:self.omitted+=1;return
  if url in self.images:return
  if len(self.images)>=4:self.omitted+=1;return
  self.images.append(url)
 def handle_endtag(self,tag):
  if tag=='a' and self.links:self.links.pop()
def extract_op_images(html):
 parser=OpImages();parser.feed(html or '');return {'images':parser.images,'omitted':parser.omitted,'ignored':parser.ignored}
