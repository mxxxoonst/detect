CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100),
    price DECIMAL(10,2)
);

INSERT INTO products (id, name, price) VALUES (1, 'Widget', 9.99);
INSERT INTO products (id, name, price) VALUES (2, 'Gadget', 19.99);
INSERT INTO products (id, name, price) VALUES (3, 'Broken quote, 29.99);
INSERT INTO products (id, name, price) VALUES (4, 'Doohickey', 39.99);
