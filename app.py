from datetime import date, datetime, time
from decimal import Decimal
import os
import secrets
from uuid import uuid4

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import and_, func, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from billing import MAX_AMOUNT, PAYMENT_METHODS, bill_summary, money, parse_money, refund_eligibility
from auth import audit, can, init_auth
from scheduling import PERIODS, PERIOD_END, schedule_available, schedule_has_ended, visit_date
from medicines import medicines

from models import (
    Bill,
    BillPayment,
    BillRefund,
    Department,
    Doctor,
    DoctorSchedule,
    MedicalRecord,
    Medicine,
    OperationLog,
    Patient,
    Prescription,
    PrescriptionItem,
    PrescriptionItemCatalog,
    PrescriptionRecordClaim,
    Registration,
    ScheduleBooking,
    db,
)


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("HIS_DATABASE_URI", "sqlite:///his.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
init_auth(app)
app.register_blueprint(medicines)


STATUS_LABELS = {
    "waiting": "待就诊",
    "done": "已完成",
    "cancelled": "已取消",
}


def get_bill_type(bill):
    if bill_summary(bill)['legacy_combined']:
        return "历史合并收费"
    return "药品处方" if bill.prescription_id else "挂号诊查"


def get_bill_items(bill):
    return bill_summary(bill)['items']


@app.context_processor
def inject_common_data():
    return {
        "status_labels": STATUS_LABELS,
        "today": date.today(),
        "get_bill_type": get_bill_type,
        "get_bill_items": get_bill_items,
        "payment_methods": PAYMENT_METHODS,
        "periods": PERIODS,
        "schedule_available": schedule_available,
        "schedule_has_ended": schedule_has_ended,
    }


@app.route("/")
def index():
    today_value = date.today()
    today_regs = Registration.query.filter_by(reg_date=today_value).count()
    waiting = Registration.query.filter_by(status="waiting", reg_date=today_value).count()
    done_today = Registration.query.filter_by(reg_date=today_value, status="done").count()
    cancelled_today = Registration.query.filter_by(
        reg_date=today_value, status="cancelled"
    ).count()
    total_patients = Patient.query.count()
    doctor_count = Doctor.query.count()
    department_count = Department.query.count()
    unpaid_bills = Bill.query.join(Registration).filter(Bill.is_paid.is_(False), Registration.status != 'cancelled').count()
    unpaid_amount = (
        db.session.query(func.coalesce(func.sum(Bill.total_amount), 0))
        .join(Registration).filter(Bill.is_paid.is_(False), Registration.status != 'cancelled')
        .scalar()
    )
    paid_today = (
        db.session.query(func.coalesce(func.sum(Bill.total_amount), 0))
        .filter(Bill.is_paid.is_(True), func.date(Bill.paid_at) == today_value.isoformat())
        .scalar()
    )

    status_counts = dict(
        db.session.query(Registration.status, func.count(Registration.id))
        .group_by(Registration.status)
        .all()
    )

    department_load = (
        db.session.query(Department.name, func.count(Registration.id))
        .select_from(Department)
        .join(Doctor)
        .join(Registration)
        .filter(Registration.reg_date == today_value)
        .group_by(Department.id)
        .order_by(func.count(Registration.id).desc())
        .all()
    )

    recent_registrations = (
        Registration.query.order_by(Registration.reg_date.desc(), Registration.id.desc())
        .limit(6)
        .all()
    )
    waiting_queue = (
        Registration.query.filter_by(status="waiting", reg_date=today_value)
        .order_by(Registration.reg_date.asc(), Registration.id.asc())
        .limit(6)
        .all()
    )
    latest_records = (
        MedicalRecord.query.order_by(MedicalRecord.created_at.desc()).limit(5).all()
    )

    completion_rate = round((done_today / today_regs) * 100) if today_regs else 0

    return render_template(
        "index.html",
        today_regs=today_regs,
        waiting=waiting,
        done_today=done_today,
        cancelled_today=cancelled_today,
        total_patients=total_patients,
        doctor_count=doctor_count,
        department_count=department_count,
        unpaid_bills=unpaid_bills,
        unpaid_amount=unpaid_amount,
        paid_today=paid_today,
        status_counts=status_counts,
        department_load=department_load,
        recent_registrations=recent_registrations,
        waiting_queue=waiting_queue,
        latest_records=latest_records,
        completion_rate=completion_rate,
    )


@app.route("/patients")
def patient_list():
    q = request.args.get("q", "").strip()
    query = Patient.query
    if g.user.role == 'doctor':
        query = query.filter(Patient.registrations.any(Registration.doctor_id == g.user.doctor_id))
    if q:
        keyword = f"%{q}%"
        query = query.filter(
            or_(
                Patient.name.like(keyword),
                Patient.phone.like(keyword),
                Patient.address.like(keyword),
            )
        )
    patients = query.order_by(Patient.id.desc()).all()
    return render_template("patients/list.html", patients=patients, q=q)


@app.route("/patients/create", methods=["GET", "POST"])
def patient_create():
    if request.method == "POST":
        patient = Patient(
            name=request.form["name"],
            gender=request.form["gender"],
            age=int(request.form.get("age", 0) or 0),
            phone=request.form.get("phone", ""),
            address=request.form.get("address", ""),
        )
        db.session.add(patient)
        db.session.flush()
        audit('patient.create', 'patient', patient.id, '新建患者档案')
        db.session.commit()
        flash("患者建档成功", "success")
        return redirect(url_for("patient_list"))
    return render_template("patients/create.html")


@app.route("/patients/<int:pid>")
def patient_detail(pid):
    patient = Patient.query.get_or_404(pid)
    if g.user.role == 'doctor' and not Registration.query.filter_by(patient_id=pid, doctor_id=g.user.doctor_id).first():
        abort(403)
    registrations = (
        Registration.query.filter_by(patient_id=pid)
        .order_by(Registration.reg_date.desc(), Registration.id.desc())
        .filter(Registration.doctor_id == g.user.doctor_id if g.user.role == 'doctor' else True).all()
    )
    records = (
        MedicalRecord.query.join(Registration)
        .filter(Registration.patient_id == pid)
        .order_by(MedicalRecord.created_at.desc())
        .filter(Registration.doctor_id == g.user.doctor_id if g.user.role == 'doctor' else True).all()
    ) if can('medical_record_list') else []
    bills = (
        Bill.query.join(Registration)
        .filter(Registration.patient_id == pid)
        .order_by(Bill.id.desc())
        .all()
    ) if can('bill_list') else []
    return render_template(
        "patients/detail.html",
        patient=patient,
        registrations=registrations,
        records=records,
        bills=bills,
    )


@app.route("/doctors")
def doctor_list():
    department_id = request.args.get("department_id", type=int)
    q = request.args.get("q", "").strip()
    query = Doctor.query.join(Department)
    if department_id:
        query = query.filter(Doctor.department_id == department_id)
    if q:
        keyword = f"%{q}%"
        query = query.filter(
            or_(Doctor.name.like(keyword), Doctor.title.like(keyword), Department.name.like(keyword))
        )
    doctors = query.order_by(Department.name, Doctor.name).all()
    departments = Department.query.order_by(Department.name).all()
    availability = {}
    for slot in DoctorSchedule.query.filter_by(visit_date=date.today()).all():
        if schedule_available(slot):
            availability[slot.doctor_id] = availability.get(slot.doctor_id, 0) + slot.capacity - slot.booked_count
    return render_template(
        "doctors/list.html",
        doctors=doctors,
        departments=departments,
        department_id=department_id,
        q=q,
        availability=availability,
    )


@app.route("/doctors/create", methods=["GET", "POST"])
def doctor_create():
    departments = Department.query.order_by(Department.name).all()
    if request.method == "POST":
        doctor = Doctor(
            name=request.form["name"],
            department_id=int(request.form["department_id"]),
            title=request.form.get("title", ""),
            phone=request.form.get("phone", ""),
        )
        db.session.add(doctor)
        db.session.flush()
        audit('doctor.create', 'doctor', doctor.id, '新增接诊医生')
        db.session.commit()
        flash("医生添加成功", "success")
        return redirect(url_for("doctor_list"))
    return render_template("doctors/create.html", departments=departments)


@app.route('/schedules')
def schedule_list():
    error = None
    try:
        day = visit_date(request.args.get('date', date.today().isoformat()))
    except ValueError as invalid:
        day, error = date.today(), str(invalid)
    doctor_id = request.args.get('doctor_id', type=int)
    department_id = request.args.get('department_id', type=int)
    query = DoctorSchedule.query.join(Doctor).filter(DoctorSchedule.visit_date == day)
    if g.user.role == 'doctor':
        query = query.filter(DoctorSchedule.doctor_id == g.user.doctor_id)
    elif doctor_id:
        query = query.filter(DoctorSchedule.doctor_id == doctor_id)
    if department_id:
        query = query.filter(Doctor.department_id == department_id)
    slots = [] if error else query.order_by(DoctorSchedule.period, Doctor.name).all()
    return render_template('schedules/list.html', slots=slots, day=day, error=error,
                           department_id=department_id, doctor_id=doctor_id,
                           doctors=Doctor.query.order_by(Doctor.name).all(),
                           departments=Department.query.order_by(Department.name).all()), 400 if error else 200


@app.route('/schedules/create', methods=['GET', 'POST'])
def schedule_create():
    error = None
    if request.method == 'POST':
        try:
            doctor_id = request.form.get('doctor_id', type=int)
            day = visit_date(request.form.get('visit_date'))
            period = request.form.get('period', '')
            capacity = request.form.get('capacity', type=int)
            fee = parse_money(request.form.get('fee', ''), '挂号费')
            if not doctor_id or not db.session.get(Doctor, doctor_id):
                raise ValueError('请选择有效医生')
            if period not in PERIODS or not capacity or not 1 <= capacity <= 999:
                raise ValueError('请选择有效时段，号源数量须为 1 至 999')
            slot = DoctorSchedule(doctor_id=doctor_id, visit_date=day, period=period, capacity=capacity, fee=fee)
            if schedule_has_ended(slot):
                raise ValueError('不能创建已结束的出诊时段')
            db.session.add(slot)
            db.session.flush()
            audit('schedule.create', 'schedule', slot.id, f'新增 {day} {PERIODS[period]} 出诊号源')
            db.session.commit()
            flash('排班已创建。', 'success')
            return redirect(url_for('schedule_list', date=day.isoformat()))
        except ValueError as invalid:
            error = str(invalid)
        except IntegrityError:
            db.session.rollback()
            error = '该医生同一日期、同一时段已有排班'
    return render_template('schedules/form.html', slot=None, error=error,
                           doctors=Doctor.query.order_by(Doctor.name).all()), 400 if error else 200


@app.route('/schedules/<int:sid>/edit', methods=['GET', 'POST'])
def schedule_edit(sid):
    slot = db.get_or_404(DoctorSchedule, sid)
    error = None
    if request.method == 'POST':
        try:
            capacity = request.form.get('capacity', type=int)
            fee = parse_money(request.form.get('fee', ''), '挂号费')
            if not capacity or not 1 <= capacity <= 999:
                raise ValueError('号源数量须为 1 至 999')
            if schedule_has_ended(slot):
                raise ValueError('已结束的排班不能调整')
            changed = db.session.execute(update(DoctorSchedule).where(
                DoctorSchedule.id == sid, DoctorSchedule.revision == request.form.get('revision', type=int),
                DoctorSchedule.booked_count <= capacity).values(
                    capacity=capacity, fee=fee, revision=DoctorSchedule.revision + 1)
                .execution_options(synchronize_session=False)).rowcount
            if not changed:
                db.session.rollback()
                raise ValueError('排班已变化，或号源数少于已挂号人数，请核对后重试')
            audit('schedule.edit', 'schedule', sid, '调整号源数量与后续挂号费，原账单不变')
            db.session.commit()
            return redirect(url_for('schedule_list', date=slot.visit_date.isoformat()))
        except ValueError as invalid:
            error = str(invalid)
    return render_template('schedules/form.html', slot=slot, doctors=[], error=error), 400 if error else 200


@app.post('/schedules/<int:sid>/status')
def schedule_status(sid):
    slot = db.get_or_404(DoctorSchedule, sid)
    opened = request.form.get('opened')
    if opened not in ('yes', 'no'):
        abort(400)
    db.session.execute(update(DoctorSchedule).where(DoctorSchedule.id == sid).values(
        is_open=(opened == 'yes'), revision=DoctorSchedule.revision + 1))
    audit('schedule.status', 'schedule', sid, '开放新增挂号' if opened == 'yes' else '暂停新增挂号，已有预约保留')
    db.session.commit()
    return redirect(url_for('schedule_list', date=slot.visit_date.isoformat()))


@app.route("/registrations")
def registration_list():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    day = request.args.get('date', '')
    query = Registration.query.join(Patient).join(Doctor).join(Department)
    if g.user.role == 'doctor':
        query = query.filter(Registration.doctor_id == g.user.doctor_id)
    if status:
        query = query.filter(Registration.status == status)
    if day:
        try:
            query = query.filter(Registration.reg_date == visit_date(day))
        except ValueError:
            return render_template('auth/error.html', message='请输入有效的就诊日期。'), 400
    if q:
        keyword = f"%{q}%"
        query = query.filter(
            or_(
                Patient.name.like(keyword),
                Doctor.name.like(keyword),
                Department.name.like(keyword),
            )
        )
    registrations = query.order_by(Registration.reg_date.desc(), Registration.id.desc()).all()
    status_counts = dict(
        db.session.query(Registration.status, func.count(Registration.id))
        .filter(Registration.doctor_id == g.user.doctor_id if g.user.role == 'doctor' else True)
        .group_by(Registration.status)
        .all()
    )
    return render_template(
        "registrations/list.html",
        registrations=registrations,
        status=status,
        q=q,
        day=day,
        status_counts=status_counts,
    )


@app.route("/registrations/create", methods=["GET", "POST"])
def registration_create():
    if request.method == "POST":
        try:
            fee = parse_money(request.form.get('expected_fee', ''), '核对挂号费')
            patient_id = request.form.get('patient_id', type=int)
            slot_id = request.form.get('schedule_id', type=int)
            day = visit_date(request.form.get('visit_date'))
            slot = db.session.get(DoctorSchedule, slot_id) if slot_id else None
            if not patient_id or not db.session.get(Patient, patient_id):
                raise ValueError('请选择有效患者')
            if not slot or slot.visit_date != day:
                raise ValueError('请选择当前日期的有效排班')
            if fee != slot.fee:
                raise ValueError('挂号费已变化，请重新查询号源并核对金额')
            if not schedule_available(slot):
                return render_registration_form('该时段已结束、暂停或号源已满，请重新选择。', 409)
        except ValueError as error:
            return render_registration_form(str(error), 400)
        try:
            now = datetime.now()
            active_periods = [period for period, end in PERIOD_END.items() if now.time() < end]
            number = db.session.execute(update(DoctorSchedule).where(
                DoctorSchedule.id == slot_id, DoctorSchedule.is_open.is_(True),
                DoctorSchedule.fee == fee, DoctorSchedule.booked_count < DoctorSchedule.capacity,
                or_(DoctorSchedule.visit_date > now.date(), and_(DoctorSchedule.visit_date == now.date(),
                                                               DoctorSchedule.period.in_(active_periods))))
                .values(booked_count=DoctorSchedule.booked_count + 1, last_number=DoctorSchedule.last_number + 1)
                .returning(DoctorSchedule.last_number).execution_options(synchronize_session=False)).scalar_one_or_none()
            if number is None:
                db.session.rollback()
                return render_registration_form('号源或价格已变化，请重新查询后挂号。', 409)
            registration = Registration(patient_id=patient_id, doctor_id=slot.doctor_id,
                                        reg_date=slot.visit_date, fee=slot.fee)
            db.session.add(registration)
            db.session.flush()
            db.session.add(ScheduleBooking(schedule_id=slot_id, registration=registration,
                                           patient_id=patient_id, queue_number=number))
            db.session.add(Bill(registration_id=registration.id, total_amount=registration.fee, is_paid=False))
            audit('registration.create', 'registration', registration.id, f'预约 {slot.visit_date} {PERIODS[slot.period]}，号序 {number}')
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return render_registration_form('该患者已挂此时段，请勿重复挂号。', 409)
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception('Failed to create appointment')
            return render_registration_form('挂号保存失败，号源与费用未扣，请核对后重试。', 503)
        flash(f'挂号成功，{PERIODS[slot.period]} {number:03d}号，已生成待缴账单。', 'success')
        return redirect(url_for('registration_list', date=day.isoformat()))
    return render_registration_form()


def render_registration_form(error=None, status=200):
    try:
        day = visit_date(request.values.get('visit_date', date.today().isoformat()))
    except ValueError as invalid:
        day, error, status = date.today(), str(invalid), 400
    department_id = request.values.get('department_id', type=int)
    doctor_id = request.args.get('doctor_id', type=int)
    query = DoctorSchedule.query.join(Doctor).filter(DoctorSchedule.visit_date == day)
    if department_id:
        query = query.filter(Doctor.department_id == department_id)
    if doctor_id:
        query = query.filter(Doctor.id == doctor_id)
    slots = query.order_by(DoctorSchedule.period, Doctor.name).all()
    return render_template('registrations/create.html', day=day, error=error, slots=slots,
                           department_id=department_id, selected_slot=request.form.get('schedule_id', ''),
                           patients=Patient.query.order_by(Patient.name).all(),
                           departments=Department.query.order_by(Department.name).all(),
                           available_count=sum(1 for slot in slots if schedule_available(slot))), status


@app.route("/registrations/<int:rid>/status", methods=["POST"])
def registration_status(rid):
    registration = Registration.query.get_or_404(rid)
    status = request.form.get('status', '')
    if status != 'cancelled':
        abort(400)
    outstanding_payment = and_(Bill.is_paid.is_(True),
                               or_(Bill.total_amount > 0, Bill.payment.has(BillPayment.amount_cents > 0)),
                               ~Bill.payment.has(BillPayment.refund.has()))
    if (registration.status != 'waiting' or registration.medical_records or
            any(bill.is_paid and not bill.refund and (bill.total_amount > 0 or
                (bill.payment and bill.payment.amount_cents > 0)) for bill in registration.bills)):
        return render_template('auth/error.html', message='仅可取消未接诊挂号；已收款账单请先退费。'), 409
    try:
        changed = db.session.execute(update(Registration).where(
            Registration.id == rid, Registration.status == 'waiting',
            ~Registration.medical_records.any(), ~Registration.bills.any(outstanding_payment))
            .values(status='cancelled').execution_options(synchronize_session=False)).rowcount
        if not changed:
            db.session.rollback()
            return render_template('auth/error.html', message='就诊或收费状态已变化，请核对后再取消。'), 409
        booking = registration.appointment
        if booking:
            released = db.session.execute(update(ScheduleBooking).where(
                ScheduleBooking.id == booking.id, ScheduleBooking.is_active.is_(True))
                .values(is_active=False).execution_options(synchronize_session=False)).rowcount
            if released:
                db.session.execute(update(DoctorSchedule).where(DoctorSchedule.id == booking.schedule_id)
                                   .values(booked_count=DoctorSchedule.booked_count - 1))
        audit('registration.status', 'registration', rid, '取消未接诊挂号，释放号源，未缴账单作废')
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception('Failed to cancel registration %s', rid)
        return render_template('auth/error.html', message='取消保存失败，原挂号与号源未改变。'), 503
    flash('挂号已取消，未缴账单已作废，号源已释放。', 'success')
    return redirect(url_for('registration_list'))


@app.route("/medical-records")
def medical_record_list():
    q = request.args.get("q", "").strip()
    query = MedicalRecord.query.join(Registration).join(Patient).join(Doctor).join(Department)
    if g.user.role == 'doctor':
        query = query.filter(Registration.doctor_id == g.user.doctor_id)
    if q:
        keyword = f"%{q}%"
        query = query.filter(
            or_(
                Patient.name.like(keyword),
                Doctor.name.like(keyword),
                Department.name.like(keyword),
                MedicalRecord.diagnosis.like(keyword),
                MedicalRecord.chief_complaint.like(keyword),
            )
        )
    records = query.order_by(MedicalRecord.created_at.desc()).all()
    return render_template("medical_records/list.html", records=records, q=q)


@app.route("/medical-records/create", methods=["GET", "POST"])
def medical_record_create():
    registrations = (
        Registration.query.filter_by(status="waiting")
        .filter(Registration.reg_date <= date.today(), ~Registration.medical_records.any())
        .filter(Registration.doctor_id == g.user.doctor_id if g.user.role == 'doctor' else True)
        .order_by(Registration.reg_date.desc(), Registration.id.desc())
        .all()
    )
    if request.method == "POST":
        registration = db.get_or_404(Registration, request.form.get('registration_id', type=int))
        if g.user.role == 'doctor' and registration.doctor_id != g.user.doctor_id:
            abort(403)
        changed = db.session.execute(update(Registration).where(
            Registration.id == registration.id, Registration.status == 'waiting',
            Registration.reg_date <= date.today(), ~Registration.medical_records.any())
            .values(status='done').execution_options(synchronize_session=False)).rowcount
        if not changed:
            db.session.rollback()
            return render_template('auth/error.html', message='该挂号尚未到就诊日期、已取消或已完成接诊。'), 409
        record = MedicalRecord(
            registration_id=registration.id,
            chief_complaint=request.form.get("chief_complaint", ""),
            diagnosis=request.form.get("diagnosis", ""),
            treatment=request.form.get("treatment", ""),
        )
        db.session.add(record)
        db.session.flush()
        audit('record.create', 'record', record.id, '保存门诊病历')
        db.session.commit()
        flash("就诊记录保存成功，挂号状态已完成", "success")
        return redirect(url_for("medical_record_list"))
    return render_template("medical_records/create.html", registrations=registrations)


@app.route("/medical-records/<int:mid>")
def medical_record_detail(mid):
    record = MedicalRecord.query.get_or_404(mid)
    if g.user.role == 'doctor' and record.registration.doctor_id != g.user.doctor_id:
        abort(403)
    return render_template("medical_records/detail.html", record=record)


@app.route("/prescriptions")
def prescription_list():
    q = request.args.get("q", "").strip()
    query = Prescription.query.join(MedicalRecord).join(Registration).join(Patient).join(Doctor)
    if g.user.role == 'doctor':
        query = query.filter(Registration.doctor_id == g.user.doctor_id)
    if q:
        keyword = f"%{q}%"
        query = query.filter(
            or_(
                Patient.name.like(keyword),
                Doctor.name.like(keyword),
                MedicalRecord.diagnosis.like(keyword),
            )
        )
    prescriptions = query.order_by(Prescription.created_at.desc()).all()
    return render_template("prescriptions/list.html", prescriptions=prescriptions, q=q)


@app.route("/prescriptions/create", methods=["GET", "POST"])
def prescription_create():
    records_without_rx = (
        MedicalRecord.query.join(Registration).filter(~MedicalRecord.prescriptions.any())
        .filter(Registration.doctor_id == g.user.doctor_id if g.user.role == 'doctor' else True)
        .order_by(MedicalRecord.created_at.desc())
        .all()
    )

    catalog = Medicine.query.filter_by(is_active=True).order_by(Medicine.name, Medicine.code).all()
    empty_row = {'medicine_id': '', 'dosage': '', 'quantity': '1', 'revision': ''}
    rows = [empty_row.copy()]
    if request.method == "POST":
        columns = {key: request.form.getlist(f'{key}[]') for key in rows[0]}
        rows = [{key: values[index] if index < len(values) else ''
                 for key, values in columns.items()}
                for index in range(max((len(values) for values in columns.values()), default=0))]
        try:
            record_id = request.form.get('medical_record_id', type=int)
            record = db.session.get(MedicalRecord, record_id) if record_id else None
            if not record or record.prescriptions:
                raise ValueError('请选择尚未开立处方的病历')
            if g.user.role == 'doctor' and record.registration.doctor_id != g.user.doctor_id:
                abort(403)
            if record.registration.status == 'cancelled':
                raise ValueError('挂号已取消，不能开立处方')
            if not 1 <= len(rows) <= 50:
                raise ValueError('每张处方须有 1 至 50 项药品')
            items = []
            selected = set()
            versions = []
            total = Decimal('0.00')
            for index, row in enumerate(rows, 1):
                try:
                    medicine_id = int(row['medicine_id'])
                except ValueError:
                    raise ValueError(f'第 {index} 项请选择目录中的药品') from None
                medicine = db.session.get(Medicine, medicine_id)
                if not medicine or not medicine.is_active:
                    raise ValueError(f'第 {index} 项药品不存在或已停用，请重新选择')
                if row['revision'] != str(medicine.revision):
                    raise ValueError(f'第 {index} 项药品价格或资料已变化，请重新选择并核对')
                if medicine_id in selected:
                    raise ValueError('同一药品不能重复添加，请合并数量')
                selected.add(medicine_id)
                versions.append((medicine_id, medicine.revision))
                if len(row['dosage'].strip()) > 64:
                    raise ValueError(f'第 {index} 项用法用量最多 64 个字')
                try:
                    quantity = int(row['quantity'])
                except ValueError:
                    raise ValueError(f'第 {index} 项数量须为正整数') from None
                if not 1 <= quantity <= 99999:
                    raise ValueError(f'第 {index} 项数量须在 1 至 99999 之间')
                unit_price = medicine.price
                total += quantity * unit_price
                if total > MAX_AMOUNT:
                    raise ValueError(f'处方合计不能超过 {MAX_AMOUNT} 元')
                items.append(PrescriptionItem(drug_name=medicine.name, dosage=row['dosage'].strip(),
                    quantity=quantity, unit_price=unit_price,
                    catalog=PrescriptionItemCatalog(medicine_id=medicine.id, code=medicine.code,
                        specification=medicine.specification, unit=medicine.unit)))
        except ValueError as error:
            flash(str(error), 'danger')
            return render_template('prescriptions/create.html', records=records_without_rx,
                                   catalog=catalog, rows=rows or [empty_row.copy()]), 400

        try:
            # Check versions under a write lock so a concurrent catalog edit cannot change this snapshot.
            for medicine_id, revision in sorted(versions):
                changed = db.session.execute(update(Medicine).where(
                    Medicine.id == medicine_id, Medicine.is_active.is_(True), Medicine.revision == revision
                ).values(revision=Medicine.revision).execution_options(synchronize_session=False)).rowcount
                if not changed:
                    db.session.rollback()
                    abort(409, description='药品资料已变化，处方未提交，请刷新并核对')
            prescription = Prescription(medical_record_id=record.id, items=items)
            db.session.add(prescription)
            db.session.flush()
            db.session.add(PrescriptionRecordClaim(medical_record_id=record.id, prescription_id=prescription.id))
            db.session.add(Bill(registration_id=record.registration_id, prescription_id=prescription.id,
                                total_amount=total, is_paid=False))
            audit('prescription.create', 'prescription', prescription.id, '按药品目录开立处方并生成药费账单')
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, description='此病历已开立处方，请刷新查看，未重复计费')
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception('Prescription creation failed')
            abort(503, description='处方与账单均未保存，请稍后重试')
        flash("处方开立成功，系统已自动生成待缴费账单", "success")
        return redirect(url_for("prescription_list"))

    return render_template("prescriptions/create.html", records=records_without_rx, rows=rows, catalog=catalog)


@app.get('/prescriptions/<int:pid>')
def prescription_detail(pid):
    prescription = db.get_or_404(Prescription, pid)
    if g.user.role == 'doctor' and prescription.medical_record.registration.doctor_id != g.user.doctor_id:
        abort(403)
    return render_template('prescriptions/detail.html', rx=prescription)


@app.route("/bills")
def bill_list():
    status = request.args.get("status", "")
    bill_type = request.args.get("type", "")
    q = request.args.get("q", "").strip()
    query = Bill.query.join(Registration).join(Patient)
    if status == "paid":
        query = query.filter(Bill.is_paid.is_(True), ~Bill.payment.has(BillPayment.refund.has()))
    elif status == "unpaid":
        query = query.filter(Bill.is_paid.is_(False), Registration.status != 'cancelled')
    elif status == 'void':
        query = query.filter(Bill.is_paid.is_(False), Registration.status == 'cancelled')
    elif status == "refunded":
        query = query.filter(Bill.payment.has(BillPayment.refund.has()))
    if bill_type == "registration":
        query = query.filter(Bill.prescription_id.is_(None))
    elif bill_type == "prescription":
        query = query.filter(Bill.prescription_id.is_not(None))
    if q:
        keyword = f"%{q}%"
        query = query.filter(Patient.name.like(keyword))
    bills = query.order_by(Bill.is_paid.asc(), Bill.id.desc()).all()
    unpaid_count = Bill.query.join(Registration).filter(Bill.is_paid.is_(False), Registration.status != 'cancelled').count()
    paid_count = Bill.query.filter(Bill.is_paid.is_(True)).count()
    unpaid_amount = (
        db.session.query(func.coalesce(func.sum(Bill.total_amount), 0))
        .join(Registration).filter(Bill.is_paid.is_(False), Registration.status != 'cancelled')
        .scalar()
    )
    paid_amount = (
        db.session.query(func.coalesce(func.sum(Bill.total_amount), 0))
        .filter(Bill.is_paid.is_(True))
        .scalar()
    )
    refunded_cents = db.session.query(func.coalesce(func.sum(BillRefund.amount_cents), 0)).scalar()
    refunded_amount = Decimal(refunded_cents) / 100
    return render_template(
        "bills/list.html",
        bills=bills,
        status=status,
        bill_type=bill_type,
        q=q,
        unpaid_count=unpaid_count,
        paid_count=paid_count,
        unpaid_amount=unpaid_amount,
        paid_amount=paid_amount,
        refunded_amount=refunded_amount,
        net_amount=paid_amount - refunded_amount,
    )


@app.route("/bills/transactions")
def bill_transactions():
    q = request.args.get('q', '').strip()
    kind = request.args.get('kind', '')
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    error = None
    rows = []
    income = Decimal('0.00')
    expense = Decimal('0.00')
    try:
        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        error = '请输入有效的查询日期'
    if not error and start and end and start > end:
        error = '开始日期不能晚于结束日期'
    if kind not in ('', 'income', 'refund'):
        error = '请选择有效的收支类型'
    if not error:
        payments = (Bill.query.join(Registration).join(Patient).outerjoin(BillPayment)
                    .filter(Bill.is_paid.is_(True)))
        refunds = (BillRefund.query.join(BillPayment).join(Bill).join(Registration).join(Patient))
        if q:
            keyword = f'%{q}%'
            payments = payments.filter(or_(Patient.name.like(keyword), Patient.phone.like(keyword),
                                            BillPayment.receipt_no.like(keyword)))
            refunds = refunds.filter(or_(Patient.name.like(keyword), Patient.phone.like(keyword),
                                          BillPayment.receipt_no.like(keyword), BillRefund.refund_no.like(keyword)))
        if start:
            payments = payments.filter(Bill.paid_at >= datetime.combine(start, time.min))
            refunds = refunds.filter(BillRefund.refunded_at >= datetime.combine(start, time.min))
        if end:
            payments = payments.filter(Bill.paid_at <= datetime.combine(end, time.max))
            refunds = refunds.filter(BillRefund.refunded_at <= datetime.combine(end, time.max))
        if kind != 'refund':
            for bill in payments.all():
                payment = bill.payment
                amount = payment.amount if payment else money(bill.total_amount)
                income += amount
                rows.append({'kind': 'income', 'bill': bill, 'amount': amount,
                             'number': payment.receipt_no if payment else None,
                             'original_number': None, 'method': payment.method if payment else None,
                             'occurred_at': bill.paid_at, 'reason': '收款' if payment else '历史收款（未登记流水号）'})
        if kind != 'income':
            for refund in refunds.all():
                expense += refund.amount
                rows.append({'kind': 'refund', 'bill': refund.payment.bill, 'amount': refund.amount,
                             'number': refund.refund_no, 'original_number': refund.payment.receipt_no,
                             'method': refund.payment.method, 'occurred_at': refund.refunded_at,
                             'reason': refund.reason})
        actor_logs = OperationLog.query.filter(OperationLog.action.in_(['bill.pay', 'bill.refund']),
                                               OperationLog.target_id.in_([row['bill'].id for row in rows])).all()
        actors = {(log.action, log.target_id): log.actor_name for log in actor_logs}
        for row in rows:
            row['operator'] = actors.get(('bill.refund' if row['kind'] == 'refund' else 'bill.pay', row['bill'].id))
        rows.sort(key=lambda row: (row['occurred_at'] or datetime.min, row['number'] or ''), reverse=True)
    return render_template('bills/transactions.html', rows=rows, q=q, kind=kind,
                           start_date=start_date, end_date=end_date, error=error,
                           income=income, expense=expense, net_amount=income-expense), 400 if error else 200


@app.route("/bills/<int:bid>")
def bill_detail(bid):
    bill = Bill.query.get_or_404(bid)
    return render_bill(bill)


def render_bill(bill, error=None, status=200):
    summary = bill_summary(bill)
    if 'payment_token' not in session:
        session['payment_token'] = secrets.token_hex(32)
    return render_template(
        "bills/detail.html",
        bill=bill,
        bill_items=summary['items'],
        summary=summary,
        error=error,
        payment_token=session['payment_token'],
        bill_type=get_bill_type(bill),
        refund_error=refund_eligibility(bill) if bill.is_paid and not bill.refund else None,
        operators={log.action: log.actor_name for log in OperationLog.query.filter(
            OperationLog.target_type == 'bill', OperationLog.target_id == bill.id).all()},
    ), status


@app.route("/bills/<int:bid>/pay", methods=["POST"])
def bill_pay(bid):
    bill = Bill.query.get_or_404(bid)
    target = url_for('bill_detail', bid=bid)
    token = request.form.get('payment_token', '')
    if not token or not secrets.compare_digest(token.encode(), session.get('payment_token', '').encode()):
        return render_bill(bill, '页面已失效，请刷新后重新核对收款。', 403)
    if bill.is_paid:
        flash("该账单已退费，不能再次收款" if bill.refund else "该账单已结清，无需重复缴费", "info")
        return redirect(target)
    summary = bill_summary(bill)
    if summary['error']:
        return render_bill(bill, summary['error'], 409)
    try:
        expected = parse_money(request.form.get('expected_amount', ''), '核对金额')
        if expected != money(bill.total_amount):
            return render_bill(bill, '账单金额已变化，请重新核对后收款。', 409)
        method = request.form.get('payment_method', '')
        if method not in PAYMENT_METHODS:
            raise ValueError('请选择支付方式')
        received = parse_money(request.form.get('received_amount', ''), '实收金额')
        if received < expected:
            raise ValueError('实收金额不能少于应收金额')
        if method != 'cash' and received != expected:
            raise ValueError('非现金支付的实收金额须等于应收金额')
        if request.form.get('confirmed') != 'yes':
            raise ValueError('请先确认患者、费用明细及到账金额')
    except ValueError as error:
        return render_bill(bill, str(error), 400)
    paid_at = datetime.now()
    try:
        # Conditional update and unique payment belong to one transaction.
        changed = db.session.execute(
            update(Bill).where(Bill.id == bid, Bill.is_paid.is_(False),
                               Bill.total_amount == expected,
                               Bill.registration.has(Registration.status != 'cancelled'))
            .values(is_paid=True, paid_at=paid_at)
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed != 1:
            db.session.rollback()
            flash('账单状态已变化，请查看最新收款记录。', 'info')
            return redirect(target)
        db.session.add(BillPayment(
            bill_id=bid, receipt_no=f'RC{paid_at:%Y%m%d}{uuid4().hex[:24].upper()}',
            method=method, amount_cents=int(expected * 100),
            received_cents=int(received * 100), change_cents=int((received - expected) * 100),
            paid_at=paid_at,
        ))
        audit('bill.pay', 'bill', bid, '确认收款并生成收款流水')
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception('Failed to settle bill %s', bid)
        return render_bill(db.session.get(Bill, bid), '收款记录保存失败，请核对账单状态后重试。', 503)
    flash("缴费成功，账单已结清", "success")
    return redirect(target)


@app.route("/bills/<int:bid>/refund", methods=["POST"])
def bill_refund(bid):
    bill = Bill.query.get_or_404(bid)
    target = url_for('bill_detail', bid=bid)
    token = request.form.get('payment_token', '')
    if not token or not secrets.compare_digest(token.encode(), session.get('payment_token', '').encode()):
        return render_bill(bill, '页面已失效，请刷新后重新核对退费。', 403)
    if bill.refund:
        flash('该账单已全额退费，无需重复办理。', 'info')
        return redirect(target)
    error = refund_eligibility(bill)
    if error:
        return render_bill(bill, error, 409)
    try:
        expected = parse_money(request.form.get('expected_amount', ''), '核对退款金额')
        if expected != bill.payment.amount or request.form.get('payment_receipt') != bill.payment.receipt_no:
            return render_bill(bill, '原收款信息已变化，请刷新后核对。', 409)
        reason = request.form.get('reason', '').strip()
        if not reason or len(reason) > 500:
            raise ValueError('请填写退费原因，最多 500 个字')
        if request.form.get('confirmed') != 'yes':
            raise ValueError('请先核对原收款流水，并确认已向患者退还款项')
    except ValueError as invalid:
        return render_bill(bill, str(invalid), 400)
    refunded_at = datetime.now()
    try:
        # Insert from current payment state; unique payment_id serializes competing refunds.
        eligible = select(
            BillPayment.id, literal(f'RF{refunded_at:%Y%m%d}{uuid4().hex[:24].upper()}'),
            BillPayment.amount_cents, literal(reason), literal(refunded_at),
        ).join(Bill).where(
            Bill.id == bid, Bill.is_paid.is_(True), Bill.total_amount == expected,
            BillPayment.receipt_no == request.form['payment_receipt'],
            BillPayment.amount_cents == int(expected * 100), ~BillPayment.refund.has(),
        )
        changed = db.session.execute(insert(BillRefund).from_select(
            ['payment_id', 'refund_no', 'amount_cents', 'reason', 'refunded_at'], eligible)).rowcount
        if changed != 1:
            db.session.rollback()
            flash('退费状态已变化，请核对最新流水。', 'info')
            return redirect(target)
        audit('bill.refund', 'bill', bid, '登记全额退费并生成退费流水')
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if db.session.get(Bill, bid).refund:
            flash('该账单已全额退费，无需重复办理。', 'info')
            return redirect(target)
        app.logger.exception('Failed to save refund for bill %s', bid)
        return render_bill(db.session.get(Bill, bid), '退费保存失败，请核对流水后重试。', 503)
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception('Failed to save refund for bill %s', bid)
        return render_bill(db.session.get(Bill, bid), '退费保存失败，请核对流水后重试。', 503)
    flash('全额退费已登记，原收款流水已保留。', 'success')
    return redirect(target)


@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("数据库表已创建。")


@app.cli.command("seed")
def seed_data():
    db.create_all()

    if Department.query.first():
        print("数据库已有基础数据，已跳过填充。")
        return

    departments = [
        Department(name="内科", description="诊治内脏器官及常见慢性疾病"),
        Department(name="外科", description="开展外伤、感染、肿瘤等手术治疗"),
        Department(name="儿科", description="儿童常见病、发育评估与健康管理"),
        Department(name="妇产科", description="妇科、产科及围产期管理"),
        Department(name="急诊科", description="急危重症初筛、抢救与转诊"),
    ]
    db.session.add_all(departments)
    db.session.flush()

    doctors = [
        Doctor(name="张伟", department_id=departments[0].id, title="主任医师", phone="13800001001"),
        Doctor(name="李娜", department_id=departments[0].id, title="副主任医师", phone="13800001002"),
        Doctor(name="王强", department_id=departments[1].id, title="主任医师", phone="13800001003"),
        Doctor(name="陈静", department_id=departments[2].id, title="主治医师", phone="13800001004"),
        Doctor(name="刘洋", department_id=departments[4].id, title="副主任医师", phone="13800001005"),
    ]
    db.session.add_all(doctors)

    patients = [
        Patient(name="赵小明", gender="男", age=35, phone="13900002001", address="北京市朝阳区"),
        Patient(name="钱小红", gender="女", age=28, phone="13900002002", address="北京市海淀区"),
        Patient(name="孙建国", gender="男", age=52, phone="13900002003", address="北京市丰台区"),
    ]
    db.session.add_all(patients)
    db.session.commit()

    print("演示数据已填充。")
    print(f"  科室 {len(departments)} 个")
    print(f"  医生 {len(doctors)} 个")
    print(f"  患者 {len(patients)} 个")


if __name__ == "__main__":
    app.run(debug=True)
