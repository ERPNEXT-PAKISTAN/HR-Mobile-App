import frappe

from hrms.payroll.doctype.salary_slip.salary_slip import SalarySlip as HRMSSalarySlip


class SalarySlip(HRMSSalarySlip):
	"""Keep Employee CTC available to salary-structure formulas in v16."""

	def get_data_for_eval(self):
		data, default_data = super().get_data_for_eval()
		employee_ctc = frappe.db.get_value("Employee", self.employee, "ctc")
		if employee_ctc is not None:
			data["ctc"] = employee_ctc
			default_data["ctc"] = employee_ctc
		return data, default_data
