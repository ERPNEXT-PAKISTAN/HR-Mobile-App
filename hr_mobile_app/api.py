import frappe
from frappe.utils import cint, flt


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
		fields=["name", "employee_name", "designation", "department", "user_id"]
	)
	is_manager = len(subordinates) > 0

	# Leave balance summary
	leave_allocations = frappe.get_all("Leave Allocation", 
		filters={"employee": emp_name, "docstatus": 1}, 
		fields=["leave_type", "total_leaves_allocated", "from_date", "to_date"]
	)

	leave_summary = []
	for alloc in leave_allocations:
		taken_result = frappe.db.sql("""
			SELECT SUM(total_leave_days) FROM `tabLeave Application`
			WHERE employee = %s AND leave_type = %s AND status = 'Approved'
			AND from_date >= %s AND to_date <= %s AND docstatus = 1
		""", (emp_name, alloc.leave_type, alloc.from_date, alloc.to_date))
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
		"employee": {
			"name": emp_doc.name,
			"employee_name": emp_doc.employee_name,
			"designation": emp_doc.designation,
			"department": emp_doc.department,
			"company": emp_doc.company,
			"branch": emp_doc.branch,
			"user_id": emp_doc.user_id,
			"gender": emp_doc.gender,
			"date_of_joining": str(emp_doc.date_of_joining) if emp_doc.date_of_joining else ""
		},
		"is_manager": is_manager,
		"subordinates": subordinates,
		"leave_summary": leave_summary,
		"last_checkin": last_checkin,
		"shift_details": shift_details
	}


@frappe.whitelist()
def mark_employee_checkin(log_type, latitude=None, longitude=None):
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not Logged In", frappe.PermissionError)

	emp_name = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
	if not emp_name and user == "Administrator":
		emp_name = "HR-EMP-00001"

	if not emp_name:
		frappe.throw("No active Employee linked to your user account.")

	# Get shift name
	shift = frappe.db.get_value("Shift Assignment", {"employee": emp_name, "status": "Active"}, "shift_type")
	if not shift:
		emp_default_shift = frappe.db.get_value("Employee", emp_name, "default_shift")
		shift = emp_default_shift or "Morning Shift"

	# Create Check-in record
	checkin = frappe.get_doc({
		"doctype": "Employee Checkin",
		"employee": emp_name,
		"log_type": log_type,
		"time": frappe.utils.now_datetime(),
		"device_id": "Mobile Web App",
		"shift": shift,
		"latitude": flt(latitude) if latitude else None,
		"longitude": flt(longitude) if longitude else None
	})
	checkin.insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Successfully checked {log_type} at {frappe.utils.format_datetime(checkin.time)}",
		"checkin": checkin.as_dict()
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

	checkins = frappe.get_all("Employee Checkin",
		filters=filters,
		fields=["name", "time", "log_type", "latitude", "longitude", "device_id", "shift"],
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
	return checkins


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

	slips = frappe.get_all("Salary Slip",
		filters={"employee": emp_name, "docstatus": 1},
		fields=["name", "start_date", "end_date", "posting_date", "gross_pay", "total_deduction", "net_pay", "currency", "status"],
		order_by="start_date desc",
		limit=50
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
