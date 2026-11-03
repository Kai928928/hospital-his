import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from auth import audit
from billing import parse_money
from models import Medicine, db


medicines = Blueprint('medicines', __name__)


def medicine_fields():
    fields = {key: request.form.get(key, '').strip() for key in ('code', 'name', 'specification', 'unit')}
    fields['code'] = fields['code'].upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{0,31}', fields['code']):
        raise ValueError('药品编码须为 1 至 32 位字母、数字、下划线或短横线，并以字母或数字开头')
    for key, label, limit in [('name', '药品名称', 128), ('specification', '规格', 128), ('unit', '计价单位', 16)]:
        if not 1 <= len(fields[key]) <= limit:
            raise ValueError(f'{label}不能为空，且最多 {limit} 个字')
    fields['price'] = parse_money(request.form.get('price', ''), '药品单价')
    return fields


@medicines.get('/medicines')
def list_():
    q = request.args.get('q', '').strip()
    status = request.args.get('status', 'active')
    query = Medicine.query
    if status in ('active', 'inactive'):
        query = query.filter(Medicine.is_active.is_(status == 'active'))
    if q:
        query = query.filter(or_(Medicine.code.like(f'%{q}%'), Medicine.name.like(f'%{q}%'),
                                 Medicine.specification.like(f'%{q}%')))
    page = query.order_by(Medicine.code).paginate(page=request.args.get('page', 1, type=int), per_page=20, error_out=False)
    return render_template('medicines/list.html', page=page, q=q, status=status)


@medicines.route('/medicines/create', methods=['GET', 'POST'])
def create():
    error = None
    if request.method == 'POST':
        try:
            medicine = Medicine(**medicine_fields())
            db.session.add(medicine)
            db.session.flush()
            audit('medicine.create', 'medicine', medicine.id, f'新增药品 {medicine.code}，单价 {medicine.price} 元/{medicine.unit}')
            db.session.commit()
            flash('药品已加入目录。', 'success')
            return redirect(url_for('medicines.list_'))
        except ValueError as invalid:
            error = str(invalid)
        except IntegrityError:
            db.session.rollback()
            error = '药品编码已存在，请使用不同编码'
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception('Medicine creation failed')
            abort(503, description='药品未保存，请稍后重试')
    return render_template('medicines/form.html', medicine=None, error=error), 400 if error else 200


@medicines.route('/medicines/<int:mid>/edit', methods=['GET', 'POST'])
def edit(mid):
    medicine = db.get_or_404(Medicine, mid)
    error = None
    if request.method == 'POST':
        try:
            fields = medicine_fields()
            old_price = medicine.price
            changed = db.session.execute(update(Medicine).where(
                Medicine.id == mid, Medicine.revision == request.form.get('revision', type=int)
            ).values(**fields, revision=Medicine.revision + 1).execution_options(synchronize_session=False)).rowcount
            if not changed:
                db.session.rollback()
                flash('药品资料已被更新，请重新核对后保存。', 'warning')
                return redirect(url_for('medicines.edit', mid=mid), code=303)
            audit('medicine.edit', 'medicine', mid, f'更新 {fields["code"]}，单价 {old_price} → {fields["price"]} 元，历史处方不变')
            db.session.commit()
            flash('药品资料已更新。', 'success')
            return redirect(url_for('medicines.list_', status='all'))
        except ValueError as invalid:
            error = str(invalid)
        except IntegrityError:
            db.session.rollback()
            error = '药品编码已存在，请使用不同编码'
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception('Medicine update failed')
            abort(503, description='药品未更新，请稍后重试')
    return render_template('medicines/form.html', medicine=medicine, error=error), 400 if error else 200


@medicines.post('/medicines/<int:mid>/status')
def status(mid):
    db.get_or_404(Medicine, mid)
    active = request.form.get('active')
    if active not in ('yes', 'no'):
        abort(400)
    try:
        changed = db.session.execute(update(Medicine).where(
            Medicine.id == mid, Medicine.revision == request.form.get('revision', type=int)
        ).values(is_active=(active == 'yes'), revision=Medicine.revision + 1)).rowcount
        if not changed:
            db.session.rollback()
            abort(409, description='药品资料已变化，请刷新后重试')
        audit('medicine.status', 'medicine', mid, '启用药品' if active == 'yes' else '停用药品，历史处方保留')
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Medicine status update failed')
        abort(503, description='状态未更新，请稍后重试')
    return redirect(url_for('medicines.list_', status='all'))
