-- Test SQL with two tables

CREATE TABLE users (
  id INT,
  name VARCHAR(50),
  phone VARCHAR(20),
  email VARCHAR(100)
);

INSERT INTO users (id, name, phone, email) VALUES (1, 'Alice', '13812345678', 'alice@example.com');
INSERT INTO users (id, name, phone, email) VALUES (2, 'Bob', '13987654321', 'bob@example.com');
INSERT INTO users (id, name, phone, email) VALUES (3, 'Carol', '13700000001', 'carol@example.com');

CREATE TABLE orders (
  order_id VARCHAR(20),
  user_id INT,
  amount FLOAT,
  status VARCHAR(20)
);

INSERT INTO orders (order_id, user_id, amount, status) VALUES ('OD001', 1, 99.9, 'paid');
INSERT INTO orders (order_id, user_id, amount, status) VALUES ('OD002', 2, 199.0, 'pending');
INSERT INTO orders (order_id, user_id, amount, status) VALUES ('OD003', 1, 49.5, 'shipped');
