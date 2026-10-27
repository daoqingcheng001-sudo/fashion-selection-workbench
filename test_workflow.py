"""Delivery tests use isolated SQLite and generated fixture pictures only."""
from contextlib import ExitStack
import io
import html
import json
from pathlib import Path
import tempfile
import re
import threading
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image, ImageDraw

import core
import server
import workflow
from access import AccessError


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.data, self.project, self.output = [self.root / n for n in ('data', 'project', 'exports')]
        for path in (self.data, self.project, self.output):
            path.mkdir()
        for module, values in ((core, {'DATA': self.data}),
                               (server, {'DATA': self.data, 'PROJECT': self.project,
                                         'OUTPUT': self.output, 'ASSET_MAP': {}})):
            for name, value in values.items():
                self.stack.enter_context(patch.object(module, name, value))
        core.init()
        with core.connect() as c:
            rules = {**core.DEFAULT, 'as_of': '2026-10-14', 'age_basis': 'sku_estimate'}
            c.execute('UPDATE profiles SET data=? WHERE id=?', (core.dumps(rules), 'icon-us'))
        self.job = core.create_job('icon-us')
        self.workers = []
        self.images = {}
        for role, color in (('front', '#b6cce4'), ('back', '#203454'), ('color', '#85634b')):
            target = self.project / (role + '.png')
            image = Image.new('RGB', (180, 240), '#f3f3f3')
            draw = ImageDraw.Draw(image)
            draw.polygon([(65, 35), (115, 35), (140, 200), (40, 200)], fill=color)
            draw.text((8, 8), 'TEST ONLY ' + role, fill='#222222')
            image.save(target); self.images[role] = str(target)
        self.add('fixture-1')

    def tearDown(self):
        for worker in self.workers:
            workflow.wait_delivery(worker, 120)
        self.stack.close()

    def add(self, pid, approved=True, **changes):
        data = {'id': pid, 'name': 'TEST ONLY Halter Dress', 'sales': '1.9k+ sold recently',
                'sales_period': '平台未公开周期', 'url': 'https://us.shein.com/test-p-123.html',
                'sku': 'sz2605000000000000000', 'breadcrumb': ['Women', 'Clothing', 'Dresses'],
                'category_leaf': 'Dresses', 'color_count': 1,
                'front': self.images['front'], 'back': self.images['back'],
                'colors': [{'name': 'Brown', 'image': self.images['color'],
                            'source_url': 'https://img.ltwebstatic.com/fixture.jpg'}], **changes}
        review = {'decision': 'approved' if approved else 'pending', 'is_basic': False,
                  'reviewer': 'Fixture Reviewer', 'primary_style': '性感', 'elements': '挂脖，露背'}
        with core.connect() as c:
            core.upsert_item(c, self.job, data)
            c.execute('UPDATE items SET review=? WHERE job=? AND id=?', (core.dumps(review), self.job, pid))
        return data

    def ref(self, pid='fixture-1'):
        return {'job': self.job, 'product_id': pid,
                'expected_revision': next(d['revision'] for d in core.items(self.job) if d['id'] == pid)}

    def create(self, refs=None):
        with server.MUTATION_LOCK:
            result = workflow.create_delivery(server, {'items': refs or [self.ref()]})
        self.workers.append(result['id'])
        return result

    def stored(self, eid):
        with core.connect() as c:
            return json.loads(c.execute('SELECT data FROM exports WHERE id=?', (eid,)).fetchone()['data'])

    def assert_denied(self, status, refs):
        with self.assertRaises(AccessError) as error:
            workflow.create_delivery(server, {'items': refs})
        self.assertEqual(error.exception.status, status)
        self.assertEqual(list(self.output.iterdir()), [])
        with core.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM exports').fetchone()[0], 0)

    def test_library_only_current_eligible_approvals(self):
        self.add('pending', approved=False)
        self.add('excluded', sales='100+ sold')
        self.add('missing-images', back='')
        self.add('age-excluded', sku='sz240101122')
        result = workflow.approved_items(server)
        self.assertEqual(result['count'], 1)
        record = result['items'][0]
        self.assertEqual(record['job'], self.job)
        self.assertEqual(record['source_rules']['min_sold'], 500)
        self.assertTrue(record['front_url'].startswith('/asset/'))
        self.assertIn('非真实上架日', record['evaluation']['age_status'])

    def test_atomic_mixed_unapproved_selection_rejected(self):
        self.add('unapproved', approved=False)
        self.assert_denied(409, [self.ref(), self.ref('unapproved')])

    def test_stale_missing_and_invalid_references(self):
        for status, ref in ((409, {**self.ref(), 'expected_revision': 'stale'}),
                            (404, {**self.ref(), 'product_id': 'missing'}),
                            (400, {'job': self.job, 'product_id': 'fixture-1'})):
            self.assert_denied(status, [ref])
        self.assert_denied(400, [{**self.ref(), 'product_id': '../private'}])
        self.assert_denied(400, [])

    def test_duplicate_and_size_limit(self):
        self.assert_denied(400, [self.ref(), self.ref()])
        self.assert_denied(400, [self.ref()] * 501)

    def test_outside_symlink_and_fake_images_rejected(self):
        outside = self.root / 'private.png'
        Image.new('RGB', (10, 10), 'red').save(outside)
        link = self.project / 'symlink.png'; link.symlink_to(outside)
        self.add('fixture-1', front=str(link))
        self.assert_denied(409, [self.ref()])
        fake = self.project / 'fake.png'; fake.write_text('not an image or a permitted business document')
        self.add('fixture-1', front=str(fake))
        self.assert_denied(409, [self.ref()])

    def test_failure_cleans_all_partial_files(self):
        def fail(app, payload_path, output_path):
            output_path.write_bytes(b'partial output')
            raise RuntimeError('fixture authoring failed')
        with patch.object(workflow, '_build_workbook', side_effect=fail):
            result = self.create(); workflow.wait_delivery(result['id'])
        self.assertEqual(self.stored(result['id'])['status'], 'failed')
        self.assertEqual(list(self.output.iterdir()), [])

    def test_ready_delivery_reused_until_builder_or_source_changes(self):
        def fake_workbook(app, payload_path, output_path):
            with zipfile.ZipFile(output_path, 'w') as book:
                book.writestr('fixture.txt', 'TEST ONLY')
        with patch.object(workflow, '_build_workbook', side_effect=fake_workbook):
            first = self.create(); workflow.wait_delivery(first['id'])
            again = self.create()
            self.assertEqual(again['id'], first['id'])
            self.assertTrue(again['reused'])
            self.assertEqual(again['status'], 'ready')
            with patch.object(workflow, '_builder_version', return_value='new-renderer'):
                next_version = self.create(); workflow.wait_delivery(next_version['id'])
            self.assertNotEqual(next_version['id'], first['id'])
        with core.connect() as c:
            records = [dict(json.loads(r['data']), id=r['id']) for r in c.execute('SELECT * FROM exports')]
        annotated = workflow.annotate_exports(server, records)
        first_row = next(r for r in annotated if r['id'] == first['id'])
        newest = next(r for r in annotated if r['id'] == next_version['id'])
        self.assertFalse(first_row['is_latest'])
        self.assertTrue(newest['is_latest'])
        self.assertTrue(newest['is_current'])
        self.assertEqual(newest['delivery_version'], 2)
        self.assertEqual(newest['source_items'][0]['product_id'], 'fixture-1')
        with core.connect() as c:
            c.execute('UPDATE items SET review=? WHERE job=?', (core.dumps({'decision':'rejected'}), self.job))
        refreshed = workflow.annotate_exports(server, records)
        self.assertFalse(next(r for r in refreshed if r['id'] == next_version['id'])['is_current'])
        self.assertTrue(Path(self.stored(first['id'])['path']).is_file())

    def test_missing_ready_file_allows_new_delivery(self):
        def fake_workbook(app, payload_path, output_path):
            with zipfile.ZipFile(output_path, 'w') as book:
                book.writestr('fixture.txt', 'TEST ONLY')
        with patch.object(workflow, '_build_workbook', side_effect=fake_workbook):
            first = self.create(); workflow.wait_delivery(first['id'])
            Path(self.stored(first['id'])['path']).unlink()
            second = self.create(); workflow.wait_delivery(second['id'])
        self.assertNotEqual(first['id'], second['id'])

    def test_legacy_delivery_without_manifest_is_not_claimed_current(self):
        legacy = {'id':'old-delivery', 'scope':'approved_delivery', 'kind':'zip',
                  'status':'ready', 'created':1, 'path':str(self.output/'missing.zip')}
        result = workflow.annotate_exports(server, [legacy])[0]
        self.assertIsNone(result['is_current'])
        self.assertFalse(result['is_latest'])

    def test_duplicate_build_and_revocation_before_publish(self):
        entered, release = threading.Event(), threading.Event()
        def stalled(app, payload_path, output_path):
            entered.set(); release.wait(5); output_path.write_bytes(b'test workbook')
        with patch.object(workflow, '_build_workbook', side_effect=stalled):
            result = self.create(); self.assertTrue(entered.wait(2))
            same = self.create(); self.assertEqual(same['id'], result['id'])
            with server.MUTATION_LOCK, core.connect() as c:
                c.execute('UPDATE items SET review=? WHERE job=?', (core.dumps({'decision': 'rejected'}), self.job))
            release.set(); workflow.wait_delivery(result['id'])
        self.assertEqual(self.stored(result['id'])['status'], 'failed')
        self.assertEqual(list(self.output.iterdir()), [])

    def test_real_workbook_images_zip_and_snapshot(self):
        # This is the only actual artifact-tool authoring test. No platform data.
        result = self.create(); workflow.wait_delivery(result['id'])
        record = self.stored(result['id'])
        self.assertEqual(record['status'], 'ready', record)
        self.assertEqual(record['count'], 1)
        with zipfile.ZipFile(record['path']) as package:
            self.assertIsNone(package.testzip())
            manifest = json.loads(package.read('交付清单.json'))
            self.assertEqual(manifest['items'][0]['review']['reviewer'], 'Fixture Reviewer')
            self.assertEqual(manifest['items'][0]['source_rules']['age_basis'], 'sku_estimate')
            self.assertEqual(len(manifest['assets']), 3)
            self.assertEqual(manifest['items'][0]['sales'], '1.9k+ sold recently')
            self.assertNotIn('.build-input.json', package.namelist())
            self.assertNotIn(str(self.root), json.dumps(manifest))
            for image in manifest['assets']:
                self.assertIn(image['path'], package.namelist())
            with zipfile.ZipFile(io.BytesIO(package.read('审核款图文交付.xlsx'))) as book:
                self.assertIsNone(book.testzip())
                self.assertGreaterEqual(sum(n.startswith('xl/media/') for n in book.namelist()), 3)
                strings = '\n'.join(book.read(n).decode() for n in book.namelist()
                                    if n == 'xl/sharedStrings.xml' or re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))
                for label in ('销量周期', 'SKU月份线索', '审核版本', 'Fixture Reviewer'):
                    self.assertIn(label, html.unescape(strings))
            # The portable backend exports editable workbooks without proprietary preview PNGs.
            from openpyxl import load_workbook
            parsed = load_workbook(io.BytesIO(package.read('审核款图文交付.xlsx')))
            self.assertEqual(parsed['选款图文']['R5'].value, '销量周期')
            self.assertEqual(parsed['选款图文']['L6'].value, 'Fixture Reviewer')
            self.assertEqual(len(parsed['选款图文']._images), 2)
            self.assertEqual(len(parsed['复色图片']._images), 1)

    def test_startup_recovery_marks_interrupted_and_cleans_only_own_stage(self):
        stage = self.output / '.delivery-fixture'; stage.mkdir()
        (stage / 'partial.xlsx').write_bytes(b'partial')
        private = self.root / 'private'; private.mkdir(); (private / 'keep').write_text('keep')
        with core.connect() as c:
            for eid, staging_path in (('interrupted', str(stage)), ('hostile', str(private))):
                content = {'scope': 'approved_delivery', 'status': 'building',
                           'delivery_id': 'abcdef123456', 'staging_path': staging_path}
                c.execute('INSERT INTO exports VALUES(?,?,?,?)', (eid, self.job, core.dumps(content), 1))
        workflow.recover_interrupted_deliveries(server)
        self.assertFalse(stage.exists())
        self.assertTrue((private / 'keep').is_file())
        self.assertEqual(self.stored('interrupted')['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
