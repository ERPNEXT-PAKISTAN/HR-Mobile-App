import frappe

def seed():
	# Insert salary detail rows for the slip created via raw SQL
	slip_name = "Sal-Slip-2026-Apr-001"
	
	# Check if already seeded
	exists = frappe.db.sql("SELECT COUNT(*) FROM `tabSalary Detail` WHERE parent=%s", slip_name)[0][0]
	if exists:
		print(f"Salary details already exist for {slip_name}")
		frappe.db.commit()
		return
	
	frappe.db.sql("""
		INSERT INTO `tabSalary Detail` 
		(name, parent, parenttype, parentfield, idx, salary_component, amount, creation, modified, owner, modified_by)
		VALUES 
		('SD-Earn-001', %s, 'Salary Slip', 'earnings', 1, 'Basic Salary', 80000.0, NOW(), NOW(), 'Administrator', 'Administrator'),
		('SD-Earn-002', %s, 'Salary Slip', 'earnings', 2, 'House Rent Allowance', 15000.0, NOW(), NOW(), 'Administrator', 'Administrator'),
		('SD-Earn-003', %s, 'Salary Slip', 'earnings', 3, 'Medical Allowance', 5000.0, NOW(), NOW(), 'Administrator', 'Administrator'),
		('SD-Ded-001', %s, 'Salary Slip', 'deductions', 1, 'Provident Fund', 3000.0, NOW(), NOW(), 'Administrator', 'Administrator'),
		('SD-Ded-002', %s, 'Salary Slip', 'deductions', 2, 'Professional Tax', 2000.0, NOW(), NOW(), 'Administrator', 'Administrator')
	""", (slip_name, slip_name, slip_name, slip_name, slip_name))
	
	frappe.db.commit()
	print(f"Inserted salary details for {slip_name}")
