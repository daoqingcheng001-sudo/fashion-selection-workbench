import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
import core
import server


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.image_dir = tempfile.TemporaryDirectory()
        paths = [Path(self.image_dir.name) / x for x in ('front.png', 'back.png', 'black.png')]
        for p in paths: p.write_bytes(b'test-only-file-presence')
        self.rules = {**core.DEFAULT, 'as_of': '2026-10-10'}
        self.d = {'id': 'test-only', 'name': 'SHEIN DEMO Ruffled Dress', 'sales': '500+ sold', 'breadcrumb': ['Women', 'Dresses', 'Mini Dresses'], 'front': str(paths[0]), 'back': str(paths[1]), 'color_count': 1, 'colors': [{'name': 'Black', 'image': str(paths[2])}]}
        self.review = {'date_verified': True, 'first_listed_at': '2026-04-10', 'date_source': 'TEST FIXTURE, not real product evidence', 'is_basic': False}

    def tearDown(self): self.image_dir.cleanup()

    def test_parent_category_does_not_exclude_cami_as_tee(self):
        self.d.update(name='Halter backless camisole', breadcrumb=['Women', 'Women Tops, Blouses & Tee', 'Women Tank Tops & Camis'])
        self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'eligible')

    def test_missing_file_is_not_image_complete(self):
        Path(self.d['back']).unlink()
        self.assertFalse(core.evaluate(self.d, self.rules, self.review)['images_complete'])

    def test_exact_six_month_boundary(self):
        self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'eligible')
        self.review['first_listed_at'] = '2026-04-09'
        self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'excluded')

    def test_unknown_date_not_zero_age(self):
        self.d['sku'] = 'sz261001234'
        e = core.evaluate(self.d, self.rules, {'is_basic': False})
        self.assertEqual(e['status'], 'pending'); self.assertEqual(e['sku_month_clue'], '2026-10')

    def test_missing_image_blocks_selected(self):
        self.d['back'] = None
        self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'pending')

    def test_future_date(self):
        self.review['first_listed_at'] = '2026-10-11'
        self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'pending')

    def test_threshold_and_exclusions(self):
        self.d['sales'] = '499+ sold'; self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'excluded')
        self.d['sales'] = '1.2k+ sold recently'; self.assertEqual(core.sold_lower(self.d['sales']), 1200)
        self.d['name'] = 'Women T-Shirt'; self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'excluded')

    def test_actual_plural_t_shirts_title_is_excluded(self):
        self.d.update(id='123456789', sales='700+ sold', name="SHEIN DEMO 3pcs Casual Minimalist Black, White & Red Cropped T-Shirts, Versatile Style, Super Short & Tight Fit, Suitable For Spring/Summer,Summer Top")
        result = core.evaluate(self.d, self.rules, self.review)
        self.assertEqual(result['status'], 'excluded')
        self.assertIn('排除品类：t-shirt', result['reasons'])

    def test_known_plural_exclusions_in_leaf_category(self):
        for leaf in ('Women T-Shirts', 'Women T Shirts', 'Women Tees', 'Women Sweatshirts', 'Women Hoodies', 'Women Pajamas', 'Women Sports Bras'):
            with self.subTest(leaf=leaf):
                self.d.update(name='SHEIN DEMO Cropped Top', breadcrumb=['Women', 'Clothing', leaf])
                self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'excluded')

    def test_plural_exclusions_preserve_fashion_items_and_word_boundaries(self):
        for title, leaf in (('SHEIN DEMO Ruffle Sleeve Fashion Shirt', 'Women Shirts'), ('SHEIN DEMO Tiered Ruffle Skirt', 'Women Skirts'), ('SHEIN DEMO Sateen Halter Camisole', 'Women Tank Tops & Camis')):
            with self.subTest(title=title):
                self.d.update(name=title, breadcrumb=['Women', 'Women Tops, Blouses & Tees', leaf])
                self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'eligible')

    def test_custom_exclusion_terms_remain_literal(self):
        self.rules['excluded_terms'] = ['art']
        self.d['name'] = 'SHEIN DEMO Arts Print Dress'
        self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'eligible')
        self.d['name'] = 'SHEIN DEMO Art Print Dress'
        self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'excluded')

    def test_calendar_end(self):
        self.assertEqual(core.subtract_months(date(2026, 8, 31), 6), date(2026, 2, 28))

    def test_comment_count_not_sales(self):
        self.d['sales'] = '2000 reviews'; self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'pending')

    def test_basics_requires_review(self):
        self.review.pop('is_basic')
        self.assertEqual(core.evaluate(self.d, self.rules, self.review)['status'], 'pending')


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.patches = [patch.object(core, 'DATA', self.root), patch.object(server, 'DATA', self.root)]
        for p in self.patches: p.start()
        core.init(); self.job = core.create_job('icon-us')

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def test_checkpoint_duplicate_and_restart(self):
        server.job_action(self.job, 'resume')
        task = server.next_task(self.job)['task']
        payload = {'job': self.job, 'task_id': task['id'], 'capture': {'kind': 'list', 'url': task['url'], 'page': 1, 'cards': [{'id': 'fixture-1', 'name': 'TEST ONLY Dress', 'url': 'https://us.shein.com/example-p-1.html', 'sales': '600+ sold'}]}}
        self.assertTrue(server.save_capture(payload)['saved'])
        self.assertTrue(server.save_capture(payload)['duplicate_callback'])
        self.assertEqual(len(core.items(self.job)), 1)
        next_task = server.next_task(self.job)['task']; self.assertEqual(next_task['product_id'], 'fixture-1')
        core.init()
        with core.connect() as c:
            self.assertEqual(c.execute('SELECT state FROM queue WHERE id=?', (next_task['id'],)).fetchone()['state'], 'pending')
            self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?', (self.job,)).fetchone()['status'], 'paused')

    def test_rule_snapshot_is_frozen(self):
        with core.connect() as c:
            p = dict(core.DEFAULT, min_sold=2000); c.execute('UPDATE profiles SET data=? WHERE id=?', (core.dumps(p), 'icon-us'))
            original = json.loads(c.execute('SELECT rules FROM jobs WHERE id=?', (self.job,)).fetchone()['rules'])
        self.assertEqual(original['min_sold'], 500)

    def test_wrong_product_does_not_advance(self):
        with core.connect() as c:
            c.execute('DELETE FROM queue WHERE job=?', (self.job,));core.enqueue(c, self.job, 'https://us.shein.com/test-p-123.html', 'detail', '123')
        server.job_action(self.job, 'resume'); task = server.next_task(self.job)['task']
        with self.assertRaises(ValueError):
            server.save_capture({'job': self.job, 'task_id': task['id'], 'capture': {'kind': 'detail', 'product': {'id': '999'}}})
        with core.connect() as c:
            self.assertEqual(c.execute('SELECT state FROM queue WHERE id=?', (task['id'],)).fetchone()['state'], 'inflight')

    def test_blocked_does_not_repeat_navigation(self):
        server.job_action(self.job, 'resume'); task = server.next_task(self.job)['task']
        server.save_capture({'job': self.job, 'task_id': task['id'], 'capture': {'blocked': True, 'url': task['url']}})
        self.assertIsNone(server.next_task(self.job)['task'])

    def test_terminate_preserves_checkpoint_and_ignores_late_capture(self):
        server.job_action(self.job, 'resume'); task = server.next_task(self.job)['task']
        self.assertEqual(server.job_action(self.job, 'terminate')['status'], 'terminated')
        self.assertEqual(server.next_task(self.job), {'task': None, 'status': 'terminated'})
        late = server.save_capture({'job': self.job, 'task_id': task['id'], 'capture': {'blocked': True, 'url': task['url']}})
        self.assertTrue(late['terminated']); self.assertFalse(late['saved'])
        self.assertEqual(server.job_action(self.job, 'pause')['status'], 'terminated')
        core.init()
        with core.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?', (self.job,)).fetchone()['status'], 'terminated')
            self.assertEqual(c.execute('SELECT state FROM queue WHERE id=?', (task['id'],)).fetchone()['state'], 'pending')
        self.assertEqual(server.job_action(self.job, 'resume')['status'], 'running')


if __name__ == '__main__': unittest.main()
