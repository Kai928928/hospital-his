import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from flask import g, request, template_rendered
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash
from werkzeug.security import check_password_hash


# Set the isolated database before importing the application's SQLAlchemy instance.
test_storage = TemporaryDirectory(prefix='his-billing-tests-')
os.environ['HIS_DATABASE_URI'] = 'sqlite:///' + (Path(test_storage.name) / 'test.db').as_posix()
os.environ['HIS_SECRET_KEY'] = 'isolated-billing-test-secret'

from app import app
import auth as auth_module
from billing import bill_summary, parse_money, refund_eligibility
from scheduling import schedule_available, schedule_has_ended
from models import Bill, BillPayment, BillRefund, Department, Doctor, DoctorSchedule, MedicalRecord, Medicine, OperationLog, Patient, Prescription, PrescriptionItem, PrescriptionItemCatalog, PrescriptionRecordClaim, Registration, ScheduleBooking, User, db

TEST_PASSWORD = 'TestPass928!'
TEST_HASH = generate_password_hash(TEST_PASSWORD)


def tearDownModule():
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    test_storage.cleanup()


class BillingTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        department = Department(name='Test department')
        patient = Patient(name='Test patient')
        doctor = Doctor(name='Test doctor', department=department)
        registration = Registration(patient=patient, doctor=doctor, fee=Decimal('10.99'))
        record = MedicalRecord(registration=registration, diagnosis='Test')
        db.session.add_all([registration, record])
        db.session.flush()
        db.session.add(Bill(registration_id=registration.id, total_amount=Decimal('10.99')))
        self.visit_day = date.today() + timedelta(days=1)
        db.session.add(DoctorSchedule(doctor_id=doctor.id, visit_date=self.visit_day,
                                      period='am', capacity=3, fee=Decimal('10.99')))
        db.session.add_all([
            Medicine(code='TEST-A', name='Medicine A', specification='Test spec A', unit='box', price=Decimal('0.10')),
            Medicine(code='TEST-B', name='Medicine B', specification='Test spec B', unit='bottle', price=Decimal('0.20')),
            User(username='admin', display_name='Test Admin', password_hash=TEST_HASH, role='admin'),
            User(username='frontdesk', display_name='Test Frontdesk', password_hash=TEST_HASH, role='frontdesk'),
            User(username='doctor', display_name='Test Doctor', password_hash=TEST_HASH, role='doctor', doctor_id=doctor.id),
            User(username='cashier', display_name='Test Cashier', password_hash=TEST_HASH, role='cashier'),
        ])
        db.session.commit()
        self.client = self.authenticated_client()

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def authenticated_client(self, username='admin'):
        client = app.test_client()
        user = User.query.filter_by(username=username).one()
        with client.session_transaction() as session:
            session['user_id'] = user.id
            session['session_version'] = user.session_version
            session['payment_token'] = 'test-token-' + username
        return client

    def token(self, client=None):
        with (client or self.client).session_transaction() as session:
            return session['payment_token']

    def payment_data(self, client=None, **changes):
        client = client or self.client
        self.assertEqual(client.get('/bills/1').status_code, 200)
        with client.session_transaction() as session:
            token = session['payment_token']
        data = dict(payment_token=token, expected_amount='10.99', payment_method='cash',
                    received_amount='20.00', confirmed='yes')
        data.update(changes)
        return data

    def prescription_data(self, **changes):
        data = {'medical_record_id': '1', 'medicine_id[]': ['1', '2'],
                'dosage[]': ['', ''], 'quantity[]': ['3', '1'], 'revision[]': ['1', '1'],
                'payment_token': self.token()}
        data.update(changes)
        return data

    def booking_data(self, client=None, **changes):
        return {'patient_id': 1, 'schedule_id': 1, 'visit_date': self.visit_day.isoformat(),
                'expected_fee': '10.99', 'payment_token': self.token(client), **changes}

    def book(self, **changes):
        self.assertEqual(self.client.post('/registrations/create', data=self.booking_data(**changes)).status_code, 302)
        db.session.expire_all()
        return Registration.query.order_by(Registration.id.desc()).first()

    def collect(self):
        self.assertEqual(self.client.post('/bills/1/pay', data=self.payment_data()).status_code, 302)
        db.session.expire_all()

    def refund_data(self, client=None, **changes):
        client = client or self.client
        self.assertEqual(client.get('/bills/1').status_code, 200)
        with client.session_transaction() as session:
            token = session['payment_token']
        payment = db.session.get(Bill, 1).payment
        data = dict(payment_token=token, expected_amount='10.99',
                    payment_receipt=payment.receipt_no if payment else '',
                    reason='Patient requested cancellation', confirmed='yes')
        data.update(changes)
        return data

    def rendered_view(self, path='/bills/transactions', **filters):
        contexts = []
        def record(sender, template, context, **extra):
            contexts.append(context)
        with template_rendered.connected_to(record, app):
            response = self.client.get(path, query_string=filters)
        return response, contexts[-1]

    def test_money_rejects_invalid_values(self):
        for value in ['', 'abc', '-1', 'NaN', 'Infinity', '0.001', '100000000']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_money(value)
        self.assertEqual(parse_money('0.10'), Decimal('0.10'))

    def test_registration_invalid_amount_leaves_no_rows(self):
        for fee in ['-1', 'NaN', 'Infinity', '0.001', 'abc', '', '100000000']:
            with self.subTest(fee=fee):
                response = self.client.post('/registrations/create', data={
                    **self.booking_data(), 'expected_fee': fee})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(Registration.query.count(), 1)
                self.assertEqual(Bill.query.count(), 1)

    def test_registration_creates_exact_separate_bill(self):
        db.session.get(DoctorSchedule, 1).fee = Decimal('0.10')
        db.session.commit()
        response = self.client.post('/registrations/create', data={
            **self.booking_data(), 'expected_fee': '0.10'})
        self.assertEqual(response.status_code, 302)
        bill = db.session.get(Bill, 2)
        self.assertEqual(bill.total_amount, Decimal('0.10'))
        self.assertIsNone(bill.prescription_id)
        self.assertIsNone(bill_summary(bill)['error'])

    def test_invalid_prescriptions_leave_no_partial_data(self):
        invalid = [{'quantity[]': ['0', '1']}, {'quantity[]': ['1.5', '1']},
                   {'quantity[]': ['-1', '1']}, {'quantity[]': ['100000', '1']},
                   {'medicine_id[]': ['999', '2']}, {'medicine_id[]': ['1', '1']},
                   {'revision[]': ['0', '1']}, {'quantity[]': []},
                   {'medicine_id[]': ['', '2']}, {'dosage[]': ['x' * 65, '']},
                   {'medicine_id[]': [], 'dosage[]': [], 'quantity[]': [], 'revision[]': []},
                   {'medicine_id[]': ['1'] * 51, 'dosage[]': [''] * 51,
                    'quantity[]': ['1'] * 51, 'revision[]': ['1'] * 51}]
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.client.post('/prescriptions/create', data=self.prescription_data(**changes))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(Prescription.query.count(), 0)
                self.assertEqual(PrescriptionItem.query.count(), 0)
                self.assertEqual(PrescriptionItemCatalog.query.count(), 0)
                self.assertEqual(PrescriptionRecordClaim.query.count(), 0)
                self.assertEqual(Bill.query.count(), 1)

    def test_prescription_decimal_total_and_duplicate_record(self):
        response = self.client.post('/prescriptions/create', data=self.prescription_data())
        self.assertEqual(response.status_code, 302)
        bill = db.session.get(Bill, 2)
        self.assertEqual(bill.total_amount, Decimal('0.50'))
        self.assertIsNone(bill_summary(bill)['error'])
        self.assertEqual(self.client.post('/prescriptions/create', data=self.prescription_data()).status_code, 400)
        self.assertEqual(Bill.query.count(), 2)

    def test_invalid_reference_does_not_create_charge(self):
        for path, data in [('/registrations/create', self.booking_data(patient_id=999)),
                           ('/prescriptions/create', self.prescription_data(medical_record_id='999'))]:
            self.assertEqual(self.client.post(path, data=data).status_code, 400)
        self.assertEqual(Bill.query.count(), 1)

    def medicine_data(self, **changes):
        return {'code': 'NEW-1', 'name': 'New medicine', 'specification': 'Test specification',
                'unit': 'box', 'price': '1.25', 'revision': '1', 'payment_token': self.token(), **changes}

    def test_medicine_catalog_create_edit_and_audit(self):
        self.assertEqual(self.client.post('/medicines/create', data=self.medicine_data(code='new-1')).status_code, 302)
        medicine = Medicine.query.filter_by(code='NEW-1').one()
        self.assertEqual(medicine.price, Decimal('1.25'))
        self.assertEqual(self.client.post(f'/medicines/{medicine.id}/edit', data=self.medicine_data(price='2.50')).status_code, 302)
        db.session.expire_all()
        self.assertEqual(medicine.price, Decimal('2.50'))
        self.assertEqual(medicine.revision, 2)
        self.assertEqual(OperationLog.query.filter(OperationLog.action.like('medicine.%')).count(), 2)
        self.assertIn('Test Admin', OperationLog.query.filter_by(action='medicine.edit').one().actor_name)

    def test_invalid_medicine_fields_and_duplicate_code_leave_no_rows(self):
        for changes in [{'code': ''}, {'code': 'bad code'}, {'code': 'TEST-A'}, {'name': ''},
                        {'specification': ''}, {'unit': ''}, {'name': 'x' * 129}, {'unit': 'x' * 17},
                        {'price': 'NaN'}, {'price': '0.001'}, {'price': '-1'}, {'price': '100000000'}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post('/medicines/create', data=self.medicine_data(**changes)).status_code, 400)
                self.assertEqual(Medicine.query.count(), 2)
                self.assertEqual(OperationLog.query.count(), 0)

    def test_catalog_read_only_roles_and_direct_post_denied(self):
        for role in ('doctor', 'cashier', 'frontdesk'):
            client = self.authenticated_client(role)
            self.assertEqual(client.get('/medicines').status_code, 403 if role == 'frontdesk' else 200)
            for path in ('/medicines/create', '/medicines/1/edit'):
                self.assertEqual(client.get(path).status_code, 403)
                self.assertEqual(client.post(path, data=self.medicine_data(payment_token=self.token(client))).status_code, 403)
            self.assertEqual(client.post('/medicines/1/status', data={
                'payment_token': self.token(client), 'active': 'no', 'revision': '1'}).status_code, 403)

    def test_medicine_search_status_and_pagination(self):
        for index in range(22):
            db.session.add(Medicine(code=f'PAGE-{index:02}', name='Pagination test', specification='Spec', unit='box', price=1))
        db.session.get(Medicine, 2).is_active = False
        db.session.commit()
        _, view = self.rendered_view('/medicines')
        self.assertEqual(view['page'].total, 23)
        self.assertEqual(len(view['page'].items), 20)
        _, view = self.rendered_view('/medicines', page=2)
        self.assertEqual(len(view['page'].items), 3)
        _, view = self.rendered_view('/medicines', status='inactive', q='TEST-B')
        self.assertEqual(view['page'].total, 1)
        _, view = self.rendered_view('/medicines', q='Test spec A')
        self.assertEqual(view['page'].total, 1)

    def test_stale_catalog_edits_and_status_do_not_overwrite(self):
        self.assertEqual(self.client.post('/medicines/1/edit', data=self.medicine_data(code='TEST-A')).status_code, 302)
        self.assertEqual(self.client.post('/medicines/1/edit', data=self.medicine_data(code='TEST-A', price='9.99')).status_code, 303)
        self.assertEqual(self.client.post('/medicines/1/status', data={
            'payment_token': self.token(), 'active': 'no', 'revision': '1'}).status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(Medicine, 1).price, Decimal('1.25'))
        self.assertTrue(db.session.get(Medicine, 1).is_active)
        self.assertEqual(OperationLog.query.count(), 1)

    def test_disabled_and_changed_medicine_reject_old_prescription(self):
        self.assertEqual(self.client.post('/medicines/1/status', data={
            'payment_token': self.token(), 'active': 'no', 'revision': '1'}).status_code, 302)
        self.assertEqual(self.client.post('/prescriptions/create', data=self.prescription_data()).status_code, 400)
        self.assertNotIn(b'value="1" data-revision=', self.client.get('/prescriptions/create').data)
        self.assertEqual(self.client.post('/medicines/1/status', data={
            'payment_token': self.token(), 'active': 'yes', 'revision': '2'}).status_code, 302)
        self.assertEqual(self.client.post('/prescriptions/create', data=self.prescription_data()).status_code, 400)
        self.assertEqual(Prescription.query.count(), 0)
        self.assertEqual(Bill.query.count(), 1)

    def test_prescription_ignores_forged_name_and_price(self):
        response = self.client.post('/prescriptions/create', data=self.prescription_data(**{
            'drug_name[]': ['Forged', 'Forged'], 'unit_price[]': ['0.00', 'NaN']}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(Bill, 2).total_amount, Decimal('0.50'))
        self.assertEqual(PrescriptionItem.query.first().drug_name, 'Medicine A')
        self.assertEqual(PrescriptionItem.query.first().unit_price, Decimal('0.10'))

    def test_prescription_snapshot_survives_catalog_edit_and_disable(self):
        self.assertEqual(self.client.post('/prescriptions/create', data=self.prescription_data()).status_code, 302)
        self.assertEqual(self.client.post('/medicines/1/edit', data=self.medicine_data(
            code='CHANGED', name='Changed name', specification='Changed spec', unit='tablet', price='19.99')).status_code, 302)
        self.assertEqual(self.client.post('/medicines/1/status', data={
            'payment_token': self.token(), 'active': 'no', 'revision': '2'}).status_code, 302)
        db.session.expire_all()
        item = PrescriptionItem.query.order_by(PrescriptionItem.id).first()
        self.assertEqual((item.drug_name, item.unit_price, item.catalog.code, item.catalog.specification, item.catalog.unit),
                         ('Medicine A', Decimal('0.10'), 'TEST-A', 'Test spec A', 'box'))
        self.assertEqual(db.session.get(Bill, 2).total_amount, Decimal('0.50'))
        self.assertIsNone(bill_summary(db.session.get(Bill, 2))['error'])
        self.assertIn(b'Test spec A', self.client.get('/bills/2').data)
        response = self.client.get('/prescriptions/1')
        self.assertIn(b'Test spec A', response.data)
        self.assertNotIn(b'Changed spec', response.data)

    def test_prescription_total_limit_uses_catalog_price(self):
        db.session.get(Medicine, 1).price = Decimal('99999999.99')
        db.session.commit()
        self.assertEqual(self.client.post('/prescriptions/create', data=self.prescription_data()).status_code, 400)
        self.assertEqual(Prescription.query.count(), 0)
        self.assertEqual(Bill.query.count(), 1)

    def test_prescription_failure_rolls_back_items_bill_claim_and_audit(self):
        with patch.object(db.session, 'commit', side_effect=OperationalError('commit', {}, Exception('test failure'))):
            with self.assertLogs(app.logger.name, level='ERROR'):
                self.assertEqual(self.client.post('/prescriptions/create', data=self.prescription_data()).status_code, 503)
        self.assertEqual(Prescription.query.count(), 0)
        self.assertEqual(PrescriptionItem.query.count(), 0)
        self.assertEqual(PrescriptionItemCatalog.query.count(), 0)
        self.assertEqual(PrescriptionRecordClaim.query.count(), 0)
        self.assertEqual(Bill.query.count(), 1)
        self.assertEqual(OperationLog.query.count(), 0)

    def test_catalog_failure_rolls_back_with_audit(self):
        for path, data in [('/medicines/create', self.medicine_data()),
                           ('/medicines/1/edit', self.medicine_data(code='TEST-A', price='9.99')),
                           ('/medicines/1/status', {'payment_token': self.token(), 'active': 'no', 'revision': '1'})]:
            with self.subTest(path=path), patch.object(db.session, 'commit', side_effect=OperationalError('commit', {}, Exception('test failure'))):
                with self.assertLogs(app.logger.name, level='ERROR'):
                    self.assertEqual(self.client.post(path, data=data).status_code, 503)
            db.session.expire_all()
            self.assertEqual(Medicine.query.count(), 2)
            self.assertEqual(db.session.get(Medicine, 1).price, Decimal('0.10'))
            self.assertTrue(db.session.get(Medicine, 1).is_active)
            self.assertEqual(OperationLog.query.count(), 0)

    def test_concurrent_prescriptions_create_one_charge(self):
        clients = [self.authenticated_client(), self.authenticated_client()]
        forms = [self.prescription_data() for _ in clients]
        barrier = Barrier(2)
        original_execute = db.session.execute
        def synchronized_write(statement, *args, **kwargs):
            if getattr(statement, 'is_update', False) and statement.table.name == 'medicines' and not g.get('catalog_write_started'):
                g.catalog_write_started = True
                barrier.wait(timeout=10)
            return original_execute(statement, *args, **kwargs)
        def create(index):
            return clients[index].post('/prescriptions/create', data=forms[index]).status_code
        with patch.object(db.session, 'execute', side_effect=synchronized_write):
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(create, range(2)))
        self.assertEqual(statuses.count(302), 1, statuses)
        self.assertTrue(all(status in (302, 400, 409) for status in statuses), statuses)
        db.session.expire_all()
        self.assertEqual(Prescription.query.count(), 1)
        self.assertEqual(PrescriptionItemCatalog.query.count(), 2)
        self.assertEqual(PrescriptionRecordClaim.query.count(), 1)
        self.assertEqual(Bill.query.count(), 2)
        self.assertEqual(OperationLog.query.filter_by(action='prescription.create').count(), 1)

    def test_catalog_change_between_validation_and_write_prevents_stale_charge(self):
        original_execute = db.session.execute
        changed = False
        def change_catalog(statement, *args, **kwargs):
            nonlocal changed
            if getattr(statement, 'is_update', False) and statement.table.name == 'medicines' and not changed:
                changed = True
                with db.engine.begin() as connection:
                    connection.execute(update(Medicine).where(Medicine.id == 1).values(price=99, revision=2))
            return original_execute(statement, *args, **kwargs)
        with patch.object(db.session, 'execute', side_effect=change_catalog):
            self.assertEqual(self.client.post('/prescriptions/create', data=self.prescription_data()).status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(Medicine, 1).price, Decimal('99.00'))
        self.assertEqual(Prescription.query.count(), 0)
        self.assertEqual(PrescriptionItemCatalog.query.count(), 0)
        self.assertEqual(PrescriptionRecordClaim.query.count(), 0)
        self.assertEqual(Bill.query.count(), 1)
        self.assertEqual(OperationLog.query.count(), 0)

    def test_prescription_detail_enforces_doctor_and_historical_fallback(self):
        other = Doctor(name='Other', department_id=1)
        visit = Registration(patient_id=1, doctor=other, fee=0)
        record = MedicalRecord(registration=visit)
        rx = Prescription(medical_record=record, items=[PrescriptionItem(drug_name='Legacy', quantity=1, unit_price=1)])
        db.session.add(rx)
        db.session.commit()
        response = self.client.get(f'/prescriptions/{rx.id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('历史未登记规格'.encode(), response.data)
        self.assertEqual(self.authenticated_client('doctor').get(f'/prescriptions/{rx.id}').status_code, 403)
        self.assertEqual(self.authenticated_client('cashier').get(f'/prescriptions/{rx.id}').status_code, 403)

    def test_cash_payment_and_repeat_preserve_one_receipt(self):
        data = self.payment_data()
        self.assertEqual(self.client.post('/bills/1/pay', data=data).status_code, 302)
        db.session.expire_all()
        bill = db.session.get(Bill, 1)
        self.assertTrue(bill.is_paid)
        self.assertEqual(bill.payment.amount_cents, 1099)
        self.assertEqual(bill.payment.received_cents, 2000)
        self.assertEqual(bill.payment.change_cents, 901)
        receipt_no, paid_at = bill.payment.receipt_no, bill.paid_at
        self.assertEqual(self.client.post('/bills/1/pay', data=data).status_code, 302)
        db.session.expire_all()
        self.assertEqual(BillPayment.query.count(), 1)
        self.assertEqual(OperationLog.query.filter_by(action='bill.pay').count(), 1)
        self.assertEqual(db.session.get(Bill, 1).paid_at, paid_at)
        self.assertEqual(db.session.get(Bill, 1).payment.receipt_no, receipt_no)
        self.assertIn(receipt_no.encode(), self.client.get('/bills/1').data)

    def test_payment_validation_does_not_mark_bill_paid(self):
        for changes, status in [({'received_amount': '10.98'}, 400),
                                ({'received_amount': 'NaN'}, 400),
                                ({'received_amount': '-1'}, 400),
                                ({'received_amount': '11.001'}, 400),
                                ({'payment_method': 'unknown'}, 400),
                                ({'payment_method': 'wechat', 'received_amount': '20'}, 400),
                                ({'confirmed': ''}, 400), ({'expected_amount': '10'}, 409),
                                ({'payment_token': ''}, 403)]:
            with self.subTest(changes=changes):
                response = self.client.post('/bills/1/pay', data=self.payment_data(**changes))
                self.assertEqual(response.status_code, status)
                db.session.expire_all()
                self.assertFalse(db.session.get(Bill, 1).is_paid)
                self.assertEqual(BillPayment.query.count(), 0)

    def test_noncash_payment_requires_exact_amount(self):
        data = self.payment_data(payment_method='wechat', received_amount='10.99')
        self.assertEqual(self.client.post('/bills/1/pay', data=data).status_code, 302)
        self.assertEqual(BillPayment.query.one().change_cents, 0)
        self.assertEqual(BillPayment.query.one().method, 'wechat')

    def test_zero_fee_can_be_settled_without_negative_change(self):
        bill = db.session.get(Bill, 1)
        bill.registration.fee = Decimal('0.00')
        bill.total_amount = Decimal('0.00')
        db.session.commit()
        data = self.payment_data(expected_amount='0.00', received_amount='0.00')
        self.assertEqual(self.client.post('/bills/1/pay', data=data).status_code, 302)
        self.assertEqual(BillPayment.query.one().amount_cents, 0)
        self.assertEqual(BillPayment.query.one().change_cents, 0)

    def test_ambiguous_historical_combined_bill_cannot_collect_registration_twice(self):
        bill = db.session.get(Bill, 1)
        bill.prescription = Prescription(medical_record_id=1, items=[
            PrescriptionItem(drug_name='Medicine', quantity=1, unit_price=Decimal('1.00'))])
        bill.total_amount = Decimal('11.99')
        db.session.add(Bill(registration_id=1, total_amount=Decimal('10.99')))
        db.session.commit()
        self.assertFalse(bill_summary(bill)['legacy_combined'])
        self.assertIsNotNone(bill_summary(bill)['error'])
        data = self.payment_data(expected_amount='11.99', received_amount='20.00')
        self.assertEqual(self.client.post('/bills/1/pay', data=data).status_code, 409)
        self.assertEqual(BillPayment.query.count(), 0)


    def test_legacy_combined_bill_preserves_amount_and_shows_all_items(self):
        bill = db.session.get(Bill, 1)
        prescription = Prescription(medical_record_id=1, items=[
            PrescriptionItem(drug_name='Medicine', quantity=30, unit_price=Decimal('999.00'))])
        bill.prescription = prescription
        bill.total_amount = Decimal('29980.99')
        bill.is_paid = True
        db.session.commit()
        summary = bill_summary(bill)
        self.assertTrue(summary['legacy_combined'])
        self.assertIsNone(summary['error'])
        self.assertEqual(summary['total'], Decimal('29980.99'))
        self.assertEqual(len(summary['items']), 2)
        self.assertIsNone(bill.payment)
        self.assertEqual(self.client.get('/bills/1').status_code, 200)

    def test_mismatched_bill_blocks_payment(self):
        bill = db.session.get(Bill, 1)
        bill.total_amount = Decimal('11.00')
        db.session.commit()
        data = self.payment_data(expected_amount='11.00')
        self.assertEqual(self.client.post('/bills/1/pay', data=data).status_code, 409)
        self.assertEqual(BillPayment.query.count(), 0)
        self.assertNotIn(b'id="payment-form"', self.client.get('/bills/1').data)

    def test_commit_failure_rolls_back_bill_and_receipt(self):
        data = self.payment_data()
        with patch.object(db.session, 'commit', side_effect=OperationalError('test failure', {}, Exception('failure'))):
            with self.assertLogs(app.logger, level='ERROR'):
                response = self.client.post('/bills/1/pay', data=data)
        self.assertEqual(response.status_code, 503)
        db.session.expire_all()
        self.assertFalse(db.session.get(Bill, 1).is_paid)
        self.assertIsNone(db.session.get(Bill, 1).paid_at)
        self.assertEqual(BillPayment.query.count(), 0)
        self.assertEqual(OperationLog.query.filter_by(action='bill.pay').count(), 0)

    def test_concurrent_collectors_create_only_one_payment(self):
        clients = [self.authenticated_client(), self.authenticated_client()]
        forms = [self.payment_data(client) for client in clients]
        barrier = Barrier(2)
        def synchronized_summary(bill):
            summary = bill_summary(bill)
            if request.endpoint == 'bill_pay':
                barrier.wait(timeout=10)
            return summary
        def collect(index):
            return clients[index].post('/bills/1/pay', data=forms[index]).status_code
        with patch('app.bill_summary', side_effect=synchronized_summary):
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(collect, range(2)))
        self.assertEqual(statuses, [302, 302])
        db.session.expire_all()
        self.assertTrue(db.session.get(Bill, 1).is_paid)
        self.assertEqual(BillPayment.query.count(), 1)

    def test_full_refund_preserves_collection_and_cannot_recollect(self):
        self.collect()
        payment = db.session.get(Bill, 1).payment
        receipt, paid_at = payment.receipt_no, payment.paid_at
        data = self.refund_data()
        self.assertEqual(self.client.post('/bills/1/refund', data=data).status_code, 302)
        db.session.expire_all()
        bill = db.session.get(Bill, 1)
        self.assertTrue(bill.is_paid)
        self.assertEqual(bill.refund.amount_cents, 1099)
        self.assertEqual(bill.refund.reason, data['reason'])
        self.assertEqual(bill.payment.received_cents, 2000)
        self.assertEqual(bill.payment.change_cents, 901)
        self.assertEqual(bill.payment.receipt_no, receipt)
        self.assertEqual(bill.payment.paid_at, paid_at)
        self.assertEqual(self.client.post('/bills/1/refund', data=data).status_code, 302)
        self.assertEqual(BillRefund.query.count(), 1)
        self.assertEqual(OperationLog.query.filter_by(action='bill.refund').count(), 1)
        self.assertEqual(self.client.post('/bills/1/pay', data=self.payment_data()).status_code, 302)
        self.assertEqual(BillPayment.query.count(), 1)
        page = self.client.get('/bills/1').data
        self.assertIn(bill.refund.refund_no.encode(), page)
        self.assertNotIn(b'id="refund-form"', page)
        self.assertNotIn(b'id="payment-form"', page)

    def test_refund_invalid_fields_do_not_create_record(self):
        self.collect()
        for changes, status in [({'reason': ''}, 400), ({'reason': ' '}, 400),
                                ({'reason': 'x' * 501}, 400), ({'confirmed': ''}, 400),
                                ({'expected_amount': '20'}, 409), ({'expected_amount': 'NaN'}, 400),
                                ({'payment_receipt': 'different-receipt'}, 409),
                                ({'payment_token': ''}, 403), ({'payment_token': '无效'}, 403)]:
            with self.subTest(changes=changes):
                response = self.client.post('/bills/1/refund', data=self.refund_data(**changes))
                self.assertEqual(response.status_code, status)
                self.assertEqual(BillRefund.query.count(), 0)
        self.assertTrue(db.session.get(Bill, 1).is_paid)

    def test_unpaid_and_legacy_bills_cannot_be_refunded(self):
        self.assertEqual(self.client.post('/bills/1/refund', data=self.refund_data()).status_code, 409)
        bill = db.session.get(Bill, 1)
        bill.is_paid = True
        db.session.commit()
        page = self.client.get('/bills/1').data
        self.assertNotIn(b'id="refund-form"', page)
        self.assertEqual(self.client.post('/bills/1/refund', data=self.refund_data()).status_code, 409)
        self.assertEqual(BillRefund.query.count(), 0)

    def test_zero_and_mismatched_bills_cannot_be_refunded(self):
        self.collect()
        bill = db.session.get(Bill, 1)
        bill.total_amount = Decimal('11.00')
        db.session.commit()
        self.assertEqual(self.client.post('/bills/1/refund', data=self.refund_data()).status_code, 409)
        bill.total_amount = Decimal('0.00')
        bill.registration.fee = Decimal('0.00')
        bill.payment.amount_cents = 0
        bill.payment.received_cents = 0
        bill.payment.change_cents = 0
        db.session.commit()
        self.assertEqual(self.client.post('/bills/1/refund', data=self.refund_data()).status_code, 409)
        self.assertEqual(BillRefund.query.count(), 0)

    def test_refund_commit_failure_leaves_original_collection_intact(self):
        self.collect()
        data = self.refund_data()
        with patch.object(db.session, 'commit', side_effect=OperationalError('test failure', {}, Exception('failure'))):
            with self.assertLogs(app.logger, level='ERROR'):
                response = self.client.post('/bills/1/refund', data=data)
        self.assertEqual(response.status_code, 503)
        db.session.expire_all()
        self.assertEqual(BillRefund.query.count(), 0)
        self.assertTrue(db.session.get(Bill, 1).is_paid)
        self.assertEqual(BillPayment.query.one().amount_cents, 1099)
        self.assertEqual(OperationLog.query.filter_by(action='bill.refund').count(), 0)

    def test_concurrent_refunds_save_one_refund(self):
        self.collect()
        clients = [self.authenticated_client(), self.authenticated_client()]
        forms = [self.refund_data(client) for client in clients]
        barrier = Barrier(2)
        def synchronized_eligibility(bill):
            error = refund_eligibility(bill)
            if request.endpoint == 'bill_refund' and not error:
                barrier.wait(timeout=10)
            return error
        def refund(index):
            return clients[index].post('/bills/1/refund', data=forms[index]).status_code
        with patch('app.refund_eligibility', side_effect=synchronized_eligibility):
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(refund, range(2)))
        self.assertEqual(statuses, [302, 302])
        db.session.expire_all()
        self.assertEqual(BillRefund.query.count(), 1)
        self.assertEqual(BillRefund.query.one().amount_cents, 1099)

    def test_transaction_dates_and_type_follow_each_cash_event(self):
        self.collect()
        self.assertEqual(self.client.post('/bills/1/refund', data=self.refund_data()).status_code, 302)
        db.session.expire_all()
        bill = db.session.get(Bill, 1)
        bill.paid_at = datetime(2026, 11, 1, 0, 0, 0)
        bill.payment.paid_at = bill.paid_at
        bill.refund.refunded_at = datetime(2026, 11, 2, 23, 59, 59, 999999)
        db.session.commit()
        for filters, income, expense, count in [({}, '10.99', '10.99', 2),
                ({'start_date': '2026-11-01', 'end_date': '2026-11-01'}, '10.99', '0', 1),
                ({'start_date': '2026-11-02', 'end_date': '2026-11-02'}, '0', '10.99', 1),
                ({'start_date': '2026-11-03'}, '0', '0', 0),
                ({'kind': 'income'}, '10.99', '0', 1), ({'kind': 'refund'}, '0', '10.99', 1)]:
            with self.subTest(filters=filters):
                response, view = self.rendered_view(**filters)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(view['income'], Decimal(income))
                self.assertEqual(view['expense'], Decimal(expense))
                self.assertEqual(view['net_amount'], Decimal(income) - Decimal(expense))
                self.assertEqual(len(view['rows']), count)

    def test_transaction_patient_phone_and_receipt_search(self):
        self.collect()
        self.assertEqual(self.client.post('/bills/1/refund', data=self.refund_data()).status_code, 302)
        db.session.expire_all()
        bill = db.session.get(Bill, 1)
        bill.registration.patient.phone = '13900001111'
        other = Registration(patient=Patient(name='Other person', phone='18800001111'),
                             doctor_id=1, fee=Decimal('100'))
        db.session.add(other)
        db.session.flush()
        db.session.add(Bill(registration_id=other.id, total_amount=Decimal('100'), is_paid=True,
                            paid_at=datetime(2026, 10, 1)))
        db.session.commit()
        for keyword in ['Test patient', '13900001111', bill.payment.receipt_no]:
            response, view = self.rendered_view(q=keyword)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(view['rows']), 2)
            self.assertEqual(view['net_amount'], Decimal('0'))
        _, view = self.rendered_view(q=bill.refund.refund_no)
        self.assertEqual(len(view['rows']), 1)
        self.assertEqual(view['rows'][0]['original_number'], bill.payment.receipt_no)
        _, view = self.rendered_view(q='Other person')
        self.assertEqual(len(view['rows']), 1)
        self.assertIsNone(view['rows'][0]['number'])
        self.assertEqual(view['income'], Decimal('100'))

    def test_invalid_transaction_filters_return_no_financial_rows(self):
        self.collect()
        for filters in [{'start_date': 'bad'}, {'end_date': '2026-02-30'},
                        {'start_date': '2026-11-03', 'end_date': '2026-11-01'}, {'kind': 'bad'}]:
            with self.subTest(filters=filters):
                response, view = self.rendered_view(**filters)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(view['rows'], [])
                self.assertEqual(view['income'], Decimal('0'))

    def test_refunded_status_propagates_without_creating_unpaid_bill(self):
        self.collect()
        self.assertEqual(self.client.post('/bills/1/refund', data=self.refund_data()).status_code, 302)
        for status, count in [('refunded', 1), ('paid', 0), ('unpaid', 0)]:
            _, view = self.rendered_view('/bills', status=status)
            self.assertEqual(len(view['bills']), count)
            self.assertEqual(view['paid_amount'], Decimal('10.99'))
            self.assertEqual(view['refunded_amount'], Decimal('10.99'))
            self.assertEqual(view['net_amount'], Decimal('0'))
        self.assertIn('已退费'.encode(), self.client.get('/patients/1').data)

    def login(self, client, username='admin', password=TEST_PASSWORD, **extra):
        client.get('/login')
        return client.post('/login', data={'username': username, 'password': password,
                                          'payment_token': self.token(client), **extra})

    def test_anonymous_cannot_read_or_change_business_data(self):
        client = app.test_client()
        for path in ['/patients/1', '/bills', '/medical-records/1', '/operation-logs', '/users']:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 303)
                self.assertTrue(response.location.startswith('/login'))
                self.assertNotIn(b'Test patient', response.data)
        self.assertEqual(client.post('/bills/1/pay', data={}).status_code, 303)
        self.assertEqual(BillPayment.query.count(), 0)

    def test_real_login_logout_and_password_hash(self):
        client = app.test_client()
        self.assertEqual(self.login(client, password='wrong').status_code, 400)
        self.assertEqual(self.login(client, next='/bills').location, '/bills')
        self.assertEqual(client.get('/bills').status_code, 200)
        user = User.query.filter_by(username='admin').one()
        self.assertNotEqual(user.password_hash, TEST_PASSWORD)
        self.assertTrue(check_password_hash(user.password_hash, TEST_PASSWORD))
        self.assertEqual(client.post('/logout', data={'payment_token': self.token(client)}).status_code, 302)
        self.assertEqual(client.get('/bills').status_code, 303)

    def test_login_rejects_external_and_malformed_return_targets(self):
        for target in ['https://example.com', '//example.com', '%2f%2fexample.com', '/\\example.com', 'http://[']:
            with self.subTest(target=target):
                response = self.login(app.test_client(), next=target)
                self.assertEqual(response.location, '/')

    def test_role_permissions_and_private_patient_summary(self):
        paths = {'/bills': {'admin', 'cashier'}, '/users': {'admin'}, '/operation-logs': {'admin'},
                 '/medical-records/1': {'admin', 'doctor'}, '/registrations/create': {'admin', 'frontdesk'},
                 '/doctors/create': {'admin'}}
        for username in ['admin', 'frontdesk', 'doctor', 'cashier']:
            client = self.authenticated_client(username)
            for path, roles in paths.items():
                self.assertEqual(client.get(path).status_code, 200 if username in roles else 403, (username, path))
            if username in ['frontdesk', 'cashier']:
                page = client.get('/patients/1').data
                self.assertNotIn('病历摘要'.encode(), page)
                self.assertNotIn(b'/medical-records/1', page)
        for username in ['doctor', 'frontdesk']:
            client = self.authenticated_client(username)
            self.assertEqual(client.post('/bills/1/pay', data={'payment_token': self.token(client)}).status_code, 403)
        self.assertEqual(BillPayment.query.count(), 0)

    def test_doctor_cannot_access_another_doctors_visit(self):
        other_doctor = Doctor(name='Other doctor', department_id=1)
        other_registration = Registration(patient=Patient(name='Foreign patient'), doctor=other_doctor,
                                          fee=Decimal('10.00'))
        other_record = MedicalRecord(registration=other_registration, diagnosis='Foreign diagnosis')
        db.session.add(other_record)
        db.session.commit()
        client = self.authenticated_client('doctor')
        for path in ['/patients', '/registrations', '/medical-records']:
            self.assertNotIn(b'Foreign', client.get(path).data)
        self.assertEqual(client.get(f'/medical-records/{other_record.id}').status_code, 403)
        self.assertEqual(client.get(f'/patients/{other_registration.patient_id}').status_code, 403)
        self.assertEqual(client.post('/medical-records/create', data={
            'payment_token': self.token(client), 'registration_id': other_registration.id}).status_code, 403)
        data = self.prescription_data(medical_record_id=str(other_record.id), payment_token=self.token(client))
        self.assertEqual(client.post('/prescriptions/create', data=data).status_code, 403)
        self.assertEqual(Prescription.query.count(), 0)
        data = self.prescription_data(payment_token=self.token(client))
        self.assertEqual(client.post('/prescriptions/create', data=data).status_code, 302)

    def test_disable_and_reenable_does_not_restore_old_session(self):
        cashier = User.query.filter_by(username='cashier').one()
        client = self.authenticated_client('cashier')
        self.assertEqual(self.client.post(f'/users/{cashier.id}/status', data={
            'payment_token': self.token(), 'enabled': 'no'}).status_code, 302)
        self.assertEqual(client.get('/bills').status_code, 303)
        self.assertEqual(self.login(app.test_client(), username='cashier').status_code, 400)
        self.client.post(f'/users/{cashier.id}/status', data={'payment_token': self.token(), 'enabled': 'yes'})
        self.assertEqual(client.get('/bills').status_code, 303)
        self.assertEqual(self.login(client, username='cashier').status_code, 302)
        self.assertEqual(client.get('/bills').status_code, 200)

    def test_password_reset_and_personal_change_revoke_sessions(self):
        cashier = User.query.filter_by(username='cashier').one()
        old_client = self.authenticated_client('cashier')
        new_password = 'ResetPassword928!'
        self.client.post(f'/users/{cashier.id}/reset-password', data={
            'payment_token': self.token(), 'password': new_password, 'confirm_password': new_password})
        self.assertEqual(old_client.get('/bills').status_code, 303)
        self.assertEqual(self.login(app.test_client(), username='cashier').status_code, 400)
        first, second = app.test_client(), app.test_client()
        self.assertEqual(self.login(first, 'cashier', new_password).status_code, 302)
        self.assertEqual(self.login(second, 'cashier', new_password).status_code, 302)
        first.get('/account/password')
        response = first.post('/account/password', data={'payment_token': self.token(first),
            'old_password': new_password, 'password': 'ChangedPass928!', 'confirm_password': 'ChangedPass928!'})
        self.assertEqual(response.location, '/login')
        self.assertEqual(second.get('/bills').status_code, 303)
        self.assertEqual(first.get('/bills').status_code, 303)
        self.assertEqual(self.login(first, 'cashier', 'ChangedPass928!').status_code, 302)

    def test_cannot_disable_current_admin(self):
        admin = User.query.filter_by(username='admin').one()
        self.assertEqual(self.client.post(f'/users/{admin.id}/status', data={
            'payment_token': self.token(), 'enabled': 'no'}).status_code, 403)
        db.session.expire_all()
        self.assertTrue(db.session.get(User, admin.id).is_active)

    def test_all_write_endpoints_require_csrf(self):
        for path in ['/patients/create', '/doctors/create', '/registrations/create', '/registrations/1/status',
                     '/medical-records/create', '/prescriptions/create', '/bills/1/pay', '/bills/1/refund',
                     '/users/create', '/users/4/status', '/users/4/reset-password', '/account/password', '/logout',
                     '/medicines/create', '/medicines/1/edit', '/medicines/1/status']:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, data={}).status_code, 403)
        self.assertEqual(OperationLog.query.count(), 0)

    def test_user_creation_and_duplicate_doctor_binding(self):
        data = {'username': 'newstaff', 'display_name': 'New Staff', 'password': TEST_PASSWORD,
                'confirm_password': TEST_PASSWORD, 'role': 'frontdesk', 'payment_token': self.token()}
        self.assertEqual(self.client.post('/users/create', data=data).status_code, 302)
        self.assertEqual(User.query.filter_by(username='newstaff').one().role, 'frontdesk')
        for changes in [{'username': 'newstaff'}, {'username': 'ab'}, {'username': 'staff2', 'role': 'invalid'},
                        {'username': 'staff2', 'role': 'doctor', 'doctor_id': 1},
                        {'username': 'staff2', 'role': 'doctor'}, {'username': 'staff2', 'confirm_password': 'different'}]:
            self.assertEqual(self.client.post('/users/create', data={**data, **changes}).status_code, 400)
        self.assertEqual(User.query.count(), 5)
        self.assertEqual(OperationLog.query.filter_by(action='user.create').count(), 1)

    def test_cashier_audit_identity_is_server_derived(self):
        client = self.authenticated_client('cashier')
        data = self.payment_data(client, operator='admin')
        self.assertEqual(client.post('/bills/1/pay', data=data).status_code, 302)
        log = OperationLog.query.filter_by(action='bill.pay').one()
        self.assertEqual(log.actor_name, 'Test Cashier (cashier)')
        self.assertEqual(client.post('/bills/1/refund', data=self.refund_data(client)).status_code, 302)
        log = OperationLog.query.filter_by(action='bill.refund').one()
        self.assertEqual(log.actor_name, 'Test Cashier (cashier)')
        self.assertIn(b'Test Cashier (cashier)', client.get('/bills/1').data)
        _, view = self.rendered_view(q='Test patient')
        self.assertTrue(all(row['operator'] == 'Test Cashier (cashier)' for row in view['rows']))
        _, view = self.rendered_view('/operation-logs', action='bill.refund', q='cashier')
        self.assertEqual(view['pagination'].total, 1)

    def test_initial_admin_setup_has_no_default_password_and_runs_once(self):
        User.query.delete()
        db.session.commit()
        client = app.test_client()
        self.assertEqual(client.get('/bills').location, '/setup')
        self.assertEqual(client.get('/setup').status_code, 200)
        response = client.post('/setup', data={'username': 'owner', 'display_name': 'Owner',
            'password': TEST_PASSWORD, 'confirm_password': TEST_PASSWORD, 'payment_token': self.token(client)})
        self.assertEqual(response.location, '/login')
        self.assertEqual(User.query.count(), 1)
        self.assertEqual(OperationLog.query.filter_by(action='auth.setup').count(), 1)
        self.assertEqual(client.get('/setup').location, '/login')
        self.assertEqual(self.login(client, username='owner').status_code, 302)

    def test_concurrent_initial_setup_creates_one_admin(self):
        User.query.delete()
        db.session.commit()
        clients = [app.test_client(), app.test_client()]
        for client in clients:
            client.get('/setup')
        barrier = Barrier(2)
        original_fields = auth_module.account_fields
        def synchronized_fields():
            result = original_fields()
            barrier.wait(timeout=10)
            return result
        forms = [{'username': f'owner{index}', 'display_name': 'Owner', 'password': TEST_PASSWORD,
                  'confirm_password': TEST_PASSWORD, 'payment_token': self.token(client)}
                 for index, client in enumerate(clients)]
        def create(index):
            return clients[index].post('/setup', data=forms[index]).status_code
        with patch('auth.account_fields', side_effect=synchronized_fields):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(create, range(2))), [302, 302])
        self.assertEqual(User.query.count(), 1)
        self.assertEqual(OperationLog.query.filter_by(action='auth.setup').count(), 1)

    def test_concurrent_admin_disabling_keeps_one_active_admin(self):
        other = User(username='otheradmin', display_name='Other Admin', password_hash=TEST_HASH, role='admin')
        db.session.add(other)
        db.session.commit()
        admin = User.query.filter_by(username='admin').one()
        clients = [self.authenticated_client(), self.authenticated_client('otheradmin')]
        targets = [other.id, admin.id]
        tokens = [self.token(client) for client in clients]
        barrier = Barrier(2)
        original_get = db.get_or_404
        def synchronized_get(*args, **kwargs):
            result = original_get(*args, **kwargs)
            if request.endpoint == 'auth.user_status':
                barrier.wait(timeout=10)
            return result
        def disable(index):
            return clients[index].post(f'/users/{targets[index]}/status', data={
                'payment_token': tokens[index], 'enabled': 'no'}).status_code
        with patch.object(db, 'get_or_404', side_effect=synchronized_get):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(disable, range(2))), [302, 302])
        db.session.expire_all()
        self.assertEqual(User.query.filter_by(role='admin', is_active=True).count(), 1)
        self.assertEqual(OperationLog.query.filter_by(action='user.status').count(), 1)

    def test_schedule_validation_permissions_and_duplicate_period(self):
        data = {'doctor_id': 1, 'visit_date': self.visit_day.isoformat(), 'period': 'pm',
                'capacity': '2', 'fee': '12.50', 'payment_token': self.token()}
        self.assertEqual(self.client.post('/schedules/create', data=data).status_code, 302)
        self.assertEqual(self.client.post('/schedules/create', data=data).status_code, 400)
        for changes in [{'capacity': '0'}, {'capacity': '1.5'}, {'fee': '-1'}, {'fee': 'NaN'},
                        {'period': 'night'}, {'doctor_id': 999}, {'visit_date': 'bad'},
                        {'visit_date': (date.today() - timedelta(days=1)).isoformat()}]:
            self.assertEqual(self.client.post('/schedules/create', data={**data, **changes}).status_code, 400)
        self.assertEqual(DoctorSchedule.query.count(), 2)
        for name in ['frontdesk', 'doctor', 'cashier']:
            client = self.authenticated_client(name)
            token = self.token(client)
            self.assertEqual(client.post('/schedules/create', data={**data, 'payment_token': token}).status_code, 403)
            self.assertEqual(client.post('/schedules/1/status', data={'opened': 'no', 'payment_token': token}).status_code, 403)
        self.assertEqual(self.client.get('/schedules?date=invalid').status_code, 400)

    def test_doctor_schedule_view_is_limited_to_self(self):
        other = Doctor(name='Foreign schedule doctor', department_id=1)
        db.session.add(other)
        db.session.flush()
        db.session.add(DoctorSchedule(doctor_id=other.id, visit_date=self.visit_day,
                                      period='am', capacity=2, fee=Decimal('5')))
        db.session.commit()
        client = self.authenticated_client('doctor')
        response = client.get('/schedules', query_string={'date': self.visit_day.isoformat(), 'doctor_id': other.id})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Foreign schedule doctor', response.data)
        self.assertIn(b'Test doctor', response.data)

    def test_booking_uses_schedule_fee_date_and_unique_queue(self):
        registration = self.book(doctor_id=999, fee='0.01')
        self.assertEqual(registration.doctor_id, 1)
        self.assertEqual(registration.fee, Decimal('10.99'))
        self.assertEqual(registration.reg_date, self.visit_day)
        self.assertEqual(registration.appointment.queue_number, 1)
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 1)
        self.assertEqual(self.client.post('/registrations/create', data=self.booking_data()).status_code, 409)
        db.session.expire_all()
        self.assertEqual(Registration.query.count(), 2)
        self.assertEqual(Bill.query.count(), 2)
        self.assertEqual(db.session.get(DoctorSchedule, 1).last_number, 1)

    def test_booking_rejects_stale_missing_closed_and_expired_slots(self):
        for changes in [{'expected_fee': '0.01'}, {'schedule_id': 999},
                        {'visit_date': 'bad'}, {'schedule_id': ''}]:
            self.assertEqual(self.client.post('/registrations/create', data=self.booking_data(**changes)).status_code, 400)
        slot = db.session.get(DoctorSchedule, 1)
        slot.is_open = False
        db.session.commit()
        self.assertEqual(self.client.post('/registrations/create', data=self.booking_data()).status_code, 409)
        slot.is_open = True
        slot.visit_date = date.today() - timedelta(days=1)
        db.session.commit()
        self.assertEqual(self.client.post('/registrations/create', data=self.booking_data(visit_date=slot.visit_date.isoformat())).status_code, 409)
        self.assertEqual(ScheduleBooking.query.count(), 0)
        self.assertEqual(Bill.query.count(), 1)

    def test_concurrent_last_slot_cannot_be_oversold(self):
        db.session.get(DoctorSchedule, 1).capacity = 1
        other_patient = Patient(name='Other booking patient')
        db.session.add(other_patient)
        db.session.commit()
        clients = [self.authenticated_client(), self.authenticated_client()]
        forms = [self.booking_data(client, patient_id=pid) for client, pid in zip(clients, [1, other_patient.id])]
        barrier = Barrier(2)
        def synchronized_available(slot):
            available = schedule_available(slot)
            if request.endpoint == 'registration_create' and request.method == 'POST' and not g.get('booking_waited'):
                g.booking_waited = True
                barrier.wait(timeout=10)
            return available
        def book(index):
            return clients[index].post('/registrations/create', data=forms[index]).status_code
        with patch('app.schedule_available', side_effect=synchronized_available):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(book, range(2))), [302, 409])
        db.session.expire_all()
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 1)
        self.assertEqual(ScheduleBooking.query.count(), 1)
        self.assertEqual(Registration.query.count(), 2)
        self.assertEqual(Bill.query.count(), 2)

    def test_booking_failure_rolls_back_slot_bill_and_audit(self):
        with patch.object(db.session, 'commit', side_effect=OperationalError('test failure', {}, Exception('failure'))):
            with self.assertLogs(app.logger, level='ERROR'):
                response = self.client.post('/registrations/create', data=self.booking_data())
        self.assertEqual(response.status_code, 503)
        db.session.expire_all()
        slot = db.session.get(DoctorSchedule, 1)
        self.assertEqual((slot.booked_count, slot.last_number), (0, 0))
        self.assertEqual(ScheduleBooking.query.count(), 0)
        self.assertEqual(Bill.query.count(), 1)
        self.assertEqual(OperationLog.query.filter_by(action='registration.create').count(), 0)

    def test_cancellation_releases_once_voids_bill_and_rebooking_gets_new_number(self):
        registration = self.book()
        rid = registration.id
        data = {'status': 'cancelled', 'payment_token': self.token()}
        self.assertEqual(self.client.post(f'/registrations/{rid}/status', data=data).status_code, 302)
        self.assertEqual(self.client.post(f'/registrations/{rid}/status', data=data).status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 0)
        self.assertFalse(db.session.get(Registration, rid).appointment.is_active)
        bill = Bill.query.filter_by(registration_id=rid).one()
        self.assertTrue(bill.is_void)
        self.assertNotIn(b'id="payment-form"', self.client.get(f'/bills/{bill.id}').data)
        payment = {'payment_token': self.token(), 'expected_amount': '10.99', 'received_amount': '20',
                   'payment_method': 'cash', 'confirmed': 'yes'}
        self.assertEqual(self.client.post(f'/bills/{bill.id}/pay', data=payment).status_code, 409)
        _, view = self.rendered_view('/bills', status='unpaid')
        self.assertEqual(len(view['bills']), 1)
        self.assertEqual(view['unpaid_amount'], Decimal('10.99'))
        again = self.book()
        self.assertEqual(again.appointment.queue_number, 2)
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 1)

    def test_paid_booking_requires_refund_before_cancelling(self):
        registration = self.book()
        bill = Bill.query.filter_by(registration_id=registration.id).one()
        pay = {'payment_token': self.token(), 'expected_amount': '10.99', 'received_amount': '20',
               'payment_method': 'cash', 'confirmed': 'yes'}
        self.assertEqual(self.client.post(f'/bills/{bill.id}/pay', data=pay).status_code, 302)
        cancel = {'payment_token': self.token(), 'status': 'cancelled'}
        self.assertEqual(self.client.post(f'/registrations/{registration.id}/status', data=cancel).status_code, 409)
        db.session.expire_all()
        refund = {'payment_token': self.token(), 'expected_amount': '10.99',
                  'payment_receipt': bill.payment.receipt_no, 'reason': 'Cancel appointment', 'confirmed': 'yes'}
        self.assertEqual(self.client.post(f'/bills/{bill.id}/refund', data=refund).status_code, 302)
        self.assertEqual(self.client.post(f'/registrations/{registration.id}/status', data=cancel).status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 0)
        self.assertEqual(BillRefund.query.count(), 1)

    def test_zero_fee_settled_booking_can_cancel_without_a_fictitious_refund(self):
        db.session.get(DoctorSchedule, 1).fee = Decimal('0')
        db.session.commit()
        registration = self.book(expected_fee='0.00')
        bill = Bill.query.filter_by(registration_id=registration.id).one()
        pay = {'payment_token': self.token(), 'expected_amount': '0.00', 'received_amount': '0.00',
               'payment_method': 'cash', 'confirmed': 'yes'}
        self.assertEqual(self.client.post(f'/bills/{bill.id}/pay', data=pay).status_code, 302)
        self.assertEqual(self.client.post(f'/registrations/{registration.id}/status', data={
            'payment_token': self.token(), 'status': 'cancelled'}).status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 0)
        self.assertEqual(BillRefund.query.count(), 0)

    def test_cancellation_failure_rolls_back_status_and_released_slot(self):
        registration = self.book()
        with patch.object(db.session, 'commit', side_effect=OperationalError('test failure', {}, Exception('failure'))):
            with self.assertLogs(app.logger, level='ERROR'):
                response = self.client.post(f'/registrations/{registration.id}/status', data={
                    'payment_token': self.token(), 'status': 'cancelled'})
        self.assertEqual(response.status_code, 503)
        db.session.expire_all()
        self.assertEqual(registration.status, 'waiting')
        self.assertTrue(registration.appointment.is_active)
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 1)

    def test_cancel_racing_payment_cannot_collect_void_bill(self):
        registration = self.book()
        bill = Bill.query.filter_by(registration_id=registration.id).one()
        clients = [self.authenticated_client(), self.authenticated_client('cashier')]
        forms = [{'payment_token': self.token(clients[0]), 'status': 'cancelled'},
                 {'payment_token': self.token(clients[1]), 'expected_amount': '10.99', 'received_amount': '20',
                  'payment_method': 'cash', 'confirmed': 'yes'}]
        paths = [f'/registrations/{registration.id}/status', f'/bills/{bill.id}/pay']
        barrier = Barrier(2)
        original_execute = db.session.execute
        def synchronized_execute(statement, *args, **kwargs):
            if (getattr(statement, 'is_update', False) and statement.table.name in ('registrations', 'bills')
                    and not g.get('financial_waited')):
                g.financial_waited = True
                barrier.wait(timeout=10)
            return original_execute(statement, *args, **kwargs)
        def submit(index):
            return clients[index].post(paths[index], data=forms[index]).status_code
        with patch.object(db.session, 'execute', side_effect=synchronized_execute):
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(submit, range(2)))
        self.assertTrue(all(status in (302, 409) for status in statuses), statuses)
        db.session.expire_all()
        state = (registration.status, bill.is_paid, db.session.get(DoctorSchedule, 1).booked_count)
        self.assertIn(state, [('waiting', True, 1), ('cancelled', False, 0)])
        self.assertEqual(BillPayment.query.count(), int(bill.is_paid))

    def test_future_cancelled_and_completed_visits_cannot_be_seen_early_or_twice(self):
        registration = self.book()
        client = self.authenticated_client('doctor')
        data = {'payment_token': self.token(client), 'registration_id': registration.id, 'diagnosis': 'New diagnosis'}
        self.assertEqual(client.post('/medical-records/create', data=data).status_code, 409)
        registration.reg_date = date.today()
        db.session.commit()
        self.assertEqual(client.post('/medical-records/create', data=data).status_code, 302)
        self.assertEqual(client.post('/medical-records/create', data=data).status_code, 409)
        self.assertEqual(self.client.post(f'/registrations/{registration.id}/status', data={
            'payment_token': self.token(), 'status': 'cancelled'}).status_code, 409)
        self.assertEqual(MedicalRecord.query.filter_by(registration_id=registration.id).count(), 1)
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 1)

    def test_schedule_edits_preserve_old_prices_and_prevent_shrinking_below_bookings(self):
        first = self.book()
        patient = Patient(name='Second appointment patient')
        db.session.add(patient)
        db.session.commit()
        self.book(patient_id=patient.id)
        slot = db.session.get(DoctorSchedule, 1)
        form = {'payment_token': self.token(), 'revision': slot.revision, 'capacity': 1, 'fee': '20.00'}
        self.assertEqual(self.client.post('/schedules/1/edit', data=form).status_code, 400)
        self.assertEqual(self.client.post('/schedules/1/edit', data={**form, 'capacity': 3}).status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(DoctorSchedule, 1).fee, Decimal('20.00'))
        self.assertEqual(first.fee, Decimal('10.99'))
        self.assertEqual(Bill.query.filter_by(registration_id=first.id).one().total_amount, Decimal('10.99'))
        self.assertEqual(self.client.post('/schedules/1/edit', data={**form, 'capacity': 3}).status_code, 400)
        self.assertEqual(self.client.post('/schedules/1/status', data={'payment_token': self.token(), 'opened': 'no'}).status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(DoctorSchedule, 1).booked_count, 2)

    def test_period_cutoff_and_future_queue_visibility(self):
        slot = db.session.get(DoctorSchedule, 1)
        slot.visit_date = date.today()
        self.assertFalse(schedule_has_ended(slot, datetime.combine(date.today(), datetime.min.time()).replace(hour=11, minute=59)))
        self.assertTrue(schedule_has_ended(slot, datetime.combine(date.today(), datetime.min.time()).replace(hour=12)))
        slot.visit_date = self.visit_day
        db.session.commit()
        self.book()
        _, view = self.rendered_view('/')
        self.assertEqual(view['today_regs'], 1)
        self.assertEqual(view['waiting'], 1)
        self.assertEqual(len(view['waiting_queue']), 1)


if __name__ == '__main__':
    unittest.main()
