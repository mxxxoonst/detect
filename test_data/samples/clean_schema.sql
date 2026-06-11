-- 用户表
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(50) NOT NULL,
    gender CHAR(1),
    age INTEGER,
    phone VARCHAR(20),
    email VARCHAR(100),
    id_card VARCHAR(18),
    address VARCHAR(200),
    province VARCHAR(20),
    score DECIMAL(5,2),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_phone ON users(phone);
CREATE INDEX idx_users_email ON users(email);

INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (1, '黄美丽', '女', 53, '13863649396', 'user2@example.com', '567381199003153602', '北京市朝阳区某某路100号', '江苏', 81.46, '2024-08-12T21:47:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (2, '黄美丽', '女', 55, '13828317585', 'user11@example.com', '492521199006177047', '成都市武侯区某某巷50号', '浙江', 78.86, '2024-10-18T10:58:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (3, '孙志强', '男', 59, '13878317928', 'user14@example.com', '506076199006156109', '成都市武侯区某某巷50号', '湖北', 64.67, '2024-01-02T09:31:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (4, '李四', '男', 33, '13871886951', 'user4@example.com', '567381199003153602', '深圳市南山区科技园某某楼', '四川', 86.82, '2024-12-23T17:26:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (5, '孙志强', '男', 44, '13898322007', 'user15@example.com', '978271199010029363', '深圳市南山区科技园某某楼', '江苏', 61.31, '2024-06-07T14:28:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (6, '赵六', '女', 24, '13863649396', 'user17@example.com', '173443199012256735', '北京市朝阳区某某路100号', '湖北', 71.04, '2024-02-28T14:05:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (7, '黄美丽', '男', 59, '13894368754', 'user0@example.com', '501350199009086854', '广州市天河区某某街道8号', '浙江', 99.55, '2024-10-07T02:53:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (8, '马大伟', '男', 55, '13839592422', 'user7@example.com', '328460199010206853', '上海市浦东新区某某大厦A座', '北京', 71.09, '2024-03-05T17:16:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (9, '马大伟', '男', 25, '13831590611', 'user4@example.com', '715103199003104213', '广州市天河区某某街道8号', '浙江', 83.55, '2024-01-06T08:03:00Z');
INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) VALUES (10, '王五', '女', 51, '13898322007', 'user2@example.com', '760126199001194830', '深圳市南山区科技园某某楼', '四川', 80.53, '2024-02-15T16:14:00Z');
