from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
from decimal import Decimal

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), nullable=False, unique=True)
    display_name = db.Column(db.String(64), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctors.id'), unique=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    doctor = db.relationship('Doctor')
    __table_args__ = (
        db.CheckConstraint("role IN ('admin', 'frontdesk', 'doctor', 'cashier')"),
        db.CheckConstraint("(role = 'doctor' AND doctor_id IS NOT NULL) OR (role != 'doctor' AND doctor_id IS NULL)"),
    )


class OperationLog(db.Model):
    __tablename__ = 'operation_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    actor_name = db.Column(db.String(100), nullable=False)
    action = db.Column(db.String(32), nullable=False)
    target_type = db.Column(db.String(32), nullable=False)
    target_id = db.Column(db.Integer, nullable=False)
    summary = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class Department(db.Model):
    __tablename__ = 'departments'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(64), nullable=False, unique=True)
    description = db.Column(db.String(256))

    doctors = db.relationship('Doctor', backref='department', lazy=True)


class Doctor(db.Model):
    __tablename__ = 'doctors'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(64), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False)
    title = db.Column(db.String(64))  # 职称
    phone = db.Column(db.String(20))

    registrations = db.relationship('Registration', backref='doctor', lazy=True)


class Patient(db.Model):
    __tablename__ = 'patients'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(64), nullable=False)
    gender = db.Column(db.String(4))  # 男/女
    age = db.Column(db.Integer)
    phone = db.Column(db.String(20))
    address = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.now)

    registrations = db.relationship('Registration', backref='patient', lazy=True)


class Registration(db.Model):
    __tablename__ = 'registrations'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctors.id'), nullable=False)
    reg_date = db.Column(db.Date, default=date.today)
    status = db.Column(db.String(16), default='waiting')  # waiting / done / cancelled
    fee = db.Column(db.Numeric(12, 2), default=0)  # 挂号费

    medical_records = db.relationship('MedicalRecord', backref='registration', lazy=True)
    bills = db.relationship('Bill', backref='registration', lazy=True)
    appointment = db.relationship('ScheduleBooking', backref='registration', uselist=False, lazy=True)


class DoctorSchedule(db.Model):
    __tablename__ = 'doctor_schedules'
    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctors.id'), nullable=False)
    visit_date = db.Column(db.Date, nullable=False)
    period = db.Column(db.String(4), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    fee = db.Column(db.Numeric(12, 2), nullable=False)
    is_open = db.Column(db.Boolean, nullable=False, default=True)
    booked_count = db.Column(db.Integer, nullable=False, default=0)
    last_number = db.Column(db.Integer, nullable=False, default=0)
    revision = db.Column(db.Integer, nullable=False, default=1)
    doctor = db.relationship('Doctor')
    bookings = db.relationship('ScheduleBooking', backref='schedule', lazy=True)
    __table_args__ = (
        db.UniqueConstraint('doctor_id', 'visit_date', 'period'),
        db.CheckConstraint("period IN ('am', 'pm')"),
        db.CheckConstraint('capacity BETWEEN 1 AND 999 AND booked_count BETWEEN 0 AND capacity'),
        db.CheckConstraint('fee >= 0 AND last_number >= booked_count'),
    )


class ScheduleBooking(db.Model):
    __tablename__ = 'schedule_bookings'
    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey('doctor_schedules.id'), nullable=False)
    registration_id = db.Column(db.Integer, db.ForeignKey('registrations.id'), nullable=False, unique=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    queue_number = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('schedule_id', 'queue_number'),
        db.CheckConstraint('queue_number > 0'),
        db.Index('uq_active_schedule_patient', 'schedule_id', 'patient_id', unique=True,
                 sqlite_where=db.text('is_active = 1')),
    )


class MedicalRecord(db.Model):
    __tablename__ = 'medical_records'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    registration_id = db.Column(db.Integer, db.ForeignKey('registrations.id'), nullable=False)
    chief_complaint = db.Column(db.String(512))  # 主诉
    diagnosis = db.Column(db.String(512))  # 诊断
    treatment = db.Column(db.String(512))  # 治疗方案
    created_at = db.Column(db.DateTime, default=datetime.now)

    prescriptions = db.relationship('Prescription', backref='medical_record', lazy=True)


class Prescription(db.Model):
    __tablename__ = 'prescriptions'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    medical_record_id = db.Column(db.Integer, db.ForeignKey('medical_records.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    items = db.relationship('PrescriptionItem', backref='prescription', lazy=True, cascade='all, delete-orphan')
    bills = db.relationship('Bill', backref='prescription', lazy=True)


class PrescriptionItem(db.Model):
    __tablename__ = 'prescription_items'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    prescription_id = db.Column(db.Integer, db.ForeignKey('prescriptions.id'), nullable=False)
    drug_name = db.Column(db.String(128), nullable=False)
    dosage = db.Column(db.String(64))  # 用法用量
    quantity = db.Column(db.Integer, default=1)
    unit_price = db.Column(db.Numeric(12, 2), default=0)  # 单价
    catalog = db.relationship('PrescriptionItemCatalog', uselist=False, backref='item', cascade='all, delete-orphan')


class Medicine(db.Model):
    __tablename__ = 'medicines'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), nullable=False, unique=True)
    name = db.Column(db.String(128), nullable=False)
    specification = db.Column(db.String(128), nullable=False)
    unit = db.Column(db.String(16), nullable=False)
    price = db.Column(db.Numeric(12, 2), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    revision = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.CheckConstraint('price BETWEEN 0 AND 99999999.99 AND revision > 0'),)


class PrescriptionItemCatalog(db.Model):
    __tablename__ = 'prescription_item_catalogs'
    item_id = db.Column(db.Integer, db.ForeignKey('prescription_items.id'), primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicines.id'), nullable=False)
    code = db.Column(db.String(32), nullable=False)
    specification = db.Column(db.String(128), nullable=False)
    unit = db.Column(db.String(16), nullable=False)


class PrescriptionRecordClaim(db.Model):
    # New prescriptions get a unique claim without altering historical prescription rows.
    __tablename__ = 'prescription_record_claims'
    medical_record_id = db.Column(db.Integer, db.ForeignKey('medical_records.id'), primary_key=True)
    prescription_id = db.Column(db.Integer, db.ForeignKey('prescriptions.id'), nullable=False, unique=True)


class Bill(db.Model):
    __tablename__ = 'bills'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    registration_id = db.Column(db.Integer, db.ForeignKey('registrations.id'), nullable=False)
    prescription_id = db.Column(db.Integer, db.ForeignKey('prescriptions.id'), nullable=True)
    total_amount = db.Column(db.Numeric(12, 2), default=0)
    is_paid = db.Column(db.Boolean, default=False)
    paid_at = db.Column(db.DateTime, nullable=True)

    payment = db.relationship('BillPayment', backref='bill', uselist=False, lazy=True)

    @property
    def refund(self):
        return self.payment.refund if self.payment else None

    @property
    def is_void(self):
        return not self.is_paid and self.registration.status == 'cancelled'


class BillPayment(db.Model):
    __tablename__ = 'bill_payments'
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey('bills.id'), nullable=False, unique=True)
    receipt_no = db.Column(db.String(40), nullable=False, unique=True)
    method = db.Column(db.String(16), nullable=False)
    amount_cents = db.Column(db.BigInteger, nullable=False)
    received_cents = db.Column(db.BigInteger, nullable=False)
    change_cents = db.Column(db.BigInteger, nullable=False)
    paid_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    refund = db.relationship('BillRefund', backref='payment', uselist=False, lazy=True)
    __table_args__ = (
        db.CheckConstraint('amount_cents >= 0 AND change_cents >= 0'),
        db.CheckConstraint('received_cents = amount_cents + change_cents'),
        db.CheckConstraint("method IN ('cash', 'card', 'wechat', 'alipay')"),
        db.CheckConstraint("method = 'cash' OR change_cents = 0"),
    )

    @property
    def amount(self):
        return Decimal(self.amount_cents) / 100

    @property
    def received_amount(self):
        return Decimal(self.received_cents) / 100

    @property
    def change_amount(self):
        return Decimal(self.change_cents) / 100


class BillRefund(db.Model):
    __tablename__ = 'bill_refunds'
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey('bill_payments.id'), nullable=False, unique=True)
    refund_no = db.Column(db.String(40), nullable=False, unique=True)
    amount_cents = db.Column(db.BigInteger, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    refunded_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    __table_args__ = (
        db.CheckConstraint('amount_cents > 0'),
        db.CheckConstraint('length(trim(reason)) BETWEEN 1 AND 500'),
    )

    @property
    def amount(self):
        return Decimal(self.amount_cents) / 100
