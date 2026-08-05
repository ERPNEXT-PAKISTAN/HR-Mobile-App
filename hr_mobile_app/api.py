
from base64 import b64encode
from html import escape
from io import BytesIO

import frappe
from frappe.utils import add_months, cint, flt, getdate, get_first_day, get_last_day, nowdate, get_url


def _resolve_employee(user=None):
	user = user or frappe.session.user
	if user == "Guest":
		return None
	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name:
		emp_name = frappe.db.get_value("Employee", {"user_id": user}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"
	return emp_name


def _employee_image_url(image):
	if not image:
		return ""
	if str(image).startswith(("http://", "https://", "/")):
		return str(image)
	return f"/files/{image.lstrip('/')}"


def _file_url(path):
	if not path:
		return ""
	# Prefer absolute URL for mobile webview reliability
	try:
		return get_url(path if str(path).startswith("/") else f"/{path}")
	except Exception:
		return path if str(path).startswith("/") else f"/{path}"


def _employee_qr_data_uri(card_data):
	"""Build a self-contained QR image so the employee card also works offline."""
	from pyqrcode import create as qrcreate

	# Low error correction keeps this data-rich code readable by mobile cameras at card size.
	qr_text = "\n".join(
		f"{label}: {card_data.get(field) or '-'}"
		for label, field in (
			("Company Name", "company_name"),
			("Company Address", "company_address"),
			("Employee Name", "employee_name"),
			("Employee Code", "employee_code"),
			("Employee Address", "employee_address"),
			("Mobile", "mobile"),
			("Designation", "designation"),
			("Department", "department"),
			("Website", "website"),
		)
	)
	stream = BytesIO()
	try:
		qrcreate(qr_text, error="L", mode="binary").svg(
			stream, scale=6, background="#ffffff", module_color="#101f45", quiet_zone=4
		)
		return f"data:image/svg+xml;base64,{b64encode(stream.getvalue()).decode()}"
	finally:
		stream.close()


def _get_company_card_details(company):
	if not company:
		return {"name": "", "logo": "", "address": ""}

	company_doc = frappe.get_doc("Company", company)
	logo = _file_url(getattr(company_doc, "company_logo", None))
	addresses = frappe.db.sql(
		"""
		SELECT a.address_line1, a.address_line2, a.city, a.state, a.pincode, a.country
		FROM `tabAddress` a
		INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name
		WHERE dl.parenttype = 'Address'
			AND dl.link_doctype = 'Company'
			AND dl.link_name = %s
		ORDER BY a.is_primary_address DESC, a.modified DESC
		LIMIT 1
		""",
		(company,),
		as_dict=True,
	)
	address = ""
	if addresses:
		row = addresses[0]
		address = ", ".join(
			str(row.get(field)).strip()
			for field in ("address_line1", "address_line2", "city", "state", "pincode", "country")
			if row.get(field)
		)
	return {
		"name": company_doc.company_name or company_doc.name,
		"logo": logo,
		"address": address,
		"website": getattr(company_doc, "website", None) or "",
	}


def _build_employee_card(emp_doc):
	company = _get_company_card_details(emp_doc.company)
	card_data = {
		"company_name": company["name"],
		"company_address": company["address"],
		"employee_name": emp_doc.employee_name,
		"employee_code": emp_doc.name,
		"employee_address": getattr(emp_doc, "current_address", None) or "",
		"mobile": getattr(emp_doc, "cell_number", None) or "",
		"designation": emp_doc.designation or "",
		"department": emp_doc.department or "",
		"website": company["website"],
	}
	return {"company": company, "qr_code": _employee_qr_data_uri(card_data), **card_data}


CHECKIN_VISIT_FIELDS = (
	"custom_doctor", "custom_hospital_name", "custom_remarks", "custom_market_shift",
)


def _checkin_visit_fields():
	"""Return only optional visit fields that exist on this site's Employee Checkin."""
	meta = frappe.get_meta("Employee Checkin")
	return {fieldname for fieldname in CHECKIN_VISIT_FIELDS if meta.has_field(fieldname)}


def _has_doctors_doctype():
	return bool(frappe.db.exists("DocType", "Doctors"))


@frappe.whitelist()
def get_hr_app_user_info():
	user = frappe.session.user
	if user == "Guest":
		return {"logged_in": False}

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name:
		emp_name = frappe.db.get_value("Employee", {"user_id": user}, "name")

	# Fallback for testing with Administrator
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	if not emp_name:
		return {
			"logged_in": True,
			"has_employee": False,
			"user": user,
			"is_manager": False
		}

	emp_doc = frappe.get_doc("Employee", emp_name)

	# Check if this employee is a manager (has subordinates reporting to them)
	subordinates = frappe.get_all("Employee",
		filters={"reports_to": emp_name, "status": "Active"},
		fields=["name", "employee_name", "designation", "department", "user_id", "image", "cell_number", "company_email"]
	)
	for s in subordinates:
		s["image"] = _file_url(_employee_image_url(s.get("image")))
	is_manager = len(subordinates) > 0

	# Leave balance summary
	year_start = getdate(nowdate()).replace(month=1, day=1)
	year_end = getdate(nowdate()).replace(month=12, day=31)
	leave_allocations = frappe.get_all("Leave Allocation",
		filters={
			"employee": emp_name, "docstatus": 1,
			"from_date": ["<=", year_end], "to_date": [">=", year_start],
		},
		fields=["leave_type", "total_leaves_allocated", "from_date", "to_date"]
	)

	leave_summary = []
	for alloc in leave_allocations:
		taken_result = frappe.db.sql("""
			SELECT SUM(total_leave_days) FROM `tabLeave Application`
			WHERE employee = %s AND leave_type = %s AND status = 'Approved'
			AND from_date <= %s AND to_date >= %s AND docstatus = 1
		""", (emp_name, alloc.leave_type, year_end, year_start))
		taken = flt(taken_result[0][0]) if taken_result and taken_result[0][0] else 0.0

		leave_summary.append({
			"leave_type": alloc.leave_type,
			"allocated": alloc.total_leaves_allocated,
			"taken": taken,
			"balance": alloc.total_leaves_allocated - taken
		})

	# Get today's last checkin status
	today = frappe.utils.today()
	checkins_today = frappe.get_all("Employee Checkin",
		filters={"employee": emp_name, "time": [">=", today + " 00:00:00"]},
		fields=["name", "time", "log_type", "latitude", "longitude"],
		order_by="time desc",
		limit=1
	)
	last_checkin = checkins_today[0] if checkins_today else None

	# Get current shift details
	shift_assignment = frappe.db.get_value("Shift Assignment", 
		{"employee": emp_name, "status": "Active"}, 
		["shift_type", "start_date", "end_date"], 
		as_dict=True
	)

	shift_details = None
	shift_type_name = shift_assignment.shift_type if shift_assignment else emp_doc.default_shift
	if not shift_type_name:
		shift_type_name = "Morning Shift"

	if shift_type_name:
		st = None
		if frappe.db.exists("Shift Type", shift_type_name):
			st = frappe.get_doc("Shift Type", shift_type_name)
		
		if st:
			shift_details = {
				"shift_type": shift_type_name,
				"start_time": str(st.start_time) if st.start_time else "09:00:00",
				"end_time": str(st.end_time) if st.end_time else "17:00:00",
				"enable_auto_attendance": st.enable_auto_attendance
			}
		else:
			shift_details = {
				"shift_type": shift_type_name,
				"start_time": "09:00:00",
				"end_time": "17:00:00",
				"enable_auto_attendance": 1
			}

	return {
		"logged_in": True,
		"has_employee": True,
		"employee_card": _build_employee_card(emp_doc),
		"employee": {
			"name": emp_doc.name,
			"employee_name": emp_doc.employee_name,
			"designation": emp_doc.designation,
			"department": emp_doc.department,
			"company": emp_doc.company,
			"branch": emp_doc.branch,
			"user_id": emp_doc.user_id,
			"gender": emp_doc.gender,
			"current_address": getattr(emp_doc, "current_address", None) or "",
			"cell_number": getattr(emp_doc, "cell_number", None) or "",
			"image": _file_url(_employee_image_url(emp_doc.image)),
			"date_of_joining": str(emp_doc.date_of_joining) if emp_doc.date_of_joining else ""
		},
		"is_manager": is_manager,
		"subordinates": subordinates,
		"leave_summary": leave_summary,
		"last_checkin": last_checkin,
		"shift_details": shift_details
	}


@frappe.whitelist()
def download_employee_card_pdf():
	"""Download the signed-in employee's card as a print-ready PDF."""
	if frappe.session.user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)
	employee = _resolve_employee()
	if not employee:
		frappe.throw("No Employee document is linked to your user account.")

	emp_doc = frappe.get_doc("Employee", employee)
	card = _build_employee_card(emp_doc)
	company = card["company"]
	photo = _file_url(_employee_image_url(emp_doc.image))
	logo_html = (
		f'<img class="logo" src="{escape(company["logo"], quote=True)}">'
		if company["logo"] else '<div class="logo-fallback">C</div>'
	)
	photo_html = (
		f'<img class="id-card-photo" src="{escape(photo, quote=True)}">'
		if photo else f'<div class="id-card-photo fallback">{escape((emp_doc.employee_name or "E")[:1])}</div>'
	)
	html = f"""
	<!doctype html><html><head><meta charset="utf-8"><style>
	@page {{ size: A5 portrait; margin: 12mm; }}
	* {{ box-sizing:border-box; -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
	body {{ margin:0; font-family:Arial,sans-serif; color:#fff; background:#fff; }}
	.employee-id-card {{ width:90mm; margin:0 auto; overflow:hidden; border-radius:26px; color:#fff; background-color:#18376f; background-image:linear-gradient(155deg,#101f45 0%,#18376f 55%,#244f92 100%); }}
	.id-card-company {{ min-height:92px; display:table; width:100%; padding:20px; color:#fff; text-align:center; background-color:#0a1735; border-bottom:1px solid #506b9d; }}
	.company-inner {{ display:table-cell; vertical-align:middle; }}
	.logo,.logo-fallback {{ display:inline-block; width:48px; height:48px; padding:5px; object-fit:contain; vertical-align:middle; border-radius:12px; background:#fff; margin-right:12px; }}
	.logo-fallback {{ padding:0; line-height:48px; color:#18376f; font-size:22px; font-weight:800; }}
	.id-card-company-name {{ display:inline-block; max-width:220px; vertical-align:middle; font-size:18px; line-height:1.2; font-weight:700; }}
	.id-card-body {{ display:table; width:100%; padding:30px 25px 22px; color:#fff; background-color:#18376f; }}
	.photo-cell,.qr-cell {{ display:table-cell; vertical-align:middle; }} .qr-cell {{ width:138px; text-align:center; }}
	.id-card-photo {{ width:146px; height:170px; object-fit:cover; border-radius:20px; border:5px solid #fff; }}
	.fallback {{ line-height:160px; text-align:center; color:#18376f; background:#dce8ff; font-size:44px; font-weight:800; }}
	.id-card-qr {{ width:138px; height:138px; padding:8px; border-radius:14px; background:#fff; }}
	.qr-label {{ margin-top:7px; color:#b9d4ff; font-size:9px; font-weight:700; letter-spacing:1.2px; text-transform:uppercase; }}
	.id-card-details {{ padding:0 25px 27px; color:#fff; background-color:#18376f; }}
	.id-card-name {{ font-size:24px; line-height:1.15; font-weight:800; }}
	.id-card-designation {{ margin-top:5px; color:#b9d4ff; font-size:13px; font-weight:700; }}
	.id-card-meta {{ display:table; width:100%; margin-top:20px; }}
	.meta-item {{ display:table-cell; width:50%; padding-top:10px; border-top:1px solid rgba(185,212,255,.3); }}
	.meta-item + .meta-item {{ padding-left:12px; }}
	.meta-label {{ display:block; color:#b9d4ff; font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.8px; }}
	.meta-value {{ display:block; margin-top:4px; color:#fff; font-size:12px; font-weight:700; }}
	.id-card-info {{ margin-top:18px; padding:14px; border-radius:13px; background-color:#315080; color:#fff; font-size:11px; line-height:1.5; }}
	.id-card-info strong {{ display:block; margin-bottom:2px; color:#b9d4ff; font-size:9px; text-transform:uppercase; letter-spacing:.8px; }}
	.mobile {{ display:block; margin-top:9px; color:#fff; font-size:13px; font-weight:700; }}
	</style></head><body>
	<article class="employee-id-card">
	<div class="id-card-company"><div class="company-inner">{logo_html}<span class="id-card-company-name">{escape(card["company_name"])}</span></div></div>
	<div class="id-card-body"><div class="photo-cell">{photo_html}</div><div class="qr-cell"><img class="id-card-qr" src="{card["qr_code"]}"><div class="qr-label">Scan to identify</div></div></div>
	<div class="id-card-details"><div class="id-card-name">{escape(card["employee_name"] or "")}</div><div class="id-card-designation">{escape(card["designation"] or "Employee")}</div>
	<div class="id-card-meta"><div class="meta-item"><span class="meta-label">Employee Code</span><span class="meta-value">{escape(card["employee_code"])}</span></div><div class="meta-item"><span class="meta-label">Department</span><span class="meta-value">{escape(card["department"] or "-")}</span></div></div>
	<div class="id-card-info"><strong>Company Address</strong>{escape(card["company_address"] or "-")}<span class="mobile">{escape(card["mobile"] or "-")}</span></div></div>
	</article></body></html>"""
	from frappe.utils.pdf import get_pdf

	frappe.local.response.filename = f"Employee-Card-{emp_doc.name}.pdf"
	frappe.local.response.filecontent = get_pdf(html, options={"background": None, "print-media-type": None})
	frappe.local.response.type = "download"


@frappe.whitelist()
def get_checkin_visit_options():
	"""Return suggestions for the optional call/visit check-in fields."""
	if frappe.session.user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	available_fields = _checkin_visit_fields()
	if "custom_doctor" not in available_fields or not _has_doctors_doctype():
		return {"enabled": False, "doctors": []}

	doctor_fields = ["name"]
	if frappe.get_meta("Doctors").has_field("hospital_name"):
		doctor_fields.append("hospital_name")
	doctors = frappe.get_all(
		"Doctors", fields=doctor_fields, order_by="modified desc",
		limit_page_length=200, ignore_permissions=True,
	)
	return {"enabled": True, "doctors": doctors}


@frappe.whitelist()
def mark_employee_checkin(
	log_type, latitude=None, longitude=None, custom_doctor=None,
	custom_hospital_name=None, custom_remarks=None, custom_market_shift=None,
):
	from datetime import timedelta

	from frappe.utils import get_datetime, now_datetime

	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = _resolve_employee(user)
	if not emp_name:
		frappe.throw("No active Employee linked to your user account.")

	log_type = (log_type or "").strip().upper()
	if log_type not in ("IN", "OUT"):
		frappe.throw("Invalid log type. Use IN or OUT.")

	custom_doctor = (custom_doctor or "").strip()
	custom_remarks = (custom_remarks or "").strip()
	custom_market_shift = (custom_market_shift or "").strip()
	visit_fields = _checkin_visit_fields()
	doctors_available = _has_doctors_doctype()
	if "custom_doctor" in visit_fields and custom_doctor and doctors_available and not frappe.db.exists("Doctors", custom_doctor):
		frappe.throw("Please select a valid Doctor.")
	# Hospital is dependent on Doctor; never accept a mismatched client value.
	custom_hospital_name = None
	if doctors_available and custom_doctor and frappe.get_meta("Doctors").has_field("hospital_name"):
		custom_hospital_name = frappe.db.get_value("Doctors", custom_doctor, "hospital_name")
	if "custom_market_shift" in visit_fields and custom_market_shift and custom_market_shift not in ("Morning", "Evening"):
		frappe.throw("Please select a valid Shift.")

	# Ignore accidental double-taps within a few seconds (same log type).
	recent = frappe.db.sql(
		"""
		SELECT name, time, log_type, latitude, longitude, shift, device_id, employee
		FROM `tabEmployee Checkin`
		WHERE employee = %s
			AND log_type = %s
			AND time >= %s
		ORDER BY time DESC
		LIMIT 1
		""",
		(emp_name, log_type, now_datetime() - timedelta(seconds=8)),
		as_dict=True,
	)
	if recent:
		row = recent[0]
		return {
			"status": "success",
			"message": f"Already checked {log_type} at {frappe.utils.format_datetime(row.time)}",
			"checkin": row,
			"duplicate": 1,
		}

	shift = frappe.db.get_value("Shift Assignment", {"employee": emp_name, "status": "Active"}, "shift_type")
	if not shift:
		shift = frappe.db.get_value("Employee", emp_name, "default_shift") or "Morning Shift"

	# HRMS strips microseconds and rejects same employee+log_type+timestamp.
	check_time = get_datetime(now_datetime()).replace(microsecond=0)
	for _ in range(5):
		exists = frappe.db.exists(
			"Employee Checkin",
			{"employee": emp_name, "time": check_time, "log_type": log_type},
		)
		if not exists:
			break
		check_time = check_time + timedelta(seconds=1)

	try:
		checkin_values = {
				"doctype": "Employee Checkin",
				"employee": emp_name,
				"log_type": log_type,
				"time": check_time,
				"device_id": "Mobile Web App",
				"shift": shift,
				"latitude": flt(latitude) if latitude else None,
				"longitude": flt(longitude) if longitude else None,
			}
		optional_values = {
			"custom_doctor": custom_doctor or None,
			"custom_hospital_name": custom_hospital_name or None,
			"custom_remarks": custom_remarks or None,
			"custom_market_shift": custom_market_shift or None,
		}
		checkin_values.update({key: value for key, value in optional_values.items() if key in visit_fields})
		checkin = frappe.get_doc(checkin_values)
		checkin.insert(ignore_permissions=True)
		frappe.db.commit()
	except frappe.ValidationError as e:
		frappe.db.rollback()
		# Soft-fail duplicate race: return latest matching log instead of traceback.
		msg = str(e)
		if "same timestamp" in msg.lower() or "already has a log" in msg.lower():
			latest = frappe.get_all(
				"Employee Checkin",
				filters={"employee": emp_name, "log_type": log_type},
				fields=["name", "time", "log_type", "latitude", "longitude", "shift", "device_id", "employee"],
				order_by="time desc",
				limit=1,
			)
			if latest:
				return {
					"status": "success",
					"message": f"Already checked {log_type} at {frappe.utils.format_datetime(latest[0].time)}",
					"checkin": latest[0],
					"duplicate": 1,
				}
		frappe.clear_messages()
		return {"status": "error", "message": frappe.utils.strip_html(msg) or "Could not save check-in."}

	return {
		"status": "success",
		"message": f"Successfully checked {log_type} at {frappe.utils.format_datetime(checkin.time)}",
		"checkin": checkin.as_dict(),
	}


@frappe.whitelist()
def get_employee_checkins(from_date=None, to_date=None):
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	if not emp_name:
		return []

	filters = {"employee": emp_name}
	if from_date and to_date:
		filters["time"] = ["between", [from_date + " 00:00:00", to_date + " 23:59:59"]]

	fields = ["name", "time", "log_type", "device_id", "shift"] + sorted(_checkin_visit_fields())
	checkins = frappe.get_all("Employee Checkin",
		filters=filters,
		fields=fields,
		order_by="time desc",
		limit=100
	)
	return checkins


@frappe.whitelist()
def get_team_checkins(from_date=None, to_date=None):
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	if not emp_name:
		return []

	# Get subordinates
	subordinates = frappe.get_all("Employee", filters={"reports_to": emp_name, "status": "Active"}, fields=["name"])
	if not subordinates:
		return []

	sub_names = [s.name for s in subordinates]
	filters = {"employee": ["in", sub_names]}
	if from_date and to_date:
		filters["time"] = ["between", [from_date + " 00:00:00", to_date + " 23:59:59"]]

	checkins = frappe.get_all("Employee Checkin",
		filters=filters,
		fields=["name", "employee", "employee_name", "time", "log_type", "latitude", "longitude", "device_id", "shift"],
		order_by="time desc",
		limit=200
	)
	images = {
		r.name: _file_url(_employee_image_url(r.image))
		for r in frappe.get_all("Employee", filters={"name": ["in", sub_names]}, fields=["name", "image"])
	}
	for c in checkins:
		c["image"] = images.get(c.employee, "")
	return checkins


@frappe.whitelist()
def get_employee_leaves():
	"""Return only leave applications overlapping the current calendar year."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)
	emp_name = _resolve_employee(user)
	if not emp_name:
		return []
	today = getdate(nowdate())
	year_start = today.replace(month=1, day=1)
	year_end = today.replace(month=12, day=31)
	return frappe.get_all(
		"Leave Application",
		filters={
			"employee": emp_name,
			"from_date": ["<=", year_end],
			"to_date": [">=", year_start],
		},
		fields=["name", "leave_type", "from_date", "to_date", "total_leave_days", "status", "posting_date"],
		order_by="posting_date desc",
		limit=100,
		ignore_permissions=True,
	)


@frappe.whitelist()
def apply_employee_leave(leave_type, from_date, to_date, half_day=0, half_day_date=None, description=None):
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	if not emp_name:
		frappe.throw("No active Employee linked to your user account.")

	leave_app = frappe.get_doc({
		"doctype": "Leave Application",
		"employee": emp_name,
		"leave_type": leave_type,
		"from_date": from_date,
		"to_date": to_date,
		"half_day": cint(half_day),
		"half_day_date": half_day_date,
		"description": description,
		"posting_date": frappe.utils.today(),
		"status": "Open"
	})
	leave_app.insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "success",
		"message": "Leave application submitted successfully.",
		"leave_application": leave_app.name
	}


@frappe.whitelist()
def get_pending_leave_applications():
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	if not emp_name:
		return []

	# Get subordinates
	subordinates = frappe.get_all("Employee", filters={"reports_to": emp_name, "status": "Active"}, fields=["name"])
	if not subordinates:
		return []

	sub_names = [s.name for s in subordinates]

	leaves = frappe.get_all("Leave Application",
		filters={"employee": ["in", sub_names], "status": "Open"},
		fields=["name", "employee", "employee_name", "leave_type", "from_date", "to_date", "total_leave_days", "description", "half_day", "posting_date"],
		order_by="posting_date desc"
	)
	return leaves


@frappe.whitelist()
def action_on_leave_application(name, action):
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	if not emp_name:
		frappe.throw("Not authorized as an employee.")

	# Verify the employee is the manager of the leave applicant
	leave_app = frappe.get_doc("Leave Application", name)
	applicant_reports_to = frappe.db.get_value("Employee", leave_app.employee, "reports_to")

	if applicant_reports_to != emp_name and user != "Administrator":
		frappe.throw("You are not authorized to approve/reject this leave application.", frappe.PermissionError)

	if action == "approve":
		leave_app.status = "Approved"
		leave_app.leave_approver = user
		leave_app.save(ignore_permissions=True)
		leave_app.submit()
	elif action == "reject":
		leave_app.status = "Rejected"
		leave_app.save(ignore_permissions=True)
	else:
		frappe.throw("Invalid action. Must be 'approve' or 'reject'.")

	frappe.db.commit()
	return {
		"status": "success",
		"message": f"Leave application {name} has been {leave_app.status}."
	}


@frappe.whitelist()
def get_employee_salary_slips():
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	if not emp_name:
		return []

	period_start = add_months(getdate(nowdate()), -12)
	slips = frappe.get_all("Salary Slip",
		filters={"employee": emp_name, "docstatus": 1, "end_date": [">=", period_start]},
		fields=["name", "start_date", "end_date", "posting_date", "gross_pay", "total_deduction", "net_pay", "currency", "status"],
		order_by="start_date desc",
		limit=24
	)
	return slips


@frappe.whitelist()
def get_salary_slip_details(name):
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	slip = frappe.get_doc("Salary Slip", name)

	if slip.employee != emp_name and user != "Administrator":
		frappe.throw("You are not authorized to view this Salary Slip.", frappe.PermissionError)

	earnings = [{"salary_component": e.salary_component, "amount": e.amount} for e in slip.earnings]
	deductions = [{"salary_component": d.salary_component, "amount": d.amount} for d in slip.deductions]

	return {
		"name": slip.name,
		"employee": slip.employee,
		"employee_name": slip.employee_name,
		"start_date": str(slip.start_date),
		"end_date": str(slip.end_date),
		"posting_date": str(slip.posting_date),
		"company": slip.company,
		"department": slip.department,
		"designation": slip.designation,
		"gross_pay": slip.gross_pay,
		"total_deduction": slip.total_deduction,
		"net_pay": slip.net_pay,
		"currency": slip.currency,
		"earnings": earnings,
		"deductions": deductions,
		"total_working_days": slip.total_working_days,
		"payment_days": slip.payment_days,
		"leave_without_pay": slip.leave_without_pay
	}


@frappe.whitelist()
def download_salary_slip_pdf(name):
	"""Download a self-contained PDF without relying on external print assets."""
	from html import escape

	from frappe.utils.pdf import get_pdf

	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)
	emp_name = _resolve_employee(user)
	slip = frappe.get_doc("Salary Slip", name)
	if slip.employee != emp_name and user != "Administrator":
		frappe.throw("You are not authorized to download this Salary Slip.", frappe.PermissionError)
	if slip.docstatus != 1:
		frappe.throw("Only submitted Salary Slips can be downloaded.")

	def text(value):
		return escape(str(value or "—"))

	def money(value):
		return f"{text(slip.currency)} {flt(value):,.0f}"

	def component_rows(items):
		return "".join(
			f"<tr><td>{text(row.salary_component)}</td><td class='amount'>{money(row.amount)}</td></tr>"
			for row in items
		) or "<tr><td colspan='2'>None</td></tr>"

	html = f"""
	<!doctype html><html><head><meta charset="utf-8"><style>
		body {{ font-family: Arial, sans-serif; color: #17202a; font-size: 11px; }}
		h1 {{ text-align: center; margin: 0 0 4px; font-size: 22px; }}
		h2 {{ text-align: center; margin: 0 0 20px; font-size: 15px; font-weight: normal; }}
		.meta, .components, .totals {{ width: 100%; border-collapse: collapse; margin-bottom: 18px; }}
		.meta td {{ width: 50%; padding: 5px 8px; border: 1px solid #dfe4e8; }}
		.components th, .components td {{ padding: 7px 8px; border: 1px solid #dfe4e8; }}
		.components th {{ background: #f2f4f6; text-align: left; }}
		.amount {{ text-align: right; }}
		.section {{ font-size: 13px; font-weight: bold; margin: 12px 0 6px; }}
		.totals td {{ padding: 6px 8px; border-bottom: 1px solid #dfe4e8; }}
		.net {{ font-size: 14px; font-weight: bold; }}
	</style></head><body>
		<h1>{text(slip.company)}</h1><h2>Salary Slip</h2>
		<table class="meta">
			<tr><td><b>Employee:</b> {text(slip.employee_name)} ({text(slip.employee)})</td><td><b>Department:</b> {text(slip.department)}</td></tr>
			<tr><td><b>Period:</b> {text(slip.start_date)} to {text(slip.end_date)}</td><td><b>Posting Date:</b> {text(slip.posting_date)}</td></tr>
			<tr><td><b>Designation:</b> {text(slip.designation)}</td><td><b>Payment Days:</b> {flt(slip.payment_days):,.0f} / {flt(slip.total_working_days):,.0f}</td></tr>
		</table>
		<div class="section">Earnings</div><table class="components"><tr><th>Component</th><th class="amount">Amount</th></tr>{component_rows(slip.earnings)}</table>
		<div class="section">Deductions</div><table class="components"><tr><th>Component</th><th class="amount">Amount</th></tr>{component_rows(slip.deductions)}</table>
		<table class="totals">
			<tr><td>Gross Pay</td><td class="amount">{money(slip.gross_pay)}</td></tr>
			<tr><td>Total Deductions</td><td class="amount">{money(slip.total_deduction)}</td></tr>
			<tr class="net"><td>Net Take-Home</td><td class="amount">{money(slip.net_pay)}</td></tr>
		</table>
	</body></html>
	"""
	frappe.local.response.filename = f"{slip.name}.pdf"
	frappe.local.response.filecontent = get_pdf(html, options={"page-size": "A4"})
	frappe.local.response.type = "download"


@frappe.whitelist()
def get_monthly_checkin_calendar(year=None, month=None):
	"""Return monthly day status for the logged-in employee check-ins."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = _resolve_employee(user)
	if not emp_name:
		return {"year": None, "month": None, "days": [], "summary": {}}

	today = getdate(nowdate())
	year = cint(year) or today.year
	month = cint(month) or today.month
	first = get_first_day(f"{year}-{month:02d}-01")
	last = get_last_day(first)

	visit_fields = _checkin_visit_fields()
	fields = ["time", "log_type"] + sorted(visit_fields)
	rows = frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": emp_name,
			"time": ["between", [str(first) + " 00:00:00", str(last) + " 23:59:59"]],
		},
		fields=fields,
		order_by="time asc",
		limit=1000,
	)
	doctor_names = list({r.get("custom_doctor") for r in rows if r.get("custom_doctor")})
	doctor_cities = {}
	if doctor_names and _has_doctors_doctype() and frappe.get_meta("Doctors").has_field("city_name"):
		doctor_cities = {
			d.name: (d.city_name or "")
			for d in frappe.get_all(
				"Doctors", filters={"name": ["in", doctor_names]}, fields=["name", "city_name"],
				ignore_permissions=True,
			)
		}
	report_rows = [
		{
			"time": str(r.time),
			"log_type": r.log_type,
			"doctor": r.get("custom_doctor") or "",
			"hospital": r.get("custom_hospital_name") or "",
			"city": doctor_cities.get(r.get("custom_doctor"), ""),
			"market_shift": r.get("custom_market_shift") or "",
		}
		for r in rows
	]

	by_day = {}
	for r in rows:
		day = str(getdate(r.time))
		bucket = by_day.setdefault(day, {"in_count": 0, "out_count": 0, "first_in": None, "last_out": None, "logs": 0})
		bucket["logs"] += 1
		if r.log_type == "IN":
			bucket["in_count"] += 1
			if not bucket["first_in"]:
				bucket["first_in"] = str(r.time)
		elif r.log_type == "OUT":
			bucket["out_count"] += 1
			bucket["last_out"] = str(r.time)

	days = []
	present = 0
	from datetime import timedelta

	cur = first
	while cur <= last:
		key = str(cur)
		info = by_day.get(key)
		status = "none"
		if info:
			if info["in_count"] and info["out_count"]:
				status = "complete"
				present += 1
			elif info["in_count"]:
				status = "in_only"
				present += 1
			else:
				status = "out_only"
		days.append({
			"date": key,
			"day": cur.day,
			"weekday": cur.weekday(),
			"status": status,
			"in_count": info["in_count"] if info else 0,
			"out_count": info["out_count"] if info else 0,
			"first_in": info["first_in"] if info else None,
			"last_out": info["last_out"] if info else None,
			"logs": info["logs"] if info else 0,
			"is_today": key == str(today),
		})
		cur = cur + timedelta(days=1)

	return {
		"year": year,
		"month": month,
		"month_label": first.strftime("%B %Y"),
		"days": days,
		"report_rows": report_rows,
		"summary": {
			"present_days": present,
			"total_logs": len(rows),
			"in_logs": sum(1 for r in rows if r.log_type == "IN"),
			"out_logs": sum(1 for r in rows if r.log_type == "OUT"),
		},
	}


@frappe.whitelist()
def get_team_attendance_board(from_date=None, to_date=None):
	"""Today/range status board for subordinates with photos and last check-in."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = _resolve_employee(user)
	if not emp_name:
		return []

	subs = frappe.get_all(
		"Employee",
		filters={"reports_to": emp_name, "status": "Active"},
		fields=["name", "employee_name", "designation", "department", "image"],
		order_by="employee_name asc",
	)
	if not subs:
		return []

	from_date = from_date or nowdate()
	to_date = to_date or from_date
	sub_names = [s.name for s in subs]
	checkins = frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": ["in", sub_names],
			"time": ["between", [str(from_date) + " 00:00:00", str(to_date) + " 23:59:59"]],
		},
		fields=["employee", "employee_name", "time", "log_type", "latitude", "longitude"],
		order_by="time desc",
		limit=500,
	)

	latest = {}
	counts = {}
	for c in checkins:
		counts[c.employee] = counts.get(c.employee, 0) + 1
		if c.employee not in latest:
			latest[c.employee] = c

	board = []
	for s in subs:
		last = latest.get(s.name)
		status = "Absent"
		if last:
			status = "IN" if last.log_type == "IN" else "OUT"
		board.append({
			"employee": s.name,
			"employee_name": s.employee_name,
			"designation": s.designation,
			"department": s.department,
			"image": _file_url(_employee_image_url(s.image)),
			"status": status,
			"last_log_type": last.log_type if last else None,
			"last_time": str(last.time) if last else None,
			"latitude": last.latitude if last else None,
			"longitude": last.longitude if last else None,
			"logs_today": counts.get(s.name, 0),
		})
	return board


@frappe.whitelist()
def get_team_attendance_report(mode="day", report_date=None, year=None, month=None, employees=None):
	"""Team attendance report for selected date or month, for all or selected subordinates."""
	from datetime import timedelta
	import json

	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	manager = _resolve_employee(user)
	if not manager:
		return {"employees": [], "summary": {}, "mode": mode}

	subs = frappe.get_all(
		"Employee",
		filters={"reports_to": manager, "status": "Active"},
		fields=["name", "employee_name", "designation", "department", "image"],
		order_by="employee_name asc",
	)
	if not subs:
		return {"employees": [], "summary": {}, "mode": mode, "from_date": None, "to_date": None}

	allowed = {s.name: s for s in subs}

	# Parse selected employees (JSON list / comma string / single)
	selected = []
	if employees:
		if isinstance(employees, str):
			try:
				parsed = json.loads(employees)
				selected = parsed if isinstance(parsed, list) else [str(parsed)]
			except Exception:
				selected = [x.strip() for x in employees.split(",") if x.strip()]
		elif isinstance(employees, (list, tuple)):
			selected = list(employees)

	if selected:
		selected = [e for e in selected if e in allowed]
	else:
		selected = list(allowed.keys())

	today = getdate(nowdate())
	mode = (mode or "day").lower()
	if mode == "month":
		year = cint(year) or today.year
		month = cint(month) or today.month
		from_date = get_first_day(f"{year}-{month:02d}-01")
		to_date = get_last_day(from_date)
		period_label = from_date.strftime("%B %Y")
	else:
		from_date = getdate(report_date or today)
		to_date = from_date
		period_label = str(from_date)
		year = from_date.year
		month = from_date.month

	rows = frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": ["in", selected],
			"time": ["between", [str(from_date) + " 00:00:00", str(to_date) + " 23:59:59"]],
		},
		fields=["employee", "employee_name", "time", "log_type", "latitude", "longitude", "shift"],
		order_by="time asc",
		limit=5000,
	)

	by_emp_day = {}
	for r in rows:
		day = str(getdate(r.time))
		key = (r.employee, day)
		bucket = by_emp_day.setdefault(
			key,
			{"in_count": 0, "out_count": 0, "first_in": None, "last_out": None, "logs": 0, "lat": None, "lng": None},
		)
		bucket["logs"] += 1
		if r.log_type == "IN":
			bucket["in_count"] += 1
			if not bucket["first_in"]:
				bucket["first_in"] = str(r.time)
				bucket["lat"] = r.latitude
				bucket["lng"] = r.longitude
		elif r.log_type == "OUT":
			bucket["out_count"] += 1
			bucket["last_out"] = str(r.time)

	# working days in range (Mon-Fri) for absent calc
	workdays = []
	cur = from_date
	while cur <= to_date:
		if cur.weekday() < 5:
			workdays.append(str(cur))
		cur = cur + timedelta(days=1)

	employee_reports = []
	total_present = 0
	total_absent = 0
	total_in = 0
	total_out = 0

	for emp_id in selected:
		s = allowed[emp_id]
		present_days = 0
		in_logs = 0
		out_logs = 0
		daily = []
		cur = from_date
		while cur <= to_date:
			key = str(cur)
			info = by_emp_day.get((emp_id, key))
			status = "Absent"
			if info:
				in_logs += info["in_count"]
				out_logs += info["out_count"]
				if info["in_count"] and info["out_count"]:
					status = "Complete"
					present_days += 1
				elif info["in_count"]:
					status = "IN only"
					present_days += 1
				else:
					status = "OUT only"
			elif cur.weekday() >= 5:
				status = "Off"
			daily.append({
				"date": key,
				"day": cur.day,
				"weekday": cur.weekday(),
				"status": status,
				"first_in": info["first_in"] if info else None,
				"last_out": info["last_out"] if info else None,
				"logs": info["logs"] if info else 0,
				"latitude": info["lat"] if info else None,
				"longitude": info["lng"] if info else None,
			})
			cur = cur + timedelta(days=1)

		absent_days = 0
		for wd in workdays:
			info = by_emp_day.get((emp_id, wd))
			if not info or not info["in_count"]:
				absent_days += 1

		# Day-mode status from last activity that day
		day_status = "Absent"
		if mode != "month":
			info = by_emp_day.get((emp_id, str(from_date)))
			if info:
				if info["last_out"] and (not info["first_in"] or str(info["last_out"]) >= str(info["first_in"])):
					day_status = "OUT"
				elif info["in_count"]:
					day_status = "IN"
				else:
					day_status = "OUT"

		total_present += present_days
		total_absent += absent_days
		total_in += in_logs
		total_out += out_logs

		employee_reports.append({
			"employee": emp_id,
			"employee_name": s.employee_name,
			"designation": s.designation,
			"department": s.department,
			"image": _file_url(_employee_image_url(s.image)),
			"present_days": present_days,
			"absent_days": absent_days,
			"in_logs": in_logs,
			"out_logs": out_logs,
			"day_status": day_status,
			"daily": daily if mode == "month" else [d for d in daily if d["date"] == str(from_date)],
		})

	return {
		"mode": mode,
		"period_label": period_label,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"year": year,
		"month": month,
		"selected_employees": selected,
		"available_employees": [
			{
				"employee": s.name,
				"employee_name": s.employee_name,
				"image": _file_url(_employee_image_url(s.image)),
				"designation": s.designation,
			}
			for s in subs
		],
		"employees": employee_reports,
		"summary": {
			"team_size": len(selected),
			"present_days": total_present,
			"absent_days": total_absent,
			"in_logs": total_in,
			"out_logs": total_out,
		},
	}
