"""Run browser fixtures in a database explicitly supplied by the test runner."""
import argparse
from decimal import Decimal
import os
from pathlib import Path
import sys


parser = argparse.ArgumentParser()
parser.add_argument('--database', required=True)
parser.add_argument('--port', type=int, required=True)
args = parser.parse_args()
os.environ['HIS_DATABASE_URI'] = 'sqlite:///' + Path(args.database).resolve().as_posix()
os.environ['HIS_SECRET_KEY'] = 'isolated-browser-test-secret'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app
from models import Bill, Department, Doctor, MedicalRecord, Patient, Registration, db


with app.app_context():
    db.create_all()
    department = Department(name='内科')
    doctor = Doctor(name='演示医生', department=department)
    patient = Patient(name='测试患者', gender='女', age=30)
    for _ in range(2):
        registration = Registration(patient=patient, doctor=doctor, fee=Decimal('10.99'))
        db.session.add(registration)
        db.session.flush()
        db.session.add(Bill(registration_id=registration.id, total_amount=Decimal('10.99')))
        db.session.add(MedicalRecord(registration=registration, diagnosis='演示病历'))
    db.session.commit()

app.run(port=args.port, debug=False, use_reloader=False)
