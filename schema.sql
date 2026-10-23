CREATE TABLE audit (
	id INTEGER NOT NULL, 
	username VARCHAR(80), 
	action VARCHAR(255), 
	created_at DATETIME, 
	PRIMARY KEY (id)
)

;

CREATE TABLE calendar_connection (
	id INTEGER NOT NULL, 
	provider VARCHAR(30), 
	credentials_json TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (provider)
)

;

CREATE TABLE customer (
	id INTEGER NOT NULL, 
	name VARCHAR(150) NOT NULL, 
	phone VARCHAR(30), 
	gst VARCHAR(30), 
	address TEXT, 
	PRIMARY KEY (id)
)

;

CREATE TABLE user (
	id INTEGER NOT NULL, 
	username VARCHAR(80) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	role VARCHAR(30), 
	PRIMARY KEY (id), 
	UNIQUE (username)
)

;

CREATE TABLE "order" (
	id INTEGER NOT NULL, 
	order_no VARCHAR(50) NOT NULL, 
	customer_id INTEGER NOT NULL, 
	product_name VARCHAR(180) NOT NULL, 
	quantity INTEGER NOT NULL, 
	rate FLOAT NOT NULL, 
	order_date DATE, 
	expected_date DATE, 
	production_status VARCHAR(30), 
	transfer_status VARCHAR(30), 
	sale_status VARCHAR(30), 
	notes TEXT, 
	calendar_event_id VARCHAR(255), 
	produced_qty INTEGER NOT NULL, 
	sent_qty INTEGER NOT NULL, 
	received_qty INTEGER NOT NULL, 
	dispatched_qty INTEGER NOT NULL, 
	cancelled_at DATETIME, 
	calendar_sync_error TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (order_no), 
	FOREIGN KEY(customer_id) REFERENCES customer (id)
)

;

CREATE TABLE movement (
	id INTEGER NOT NULL, 
	order_id INTEGER NOT NULL, 
	action VARCHAR(40) NOT NULL, 
	quantity INTEGER NOT NULL, 
	username VARCHAR(80) NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(order_id) REFERENCES "order" (id)
)

;

CREATE TABLE payment (
	id INTEGER NOT NULL, 
	order_id INTEGER NOT NULL, 
	amount_due FLOAT, 
	amount_paid FLOAT, 
	due_date DATE, 
	status VARCHAR(30), 
	last_reminder DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (order_id), 
	FOREIGN KEY(order_id) REFERENCES "order" (id)
)

;

CREATE TABLE reminder_log (
	id INTEGER NOT NULL, 
	order_id INTEGER NOT NULL, 
	reminder_date DATE NOT NULL, 
	days_left INTEGER NOT NULL, 
	channel VARCHAR(30) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_order_reminder_day UNIQUE (order_id, reminder_date, channel), 
	FOREIGN KEY(order_id) REFERENCES "order" (id)
)

;
CREATE INDEX ix_movement_order_id ON movement (order_id);
CREATE INDEX ix_reminder_log_order_id ON reminder_log (order_id);
