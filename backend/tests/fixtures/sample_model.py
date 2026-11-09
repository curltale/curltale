"""The model the test suite runs on: a fictional subscription business, written for the tests (nothing here comes from a real company).

It exercises every concept the product supports: entities of every common kind, a supertype with subtypes, a self-link hierarchy, links that are
optional or unreliable, chains that meet at the same ancestor, code lists, masked personal fields and restricted identity fields, filters with
synonyms, data-quality checks, calculated fields, pinned and entity-specific rules, worked examples, a process, terms and custom properties.
The tests read it through the product's own Excel format (conftest.fixture_model), so the round trip is exercised too.
"""


def _f(role, type_, name, desc, sens='internal', access=None, review='confirmed_by_team', **extra):
    out = {'role': role, 'type': type_, 'business_name': name, 'description': desc, 'sensitivity': sens,
           'agent_access': access or ('none' if sens == 'restricted' else 'full'), 'review_status': review}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def pk(name, desc, example=None, fmt=None, **kw):
    return _f('primary_key', 'text', name, desc, example=example, format=fmt, **kw)


def fk(name, target, desc, **kw):
    return _f('foreign_key', 'text', name, desc, references=target, **kw)


def attr(type_, name, desc, **kw):
    return _f(kw.pop('role', 'attribute'), type_, name, desc, **kw)


def codes(name, desc, values, labels=None, role='dimension', **kw):
    return _f(role, 'text', name, desc, allowed_values=list(values), value_labels=labels, **kw)


def money(name, desc, sens='confidential', **kw):
    return _f('fact_measure', 'decimal', name, desc, sens=sens, unit='EUR', **kw)


def calc(type_, name, desc, sql=None, sens='internal', unit=None, **kw):
    return _f('calculated', type_, name, desc, sens=sens, unit=unit, sql=sql, **kw)


def entity(name, domain, kind, table, definition, attributes, business_key='id', system='CRM', synonyms=(), filters=(), checks=(), calculated=None, **extra):
    e = {'name': name, 'domain': domain, 'kind': kind, 'definition': definition, 'semantic_table': table, 'primary_key': 'id', 'business_key': business_key,
         'system_of_record': system, 'synonyms': list(synonyms), 'model_status': 'confirmed', 'attributes': attributes, 'calculated': calculated or {},
         'filters': [{'synonyms': [], 'status': 'working', **f} for f in filters], 'checks': list(checks)}
    e.update(extra)
    return e


def flt(name, sql, desc, synonyms=(), status='working', source=None):
    return {'name': name, 'filter': sql, 'description': desc, 'synonyms': list(synonyms), 'status': status, **({'source': source} if source else {})}


E = {}
# ---------------------------------------------------------------- Customer
E['customer'] = entity(
    'Customer', 'Customer', 'master', 'crm.customer', 'A company or household that buys from us. One row per customer; groups of companies form a hierarchy.',
    {
        'id': pk('Customer ID', 'Unique customer number.', example='C1000001', fmt='C followed by 7 digits', custom={'source column': 'CRM.CUSTOMER_NO'}),
        'customer_name': attr('text', 'Customer name', 'Registered name of the customer. Not unique: several customers can share a name.'),
        'segment': codes('Segment', 'Commercial segment of the customer.', ['Enterprise', 'SMB', 'Consumer'],
                         {'Enterprise': 'Large companies', 'SMB': 'Small and medium businesses', 'Consumer': 'Private households'}),
        'status': codes('Customer status', 'Lifecycle status of the customer.', ['Prospect', 'Active', 'Inactive'], role='attribute'),
        'parent_customer_id': fk('Parent customer', 'Customer', 'The customer one level up in a group of companies; empty for the top of a group.'),
        'account_manager_id': fk('Account manager', 'Employee', 'Employee responsible for the customer.'),
        'email': attr('text', 'Customer email', 'Main contact email of the customer.', sens='personal', access='masked'),
        'email_opt_in': attr('boolean', 'Marketing emails allowed', 'Whether the customer accepts marketing emails.'),
        'created_at': attr('timestamp', 'Created at', 'When the customer was created.'),
        'loaded_at': attr('timestamp', 'Loaded at', 'When the row was loaded into the warehouse.', role='lineage'),
    },
    business_key='Customer ID', synonyms=['client', 'buyer', 'account holder'],
    filters=[flt('active customer', "status = 'Active'", 'A customer with status Active.'),
             flt('prospect', "status = 'Prospect'", 'A customer that has not bought anything yet.', ['prospective customer'], 'governed')],
    checks=[{'id': 'DQ01', 'rule': 'Every customer has a segment.', 'sql': 'SELECT count(*) AS n FROM crm.customer WHERE segment IS NULL'},
            {'id': 'DQ02', 'rule': 'A customer is never its own parent.', 'sql': 'SELECT count(*) AS n FROM crm.customer WHERE parent_customer_id = id'}],
    calculated={
        'open_complaints': calc('integer', 'Open complaints', 'Number of complaints of the customer that are neither closed nor resolved.',
                                'SELECT customer_id, count(*) FROM support.complaint WHERE closed_at IS NULL AND resolved_at IS NULL GROUP BY customer_id', unit='count'),
        'lifetime_value': calc('decimal', 'Customer lifetime value', 'Sum of everything the customer has been invoiced.', sens='confidential', unit='EUR'),
        'number_of_subsidiaries': calc('integer', 'Number of subsidiaries', 'Customers whose parent is this customer.',
                                       'SELECT parent_customer_id, count(*) FROM crm.customer WHERE parent_customer_id IS NOT NULL GROUP BY 1', unit='count'),
    },
    tmf={'resource': 'Party (customer role)', 'reference': 'Example Industry Standard 1.0'},
    lifecycle_states={'status': ['Prospect', 'Active', 'Inactive']},
    example_questions=['How many SMB customers do we have?', 'Which customers belong to this group?'],
    not_to_confuse_with=['Billing Account: a customer can have several billing accounts'],
    custom={'data owner': 'Customer data office'},
)
E['person'] = entity(
    'Person', 'Customer', 'master', 'crm.person', 'A person who acts for a customer: a contact, a decision maker, a user.',
    {
        'id': pk('Person ID', 'Unique person number.', example='P0000001'),
        'customer_id': fk('Customer', 'Customer', 'The customer the person acts for.'),
        'full_name': attr('text', 'Full name', 'First and last name.', sens='personal', access='masked'),
        'email': attr('text', 'Email', 'Personal email address.', sens='personal', access='masked'),
        'phone': attr('text', 'Phone', 'Mobile phone number.', sens='personal', access='masked'),
        'job_title': attr('text', 'Job title', 'Role of the person at the customer.'),
        'date_of_birth': attr('date', 'Date of birth', 'Date of birth.', sens='personal', access='masked'),
        'national_id': attr('text', 'National ID number', 'Government identity number.', sens='restricted'),
        'passport_number': attr('text', 'Passport number', 'Passport number, when given.', sens='restricted'),
        'national_id_expiry': attr('date', 'National ID expiry', 'Expiry date of the identity document.', sens='restricted'),
        'identity_verified': attr('boolean', 'Identity verified', 'Whether the identity document was checked.', sens='restricted'),
    },
    business_key='Person ID', synonyms=['contact', 'individual'],
    example_questions=['Who are the contacts of this customer?'],
)
E['address'] = entity(
    'Address', 'Customer', 'master', 'crm.address', 'A postal address of a person.',
    {
        'id': pk('Address ID', 'Unique address number.'),
        'person_id': fk('Person', 'Person', 'The person living or working at the address.'),
        'street': attr('text', 'Street', 'Street and house number.', sens='personal', access='masked'),
        'city': attr('text', 'City', 'City.'),
        'postcode': attr('text', 'Postcode', 'Postal code.'),
        'country_code': fk('Country', 'Country', 'Country of the address.'),
    },
    business_key='Address ID',
)
E['customer_score'] = entity(
    'Customer Score', 'Customer', 'derived', 'analytics.customer_score', 'A score computed for a customer by an analytical model, once a month.',
    {
        'id': pk('Score ID', 'Unique score row.'),
        'customer_id': fk('Customer', 'Customer', 'The scored customer.'),
        'score_type': codes('Score type', 'What the score measures.', ['churn', 'satisfaction', 'credit']),
        'score_value': attr('decimal', 'Score value', 'The score, from 0 to 100.', role='fact_measure'),
        'scored_at': attr('date', 'Scored at', 'Month the score was computed for.'),
    },
    system='Analytics platform', synonyms=['churn score', 'health score'],
)
E['employee'] = entity(
    'Employee', 'Organization', 'master', 'org.employee', 'A person employed by the company.',
    {
        'id': pk('Employee ID', 'Unique employee number.', example='E00001'),
        'full_name': attr('text', 'Full name', 'Name of the employee.', sens='personal', access='masked'),
        'email': attr('text', 'Work email', 'Work email address.', sens='personal', access='masked'),
        'job_role': codes('Job role', 'What the employee does.', ['Account manager', 'Support agent', 'Sales representative', 'Engineer'], role='attribute'),
        'department': attr('text', 'Department', 'Department name.'),
        'salary': money('Salary', 'Yearly gross salary.'),
        'is_active': attr('boolean', 'Active', 'Whether the employee still works here.'),
    },
    system='HR system', synonyms=['staff member', 'account manager'],
    filters=[flt('active employee', 'is_active', 'An employee who still works here.')],
)
# ---------------------------------------------------------------- Sales
E['opportunity'] = entity(
    'Opportunity', 'Sales', 'transaction', 'sales.opportunity', 'A potential sale being worked on by sales.',
    {
        'id': pk('Opportunity ID', 'Unique opportunity number.'),
        'customer_id': fk('Customer', 'Customer', 'The customer the sale is for.'),
        'owner_id': fk('Owner', 'Employee', 'Sales representative working on it.'),
        'opp_name': attr('text', 'Opportunity name', 'Short description of the deal.'),
        'stage': codes('Stage', 'Sales stage.', ['Qualification', 'Proposal', 'Negotiation', 'Closed'], role='attribute'),
        'opp_status': codes('Opportunity status', 'Whether the deal is still open.', ['Open', 'Won', 'Lost'], role='attribute'),
        'expected_value': money('Expected value', 'Expected order value of the deal.'),
        'created_at': attr('timestamp', 'Created at', 'When the opportunity was opened.'),
        'close_date': attr('date', 'Close date', 'Expected or actual close date.'),
    },
    system='CRM', synonyms=['deal', 'sales opportunity'],
    filters=[flt('open opportunity', "opp_status = 'Open'", 'A deal still being worked on.', ['open deal', 'pipeline'], 'governed'),
             flt('won opportunity', "opp_status = 'Won'", 'A deal that became an order.', ['won deal'], 'governed')],
)
E['sales_order'] = entity(
    'Sales Order', 'Sales', 'transaction', 'sales.sales_order', 'An order placed by a customer.',
    {
        'id': pk('Order ID', 'Unique order number.', example='SO-000001'),
        'customer_id': fk('Customer', 'Customer', 'The ordering customer.'),
        'opportunity_id': fk('Opportunity', 'Opportunity', 'The deal the order came from, when sales recorded it.', review='needs_review'),
        'order_status': codes('Order status', 'Where the order is.', ['Draft', 'Submitted', 'In progress', 'Completed', 'Cancelled'], role='attribute'),
        'order_date': attr('date', 'Order date', 'When the order was placed.'),
        'total_value': money('Order value', 'Total value of the order.'),
    },
    system='Order management', synonyms=['purchase order'],
    filters=[flt('pending order', "order_status NOT IN ('Completed', 'Cancelled')", 'An order that is neither completed nor cancelled.', ['open order']),
             flt('completed order', "order_status = 'Completed'", 'An order that was delivered.')],
    lifecycle_states={'order_status': ['Draft', 'Submitted', 'In progress', 'Completed']},
)
E['order_line'] = entity(
    'Order Line', 'Sales', 'transaction', 'sales.order_line', 'One product on an order.',
    {
        'id': pk('Order line ID', 'Unique order line.'),
        'order_id': fk('Order', 'Sales Order', 'The order the line belongs to.'),
        'product_id': fk('Product', 'Product', 'The product ordered.'),
        'subscription_id': fk('Subscription', 'Subscription', 'The subscription the line created or changed, if any.'),
        'quantity': attr('integer', 'Quantity', 'Number of units.', role='fact_measure', unit='count'),
        'unit_price': money('Unit price', 'Price of one unit.'),
        'line_amount': money('Line amount', 'Quantity times unit price.'),
    },
    system='Order management',
)
E['subscription'] = entity(
    'Subscription', 'Sales', 'agreement', 'sales.subscription', 'A product a customer subscribes to, billed every month on one billing account.',
    {
        'id': pk('Subscription ID', 'Unique subscription number.', example='S0000001'),
        'customer_id': fk('Customer', 'Customer', 'The subscribing customer.'),
        'billing_account_id': fk('Billing account', 'Billing Account', 'Where the subscription is billed.'),
        'product_id': fk('Product', 'Product', 'The product subscribed to.'),
        'subscription_status': codes('Subscription status', 'Status of the subscription.', ['Active', 'Suspended', 'Cancelled'], role='attribute'),
        'start_date': attr('date', 'Start date', 'First day of the subscription.'),
        'end_date': attr('date', 'End date', 'Last day, when cancelled.'),
    },
    system='Subscription platform', synonyms=['service', 'plan subscription'],
    filters=[flt('active subscription', "subscription_status = 'Active'", 'A subscription that is billed and in use.', ['active service']),
             flt('cancelled subscription', "subscription_status = 'Cancelled'", 'A subscription that ended.', ['churned subscription'], 'governed')],
    checks=[{'id': 'DQ01', 'rule': 'A subscription never ends before it starts.', 'sql': 'SELECT count(*) AS n FROM sales.subscription WHERE end_date < start_date'}],
    calculated={'total_monthly_fee': calc('decimal', 'Total monthly fee', 'Sum of the current monthly fees of the subscription.',
                                          'SELECT subscription_id, sum(monthly_fee) FROM sales.subscription_price WHERE valid_to IS NULL OR valid_to > now() GROUP BY 1',
                                          sens='confidential', unit='EUR')},
    lifecycle_states={'subscription_status': ['Active', 'Suspended', 'Cancelled']},
    example_questions=['How many active subscriptions does this customer have?'],
)
E['subscription_price'] = entity(
    'Subscription Price', 'Sales', 'agreement', 'sales.subscription_price', 'The price a subscription pays per month, over a period of time.',
    {
        'id': pk('Price row ID', 'Unique price row.'),
        'subscription_id': fk('Subscription', 'Subscription', 'The subscription priced.'),
        'price_plan_id': fk('Price plan', 'Price Plan', 'The catalogue price plan used.'),
        'monthly_fee': money('Monthly fee', 'Amount charged every month.'),
        'valid_from': attr('date', 'Valid from', 'First day the price applies.'),
        'valid_to': attr('date', 'Valid to', 'Last day the price applies; empty while current.'),
    },
    system='Subscription platform',
    filters=[flt('monthly fee', 'valid_to IS NULL OR valid_to > now()', 'The price rows in force today: what the subscription pays per month.',
                 ['current price', 'recurring charge'], 'governed', 'rule R14')],
)
# ---------------------------------------------------------------- Product
E['product'] = entity(
    'Product', 'Product', 'master', 'catalog.product', 'Something we sell: software, hardware or a service.',
    {
        'id': pk('Product ID', 'Unique product code.', example='PRD-001'),
        'product_name': attr('text', 'Product name', 'Name shown in the catalogue.'),
        'category': codes('Category', 'Product category.', ['Software', 'Hardware', 'Service']),
        'is_active': attr('boolean', 'Sold', 'Whether the product can still be ordered.'),
    },
    system='Product catalogue',
    filters=[flt('active product', 'is_active', 'A product that can still be ordered.')],
)
E['price_plan'] = entity(
    'Price Plan', 'Product', 'master', 'catalog.price_plan', 'A catalogue price for a product.',
    {
        'id': pk('Price plan ID', 'Unique price plan.'),
        'product_id': fk('Product', 'Product', 'The product priced.'),
        'plan_name': attr('text', 'Plan name', 'Name of the plan (Basic, Pro...).'),
        'monthly_fee': money('List monthly fee', 'Catalogue price per month, before discounts.', sens='internal'),
        'currency': codes('Currency', 'Currency of the price.', ['EUR', 'USD', 'GBP'], role='attribute'),
        'valid_from': attr('date', 'Valid from', 'First day the plan can be sold.'),
    },
    system='Product catalogue', synonyms=['tariff', 'rate card'],
    filters=[flt('current price plan', 'valid_from <= current_date', 'A plan already on sale.')],
)
# ---------------------------------------------------------------- Billing
E['billing_account'] = entity(
    'Billing Account', 'Billing', 'master', 'billing.billing_account', 'The account a customer is invoiced on. A customer can split its billing over several accounts.',
    {
        'id': pk('Billing account ID', 'Unique billing account number.', example='BA000001'),
        'customer_id': fk('Customer', 'Customer', 'The customer that owns the account.'),
        'account_name': attr('text', 'Account name', 'Name of the account (often a department or site).'),
        'payment_method': codes('Payment method', 'How the account pays.', ['card', 'transfer', 'direct_debit'], role='attribute'),
        'bill_cycle_day': attr('integer', 'Bill cycle day', 'Day of the month invoices are produced.'),
    },
    system='Billing', synonyms=['cost center', 'invoice account'],
    calculated={'outstanding_balance': calc('decimal', 'Outstanding balance', 'Sum of amount_due over the invoices of the account.',
                                            'SELECT billing_account_id, sum(amount_due) FROM billing.invoice GROUP BY 1', sens='confidential', unit='EUR')},
)
E['invoice'] = entity(
    'Invoice', 'Billing', 'transaction', 'billing.invoice', 'A bill sent for one billing account and one period.',
    {
        'id': pk('Invoice ID', 'Unique invoice number.', example='INV0000001'),
        'billing_account_id': fk('Billing account', 'Billing Account', 'The billed account.'),
        'invoice_date': attr('date', 'Invoice date', 'When the invoice was issued.'),
        'due_date': attr('date', 'Due date', 'When payment is due.'),
        'invoice_status': codes('Invoice status', 'Payment status.', ['Issued', 'Paid', 'Overdue', 'Cancelled'], role='attribute'),
        'invoice_amount': money('Invoice amount', 'Total of the invoice, taxes included.'),
        'amount_due': money('Amount due', 'Part of the invoice not paid yet.'),
    },
    system='Billing', synonyms=['bill', 'billing'],
    filters=[flt('unpaid invoice', 'amount_due > 0', 'An invoice with something left to pay.', ['unpaid', 'outstanding invoice'], 'governed'),
             flt('overdue invoice', "invoice_status = 'Overdue' AND amount_due > 0", 'Unpaid after its due date.', ['late invoice'], 'governed'),
             flt('paid invoice', 'amount_due = 0', 'Fully paid.')],
    checks=[{'id': 'DQ01', 'rule': 'The amount due never exceeds the invoice amount.', 'sql': 'SELECT count(*) AS n FROM billing.invoice WHERE amount_due > invoice_amount'}],
    calculated={'days_overdue': calc('integer', 'Days overdue', 'Days since the due date for an unpaid invoice.', 'current_date - due_date', unit='days')},
)
E['invoice_line'] = entity(
    'Invoice Line', 'Billing', 'transaction', 'billing.invoice_line', 'One charge on an invoice.',
    {
        'id': pk('Invoice line ID', 'Unique invoice line.'),
        'invoice_id': fk('Invoice', 'Invoice', 'The invoice the charge is on.'),
        'subscription_id': fk('Subscription', 'Subscription', 'The subscription charged, for recurring and usage charges.'),
        'charge_type': codes('Charge type', 'Kind of charge.', ['recurring', 'one_off', 'usage', 'discount'], role='attribute'),
        'amount': money('Amount', 'Amount of the charge.'),
    },
    system='Billing', synonyms=['invoice charge'],
)
E['payment'] = entity(
    'Payment', 'Billing', 'transaction', 'billing.payment', 'Money received on a billing account.',
    {
        'id': pk('Payment ID', 'Unique payment.'),
        'billing_account_id': fk('Billing account', 'Billing Account', 'The account that paid.'),
        'payment_date': attr('date', 'Payment date', 'When the money arrived.'),
        'amount': money('Amount', 'Amount received.'),
        'method': codes('Method', 'How it was paid.', ['card', 'transfer', 'direct_debit'], role='attribute'),
    },
    system='Billing',
    filters=[flt('card payment', "method = 'card'", 'Paid by card.')],
)
E['payment_allocation'] = entity(
    'Payment Allocation', 'Billing', 'bridge', 'billing.payment_allocation', 'The part of a payment that settles one invoice.',
    {
        'id': pk('Allocation ID', 'Unique allocation.'),
        'invoice_id': fk('Invoice', 'Invoice', 'The invoice settled.'),
        'payment_id': fk('Payment', 'Payment', 'The payment used.'),
        'allocated_amount': money('Allocated amount', 'Amount of the payment applied to the invoice.'),
    },
    system='Billing',
)
# ---------------------------------------------------------------- Support
E['support_case'] = entity(
    'Support Case', 'Support', 'supertype', 'support.support_case', 'Any request a customer raises with support: complaints, incidents and service requests in one table.',
    {
        'id': pk('Case ID', 'Unique case number.'),
        'customer_id': fk('Customer', 'Customer', 'The customer that raised the case.'),
        'subscription_id': fk('Subscription', 'Subscription', 'The subscription concerned, when known.'),
        'case_type': codes('Case type', 'Which kind of case this is.', ['complaint', 'incident', 'request'], role='attribute'),
        'priority': codes('Priority', 'How urgent the case is.', ['Low', 'Medium', 'High'], role='attribute'),
        'opened_at': attr('timestamp', 'Opened at', 'When the case was opened.'),
        'closed_at': attr('timestamp', 'Closed at', 'When the case was closed.'),
    },
    system='Support desk', synonyms=['ticket'],
    filters=[flt('open case', 'closed_at IS NULL', 'A case not closed yet.', ['open ticket'])],
)
E['complaint'] = entity(
    'Complaint', 'Support', 'transaction', 'support.complaint', 'A customer saying something went wrong and asking for it to be put right.',
    {
        'id': pk('Complaint ID', 'Unique complaint number.'),
        'customer_id': fk('Customer', 'Customer', 'The complaining customer.'),
        'subscription_id': fk('Subscription', 'Subscription', 'The subscription concerned, when known.'),
        'category': codes('Category', 'What the complaint is about.', ['Billing', 'Service', 'Delivery', 'Other'], role='attribute'),
        'opened_at': attr('timestamp', 'Opened at', 'When the complaint was raised.'),
        'resolved_at': attr('timestamp', 'Resolved at', 'When a solution was given.'),
        'closed_at': attr('timestamp', 'Closed at', 'When the complaint was closed.'),
        'compensation_amount': money('Compensation', 'Credit given to settle the complaint.'),
    },
    system='Support desk', synonyms=['grievance', 'dispute'], subtype_of='Support Case', belongs_to='Customer',
    filters=[flt('open complaint', 'closed_at IS NULL AND resolved_at IS NULL', 'Neither resolved nor closed.', ['unresolved complaint'], 'governed', 'calculated field open_complaints'),
             flt('closed complaint', 'closed_at IS NOT NULL', 'Closed.', status='governed'),
             flt('billing complaint', "category = 'Billing'", 'About an invoice or a charge.', status='governed'),
             flt('service complaint', "category = 'Service'", 'About the service itself.', status='governed')],
    example_questions=['How many complaints are open for this customer?'],
)
E['incident'] = entity(
    'Incident', 'Support', 'transaction', 'support.incident', 'A failure of a subscribed service reported by the customer.',
    {
        'id': pk('Incident ID', 'Unique incident number.'),
        'customer_id': fk('Customer', 'Customer', 'The affected customer.'),
        'subscription_id': fk('Subscription', 'Subscription', 'The failing subscription.'),
        'severity': codes('Severity', 'Impact of the failure.', ['Critical', 'Major', 'Minor'], role='attribute'),
        'reported_at': attr('timestamp', 'Reported at', 'When it was reported.'),
        'resolved_at': attr('timestamp', 'Resolved at', 'When service was restored.'),
        'closed_at': attr('timestamp', 'Closed at', 'When the incident was closed.'),
    },
    system='Support desk', synonyms=['outage', 'fault'], subtype_of='Support Case',
    filters=[flt('open incident', 'closed_at IS NULL AND resolved_at IS NULL', 'Not resolved yet.', status='governed'),
             flt('critical incident', "severity = 'Critical'", 'Service down for the customer.')],
    calculated={'mttr_hours': calc('decimal', 'Mean time to resolve', 'Average hours from report to resolution.',
                                   'SELECT avg(extract(epoch FROM resolved_at - reported_at) / 3600.0) FROM support.incident WHERE resolved_at IS NOT NULL', unit='hours')},
)
E['service_request'] = entity(
    'Service Request', 'Support', 'transaction', 'support.service_request', 'A customer asking for a change: a move, an upgrade, a new user.',
    {
        'id': pk('Request ID', 'Unique request number.'),
        'customer_id': fk('Customer', 'Customer', 'The requesting customer.'),
        'request_type': codes('Request type', 'What is asked for.', ['upgrade', 'downgrade', 'move', 'new user'], role='attribute'),
        'opened_at': attr('timestamp', 'Opened at', 'When it was asked.'),
        'completed_at': attr('timestamp', 'Completed at', 'When it was done.'),
    },
    system='Support desk', subtype_of='Support Case',
    filters=[flt('open request', 'completed_at IS NULL', 'Not done yet.')],
)
# ---------------------------------------------------------------- Usage and reference
E['usage_monthly'] = entity(
    'Usage Monthly', 'Usage', 'fact', 'usage.usage_monthly', 'How much a subscription was used in a month, per metric (long format: one row per metric).',
    {
        'id': pk('Usage row ID', 'Unique usage row.'),
        'subscription_id': fk('Subscription', 'Subscription', 'The subscription used.'),
        'usage_month': attr('date', 'Month', 'First day of the month.'),
        'metric': codes('Metric', 'What was measured.', ['api_calls', 'storage_gb', 'active_users']),
        'quantity': attr('decimal', 'Quantity', 'Amount used in the month, in the unit of the metric.', role='fact_measure'),
    },
    system='Metering', synonyms=['consumption'],
)
E['country'] = entity(
    'Country', 'Reference', 'reference', 'reference.country', 'A country, by ISO code.',
    {
        'id': codes('Country code', 'Two-letter ISO country code.', ['DE', 'FR', 'GB', 'IT', 'ES', 'NL', 'US'], role='primary_key', review='by_design'),
        'country_name': attr('text', 'Country name', 'Name in English.'),
    },
    system='Reference data',
)


def _rel(i, frm, to, pred, col, status='confirmed', card='many-to-one', detail=None, note=None):
    a, b = E[frm], E[to]
    return {'id': f'R-{i:03d}', 'from': a['name'], 'predicate': pred, 'to': b['name'], 'cardinality': card,
            'join': f"{a['semantic_table'].split('.')[1]}.{col} = {b['semantic_table'].split('.')[1]}.id", 'status': status,
            'status_detail': detail or {'confirmed': 'Foreign key in the source system.', 'optional': 'Filled only when it applies.', 'unreliable': 'Often empty or wrong.'}[status],
            'sentence': f"Each {a['name']} {pred} a {b['name']}.", **({'note': note} if note else {})}


OPT = 'many-to-zero-or-one (optional)'
RELATIONSHIPS = [_rel(i + 1, *r[:4], **r[4]) if len(r) > 4 else _rel(i + 1, *r) for i, r in enumerate([
    ('customer', 'customer', 'belongs to the group of', 'parent_customer_id', {'status': 'optional', 'card': OPT, 'note': 'Groups of companies: a shallow tree.'}),
    ('customer', 'employee', 'is managed by', 'account_manager_id'),
    ('person', 'customer', 'acts for', 'customer_id'),
    ('address', 'person', 'is the address of', 'person_id'),
    ('address', 'country', 'is in', 'country_code'),
    ('customer_score', 'customer', 'scores', 'customer_id'),
    ('opportunity', 'customer', 'is a potential sale to', 'customer_id'),
    ('opportunity', 'employee', 'is worked on by', 'owner_id'),
    ('sales_order', 'customer', 'is placed by', 'customer_id'),
    ('sales_order', 'opportunity', 'comes from', 'opportunity_id', {'status': 'unreliable', 'card': OPT, 'detail': 'Sales fill it in only now and then; often empty.'}),
    ('order_line', 'sales_order', 'is part of', 'order_id'),
    ('order_line', 'product', 'orders', 'product_id'),
    ('order_line', 'subscription', 'creates or changes', 'subscription_id', {'status': 'optional', 'card': OPT}),
    ('subscription', 'customer', 'is held by', 'customer_id'),
    ('subscription', 'billing_account', 'is billed on', 'billing_account_id'),
    ('subscription', 'product', 'is for', 'product_id'),
    ('subscription_price', 'subscription', 'prices', 'subscription_id'),
    ('subscription_price', 'price_plan', 'uses', 'price_plan_id'),
    ('price_plan', 'product', 'prices', 'product_id'),
    ('billing_account', 'customer', 'belongs to', 'customer_id'),
    ('invoice', 'billing_account', 'is sent to', 'billing_account_id'),
    ('invoice_line', 'invoice', 'is on', 'invoice_id'),
    ('invoice_line', 'subscription', 'charges', 'subscription_id', {'status': 'optional', 'card': OPT}),
    ('payment', 'billing_account', 'is received on', 'billing_account_id'),
    ('payment_allocation', 'invoice', 'settles', 'invoice_id'),
    ('payment_allocation', 'payment', 'uses', 'payment_id'),
    ('support_case', 'customer', 'is raised by', 'customer_id'),
    ('support_case', 'subscription', 'concerns', 'subscription_id', {'status': 'optional', 'card': OPT}),
    ('complaint', 'customer', 'is raised by', 'customer_id'),
    ('complaint', 'subscription', 'concerns', 'subscription_id', {'status': 'optional', 'card': OPT}),
    ('incident', 'customer', 'affects', 'customer_id'),
    ('incident', 'subscription', 'is a failure of', 'subscription_id'),
    ('service_request', 'customer', 'is asked by', 'customer_id'),
    ('usage_monthly', 'subscription', 'measures', 'subscription_id'),
])]


def _ex(id_, title, body, applies_to=(), **extra):
    return {'id': id_, 'title': title, 'body': body, 'applies_to': list(applies_to), **extra}


EXAMPLES = [
    _ex('EX01', 'Subscriptions of a customer with their billing account', 'Path: Customer -> Subscription -> Billing Account', ['customer', 'subscription', 'billing_account'],
        sql='SELECT s.id AS subscription_id, s.subscription_status, b.account_name FROM sales.subscription s JOIN billing.billing_account b ON b.id = s.billing_account_id '
            'WHERE s.customer_id = :customer_id',
        params={'customer_id': 'customer'}, sample_sql='SELECT customer_id FROM sales.subscription ORDER BY id LIMIT 1', source='query pattern'),
    _ex('EX02', 'Unpaid invoices of a customer', 'Invoices hang off billing accounts, not directly off the customer.', ['customer', 'billing_account', 'invoice'],
        sql='SELECT i.id, i.invoice_date, i.amount_due FROM billing.invoice i JOIN billing.billing_account b ON b.id = i.billing_account_id '
            'WHERE b.customer_id = :customer_id AND i.amount_due > 0',
        params={'customer_id': 'customer'}, sample_sql='SELECT customer_id FROM billing.billing_account ORDER BY id LIMIT 1', source='query pattern'),
    _ex('EX03', 'Open complaints of a customer', 'Uses the governed filter open complaint.', ['customer', 'complaint'],
        sql='SELECT id, category, opened_at FROM support.complaint WHERE customer_id = :customer_id AND closed_at IS NULL AND resolved_at IS NULL',
        params={'customer_id': 'customer'}, sample_sql='SELECT customer_id FROM support.complaint ORDER BY id LIMIT 1', source='query pattern'),
]

MODEL = {
    'ontology': {'name': 'Example Subscription Company', 'id': 'example-subscriptions', 'version': '1.0', 'date': '2026-01-01',
                 'source': 'Written for the Curltale AI test suite: fictional', 'status': 'Illustrative model.',
                 'standards': ['Example Industry Standard 1.0 (fictional)'], 'how_to_read': ['One entity per business concept.', 'Amounts are in EUR unless a currency column says otherwise.'],
                 'custom': {'owner': 'Data office'}},
    'access_policy': {'internal': 'Anyone in the company.', 'confidential': 'Financial values: people entitled to customer financials.',
                      'personal': 'Names, emails and phone numbers of persons: masked unless entitled.', 'restricted': 'Identity numbers: never returned.',
                      'row_level': 'None: access is decided per column.'},
    'entities': E,
    'relationships': RELATIONSHIPS,
    'examples': EXAMPLES,
}
