import unittest
from unittest.mock import patch
from collector import Collector
from op_images import extract_op_images

class Store:
    def __init__(self, topics): self.data = {t['id']: t for t in topics}
    def get(self, tid): return self.data.get(tid)
    def set_fields(self, tid, **fields): self.data[tid].update(fields)
    def add_failure(self, *a, **kw): pass
    def save(self): pass

class ImageCollectionRevision(unittest.TestCase):
    def setUp(self):
        self.store = Store([{'id': 1, 'state': 'pending'}])
        self.c = Collector({'site_url':'https://linux.do','tag_page_url':'https://linux.do/tag/ai','request_timeout_s':1,'future_flex':{'images_enabled':True},'fetch':{'body_cap':1000,'detail_delay_s':0}}, self.store, lambda x: None)

    def test_missing_cooked_pending_and_reinspection(self):
        missing = self.c._body_from_topic_json({'post_stream':{'posts':[{'post_number':1}]}})
        self.assertFalse(missing['images_inspected'])
        self.assertTrue(missing['image_inspection_pending'])
        self.c.detail_direct = lambda tid: missing
        self.c.ensure_details([1])
        self.assertTrue(self.store.get(1)['detail_fetched'])
        self.assertTrue(self.store.get(1)['image_inspection_pending'])
        self.c.detail_direct = lambda tid: self.c._body_from_topic_json({'post_stream':{'posts':[{'post_number':1,'cooked':'<p>no images</p>'}]}})
        self.c.ensure_details([1])
        self.assertFalse(self.store.get(1)['image_inspection_pending'])
        self.assertTrue(self.store.get(1)['images_inspected'])

    def test_reply_is_not_op(self):
        result = self.c._body_from_topic_json({'post_stream':{'posts':[{'post_number':2,'cooked':'<img src="https://linux.do/a.png">'}]}})
        self.assertFalse(result['images_inspected'])
        self.assertTrue(result['image_inspection_pending'])

    def test_rss_missing_description_and_retry(self):
        rss = b'<rss><item><link>https://linux.do/t/topic/1</link></item></rss>'
        with patch('http_util.get', return_value=(200, rss, {})):
            bodies = self.c.fetch_rss_bodies()
        self.assertFalse(bodies[1]['images_inspected'])
        self.c.apply_rss_bodies(bodies)
        self.assertTrue(self.store.get(1)['image_inspection_pending'])
        rss = b'<rss><item><link>https://linux.do/t/topic/1</link><description><![CDATA[<p>ok</p>]]></description></item></rss>'
        with patch('http_util.get', return_value=(200, rss, {})):
            self.c.apply_rss_bodies(self.c.fetch_rss_bodies())
        self.assertFalse(self.store.get(1)['image_inspection_pending'])

    def test_links_do_not_replace_image_except_same_host_image(self):
        html = '<a href="https://example.com/page"><img src="https://linux.do/uploads/a.jpg"></a>'
        self.assertEqual(extract_op_images(html)['images'], ['https://linux.do/uploads/a.jpg'])
        html = '<a href="https://linux.do/uploads/full.png"><img src="https://linux.do/uploads/thumb.png"></a>'
        self.assertEqual(extract_op_images(html)['images'], ['https://linux.do/uploads/full.png'])

if __name__ == '__main__': unittest.main()
