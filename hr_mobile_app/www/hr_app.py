import frappe

def get_context(context):
	# Do not cache this page as it contains dynamic user-specific status
	context.no_cache = 1
	context.show_sidebar = False
	context.read_only = False
	return context
