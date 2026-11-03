"""初始化数据库并填充演示数据。"""
from app import app
from models import Department, Doctor, Patient, db


with app.app_context():
    db.create_all()

    if Department.query.first():
        print("数据库已初始化，跳过。若需重新初始化请删除 instance/his.db")
    else:
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
