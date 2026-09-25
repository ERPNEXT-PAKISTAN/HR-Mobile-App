import frappe

from hrms.payroll.doctype.payroll_entry.payroll_entry import PayrollEntry as HRMSPayrollEntry


_UNLINKED_PARTY_DEDUCTION = "__unlinked_party_deduction__"


class PayrollEntry(HRMSPayrollEntry):
	"""Keep employee parties on payroll deductions posted to party accounts."""

	def get_advance_deduction(self, component_type, item):
		advance = super().get_advance_deduction(component_type, item)
		if advance or component_type != "deductions":
			return advance

		account = self.get_salary_component_account(item.salary_component)
		account_type = frappe.db.get_value("Account", account, "account_type")
		if account_type in ("Receivable", "Payable"):
			return _UNLINKED_PARTY_DEDUCTION

		return None

	def add_advance_deduction_entry(self, item, amount, cost_center, employee_advance):
		if employee_advance != _UNLINKED_PARTY_DEDUCTION:
			return super().add_advance_deduction_entry(
				item, amount, cost_center, employee_advance
			)

		self._advance_deduction_entries.append(
			{
				"employee": item.employee,
				"account": self.get_salary_component_account(item.salary_component),
				"amount": amount,
				"cost_center": cost_center,
			}
		)
