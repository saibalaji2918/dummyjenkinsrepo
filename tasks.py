#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author: Arvin
# datetime: 5/24/2019 4:33 PM
# software: PyCharm
import datetime
import time
import json
import os
import xlsxwriter
import requests
import calendar
from requests.auth import HTTPBasicAuth
from mysite.utils import tempDir
from django.conf import settings
from django.db.models import Q
from django.db import DatabaseError
from mysite.celery_init import periodic_task
from celery.schedules import crontab
from django.utils.translation import gettext_lazy as _
from django.core.cache import cache
from mysite import celery_app
from mysite.att.models import PayloadBase
from mysite.base import tasks as base_tasks
from mysite.mobile import tasks as mobile_tasks
from mysite.personnel import tasks as personnel_task
from mysite.utils import get_system_setting
from mysite.personnel.models import Employee
from mysite.att.calc.views import att_calculate
from mysite.base.models import SystemSetting
from mysite.utils import tempFile, truncTime, trunc
from mysite.iclock.models.model_transaction import Transaction
from mysite.admin.services.email import send_one_mail_with_attachments

DAILY, WEEKLY, MONTHLY = 3, 2, 1


def process_calculation(start_date, end_date, company_id):
    emps = list(Employee.objects.filter(enable_att=1, company_id=company_id).values_list('id', flat=True))

    for i in range(int(len(emps) / 1000) + 1):
        eids = emps[i * 1000:(i + 1) * 1000]
        att_calculate(eids, start_date, end_date, company_id=company_id)

    tempFile("job_%s.txt" % (datetime.datetime.now().strftime("%Y%m")),
             '%s %s' % (datetime.datetime.now().strftime("%Y%m%d%H%M%S"), 'exception email calculation finished'))


def process_notify(start_date, end_date, params, company_id, frequency):
    _start_date = datetime.datetime(start_date.year, start_date.month, start_date.day)
    _end_date = datetime.datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59)
    process_calculation(_start_date, _end_date, company_id)
    datas = PayloadBase.objects.filter(att_date__range=(_start_date, _end_date)).filter(
        Q(late__gt=0) | Q(early_leave__gt=0) | Q(absent__gt=0), emp__company_id=company_id
    ).select_related('emp')
    # datas = datas[:10]

    exception_emp = {}
    for data in datas:
        detail = exception_emp.get(data.emp.id)
        if detail:
            detail['late_times'] += 1 if data.late else 0
            detail['early_leave_times'] += 1 if data.early_leave else 0
            detail['absent_times'] += 1 if data.absent else 0
        else:
            detail = {}
            detail['emp_code'] = data.emp.emp_code
            detail['first_name'] = data.emp.first_name
            detail['email'] = data.emp.email
            detail['mobile'] = data.emp.mobile
            detail['app'] = data.emp.app_status
            detail['emp_obj'] = data.emp
            detail['late_times'] = 1 if data.late else 0
            detail['early_leave_times'] = 1 if data.early_leave else 0
            detail['absent_times'] = 1 if data.absent else 0
            detail['sms'] = data.emp.mobile if data.emp.enable_sms and data.emp.sms_exception else False
            detail['whatsapp'] = data.emp.mobile if data.emp.enable_whatsapp and data.emp.whatsapp_exception else False
            exception_emp[data.emp.id] = detail

    keys = ('late_times', 'early_leave_times', 'absent_times')
    exception_dept = {}
    for k in exception_emp:
        hit = False
        for key in keys:
            if exception_emp[k][key] > params[key]:
                hit = True
                break

        if hit:
            print(exception_emp[k]['emp_code'],
                  exception_emp[k]['late_times'],
                  exception_emp[k]['early_leave_times'],
                  exception_emp[k]['absent_times'])

            detail = exception_dept.get(exception_emp[k]['emp_obj'].department.id)
            if detail:
                detail['emps'].append(exception_emp[k])
            else:
                detail = {}
                detail['dept'] = exception_emp[k]['emp_obj'].department
                detail['emps'] = [exception_emp[k]]
                exception_dept[exception_emp[k]['emp_obj'].department.id] = detail

            if exception_emp[k].get('email', None):
                print('send a email to ', exception_emp[k]['emp_code'])
                base_tasks.delivery_exception_email.delay(exception_emp[k]['emp_obj'], exception_emp[k],
                                                          _start_date, _end_date, company_id, frequency)
                pass
            if exception_emp[k].get('email', None) and exception_emp[k]['emp_obj'].app_status == 1:
                mobile_tasks.delivery_attendance_exception.delay(exception_emp[k]['emp_obj'], exception_emp[k],
                                                                 _start_date, _end_date, company_id)
                pass
            if exception_emp[k].get('sms') and params['sms_alert']:
                personnel_task.delivery_exception_sms.delay(exception_emp[k]['emp_obj'], exception_emp[k],
                                                            start_date, end_date, company_id)
            if exception_emp[k].get('whatsapp') and params['whatsapp_alert']:
                personnel_task.delivery_exception_whatsapp.delay(exception_emp[k]['emp_obj'], exception_emp[k],
                                                                 start_date, end_date, company_id)

    for k in exception_dept:
        print('send a email to ', exception_dept[k]['dept'].dept_name)
        dept = exception_dept[k]['dept']
        all_membership = dept.membership_set.all().filter(company_id=company_id)
        managers = [m.user for m in all_membership]
        for manager in managers:
            base_tasks.delivery_exception_email_to_depament_manage(
                manager, exception_dept[k]['emps'], _start_date, _end_date, company_id)
            pass


def test_att_exception_alert():
    start_date = datetime.datetime.strptime('2019-11-01', '%Y-%m-%d')
    end_date = datetime.datetime.strptime('2019-11-15', '%Y-%m-%d')
    alert_setting = get_system_setting('alert_setting')
    params = {
        'late_times': int(alert_setting.get('late_exceed', 0)),
        'early_leave_times': int(alert_setting.get('early_leave_exceed', 0)),
        'absent_times': int(alert_setting.get('absent_exceed', 0))
    }
    # process_notify(start_date, end_date, params)


@periodic_task(run_every=60, name='att.tasks.check_alert_setting')
def check_alert_setting():
    if 'att' not in settings.SALE_MODULE:
        return
    alert_query = SystemSetting.objects.filter(name='alert_setting')
    for alert in alert_query:
        company_id = str(alert.company_id)
        att_exception_alert(company_id)


def att_exception_alert(company_id):
    """
    Sending attendance exception alert email after attendance calculation,
    It is related with alert setting(System -> Configuration -> Alert Setting)
    :return:
    """
    alert_setting = get_system_setting('alert_setting', company_id)
    if not alert_setting:
        return
    frequency = int(alert_setting.get('email_frequency', 0))
    if not frequency:
        return
    if frequency not in (DAILY, WEEKLY, MONTHLY):
        return
    processing_time = alert_setting.get('email_time', None)
    if not processing_time:
        return
    now = datetime.datetime.now()
    if now.strftime('%H:%M') != processing_time[:5]:
        return
    day_interval = int(alert_setting.get('sending_day', -1))
    email_day = int(alert_setting.get('email_day', 1))
    params = {
        'late_times': int(alert_setting.get('late_exceed', 0)),
        'early_leave_times': int(alert_setting.get('early_leave_exceed', 0)),
        'absent_times': int(alert_setting.get('absent_exceed', 0)),
        'sms_alert': bool(alert_setting.get('sms_alert', False)),
        'whatsapp_alert': bool(alert_setting.get('whatsapp_alert', False))
    }
    end_date = now + datetime.timedelta(days=day_interval)
    result_flag = False
    if frequency == DAILY:
        start_date = end_date
        process_notify(start_date, end_date, params, company_id, frequency)
        result_flag = True
    elif frequency == WEEKLY:
        week_day = now.weekday()
        if week_day == email_day:
            start_date = end_date + datetime.timedelta(days=-6)
            process_notify(start_date, end_date, params, company_id, frequency)
            result_flag = True
    elif frequency == MONTHLY:
        """
        If email day = 1 then the period will be last month
        if email day > 1 then the period will be current month 1st to email day
        """
        day = now.day
        if day == email_day:
            last_date = end_date + datetime.timedelta(days=-1)
            if last_date.month == now.month:
                start_date = datetime.datetime(end_date.year, end_date.month, 1)
            else:
                start_date = datetime.datetime(last_date.year, last_date.month, 1)
                end_date = last_date
            process_notify(start_date, end_date, params, company_id, frequency)
            result_flag = True

    if result_flag:
        obj = SystemSetting.objects.filter(name='alert_setting', company_id=company_id).first()
        if obj:
            alert_setting['last_alert_time'] = now.strftime('%Y-%m-%d %H:%M')
            obj.value = json.dumps(alert_setting)
            obj.save()


@periodic_task(run_every=10, name='att.tasks.circle_check_att_data_file')
def circle_check_att_data_file():
    alert_query = SystemSetting.objects.filter(name='alert_setting')
    for alert in alert_query:
        company_id = str(alert.company_id)
        check_att_data_file(alert.value, company_id)


def check_att_data_file(alert_setting, company_id):
    import os
    import json
    # from django.conf import ADDITION_FILE_ROOT
    from mysite.utils import get_system_setting
    if not alert_setting:
        return
    else:
        alert_setting = json.loads(alert_setting)
    sms_alert = alert_setting.get('sms_alert', False)
    whatsapp_alert = alert_setting.get('whatsapp_alert', False)
    temperature_alert = alert_setting.get('temperature_alert', False)
    mask_status = alert_setting.get('mask_status', False)
    sms_setting = whatsapp_setting = ''
    if sms_alert:
        sms_setting = get_system_setting('sms_setting', company_id=company_id)
    if whatsapp_alert:
        whatsapp_setting = get_system_setting('whatsapp_setting', company_id=company_id)
    if sms_setting or whatsapp_setting or temperature_alert or mask_status:
        att_data_path = os.path.join(settings.ADDITION_FILE_ROOT, 'att_data/')
        if not os.path.exists(att_data_path):
            try:
                os.makedirs(att_data_path)
            except FileExistsError:
                # directory already exists
                pass
        file_list = os.listdir(att_data_path)
        if not file_list:
            return
        for file in file_list:
            path = os.path.join(att_data_path, file)
            if path:
                if os.path.isfile(path):
                    with open(path, 'r') as rf:
                        rdata = rf.read()
                    os.remove(path)
                    if rdata:
                        att_data = json.loads(rdata)
                        send_push_message(att_data, sms_setting, whatsapp_setting, temperature_alert, mask_status)


def send_push_message(att_data, sms_setting, whatsapp_setting, temperature_alert, mask_status):
    from mysite.personnel.models.model_employee import Employee
    from mysite.base.tasks import delivery_email_alert_for_temp_and_mask_status
    from decimal import Decimal
    temperature_setting = SystemSetting.objects.filter(name='temp_mask_setting')
    emp = Employee.objects.filter(id=att_data.get('emp_id'))
    if emp:
        emp = emp[0]
        if sms_setting and emp.enable_sms and emp.sms_punch:
            sms_push_send.delay(emp, sms_setting, att_data)
        if whatsapp_setting and emp.enable_whatsapp and emp.whatsapp_punch:
            whatsapp_push_send.delay(emp, whatsapp_setting, att_data)
        if temperature_alert or mask_status:
            from mysite.accounts.models import MyUser
            from mysite.att.utils import temperature_update
            get_admin_users = MyUser.objects.filter(current_company=emp.company_id)
            transaction_data = Transaction.objects.filter(punch_time=att_data.get('punch_time'),
                                                          company_id=emp.company_id).select_related('emp')
            email_list = []
            for each in get_admin_users:
                if each.is_superuser and each.email:
                    email_list.append(each.email)
            if temperature_setting:
                temperature_setting = get_system_setting('temp_mask_setting', company_id=emp.company_id)
            for data in transaction_data:
                company_id = emp.company_id
                punch_time = att_data.get('punch_time')
                temp = temperature_update(data.temperature, company_id)
                if data.mask_flag == 0 and mask_status:
                    subject = 'No mask alert'
                    alert_name = 'mask_alert'
                    delivery_email_alert_for_temp_and_mask_status(subject, emp.emp_code, email_list, company_id,
                                                                  emp.first_name, alert_name, punch_time)
                else:
                    if isinstance(temperature_setting, dict):
                        if 'high_temp_min' in temperature_setting and 'high_temp_max' in temperature_setting:
                            if temperature_setting['high_temp_min'] and temperature_setting['high_temp_max']:
                                if isinstance(temp, Decimal) and float(temperature_setting['high_temp_min']) <= temp <= float(
                                        temperature_setting['high_temp_max']) and temperature_alert:
                                    subject = 'Abnormal temperature alert'
                                    alert_name = 'abnormal_temperature_alert'
                                    delivery_email_alert_for_temp_and_mask_status(subject, emp.emp_code,
                                                                                  email_list, company_id,
                                                                                  emp.first_name, alert_name,
                                                                                  punch_time, temp)


@celery_app.task(bind=True, name='att.tasks.sms_push_send')
def sms_push_send(task, emp, sms_setting, att_data):
    from mysite.personnel.send_tripartite_message import format_punch_text
    from mysite.personnel.send_tripartite_message import send_sms
    if not sms_setting:
        return
    punch_text = format_punch_text(emp.first_name, 'punch', att_data.get('punch_time', ''))
    current_user = sms_setting.get('user_id', 1)
    send_sms(current_user=current_user, api_key=sms_setting['sms_apikey'], sms_provider=sms_setting['provider'],
             number=emp.mobile, text=punch_text, sender=sms_setting['sender'], company_id=emp.company_id)


@celery_app.task(bind=True, name='att.tasks.whatsapp_push_send')
def whatsapp_push_send(task, emp, whatsapp_setting, att_data):
    from mysite.personnel.send_tripartite_message import format_punch_text
    from mysite.personnel.send_tripartite_message import sent_whatsapp
    punch_text = format_punch_text(emp.first_name, 'punch', att_data.get('punch_time', ''))
    if whatsapp_setting:
        current_user = whatsapp_setting.get('user_id', 1)
        sent_whatsapp(current_user=current_user, api_key=whatsapp_setting['whatsapp_apikey'],
                      number=emp.mobile, text=punch_text, company_id=emp.company_id)


# @periodic_task(run_every=crontab(minute=0, hour=23), name="att.tasks.auto_calculation")
@periodic_task(run_every=60, name='att.tasks.auto_calculation')
def auto_calculation():
    from datetime import datetime, timedelta
    from mysite.att.calc.views import att_calculate
    from mysite.cloud.models import Company
    from mysite.personnel.models import Employee
    from mysite.admin.const import EASYTIMEPRO
    from mysite.admin.utils import get_software_type_without_load

    nt = datetime.now()
    d1 = trunc(nt) - timedelta(days=1)  # calculate more 1 day
    d2 = truncTime(nt)
    if get_software_type_without_load()['software_type'] != EASYTIMEPRO:
        return

    companies = Company.objects.values_list('id', flat=True)
    for company_id in companies:
        alert_setting = get_system_setting('alert_setting', company_id)
        if not alert_setting: continue

        auto_cal_settings = alert_setting.get('autocalculation_alert', False)
        if not auto_cal_settings: continue

        autocalculation_alert_time = alert_setting.get('autocalculation_alert_time', '23:00')
        if nt.strftime("%H:%M") in [autocalculation_alert_time]:
            emps = Employee.objects.filter(company_id=company_id).values_list('id', flat=True)
            if not emps: continue

            att_calculate(emps, d1, d2, request=None, company_id=company_id)


@celery_app.task(bind=True, name='att.tasks.auto_calculation_approved_records')
def auto_calculation_approved_records(task, emps, d1, d2, request, company_id):
    from mysite.att.calc.views import att_calculate
    alert_setting = get_system_setting('alert_setting', company_id)
    if alert_setting:
        data = alert_setting.get('autocalculation_alert', False)
        if data:
            att_calculate(emps, d1, d2, request, company_id)
        else:
            pass


@periodic_task(run_every=60, name='att.tasks.api_sync_transaction')
def api_sync_transaction():
    from datetime import datetime, timedelta
    from mysite.cloud.models import Company
    from mysite.admin.const import EASYTIMEPRO, EASYWDMS
    from mysite.admin.utils import get_software_type_without_load
    import requests
    from requests.auth import HTTPBasicAuth

    nt = datetime.now()
    if get_software_type_without_load()['software_type'] not in [EASYTIMEPRO, EASYWDMS]:
        return

    companies = Company.objects.values_list('id', flat=True)
    for company_id in companies:
        cache_key = "%s_%s_%s" % (settings.UNIT, 'api_setting', str(company_id))
        api_setting = get_system_setting('api_setting', company_id)
        if not api_setting: continue
        username = api_setting.get('api_username', '')
        password = api_setting.get('api_userpassword', '')
        url = api_setting.get('api_url', '')
        data_template = api_setting.get('api_dataTemplate', '')
        interval_time = api_setting.get('api_intervalTime', '')
        last_sync_time_str = api_setting.get('api_lastsyncTime', '')
        last_sync_time = datetime.strptime(last_sync_time_str, "%Y-%m-%d %H:%M:%S") if last_sync_time_str else datetime(
            1970, 1, 1, 0, 0, 0)
        check_last_sync_time = last_sync_time + timedelta(minutes=int(interval_time))

        if check_last_sync_time.strftime('%Y-%m-%d %H:%M') <= nt.strftime('%Y-%m-%d %H:%M'):
            data = get_transactions(last_sync_time, nt, data_template, company_id)
            if not data:
                continue
            response = requests.post(url, json=data, headers={"Content-Type": "application/json"},
                                     auth=HTTPBasicAuth(username, password))
            if response.status_code == 200:
                obj = SystemSetting.objects.filter(name='api_setting', company_id=company_id).first()
                if obj:
                    api_setting['api_lastsyncTime'] = nt.strftime('%Y-%m-%d %H:%M:%S')
                    obj.value = json.dumps(api_setting)
                    obj.save()
                    setting_data = json.loads(obj.value)
                    cache.set(cache_key, setting_data)


def get_transactions(start, end, data_template, company_id):
    import re
    from mysite.iclock.auto_export import format_verify, format_date, format_punch, format_short_time, \
        format_short_date, format_emp_code, data_string
    from mysite.iclock.models import Transaction

    _param = re.compile(r"'?(\w+)'?,?")
    column_names = _param.findall(data_template)
    _column_names = [name.upper() for name in column_names]
    db_keys = {
        'company_name': 'company__name',
        'emp_code': 'emp_code',
        'first_name': 'emp__first_name',
        'last_name': 'emp__last_name',
        'dept_name': 'emp__department__dept_name',
        'position_name': 'emp__position__position_name',
        'punch_datetime': 'punch_time',
        'punch_date': 'punch_time',
        'punch_time': 'punch_time',
        'punch_state': 'punch_state',
        'verify_type': 'verify_type',
        'work_code': 'work_code',
        'gps_location': 'gps_location',
        'longitude': 'longitude',
        'latitude': 'latitude',
        'area_name': 'terminal__area__area_name',
        'card_number': 'emp__card_no',
        'terminal_sn': 'terminal_sn',
        'terminal_alias': 'terminal__alias',
        'upload_time': 'upload_time',
        'temperature': 'temperature',
        'mask_flag': 'mask_flag',
    }
    params = {
        'short_date': '5',
        'short_time': '5',
    }
    export_list = [db_keys.get(item, None) for item in column_names]
    export_list = filter(None, export_list)
    export_keys = [item for item in column_names if db_keys.get(item, None)]
    payload = []
    if export_list:
        trans = {
            'verify_type': format_verify,
            'emp_code': format_emp_code,
            'punch_datetime': format_date,
            'punch_date': format_short_date,
            'punch_time': format_short_time,
            'punch_state': format_punch,
        }
        queryset = Transaction.objects.filter(upload_time__gt=start, upload_time__lte=end,
                                              company_id=company_id).order_by('upload_time')

        queryset = queryset.values(*export_list)
        if queryset:
            for r in queryset:
                tmp = [trans.get(db_f, data_string)(r, r[db_keys[db_f]], params, company_id) for db_f in
                       export_keys]
                data = dict(zip(_column_names, tmp))
                payload.append(data)
    return payload


@celery_app.task(bind=True, name='att.tasks.build_workflow_for_leave')
def build_workflow_for_leave(signal, sender, created, **kwargs):
    instance = kwargs.get('instance', None)

    if not instance:
        return None

    from django.dispatch.dispatcher import receiver
    from django.contrib.contenttypes.models import ContentType
    from mysite.workflow.views import approve_by_admin, reject_by_admin, revoke_by_admin
    from mysite.mobile.tasks import prepare_mobile_notifications
    from mysite.att.models_choices import AUDIT_SUCCESS, CANCEL_AUDIT_SUCCESS
    commit_time = instance.apply_time
    start_date = instance.start_time.date()
    end_date = instance.end_time.date()
    ct = ContentType.objects.get_for_model(instance).id

    if created:
        time.sleep(0.2)
        from mysite.workflow.models.workflow_builder import WorkflowInstanceBuilder
        WorkflowInstanceBuilder().build_workflow_instance_for(instance.employee, ct, instance.id, start_date, end_date)
        prepare_mobile_notifications.delay(instance, 'leave', instance.employee, commit_time,
                                           applicant=instance.employee, approval_status=instance.audit_status)
        return True

    else:

        if not hasattr(instance, '_approve_user'):
            return None

        approver_auth_user = instance._approve_user
        approve_audit_status = instance.audit_status
        remark = instance.audit_reason

        if not approve_audit_status or not approver_auth_user:
            return None

        if approve_audit_status == AUDIT_SUCCESS:
            approve_by_admin(approver_auth_user, instance, instance.employee, ct, instance.id, remark)
        elif approve_audit_status == CANCEL_AUDIT_SUCCESS:
            revoke_by_admin(approver_auth_user, instance, instance.employee, ct, instance.id, remark)
        else:
            reject_by_admin(approver_auth_user, instance, instance.employee, ct, instance.id, remark)
        prepare_mobile_notifications.delay(instance, 'leave', instance.employee, commit_time,
                                           applicant=instance.employee,
                                           approval_status=instance.audit_status,
                                           node_approver=approver_auth_user)
        if instance.audit_status in [AUDIT_SUCCESS, CANCEL_AUDIT_SUCCESS]:
            d1 = instance.start_time
            d2 = instance.end_time
            auto_calculation_approved_records.delay([instance.employee_id], d1, d2, request=None,
                                                    company_id=instance.company_id)
        return True

def creating_and_sending_reports_for_mail(report, date, url, mails, company_id, username, password, basic_url, file_extension):
    current_year_month = datetime.datetime.now().strftime("%Y%m")
    current_datetime = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    tempFile(f"job_{current_year_month}.txt", f'{current_datetime} task running with url={url}')
    file_path = os.path.join(tempDir(), f"{report}{date}.{file_extension}")
    headers_data = {"Accept": "application/json"}
    login_url = f'{basic_url}/login/'
    with requests.Session() as client:
        client.get(login_url)
        csrftoken = client.cookies.get('csrftoken', '')
        login_data = {'username': username, 'password': password, 'csrfmiddlewaretoken': csrftoken}
        client.post(login_url, data=login_data, headers=headers_data)
        response_data = client.get(url, headers=headers_data)
        tempFile(f'job_{current_year_month}.txt', f'{current_datetime} url status={response_data.status_code}')
        if response_data.status_code == 200:
            if file_extension in ["xlsx", "csv"]:
                with open(file_path, 'wb') as file_obj:
                    file_obj.write(response_data.content)
            for mail_id in mails:
                send_one_mail_with_attachments(report, report, [mail_id], attachments=[file_path], company_id=company_id)


def get_columns_date(start_date, end_date):
    from datetime import timedelta
    column_dates = ''
    start_date = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    end_date = datetime.datetime.strptime(end_date, '%Y-%m-%d')
    delta = end_date - start_date
    for i in range(delta.days + 1):
        column_dates = column_dates + (start_date + timedelta(days=i)).strftime('%m/%d') + ','
    return column_dates


@celery_app.task(bind=True, name="AutoCalculateAndSendReports")
def get_att_reports_urls(task, reports_list, now, start_date, end_date, mails, company_id, username, password, scheme,
                         ip_address, port, file_extension):
    from mysite.att.calc.views import att_calculate

    emp_ids = Employee.objects.filter(company_id=company_id).values_list('id', flat=True)

    # Calculating before sending mails
    calc_start_date = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    calc_end_date = datetime.datetime.strptime(end_date, '%Y-%m-%d')
    att_calculate(emp_ids, calc_start_date, calc_end_date, request=None, company_id=company_id)

    today_date = str(datetime.datetime.strptime(now.strftime('%Y-%m-%d'),
                                                '%Y-%m-%d').date())
    first_day_of_month = datetime.datetime(int(now.strftime('%Y')),
                                           int(now.strftime('%m')), 1
                                           ).date()
    last_day_of_month = datetime.datetime(int(now.strftime('%Y')),
                                          int(now.strftime('%m')),
                                          int(calendar.monthrange(int(now.strftime('%Y')),
                                                                  int(now.strftime('%m')))[1])
                                          ).date()
    basic_url = '{scheme}//{ip}:{port}'.format(scheme=scheme, ip=ip_address, port=port)
    for report in reports_list:
        if report == 'transaction':
            report_name = 'Transaction Report'
            url = basic_url + f'/att/api/transactionReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,att_date,punch_time,punch_state,work_code,source,displayed_temp&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'mobile_transaction':
            report_name = 'Mobile Transaction Report'
            url = basic_url + f'/att/api/mobiletransactionReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,att_date,punch_time,punch_state,work_code,source,gps_location&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'total_punches':
            report_name = 'Total Punches Report'
            url = basic_url + f'/att/api/timeCardReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,att_date,punch_times,punch_set&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'first_last':
            report_name = 'First&Last Report'
            url = basic_url + f'/att/api/firstLastReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,att_date,weekday,first_punch,last_punch,total_time,in_temp,out_temp&company_id={company_id}&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'schedule_log':
            report_name = 'Scheduled Log Report'
            url = basic_url + f'/att/api/scheduledLogReport/export/?export_headers=emp_code,first_name,dept_name,att_date,weekday,punch_time,punch_state,correct_state&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'total_timecard':
            report_name = 'Total Time Card Report'
            url = basic_url + f'/att/api/totalTimeCardReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,att_date,weekday,att_exception,timetable,duration,check_in,check_out,duty_duration,work_day,clock_in,clock_out,total_time,duty_wt,actual_wt,unscheduled,remaining,late,break_late,early_leave,break_early,absent,break_absent,leave,total_worked,normal_wt,break_duration,normal_ot,weekend_ot,holiday_ot,ot_lv1,ot_lv2,ot_lv3,attendance_status&company_id={company_id}&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'missed_in_out':
            report_name = 'Missed In & Out Punch Report'
            url = basic_url + f'/att/api/exceptionReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,timetable,att_date,description&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'late':
            report_name = 'Late Report'
            url = basic_url + f'/att/api/lateReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,att_date,weekday,timetable,check_in,check_out,clock_in,clock_out,total_time,late&company_id={company_id}&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'early_leave':
            report_name = 'Early Leave Report'
            url = basic_url + f'/att/api/earlyLeaveReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,att_date,weekday,timetable,check_in,check_out,clock_in,clock_out,total_time,early_leave&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'birthday':
            report_name = 'Birthday Report'
            url = basic_url + f'/att/api/empBirthdayReport/export/?export_headers=emp_code,first_name,last_name,nick_name,birthday,dept_code,dept_name&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'absent':
            report_name = 'Absent Report'
            url = basic_url + f'/att/api/absentReport/export/?export_headers=emp_code,first_name,dept_name,att_date,weekday,timetable,check_in,check_out,clock_in,clock_out,total_time,late,early_leave,absent,status,Remarks&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'half_day':
            report_name = 'Half Day Report'
            url = basic_url + f'/att/api/halfDayReport/export/?export_headers=emp_code,first_name,dept_name,att_date,weekday,timetable,check_in,check_out,duty_duration,clock_in,clock_out,total_time,half_day&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'daily_attendance':
            report_name = 'Daily Attendance Report'
            url = basic_url + f'/att/api/dailyAttendanceReport/export/?export_headers=emp_code,first_name,dept_name,att_date,timetable,duration,clock_in,clock_out,actual_wt,total_ot,normal_ot,weekend_ot,holiday_ot,total_worked,attendance_status,Remarks&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'daily_details':
            report_name = 'Daily Details Report'
            url = basic_url + f'/att/api/dailyDetailsReport/export/?export_headers=emp_code,first_name,dept_name,att_date,timetable,duration,check_in,check_out,clock_in_old,clock_out_old,actual_wt,total_ot,late,early_leave,total_worked,attendance_status,punch_set&company_id={company_id}&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=2&limit=999999'
        elif report == 'daily_summary':
            report_name = 'Daily Summary Report'
            url = basic_url + f'/att/api/dailySummaryReport/export/?export_headers=emp_code,first_name,dept_name,att_date,timetable,clock_in,clock_out,total_worked,attendance_status,Remarks&start_date={start_date}&end_date={end_date}&departments=-1&areas=-1&positions=-1&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'daily_status':
            report_name = 'Daily Status Report'
            column_dates = get_columns_date(start_date, end_date)
            url = basic_url + f'/att/api/dailyStatusReport/export/?export_headers=emp_code,first_name,last_name,nick_name,gender,dept_code,dept_name,position_code,position_name,{column_dates},total_late,total_early_leave,total_absent,total_worked,total_not,total_wot,total_hot,total_leave,leave_1,leave_2,leave_3,leave_4,leave_5,leave_6&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'basic_status':
            report_name = 'Basic Status Report'
            column_dates = get_columns_date(str(first_day_of_month), str(last_day_of_month))
            url = basic_url + f'/att/api/monthlyBasicStatusReport/export/?export_headers=emp_code,first_name,dept_name,{column_dates},total_present_times,total_absent_times,total_leave_times,total_holiday_times,total_holiday_present_times,total_week_off_times,total_week_off_present_times&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'status_summary':
            report_name = 'Status Summary Report'
            url = basic_url + f'/att/api/monthlyStatusSummaryReport/export/?export_headers=emp_code,first_name,dept_name,present,total_absent,total_holiday_times,total_holiday_present,total_week_off,total_week_off_present,leave_1,leave_2,leave_3,leave_4,leave_5,leave_6,total_leave,total_present&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        elif report == 'work_detailed':
            report_name = 'Work Detailed Report'
            column_dates = get_columns_date(str(first_day_of_month), str(last_day_of_month))
            url = basic_url + f'/att/api/monthlyDetailedSummaryReport/export/?export_headers=emp_code,first_name,dept_name,data_type,{column_dates}&company_id={company_id}&start_date={start_date}&end_date={end_date}&page_size=999999&export_type=xls&page=1&export_style=&limit=999999'
        else:
            return
        creating_and_sending_reports_for_mail(report_name, today_date, url, mails, company_id, username, password, basic_url, file_extension)


@celery_app.task(bind=True)
def get_report_settings_data(task, report_setting, company_id):
    from mysite.zkauth import zkDecrypt

    if not report_setting:
        return
    frequency = int(report_setting.get('auto_email_frequency', 0))
    if not frequency:
        return
    if frequency not in (DAILY, WEEKLY, MONTHLY):
        return
    processing_time = report_setting.get('email_time', None)
    if not processing_time:
        return
    now = datetime.datetime.now()
    mails = report_setting.get('mails', None)
    username = report_setting.get('admin_username', None)
    admin_password = report_setting.get('admin_password', None)
    password = zkDecrypt(bytes(admin_password, 'utf8'), 'biotime').decode('utf8')
    scheme = report_setting.get('scheme', None)
    ip_address = report_setting.get('ip_address', None)
    file_extension = report_setting.get('file_extension', None)
    port = report_setting.get('port', None)
    if now.strftime('%H:%M') != processing_time:
        return
    day_interval = int(report_setting.get('mail_sending_day', -1))
    start_date = now
    email_day = report_setting.get('auto_email_day', '1')
    end_date = now + datetime.timedelta(days=day_interval)
    result_flag = False
    if frequency == DAILY:
        start_date = end_date
        result_flag = True
    elif frequency == WEEKLY:
        week_day = now.weekday()
        if week_day == int(email_day):
            start_date = end_date + datetime.timedelta(days=-6)
            result_flag = True
    elif frequency == MONTHLY:
        """
        If email day = 1 then the period will be last month
        if email day > 1 then the period will be current month 1st to email day
        """
        day = now.day
        if day == int(email_day):
            start_date = (now.replace(day=1) - datetime.timedelta(days=1)).replace(day=now.day)
            result_flag = True
    else:
        return
    start_date = start_date.strftime('%Y-%m-%d')
    end_date = end_date.strftime('%Y-%m-%d')
    if result_flag:
        obj = SystemSetting.objects.filter(name='report_setting', company_id=company_id).first()
        if obj:
            report_setting['last_email_alert_time'] = now.strftime('%Y-%m-%d %H:%M')
            obj.value = json.dumps(report_setting)
            obj.save()
    reports_list = list(report_setting)
    if result_flag and now.strftime("%H:%M") in [processing_time]:
        if reports_list and len(mails) >= 1:
            get_att_reports_urls(reports_list, now, start_date, end_date, mails, company_id, username, password, scheme,
                                 ip_address, port, file_extension)


@periodic_task(run_every=60, name='att.tasks.send_att_reports')
def send_att_reports():
    if settings.CLOUD_VERSION:
        return
    if 'att' not in settings.SALE_MODULE:
        return
    report_query = SystemSetting.objects.filter(name='report_setting')
    for report in report_query:
        if not report.company_id:
            return

        report_setting = json.loads(report.value)
        auto_mail_trigger = report_setting.get('auto_mail_trigger', False)
        email_setting = get_system_setting('email_setting', report.company_id)
        alert_setting = get_system_setting('alert_setting', report.company_id)
        if auto_mail_trigger and email_setting and alert_setting:
            mail_alert = alert_setting.get('email_alert', False)
            if not mail_alert:
                return
            get_report_settings_data.delay(report_setting, str(report.company_id))
